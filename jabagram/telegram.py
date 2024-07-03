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

import asyncio
from json import dumps
import logging
import mimetypes

import aiohttp
from aiohttp import ClientConnectionError
from slixmpp.jid import InvalidJID, JID

from .cache import Cache
from .database import ChatService
from .dispatcher import MessageDispatcher
from .model import (
    Attachment,
    ChatHandler,
    ChatHandlerFactory,
    Event,
    Message,
    Sticker,
    UnbridgeEvent,
)

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

UNBRIDGE_TELEGRAM_MESSAGE = """
This chat was automatically unbridged due to a bot kick in XMPP.
If you want to bridge it again, invite this bot to this chat again
and use the /jabagram command.
"""

MISSING_MUC_JID_MESSAGE = """
Please specify the MUC address of room you want to pair with
this Telegram chat.
"""

class TelegramApiError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"Telegram API error ({code}): {desc}")
        self.code = code
        self.desc = desc

class TelegramApi():
    def __init__(self, token):
        self.__token = token
        self.__logger  = logging.getLogger(__class__.__name__)

    def __getattr__(self, method):
        async def wrapper(*file, **kwargs):
            url = f"https://api.telegram.org/bot{self.__token}/{method}"
            timeout = aiohttp.client.ClientTimeout(total=300)

            if method == "getUpdates":
                timeout = aiohttp.client.ClientTimeout(total=0)

            params = {
                "timeout": timeout,
                "params": kwargs,
                "data": file[0] if file else None
            }
            retry_attempts = 5

            async with aiohttp.ClientSession() as session:
                http_method = session.post if file else session.get
                while retry_attempts > 0:
                    try:
                        async with http_method(url, **params) as response:
                            results = await response.json()

                            if file and response.status != 200:
                                raise TelegramApiError(
                                    response.status, "Failed to upload file"
                                )

                            if not results.get("ok"):
                                params = results.get("parameters")
                                error_code = results['error_code']
                                desc = results['description']

                                if params and params.get("retry_after"):
                                    await asyncio.sleep(params["retry_after"])
                                    retry_attempts = retry_attempts - 1
                                    self.__logger.warning(
                                        "Too many requests, " \
                                        "request will be executed again in: %d",
                                        params['retry_after']
                                    )
                                    continue

                                raise TelegramApiError(error_code, desc)

                            return results['result']
                    except ClientConnectionError as error:
                        self.__logger.error(
                            "Failed to execute the request: %s", error
                        )
                        retry_attempts = retry_attempts - 1

                raise TelegramApiError(
                    -1, "The number of retry attempts has been exhausted"
                )

        return wrapper

class TelegramChatHandler(ChatHandler):
    def __init__(
        self,
        address: str,
        api: TelegramApi,
        cache: Cache
    ) -> None:
        super().__init__(address)
        self.__cache = cache
        self.__logger = logging.getLogger(f"TelegramChatHandler ({address})")
        self.__api = api

    def __make_bold_entity(self, text: str, offset: int):
        message_entities = [
            {
                "type": "bold",
                "offset": offset,
                "length": len(text)
            }
        ]
        return dumps(message_entities)

    async def send_message(self, message: Message) -> None:
        params = {
            "text": f"{message.sender}: {message.content}",
            "chat_id": self.address,
            "entities": self.__make_bold_entity(message.sender, 0)
        }

        if message.reply:
            telegram_id = self.__cache.reply_map.get(message.reply)
            if telegram_id:
                params["text"] = f"{message.sender}: {message.content}"
                params["reply_to_message_id"] = telegram_id
            else:
                formatted_reply = "> " + message.reply.replace("\n", "\n> ")
                params["text"] = f"{formatted_reply}\n{message.sender}: {message.content}"
                params["entities"] = self.__make_bold_entity(
                    message.sender, len(formatted_reply) + 1
                )

        try:
            response = await self.__api.sendMessage(**params)
            self.__cache.reply_map.add(message.content, response['message_id'])
            self.__cache.message_ids.add(message.event_id, response['message_id'])
        except TelegramApiError as error:
            self.__logger.error("Error sending a message: %s", error)

    async def send_attachment(self, attachment: Attachment) -> None:
        url = await attachment.url_callback()

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status not in (200, 201):
                    self.__logger.error(
                        "Error while getting %s file: %d", url, resp.status
                    )
                    return

                mime = resp.content_type
                form_data = aiohttp.FormData()
                form_data.add_field(
                    'file', resp.content, filename=attachment.fname,
                    content_type=mime
                )

                method = self.__api.sendDocument
                params = {
                    "chat_id": self.address,
                    "caption": f"{attachment.sender}: ",
                    "caption_entities": self.__make_bold_entity(attachment.sender, 0)
                }

                if mime == "image/gif":
                    method = self.__api.sendAnimation
                    params['animation'] = "attach://file"
                elif mime.startswith("image"):
                    method = self.__api.sendPhoto
                    params['photo'] = "attach://file"
                elif mime.startswith("video"):
                    method = self.__api.sendVideo
                    params['video'] = "attach://file"
                elif mime.startswith("audio"):
                    method = self.__api.sendAudio
                    params['audio'] = "attach://file"
                else:
                    params['document'] = "attach://file"

                try:
                    message = await method(form_data, **params)
                    self.__cache.reply_map.add(attachment.fname, message['message_id'])
                except TelegramApiError:
                    await self.__api.sendMessage(
                        chat_id=self.address,
                        text=f"Couldn't transfer file {attachment.fname} from {attachment.sender}"
                    )
                    self.__logger.exception("Failed to send file to telegram")

    async def edit_message(self, message: Message) -> None:
        telegram_id: str | None = self.__cache.message_ids.get(message.event_id)

        if not telegram_id:
            self.__logger.info(
                "Failed to found telegram message id for event: %d", message.event_id
            )
            return

        params = {
            "chat_id": self.address,
            "text": f"{message.sender}: {message.content}",
            "message_id": telegram_id,
            "entities": self.__make_bold_entity(message.sender, 0)
        }

        if message.reply:
            # Be sure that replies to messages was sent as native in Telegram
            if self.__cache.reply_map.get(message.reply):
                params["text"] = f"{message.sender}: {message.content}"
            else:
                formatted_reply = "> " + message.reply.replace("\n", "\n> ")
                params["text"] = f"{formatted_reply}\n{message.sender}: {message.content}"
                params["entities"] = self.__make_bold_entity(
                    message.sender, len(formatted_reply) + 1
                )
        try:
            response = await self.__api.editMessageText(**params)
            self.__cache.reply_map.add(message.content, response['message_id'])
        except TelegramApiError as error:
            self.__logger.error("Error while editing a message: %s", error)

    async def send_event(self, event: Event) -> None:
        await self.__api.sendMessage(
            chat_id=self.address,
            text=event.content
        )

    async def unbridge(self) -> None:
        await self.__api.sendMessage(
            chat_id=self.address, text=UNBRIDGE_TELEGRAM_MESSAGE
        )
        await self.__api.leaveChat(chat_id=self.address)

class TelegramClient(ChatHandlerFactory):
    def __init__(
        self,
        token: str,
        jid: str,
        service: ChatService,
        dispatcher: MessageDispatcher
    ) -> None:
        self.__api: TelegramApi = TelegramApi(token)
        self.__token: str = token
        self.__jid: str = jid
        self.__logger = logging.getLogger(__class__.__name__)
        self.__disptacher: MessageDispatcher = dispatcher
        self.__service: ChatService = service
        self.__supported_attachment_types: tuple = (
            "sticker", "photo", "voice", "video",
             "video_note", "document", "audio"
        )
        self.__service.register_factory(self)

    async def create_handler(
        self,
        address: str,
        muc: str,
        cache: Cache,
    ) -> None:
        handler = TelegramChatHandler(address, self.__api, cache)
        self.__disptacher.add_handler(muc, handler)

    def __filter_update(self, update: dict):
        chat = update.get('chat')

        if chat and chat['type'] != "private":
            chat_id = str(chat['id'])
            if self.__disptacher.is_chat_bound(chat_id):
                return True

        return False

    async def start(self):
        params = {
            "allowed_updates": ['message', 'edited_message', 'my_chat_member']
        }
        while True:
            updates = None
            try:
                updates = await self.__api.getUpdates(**params)
            except TelegramApiError as error:
                self.__logger.error("Error receiving updates: %s", error)

            if not updates:
                continue

            for update in updates:
                if update.get("message"):
                    message = update.get("message")
                    if self.__filter_update(message):
                        await self.__process_message(message)
                    else:
                        if message['chat']['type'] != "private":
                            text = message.get("text")
                            if text and text.startswith("/jabagram"):
                                await self.__bridge_command(
                                    str(message['chat']['id']), text
                                )

                elif update.get("edited_message"):
                    edit_message = update.get("edited_message")
                    if self.__filter_update(edit_message):
                        await self.__process_message(edit_message, edit=True)
                elif update.get("my_chat_member"):
                    member = update.get("my_chat_member")
                    if self.__filter_update(member):
                        await self.__process_kick_event(member)

                params["offset"] = updates[len(updates) - 1]['update_id'] + 1


    async def __bridge_command(self, chat_id: str, cmd: str):
        try:
            muc_address = cmd.split(" ")[1]

            # Check that MUC jid is valid
            JID(muc_address)

            self.__service.pending(muc_address, chat_id)

            await self.__api.sendMessage(
                chat_id=chat_id,
                text=QUEUEING_MESSAGE.format(self.__jid)
            )
        except IndexError:
            await self.__api.sendMessage(
                chat_id=chat_id,
                text=MISSING_MUC_JID_MESSAGE
            )
        except TelegramApiError as err:
            self.__logger.exception(err)
        except InvalidJID:
            await self.__api.sendMessage(
                "sendMessage", chat_id=chat_id,
                text=INVALID_JID_MESSAGE
            )

    def __unpack_attachment(self, sender: str, message: dict) -> tuple | None:
        attachment = None
        attachment_type = None
        for name in self.__supported_attachment_types:
            attachment = message.get(name)
            if attachment:
                attachment_type = name
                break
        else:
            return None

        # Check for maximum available PhotoSize
        if isinstance(attachment, list):
            attachment = attachment[-1]

        file_id = attachment.get("file_id")
        file_unique_id = attachment.get("file_unique_id")
        fname = attachment.get("file_name") or file_unique_id
        mime = attachment.get("mime_type")
        fsize = attachment.get("file_size")

        if attachment_type == "photo":
            # Telegram compresses all photos to JPEG
            # if they were not sent as a document
            mime = "image/jpeg"
            fname += ".jpg"
        elif attachment_type == "sticker":
            fname = f"Sticker from {sender}"
            if attachment.get("emoji"):
                fname += " " + attachment["emoji"]
            if attachment.get("is_video"):
                mime = "video/mp4"
                fname += ".mp4"
            elif not attachment.get("is_animated"):
                mime = "image/webp"
                fname += ".webp"
        elif attachment_type == "voice":
            fname = f"Voice message from {sender}.ogg"
        elif attachment_type == "video_note":
            fname = f"Video from {sender}.mp4"
        else:
            if mime:
                extension = mimetypes.guess_extension(mime)

                if extension and not fname.endswith(extension):
                    fname += extension

        return attachment_type, file_id, file_unique_id, fname, mime, fsize


    def __get_reply(self, message: dict) -> str | None:
        reply: dict | None = message.get("reply_to_message")
        if reply:
            sender: str = self.__get_full_name(reply['from'])
            attachment = self.__unpack_attachment(sender, reply)
            reply_body = reply.get("text") or reply.get("caption")

            if not reply_body and attachment:
                _, _, _, fname, _, _ = attachment
                reply_body = fname

            return reply_body

        return None

    async def __process_message(self, raw_message: dict, edit=False) -> None:
        chat_id = str(raw_message['chat']['id'])
        sender: str = self.__get_full_name(raw_message['from'])
        message_id = str(raw_message['message_id'])
        text: str | None = raw_message.get("text") or raw_message.get("caption")
        reply: str | None = self.__get_reply(raw_message)
        attachment: tuple | None = self.__unpack_attachment(sender, raw_message)

        if attachment:
            attachment_type, file_id, file_unique_id, fname, mime, fsize = attachment

            async def url_callback():
                try:
                    file = await self.__api.getFile(file_id=file_id)
                    file_path = file['file_path']
                    url = f"https://api.telegram.org/file/bot{self.__token}/{file_path}"
                    return url
                except TelegramApiError as error:
                    self.__logger.error(
                        "Failed to get url of attachment: %s", error
                    )

            if attachment_type == "sticker":
                await self.__disptacher.send(
                    Sticker(
                       address=chat_id, sender=sender, file_id=file_unique_id,
                       fname=fname, mime=mime, fsize=fsize,
                       url_callback=url_callback
                    )
                )
            else:
                await self.__disptacher.send(
                    Attachment(
                       address=chat_id, sender=sender,
                       fname=fname, mime=mime, fsize=fsize,
                       url_callback=url_callback
                    )
                )

        if text:
            await self.__disptacher.send(
                Message(
                    address=chat_id, event_id=message_id,
                    content=text, sender=sender,
                    reply=reply, edit=edit
                )
            )

    async def __process_kick_event(self, chat_member: dict) -> None:
        new_state = chat_member.get("new_chat_member")
        if new_state and new_state.get("status") == "left":
            await self.__disptacher.send(
                UnbridgeEvent(address=str(chat_member['chat']['id']))
            )

    def __get_full_name(self, user: dict) -> str:
        name: str = user['first_name']
        if user.get("last_name"):
            name = name + " " + user['last_name']

        return name
