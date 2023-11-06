#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (C) 2023 Vasiliy Stelmachenok <ventureo@yandex.ru>
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
import aioxmpp
import aiohttp
import asyncio
import configparser
import logging
import dbm
import argparse
import mimetypes

from json import dumps
from datetime import datetime
from collections import OrderedDict
from aiohttp import ClientConnectionError
from aioxmpp import PresenceManagedClient, JID
from aioxmpp.muc import Room, Occupant
from aioxmpp.muc.xso import History
from aioxmpp.muc.service import LeaveMode
from aioxmpp.stanza import Message
from aioxmpp.tracking import MessageState
from aioxmpp.utils import namespaces
from aioxmpp.misc import Replace
from aioxmpp.misc.oob import OOBExtension

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


class Singleton(type):
    _instances = {}

    def __call__(c, *args, **kwargs):
        if c not in c._instances:
            c._instances[c] = super(Singleton, c).__call__(*args, **kwargs)
        return c._instances[c]


class ChatManager(metaclass=Singleton):
    def __init__(self, path):
        self._path = path
        self._logger = logging.getLogger("ChatManager")
        self._handlers = {}
        self._pending_rooms = {}

    def add_chats_pair(self, chat, muc):
        with dbm.open(self._path, "w") as db:
            db[str(chat)] = str(muc)
            db.sync()

    def load_chats(self, callback):
        try:
            with dbm.open(self._path, "c") as db:
                chat_id = db.firstkey()
                while chat_id is not None:
                    muc = db.get(chat_id).decode()
                    callback(int(chat_id), JID.fromstr(muc))
                    chat_id = db.nextkey(chat_id)
        except Exception:
            self._logger.exception(
                "Failed to load chats from database"
            )

    def remove_chat(self, chat_id: int):
        try:
            with dbm.open(self._path, "c") as db:
                del db[str(chat_id)]
        except Exception:
            self._logger.exception(
                f"Failed to remove chat {chat_id} from database"
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


class XmppClient(metaclass=Singleton):
    def __init__(self, login: str, password: str, key: str):
        self._data = ChatManager()
        self._client = PresenceManagedClient(
            JID.fromstr(login), aioxmpp.make_security_layer(password)
        )
        self._logger = logging.getLogger("XmppClient")
        self._muc = None
        self._key = key
        self._service_addr = None

    def _invite_callback(self, stanza, muc, inviter_address,
                         mode, *, reason=None, **kwargs):
        muc_address = str(muc)

        if not self._data.pending_rooms.get(muc_address):
            return

        if reason != self._key:
            self._logger.info(f"Wrong key was recieved: {reason}")
            return

        chat_id = self._data.pending_rooms.get(muc_address)
        del self._data.pending_rooms[muc_address]

        self._add_chat(chat_id, muc)
        self._data.add_chats_pair(chat_id, muc)

    async def run(self):
        self._muc = self._client.summon(aioxmpp.MUCClient)
        self._muc.on_muc_invitation.connect(self._invite_callback)
        disco = self._client.summon(aioxmpp.DiscoClient)

        async with self._client.connected():
            items = await disco.query_items(
                self._client.local_jid.replace(localpart=None, resource=None),
                timeout=10
            )

            # Check if server supports XEP-0363
            addrs = [item.jid for item in items.items if not item.node
                     if namespaces.xep0363_http_upload in
                     (await disco.query_info(item.jid)).features]

            if addrs:
                self._service_addr = addrs[0]
                self._logger.info(
                    "Found address of HTTP Upload service: %s",
                    self._service_addr
                )
            else:
                self._logger.warning(
                    "XMPP server doesn't support XEP-0363"
                    "It is not possible to send attachments."
                )

            self._data.load_chats(self._add_chat)

            while True:
                await asyncio.sleep(1)

    def _add_chat(self, chat, muc):
        room, room_future = self._muc.join(muc.bare(), BRIDGE_DEFAULT_NAME,
                                           history=History(maxstanzas=0),
                                           autorejoin=False)
        map = MessageMap(MESSAGE_MAP_SIZE)

        xmpp_handler = XmppRoomHandler(room, room_future, map)
        telegram_handler = TelegramChatHandler(chat, map)

        # Event handlers
        room.on_message.connect(xmpp_handler.process_message)
        room.on_leave.connect(xmpp_handler.on_leave)
        room.on_exit.connect(xmpp_handler.on_exit)
        room.on_join.connect(xmpp_handler.on_muc_enter)
        room.on_nick_changed.connect(xmpp_handler.on_nick_changed)
        room.on_topic_changed.connect(xmpp_handler.on_topic_changed)

        xmpp_handler.pair(telegram_handler)
        telegram_handler.pair_xmpp(xmpp_handler)

        self._data.handlers[chat] = telegram_handler

    async def get_slot(self, fname, fsize, mime):
        if not self._service_addr:
            self._logger.warning(
                "File transfer via XEP-0363 is not supported by server"
            )
            return None

        slot = await self._client.send(
            aioxmpp.IQ(
                to=self._service_addr,
                type_=aioxmpp.IQType.GET,
                payload=aioxmpp.httpupload.Request(fname, fsize, mime)
            )
        )

        return slot


class TelegramApiError(Exception):
    def __init__(self, code, desc, retry_after=None):
        super().__init__(f"Telegram API error occured ({code}): {desc}")
        self.code = code
        self.desc = desc
        self.retry_after = retry_after


class TelegramClient(metaclass=Singleton):
    def __init__(self, token, xmpp_jid):
        self._token = token
        self._logger = logging.getLogger("TelegramClient")
        self._loop = asyncio.get_event_loop()
        self._data = ChatManager()
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
                                    f"New chat title recieved: {title}"
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
                        f"Too many requests, sleeping on {retry_after}s..."
                    )
                    await asyncio.sleep(retry_after)
                else:
                    self._logger.exception("Error while receiving updates")
            except ClientConnectionError as error:
                self._logger.error(
                    "Connection failure while getting updates: %s", error
                )

    def unbridge_chat(self, chat_id):
        del self._data.handlers[chat_id]
        self._logger.info(f"Unbridging chat with id {chat_id}")
        self._data.remove_chat(chat_id)

    async def _bridge_command(self, chat_id, text):
        try:
            room = text.split(" ")[1]

            # Check that MUC jid is valid
            JID.fromstr(room)

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
        except ValueError:
            await self.api_call(
                "sendMessage", chat_id=chat_id,
                text=INVALID_JID_MESSAGE
            )

    def get_file_url(self, path):
        return f"https://api.telegram.org/file/bot{self._token}/{path}"


class TelegramChatHandler():
    def __init__(self, chat: int, message_map: MessageMap):
        self._telegram = TelegramClient()
        self._chat = chat
        self._message_map = message_map
        self._xmpp = None
        self._logger = logging.getLogger(f"TelegramChatHandler ({str(chat)})")
        self._reply_map = MessageMap(MESSAGE_MAP_SIZE)
        self._logger.info("New TelegramChatHandler created")

    async def process_message(self, message: dict):
        sender = message.get('from')
        name = sender.get('first_name')

        if sender.get("last_name") is not None:
            name += " " + sender.get("last_name")

        name += " (Telegram)"
        message_id = message['message_id']

        text = message.get("text") or message.get("caption")
        attachment = message.get("photo") or message.get("document") \
            or message.get("video") or message.get("audio") \
            or message.get("voice") or message.get("video_note")
        sticker = message.get("sticker")

        if attachment:
            # Check for maximum available PhotoSize
            if isinstance(attachment, list):
                attachment = attachment[-1]

            file_id = attachment.get("file_id")
            file_unique_id = attachment.get("file_unique_id")
            file = await self._telegram.api_call("getFile", file_id=file_id)
            url = self._telegram.get_file_url(file["file_path"])

            fname = attachment.get("file_name") or file_unique_id
            mime = attachment.get("mime_type")

            if message.get("photo"):
                # Telegram compresses all photos to JPEG
                # if they were not sent as a document
                mime = "image/jpeg"

            if not mime:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url) as resp:
                        mime = resp.content_type

            if message.get("voice"):
                fname = f"Voice message from {sender}.ogg"
            elif message.get("video_note"):
                fname = f"Video from {sender}.mp4"
            else:
                extension = mimetypes.guess_extension(mime)

                if extension:
                    fname += extension

            await self._xmpp.send_attachment(
                sender=name,
                url=url,
                fname=fname,
                fsize=attachment.get("file_size"),
                mime=mime
            )

        if text:
            reply = message.get("reply_to_message")

            if reply:
                reply_body = reply.get("text") or reply.get("caption") or ""
                reply_body = "> " + reply_body.replace("\n", "\n> ")
                await self._xmpp.send_message(f"{reply_body}\n{text}",
                                              name, message_id)
            else:
                await self._xmpp.send_message(text, name, message_id)

            self._logger.debug(f"Reply from telegram: {hash(text)}, '{text}'")
            self._reply_map.add(text, message_id)

        if sticker:
            file_id = sticker.get('file_id')
            file = await self._telegram.api_call("getFile", file_id=file_id)
            url = self._telegram.get_file_url(file['file_path'])
            fname = sticker.get("set_name") + " " + sticker.get("emoji")

            is_video = sticker.get("is_video")

            if is_video:
                fname += ".mp4"

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
                "Can't find any messages in map with id "
                f"{edit.get('message_id')}"
            )
            return

        reply = edit.get("reply_to_message")

        if reply:
            reply_body = reply.get("text") or reply.get("caption") or ""
            reply_body = "> " + reply_body.replace("\n", "\n> ")
            await self._xmpp.edit_message(stanza_id, f"{reply_body}\n{text}")
        else:
            await self._xmpp.edit_message(stanza_id, text)

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
            # Ignore nested replies
            if _safe_get(line, 0) == ">" and _safe_get(line, 2) == ">":
                continue
            elif _safe_get(line, 0) == ">":
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
                id = self._reply_map.get(reply)

                if id:
                    message = await self._telegram.api_call(
                        "sendMessage", chat_id=self._chat,
                        text=f"{sender}: {body}",
                        reply_to_message_id=id,
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
                        f"Error while getting {url} file: {resp.status}"
                    )
                    return

                mime = resp.content_type
                form_data = aiohttp.FormData()
                form_data.add_field(
                    'file', resp.content, filename=fname,
                    content_type=mime
                )

                try:
                    message = None
                    if mime.startswith("image"):
                        message = await self._telegram.api_call(
                            "sendPhoto", chat_id=self._chat,
                            photo="attach://file", _file=form_data,
                            caption=f"{sender}: ",
                            caption_entities=self._make_bold_entity(sender, 0)
                        )
                    elif mime.startswith("video"):
                        message = await self._telegram.api_call(
                            "sendVideo", chat_id=self._chat,
                            video="attach://file", _file=form_data,
                            caption=f"{sender}: ",
                            caption_entities=self._make_bold_entity(sender, 0)
                        )
                    elif mime.startswith("audio"):
                        message = await self._telegram.api_call(
                            "sendAudio", chat_id=self._chat,
                            audio="attach://file", _file=form_data,
                            caption=f"{sender}: ",
                            caption_entities=self._make_bold_entity(sender, 0)
                        )
                    else:
                        message = await self._telegram.api_call(
                            "sendDocument", chat_id=self._chat,
                            document="attach://file", _file=form_data,
                            caption=f"{sender}: ",
                            caption_entities=self._make_bold_entity(sender, 0)
                        )

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
                f"Message with {stanza_id} not found in the message map",
                "It is not possible to edit a message"
            )
            return

        try:
            if not reply:

                message = await self._telegram.api_call(
                    "editMessageText", chat_id=self._chat,
                    text=f"{sender}: {text}",
                )
            else:
                id = self._reply_map.get(reply)

                if id:
                    self._logger.debug(
                        f"Found reply to a message with ID {id} in reply map"
                    )
                    message = await self._telegram.api_call(
                        "editMessageText", chat_id=self._chat,
                        text=f"{sender}: {body}",
                        message_id=telegram_id
                    )
                else:
                    self._logger.debug(
                        f"Reply with body {reply} not found in the reply map"
                    )
                    formatted_reply = "> " + reply.replace("\n", "\n> ")
                    message = await self._telegram.api_call(
                        "editMessageText", chat_id=self._chat,
                        text=f"{formatted_reply}\n{sender}: {body}",
                        message_id=telegram_id
                    )
        except TelegramApiError:
            self._logger.exception("Error while editing a message")

        self._reply_map.add(body, message['message_id'])
        self._message_map.add(stanza_id, message['message_id'])

    async def process_on_join(self, user: dict, event_id: int):
        name = user.get("first_name")

        if user.get("last_name"):
            name += " " + user.get("last_name")

        await self._xmpp.send_message(
            f"*{name}* joined the chat", BRIDGE_DEFAULT_NAME, event_id
        )

    async def process_on_title_changed(self, title: str, event_id: int):
        await self._xmpp.send_message(
            f"The name of chat chat has been changed to \"{title}\"",
            BRIDGE_DEFAULT_NAME, event_id
        )

    async def process_on_leave(self, user: dict, event_id: int):
        name = user.get("first_name")

        if user.get("last_name"):
            name += " " + user.get("last_name")

        await self._xmpp.send_message(
            f"*{name}* left the chat", BRIDGE_DEFAULT_NAME, event_id
        )

    async def send_event(self, actor: str, event: str):
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
                f"An error occurred while sending the event: {event}"
            )

    async def unbridge(self):
        await self.send_event(None, UNBRIDGE_TELEGRAM_MESSAGE)
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
    def __init__(self, room: Room, room_future, message_map: MessageMap):
        self._room: Room = room
        self._room_future = room_future
        self._loop = asyncio.get_event_loop()
        self._xmpp = XmppClient()
        self._telegram: TelegramChatHandler = None
        self._message_map: MessageMap = message_map
        self._logger = logging.getLogger(
            f"XmppRoomHandler ({str(room.jid.bare())})"
        )
        self._logger.info("New XmppRoomHandler created")
        self._nick_lock = asyncio.Lock()
        self._last_sender = None

    def on_exit(self, *, muc_leave_mode=None, muc_actor=None, muc_reason=None,
                muc_status_codes=set(), **kwargs):
        if muc_leave_mode in (LeaveMode.DISCONNECTED,
                              LeaveMode.SYSTEM_SHUTDOWN,
                              LeaveMode.NORMAL,
                              LeaveMode.ERROR):
            return

        self._logger.info(
            "Exit event received, execute unbridge from Telegram"
        )
        self._loop.create_task(self._telegram.unbridge())
        self._telegram = None

    def on_leave(self, member, *, muc_leave_mode=None, muc_actor=None,
                 muc_reason=None, **kwargs):
        nick = member.nick
        if muc_leave_mode == LeaveMode.NORMAL:
            if not nick.endswith("(Telegram)") and not BRIDGE_DEFAULT_NAME:
                self._loop.create_task(
                    self._telegram.send_event(nick, "left the room")
                )

    def on_muc_enter(self, member, **kwargs):
        if self._room.me is None:
            return

        if member == self._room.me:
            return

        nick = member.nick
        if not nick.endswith("(Telegram)") and not BRIDGE_DEFAULT_NAME:
            self._loop.create_task(
                self._telegram.send_event(nick, "joined the room")
            )

    def on_nick_changed(self, member, old_nick, new_nick, *,
                        muc_status_codes=None, **kwargs):
        if member == self._room.me:
            self._last_sender = new_nick
            return

        self._loop.create_task(
            self._telegram.send_event(
                old_nick, f"has changed nickname to \"{new_nick}\""
            )
        )

    def on_topic_changed(self, member, new_topic, *, muc_nick, **kwargs):
        topic = new_topic.any()

        if not topic:
            return

        self._logger.info("Changed the room name to ", topic)

        self._loop.create_task(
            self._telegram.send_event(
                member.nick, f"has changed room name to \"{topic}\""
            )
        )

    def process_message(self, message: Message, member: Occupant, source,
                        **kwargs):
        # Not handling your own messages
        if member == self._room.me:
            return

        if message.xep0066_oob:
            self._loop.create_task(
                self._telegram.send_attachment(
                    member.nick, message.xep0066_oob.url,
                )
            )
        elif message.xep0308_replace:
            self._loop.create_task(
                self._telegram.edit_message(
                    message.xep0308_replace.id_,
                    message.body.any(), member.nick
                )
            )
        else:
            self._loop.create_task(
                self._telegram.send_message(
                    member.nick, message.body.any(), message.id_
                )
            )

    async def send_message(self, text: str, sender: str, telegram_id: str):
        msg = Message(type_=aioxmpp.MessageType.GROUPCHAT)
        msg.body[None] = text

        self._logger.info(f"Recieved message from telegram: {telegram_id}")
        try:
            await self._set_nick(sender)

            (base, tracker) = self._room.send_message_tracked(msg)

            def state_callback(state, response):
                if state != MessageState.DELIVERED_TO_RECIPIENT:
                    return

                self._message_map.add(telegram_id, response.id_)

            tracker.on_state_changed.connect(state_callback)
        except Exception as ex:
            self._logger.exception("Error occured while sending message", ex)

    async def send_attachment(self, sender: str, url: str, fname: str,
                              fsize: int, mime: str):
        slot = await self._xmpp.get_slot(fname, fsize, mime)

        if slot is None:
            return

        headers = slot.put.headers
        headers["Content-Type"] = mime
        headers["Content-Length"] = str(fsize)
        headers["User-Agent"] = "jabagram"

        async with aiohttp.ClientSession() as session:
            async with session.put(
                slot.put.url, headers=headers, data=self._file_fetcher(url)
            ) as resp:
                if resp.status not in (200, 201):
                    self._logger.error(
                        f"Upload {fname} failed: {resp.status}"
                    )
                    return

        msg = Message(type_=aioxmpp.MessageType.GROUPCHAT)

        # We should force the language tag because
        # some clients ignore attachments without it
        # https://dev.gajim.org/gajim/gajim/-/issues/9178
        tag = aioxmpp.structs.LanguageTag.fromstr("en")
        msg.body[tag] = slot.get.url
        msg.xep0066_oob = OOBExtension()
        msg.xep0066_oob.url = slot.get.url

        try:
            await self._set_nick(sender)
            await self._room.send_message(msg)
        except Exception:
            self._logger.exception("Error while sending attachment")

    async def edit_message(self, stanza_id: str, text: str):
        msg = Message(type_=aioxmpp.MessageType.GROUPCHAT)
        msg.body[None] = text
        msg.xep0308_replace = Replace()
        msg.xep0308_replace.id_ = stanza_id

        try:
            await self._room.send_message(msg)
        except Exception:
            self._logger.exception("Error while editing a message")

    async def _file_fetcher(self, url: str, max_chunk_size=4096):
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                async for chunk in resp.content.iter_chunked(max_chunk_size):
                    self._logger.debug(f"Send chunk with size {len(chunk)}")
                    yield chunk

    async def _set_nick(self, sender: str):
        if self._last_sender == sender:
            return

        async with self._nick_lock:
            try:
                await self._room.set_nick(sender)

                # Wait at least 100ms for the bot to change
                # its nickname before sending a message
                while self._last_sender != sender:
                    asyncio.sleep(0.1)
            # BUG: Raises when changing a nickname containing an emoji
            except ValueError as ex:
                self._logger.exception(
                    "An invalid user name has been passed", ex
                )

    async def unbridge(self):
        msg = Message(type_=aioxmpp.MessageType.GROUPCHAT)
        msg.body[None] = UNBRIDGE_XMPP_MESSAGE
        await self._set_nick(BRIDGE_DEFAULT_NAME)
        await self._room.send_message(msg)
        await self._room.leave()

        self._telegram = None
        self._room = None

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

    logging.basicConfig(
        format="[%(asctime)s] %(name)s - %(levelname)s: %(message)s",
        level=(logging.DEBUG if args.verbose else logging.INFO)
    )

    logger = logging.Logger("Main")

    try:
        config = configparser.ConfigParser()

        with open(args.config, "r") as f:
            config.read_file(f)

        ChatManager(args.data)

        telegram = TelegramClient(
            config.get("telegram", "token"),
            config.get("xmpp", "login")
        )
        xmpp = XmppClient(
            config.get("xmpp", "login"),
            config.get("xmpp", "password"),
            config.get("general", "key")
        )

        loop = asyncio.get_event_loop()
        loop.create_task(xmpp.run())
        loop.create_task(telegram.run())

        def exception_handler(_, context):
            exception = context.get("exception")

            if exception:
                logger.warning("Some unhandled error occured: %s", exception)

        loop.set_exception_handler(exception_handler)
        loop.run_forever()
    except FileNotFoundError:
        logger.error(
            """Configuration file not found
            Perhaps you forgot to rename config.ini.example?
            Use the -c key to specify the full path to the config.
            """
        )
    except configparser.NoOptionError:
        logger.exception("Missing mandatory option")
    except configparser.Error:
        logger.exception("Config parsing error")


if __name__ == "__main__":
    main()
