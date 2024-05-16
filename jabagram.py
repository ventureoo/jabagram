#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2024 Vasiliy Stelmachenok <ventureo@yandex.ru>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import argparse
import asyncio
import aiohttp
import configparser
import dbm
import logging
import mimetypes
import stringprep

from aiohttp import ClientConnectionError
from collections import OrderedDict
from datetime import datetime
from functools import lru_cache
from json import dumps
from os import path
from slixmpp import ClientXMPP
from slixmpp.jid import JID, InvalidJID
from slixmpp.plugins.xep_0363.http_upload import HTTPError

# Store the last 150 message IDs in the map
MESSAGE_MAP_SIZE = 150

# Messages
QUEUEING_MESSAGE = """
Specified room has been successfully placed on the queue.
Please invite this {} bot to your XMPP room, and as the
reason for the invitation specify the secret key that is
specified in bot's config or ask the owner of this bridge
instance for it.

If you have specified an incorrect room address, simply repeat
the pair command (/jabagram) with the corrected address.
"""

INVALID_JID_MESSAGE = """
You have specified an incorrect room JID. Please try again.
"""

MISSING_MUC_JID_MESSAGE = """
Please specify the MUC address of room you want to pair with
this Telegram chat.
"""

UNBRIDGE_TELEGRAM_MESSAGE = """
This chat was automatically unbridged due to a bot kick in XMPP.
If you want to bridge it again, invite this bot to this chat again
and use the /jabagram command.
"""

UNBRIDGE_XMPP_MESSAGE = """
This chat was automatically unbridged due to a bot kick in Telegram.
"""

BRIDGE_DEFAULT_NAME = "Telegram Bridge"

CONFIG_FILE_NOT_FOUND = """
Configuration file not found.
Perhaps you forgot to rename config.ini.example?
Use the -c key to specify the full path to the config.
"""


# XMPP does not support all available characters for resourcepart in JIDs, so
# we need to filter a range of characters.
BLACKLIST_USERNAME_CHARS = (
    stringprep.in_table_c12,
    stringprep.in_table_c21,
    stringprep.in_table_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_a1,
    stringprep.in_table_c9
)


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class ChatManager(metaclass=Singleton):
    def __init__(self, fpath):
        self._path = fpath
        self._logger = logging.getLogger(self.__class__.__name__)
        self._handlers = {}
        self._pending_rooms = {}

    def add_chats_pair(self, chat, muc):
        with dbm.open(self._path, "w") as database:
            database[str(chat)] = str(muc)
            database.sync()

    def load_chats(self, callback):
        try:
            with dbm.open(self._path, "c") as database:
                chat_id = database.firstkey()
                while chat_id is not None:
                    muc = database.get(chat_id).decode()
                    callback(chat_id, muc)
                    chat_id = database.nextkey(chat_id)
        except dbm.error:
            self._logger.exception(
                "Failed to load chats from database"
            )

    def remove_chat(self, chat_id: int):
        try:
            with dbm.open(self._path, "c") as database:
                del database[str(chat_id)]
        except dbm.error:
            self._logger.exception(
                "Failed to remove chat %d from database", chat_id
            )

    @property
    def pending_rooms(self):
        return self._pending_rooms

    @property
    def handlers(self):
        return self._handlers


class MessageMap():
    def __init__(self, size):
        self._size = size
        self._map = OrderedDict()

    def get(self, key):
        if key in self._map.keys():
            self._map.move_to_end(key)
            return self._map[key]
        else:
            return None

    def add(self, key, value):
        self._map[key] = value
        self._map.move_to_end(key)
        if len(self._map) > self._size:
            self._map.popitem(last=False)


class XmppClient(ClientXMPP, metaclass=Singleton):
    def __init__(self, data: ChatManager, jid: str,
                 password: str, key: str):
        ClientXMPP.__init__(self, jid, password)
        self._data = data
        self._logger = logging.getLogger(self.__class__.__name__)
        self._key = key
        self._reconnecting = False

        # Used XEPs
        self.register_plugin('xep_0030')
        self.register_plugin('xep_0249')
        self.register_plugin('xep_0071')
        self.register_plugin('xep_0363')
        self.register_plugin('xep_0308')
        self.register_plugin('xep_0045')
        self.register_plugin('xep_0066')
        self.register_plugin('xep_0199')

        # Common event handlers
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler('groupchat_direct_invite', self._invite_callback)
        self.add_event_handler("disconnected", self._on_connection_reset)
        self.add_event_handler("connected", self._on_connected)

    async def _on_connected(self, _):
        self._logger.info("Successfully connected.")

    async def _on_connection_reset(self, event):
        self._logger.warning("Connection reset: %d. Attempting to reconnect...", event)

        # Wait for synchronous handlers
        await asyncio.sleep(1)

        self._reconnecting = True
        self.connect()

    def _invite_callback(self, invite):
        muc = str(invite['groupchat_invite']['jid'])
        reason = invite['groupchat_invite']['reason']

        chat_id = self._data.pending_rooms.get(muc)
        if not chat_id:
            return

        if reason != self._key:
            self._logger.info("Wrong key was recieved: %s", reason)
            return

        del self._data.pending_rooms[muc]

        self._add_chat(chat_id, muc)
        self._data.add_chats_pair(chat_id, muc)

    async def _session_start(self, _):
        await self.get_roster()
        self.send_presence()
        self._data.load_chats(self._add_chat)

    def _add_chat(self, chat: str, muc: str):
        # On reconnecting we need to rejoin the rooms
        if self._reconnecting:
            self.plugin['xep_0045'].join_muc(muc, BRIDGE_DEFAULT_NAME)
            return

        message_map = MessageMap(MESSAGE_MAP_SIZE)
        xmpp_handler = XmppRoomHandler(JID(muc), message_map)
        telegram_handler = TelegramChatHandler(int(chat), message_map)

        # Event handlers
        self.add_event_handler(f"muc::{muc}::message", xmpp_handler.process_message)
        self.add_event_handler(f"muc::{muc}::got_online", xmpp_handler.nick_change_handler)
        self.plugin['xep_0045'].join_muc(muc, BRIDGE_DEFAULT_NAME)

        xmpp_handler.pair(telegram_handler)
        telegram_handler.pair_xmpp(xmpp_handler)

        self._data.handlers[int(chat)] = telegram_handler


class TelegramApiError(Exception):
    def __init__(self, code, desc, retry_after=None):
        super().__init__(f"Telegram API error occured ({code}): {desc}")
        self.code = code
        self.desc = desc
        self.retry_after = retry_after


class TelegramClient(metaclass=Singleton):
    def __init__(self, data, token, xmpp_jid):
        self._token = token
        self._logger = logging.getLogger(self.__class__.__name__)
        self._loop = asyncio.get_event_loop()
        self._data = data
        self._xmpp_jid = xmpp_jid

    async def api_call(self, method, **kwargs):
        file = kwargs.get("_file")

        # TODO: Rewrite the lame crutch
        if file:
            del kwargs["_file"]

        url = f"https://api.telegram.org/bot{self._token}/{method}"

        timeout = aiohttp.client.ClientTimeout(total=300)

        if method == "getUpdates":
            timeout = aiohttp.client.ClientTimeout(total=0)

        async with aiohttp.ClientSession() as session:
            resp = None
            if file:
                async with session.post(url=url, data=file,
                                        params=kwargs) as request:
                    # Telegram can return an HTTP error code
                    # when uploading files
                    if request.status != 200:
                        raise TelegramApiError(request.status, request.reason)

                    resp = await request.json()
            else:
                async with session.get(url=url, timeout=timeout,
                                       params=kwargs) as request:
                    resp = await request.json()

            if not resp.get("ok"):
                params = resp.get("parameters")
                retry = params.get("retry_after") if params else None
                raise TelegramApiError(
                    resp['error_code'], resp['description'], retry
                )

            return resp['result']

    async def run(self):
        params = {
            "allowed_updates": ['message', 'edited_message', 'my_chat_member']
        }

        while True:
            updates = []

            try:
                updates = await self.api_call("getUpdates", **params)

                if not updates:
                    continue

                for update in updates:
                    if update.get("message"):
                        message = update.get("message")
                        chat = message.get("chat")

                        if not chat or chat.get("type") == "private":
                            continue

                        chat_id = chat.get("id")
                        handler = self._data.handlers.get(chat_id)

                        if handler:
                            if message.get("new_chat_members"):
                                members = message.get("new_chat_members")
                                for member in members:
                                    self._loop.create_task(
                                        handler.process_on_join(
                                            member, message['message_id']
                                        )
                                    )
                            elif message.get("new_chat_title"):
                                title = message.get("new_chat_title")
                                self._logger.info(
                                    "New chat title recieved: %s", title
                                )
                                self._loop.create_task(
                                    handler.process_on_title_changed(
                                        title, message['message_id']
                                    )
                                )
                            elif message.get("left_chat_member"):
                                left = message.get("left_chat_member")
                                self._loop.create_task(
                                    handler.process_on_leave(
                                        left, message['message_id']
                                    )
                                )
                            else:
                                self._loop.create_task(
                                    handler.process_message(message)
                                )
                        else:
                            text = message.get("text")
                            if text and text.startswith("/jabagram"):
                                await self._bridge_command(chat_id, text)

                    elif update.get("edited_message"):
                        edit = update.get("edited_message")
                        chat_id = edit.get("chat").get("id")
                        handler = self._data.handlers.get(chat_id)

                        if handler:
                            self._loop.create_task(
                                handler.process_edit_message(edit)
                            )
                    elif update.get("my_chat_member"):
                        member = update.get("my_chat_member")
                        new = member.get("new_chat_member")

                        if new and new.get("status") == "left":
                            chat_id = member.get("chat").get("id")

                            handler = self._data.handlers.get(chat_id)

                            if handler:
                                await handler.unbridge_xmpp()

                params['offset'] = updates[len(updates) - 1]['update_id'] + 1
            except TelegramApiError as error:
                # Wait when requests limit is exceeded
                retry_after = error.retry_after
                if retry_after:
                    self._logger.warning(
                        "Too many requests, sleeping on %d sec...", retry_after
                    )
                    await asyncio.sleep(retry_after)
                else:
                    self._logger.exception("Error while receiving updates")
            except ClientConnectionError as error:
                self._logger.error(
                    "Connection failure while getting updates: %s", error
                )

    def unbridge_chat(self, chat_id: int):
        del self._data.handlers[chat_id]
        self._logger.info("Unbridging chat with id %d", chat_id)
        self._data.remove_chat(chat_id)

    async def _bridge_command(self, chat_id, text):
        try:
            room = text.split(" ")[1]

            # Check that MUC jid is valid
            JID(room)

            # Check if this chat has already been attempted to pair
            queue = self._data.pending_rooms
            for muc, chat in queue.items():
                if chat == chat_id:
                    del queue[muc]
                    break

            queue[room] = chat_id
            await self.api_call(
                "sendMessage", chat_id=chat_id,
                text=QUEUEING_MESSAGE.format(self._xmpp_jid)
            )
        except IndexError:
            await self.api_call(
                "sendMessage", chat_id=chat_id,
                text=MISSING_MUC_JID_MESSAGE
            )
        except TelegramApiError as err:
            self._logger.exception(err)
        except InvalidJID:
            await self.api_call(
                "sendMessage", chat_id=chat_id,
                text=INVALID_JID_MESSAGE
            )

    def get_file_url(self, fpath):
        return f"https://api.telegram.org/file/bot{self._token}/{fpath}"


class TelegramChatHandler():
    def __init__(self, chat: int, message_map: MessageMap):
        self._telegram = TelegramClient()
        self._chat = chat
        self._message_map = message_map
        self._xmpp: XmppRoomHandler = None
        self._logger = logging.getLogger(f"TelegramChatHandler ({str(chat)})")
        self._reply_map = MessageMap(MESSAGE_MAP_SIZE)
        self._logger.info("New TelegramChatHandler created")


    def _get_attachment(self, message: dict):
        sender: dict = message['from']
        last_name: str | None = sender.get("last_name")
        name: str = sender['first_name'] + (" " + last_name if last_name else "")

        attachment = message.get("photo") or message.get("document") \
            or message.get("video") or message.get("audio") \
            or message.get("voice") or message.get("video_note")

        if not attachment:
            return None

        # Check for maximum available PhotoSize
        if isinstance(attachment, list):
            attachment = attachment[-1]

        file_id = attachment.get("file_id")
        file_unique_id = attachment.get("file_unique_id")
        fname = attachment.get("file_name") or file_unique_id
        mime = attachment.get("mime_type")
        fsize = attachment.get("file_size")

        if message.get("photo"):
            # Telegram compresses all photos to JPEG
            # if they were not sent as a document
            mime = "image/jpeg"

        if message.get("voice"):
            fname = f"Voice message from {name}.ogg"
        elif message.get("video_note"):
            fname = f"Video from {name}.mp4"
        else:
            extension = mimetypes.guess_extension(mime)

            if extension and not fname.endswith(extension):
                fname += extension

        return file_id, fname, mime, fsize

    async def process_message(self, message: dict) -> None:
        sender: dict = message['from']
        last_name: str | None = sender.get("last_name")
        name: str = sender['first_name'] + (" " + last_name if last_name else "")

        message_id: int = message['message_id']
        self._logger.info("Received message with id: %d", message_id)

        text = message.get("text") or message.get("caption")
        attachment = self._get_attachment(message)
        sticker = message.get("sticker")

        if attachment:
            file_id, fname, mime, fsize = attachment
            file = await self._telegram.api_call("getFile", file_id=file_id)
            url = self._telegram.get_file_url(file["file_path"])

            if not mime:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url) as resp:
                        mime = resp.content_type

            await self._xmpp.send_attachment(
                sender=name,
                url=url,
                fname=fname,
                fsize=fsize,
                mime=mime
            )

        if text:
            reply = message.get("reply_to_message")

            if reply:
                attachment = self._get_attachment(reply)
                reply_body = reply.get("text") or reply.get("caption") or ""

                if not reply_body and attachment:
                    _, fname, _, _ = attachment
                    reply_body = fname

                reply_body = "> " + reply_body.replace("\n", "\n> ")
                await self._xmpp.send_message(f"{reply_body}\n{text}",
                                              name, message_id)
            else:
                await self._xmpp.send_message(text, name, message_id)

            self._logger.debug(
                "Reply from telegram: %d (%s)", hash(text), text
            )
            self._reply_map.add(text, message_id)

        elif sticker:
            file_id = sticker.get('file_id')
            file = await self._telegram.api_call("getFile", file_id=file_id)
            url = self._telegram.get_file_url(file['file_path'])
            fname = sticker.get("set_name") + " " + sticker.get("emoji")

            is_video = sticker.get("is_video")
            is_animated = sticker.get("is_animated")

            if is_video:
                fname += ".mp4"
            elif not is_animated:
                fname += ".webp"

            await self._xmpp.send_attachment(
                sender=name,
                url=url,
                fname=fname,
                fsize=sticker.get("file_size"),
                mime="video/mp4" if is_video else "image/webp"
            )

    async def process_edit_message(self, edit: dict):
        text = edit.get("text") or edit.get("caption") or ""
        stanza_id = self._message_map.get(edit['message_id'])

        if not stanza_id:
            self._logger.warning(
                "Can't find any messages in map with id: %s", edit.get('message_id')
            )
            return

        self._logger.info("Found stanaza %s matching Telegram message %s",
                          stanza_id, edit["message_id"])

        reply = edit.get("reply_to_message")

        sender: dict = edit['from']
        last_name: str | None = sender.get("last_name")
        name: str = sender['first_name'] + (" " + last_name if last_name else "")

        if reply:
            reply_body = reply.get("text") or reply.get("caption") or ""
            reply_body = "> " + reply_body.replace("\n", "\n> ")
            await self._xmpp.edit_message(
                stanza_id, f"{reply_body}\n{text}", name
            )
        else:
            await self._xmpp.edit_message(stanza_id, text, name)

    def _make_bold_entity(self, text: str, offset: int):
        message_entities = [
            {
                "type": "bold",
                "offset": offset,
                "length": len(text)
            }
        ]
        return dumps(message_entities)

    def _parse_reply(self, message: str):
        def _safe_get(line: str, index: int):
            try:
                return line[index]
            except IndexError:
                return None

        replies = []
        parts = []

        for line in message.splitlines():
            if _safe_get(line, 0) == ">":
                # Ignore nested replies
                if _safe_get(line, 2) == ">":
                    continue

                line = line.replace("> ", "")

                # Attempt to detect a replies format of some mobile clients
                # that add time and sender name of the message sent
                try:
                    datetime.strptime(line, '%Y-%m-%d  %H:%M (GMT%z)')

                    # Remove sender name of message being replied to
                    replies.pop()
                except ValueError:
                    replies.append(line)
            else:
                parts.append(line)

        reply = "\n".join(replies)
        body = "\n".join(parts)

        return reply, body

    async def send_message(self, sender: str, text: str, stanza_id: str):
        reply, body = self._parse_reply(text)
        message = None

        try:
            if not reply:
                message = await self._telegram.api_call(
                    "sendMessage", chat_id=self._chat,
                    text=f"{sender}: {text}",
                    entities=self._make_bold_entity(sender, 0)
                )
            else:
                telegram_id = self._reply_map.get(reply)

                if telegram_id:
                    message = await self._telegram.api_call(
                        "sendMessage", chat_id=self._chat,
                        text=f"{sender}: {body}",
                        reply_to_message_id=telegram_id,
                        entities=self._make_bold_entity(sender, 0)
                    )
                else:
                    formatted_reply = "> " + reply.replace("\n", "\n> ")
                    message = await self._telegram.api_call(
                        "sendMessage", chat_id=self._chat,
                        text=f"{formatted_reply}\n{sender}: {body}",
                        entities=self._make_bold_entity(
                            sender, len(formatted_reply) + 1
                        )
                    )
            self._reply_map.add(body, message['message_id'])
            self._message_map.add(stanza_id, message['message_id'])
        except TelegramApiError:
            self._logger.exception("Error sending a message")

    async def send_attachment(self, sender: str, url: str):
        fname = url.split("/")[-1]

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status not in (200, 201):
                    self._logger.error(
                        "Error while getting %s file: %d", url, resp.status
                    )
                    return

                mime = resp.content_type
                form_data = aiohttp.FormData()
                form_data.add_field(
                    'file', resp.content, filename=fname,
                    content_type=mime
                )

                method = "sendDocument"
                params = {
                    "chat_id": self._chat,
                    "_file": form_data,
                    "caption": f"{sender}: ",
                    "caption_entities": self._make_bold_entity(sender, 0)
                }

                if mime == "image/gif":
                    method = "sendAnimation"
                    params['animation'] = "attach://file"
                elif mime.startswith("image"):
                    method = "sendPhoto"
                    params['photo'] = "attach://file"
                elif mime.startswith("video"):
                    method = "sendVideo"
                    params['video'] = "attach://file"
                elif mime.startswith("audio"):
                    method = "sendAudio"
                    params['audio'] = "attach://file"
                else:
                    params['document'] = "attach://file"

                try:
                    message = await self._telegram.api_call(method, **params)
                    self._reply_map.add(url, message['message_id'])
                except TelegramApiError:
                    await self._telegram.api_call(
                        "sendMessage",
                        chat_id=self._chat,
                        text=f"Couldn't transfer file {fname} from {sender}"
                    )
                    self._logger.exception("Failed to send file to telegram")

    async def edit_message(self, stanza_id: str, text: str, sender: str):
        reply, body = self._parse_reply(text)
        telegram_id = self._message_map.get(stanza_id)
        message = None

        if telegram_id is None:
            self._logger.debug(
                "Message with %s not found in the message map", stanza_id
            )
            return

        self._logger.info("Found Telegram message %s matching XMPP stanza %s",
                          telegram_id, stanza_id)

        try:
            if not reply:

                message = await self._telegram.api_call(
                    "editMessageText", chat_id=self._chat,
                    text=f"{sender}: {text}",
                )
            else:
                if self._reply_map.get(reply):
                    self._logger.debug(
                        "Found reply to a message with ID %s in reply map", id
                    )
                    message = await self._telegram.api_call(
                        "editMessageText", chat_id=self._chat,
                        text=f"{sender}: {body}",
                        message_id=telegram_id
                    )
                else:
                    self._logger.debug(
                        "Reply with body %s not found in the reply map", reply
                    )
                    formatted_reply = "> " + reply.replace("\n", "\n> ")
                    message = await self._telegram.api_call(
                        "editMessageText", chat_id=self._chat,
                        text=f"{formatted_reply}\n{sender}: {body}",
                        message_id=telegram_id
                    )

            self._reply_map.add(body, message['message_id'])
            self._message_map.add(stanza_id, message['message_id'])
        except TelegramApiError:
            self._logger.exception("Error while editing a message")


    async def process_on_join(self, user: dict, event_id: int):
        last_name: str | None = user.get("last_name")
        name: str = user['first_name'] + (" " + last_name if last_name else "")

        await self._xmpp.send_message(
            f"*{name}* joined the chat", BRIDGE_DEFAULT_NAME, event_id
        )

    async def process_on_title_changed(self, title: str, event_id: int):
        await self._xmpp.send_message(
            f"The name of chat chat has been changed to \"{title}\"",
            BRIDGE_DEFAULT_NAME, event_id
        )

    async def process_on_leave(self, user: dict, event_id: int):
        last_name: str | None = user.get("last_name")
        name: str = user['first_name'] + (" " + last_name if last_name else "")

        await self._xmpp.send_message(
            f"*{name}* left the chat", BRIDGE_DEFAULT_NAME, event_id
        )

    async def send_event(self, event: str, actor: str | None = None):
        try:
            if actor:
                await self._telegram.api_call(
                    "sendMessage", chat_id=self._chat,
                    text=f"{actor} {event}",
                    entities=self._make_bold_entity(actor, 0)
                )
            else:
                await self._telegram.api_call(
                    "sendMessage", chat_id=self._chat, text=event
                )
        except TelegramApiError:
            self._logger.exception(
                "An error occurred while sending the event: %s", event
            )

    async def unbridge(self):
        await self.send_event(UNBRIDGE_TELEGRAM_MESSAGE)
        await self._telegram.api_call("leaveChat", chat_id=self._chat)
        self._telegram.unbridge_chat(self._chat)
        self._xmpp = None

    async def unbridge_xmpp(self):
        await self._xmpp.unbridge()
        self._telegram.unbridge_chat(self._chat)
        self._xmpp = None

    def pair_xmpp(self, xmpp):
        if self._xmpp:
            return

        self._xmpp = xmpp


class XmppRoomHandler():
    def __init__(self, muc: JID, message_map: MessageMap):
        self._loop = asyncio.get_event_loop()
        self._xmpp = XmppClient()
        self._telegram = None
        self._message_map: MessageMap = message_map
        self._logger = logging.getLogger(f"XmppRoomHandler ({muc})")
        self._muc = muc
        self._last_sender = BRIDGE_DEFAULT_NAME
        self._current_telegram_message = None
        self._message_lock = asyncio.Lock()
        self._nick_change_event = asyncio.Event()
        self._logger.info("New XmppRoomHandler created")

    def process_message(self, message):
        sender = message['mucnick']

        if sender.endswith("(Telegram)") or sender == BRIDGE_DEFAULT_NAME:
            if self._current_telegram_message:
                self._message_map.add(self._current_telegram_message, message['id'])
                self._message_lock.release()
                self._current_telegram_message = None

            return

        if message['oob']['url']:
            self._loop.create_task(
                self._telegram.send_attachment(
                    sender,
                    message['oob']['url'],
                )
            )
        elif message['replace']['id']:
            self._loop.create_task(
                self._telegram.edit_message(
                    message['replace']['id'],
                    message['body'],
                    sender
                )
            )
        else:
            self._loop.create_task(
                self._telegram.send_message(
                    sender,
                    message['body'],
                    message['id']
                )
            )

    async def send_message(self, text: str, sender: str, telegram_id: int):
        self._logger.info("Recieved message from telegram: %s", telegram_id)

        # Timeout 5 seconds to avoid deadlock if for some reason the message
        # was not sent
        await asyncio.wait_for(self._message_lock.acquire(), 5)

        await self._change_nick(sender)
        self._current_telegram_message = telegram_id
        self._xmpp.send_message(mto=self._muc, mbody=text, mtype='groupchat')

    async def send_attachment(self, sender: str, url: str, fname: str,
                              fsize: int, mime: str):
        self._logger.debug("Received telegram attachment %s from %s", fname, sender)
        await self._change_nick(sender)

        upload_file = self._xmpp.plugin['xep_0363'].upload_file

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                try:
                    upload = await upload_file(
                        filename=fname,
                        size=fsize,
                        content_type=mime,
                        input_file=resp.content
                    )
                    html = (
                        f'<body xmlns="http://www.w3.org/1999/xhtml">'
                        f'<a href="{url}">{url}</a></body>'
                    )
                    self._logger.info(upload)

                    message = self._xmpp.make_message(mbody=upload, mto=self._muc,
                                                      mtype='groupchat', mhtml=html)
                    message['oob']['url'] = upload
                    message.send()
                except HTTPError as error:
                    self._logger.error("Cannot upload file: %s", error)


    def nick_change_handler(self, presence):
        nick = presence['from'].resource
        if nick == self._last_sender:
            self._nick_change_event.set()


    async def edit_message(self, stanza_id: str, text: str, sender: str):
        self._logger.info("Editing the stanza: %s", stanza_id)
        message = self._xmpp.make_message(mbody=text, mto=self._muc,
                                          mtype='groupchat')
        message['replace']['id'] = stanza_id
        await self._change_nick(sender)
        message.send()

    @lru_cache(maxsize=100)
    def _validate_name(self, sender: str) -> str:
        valid = []
        for char in sender:
            for check in BLACKLIST_USERNAME_CHARS:
                if check(char):
                    break
            else:
                valid.append(char)

        return "".join(valid)

    async def _change_nick(self, sender: str):
        sender = self._validate_name(sender) + " (Telegram)"

        if sender == self._last_sender:
            return

        self._logger.debug("Changing nick to %s", sender)
        self._xmpp.send_presence(
            pto=f"{self._muc.bare}/{sender}",
            pfrom=self._xmpp.boundjid.full
        )
        self._last_sender = sender

        # To avoid getting deadlock if for some reason the nickname has not
        # been changed, even though we have processed its validity in advance.
        await asyncio.wait_for(self._nick_change_event.wait(), 15)


    async def unbridge(self):
        await self._change_nick(BRIDGE_DEFAULT_NAME)
        self._xmpp.send_message(
            mto=self._muc,
            mbody=UNBRIDGE_XMPP_MESSAGE,
            mtype='groupchat'
        )
        self._xmpp.plugin['xep_0045'].leave_muc(self._muc, self._last_sender)
        self._telegram = None

    def pair(self, telegram):
        if self._telegram:
            return

        self._telegram = telegram


def main():
    parser = argparse.ArgumentParser(
        prog='jabagram',
        description='Bridge beetween Telegram and XMPP',
    )
    parser.add_argument(
        '-c', '--config', default="config.ini",
        dest="config", help="path to configuration file"
    )
    parser.add_argument(
        '-d', '--data', default="jabagram.db",
        dest="data", help="path to bridge database"
    )
    parser.add_argument(
        '-v', '--verbose', dest="verbose",
        action='store_true', help="output debug information",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(
            filename=None if path.exists("/.dockerenv") else "jabagram.log",
            filemode='a',
            format="[%(asctime)s] %(name)s - %(levelname)s: %(message)s",
            level=logging.DEBUG
        )
    else:
        logging.basicConfig(
            format="[%(asctime)s] %(name)s - %(levelname)s: %(message)s",
            level=logging.INFO
        )

    logger = logging.Logger("Main")

    try:
        config = configparser.ConfigParser()

        with open(args.config, "r", encoding="utf-8") as f:
            config.read_file(f)

        database_service = ChatManager(args.data)

        telegram = TelegramClient(
            database_service,
            config.get("telegram", "token"),
            config.get("xmpp", "login")
        )
        xmpp = XmppClient(
            database_service,
            config.get("xmpp", "login"),
            config.get("xmpp", "password"),
            config.get("general", "key")
        )

        loop = asyncio.get_event_loop()

        def exception_handler(_, context):
            exception = context.get("exception")

            if exception:
                logger.exception("Some unhandled error occured: %s", exception)

        loop.set_exception_handler(exception_handler)

        loop.create_task(telegram.run())
        xmpp.connect()
        loop.run_forever()
    except FileNotFoundError:
        logger.error(CONFIG_FILE_NOT_FOUND)
    except configparser.NoOptionError:
        logger.exception("Missing mandatory option")
    except configparser.Error:
        logger.exception("Config parsing error")


if __name__ == "__main__":
    main()
