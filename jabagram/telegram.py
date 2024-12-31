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
from typing import Optional, Type
from types import TracebackType
import logging
import mimetypes

import aiohttp
from aiohttp import ClientConnectionError, ClientResponseError, TCPConnector
from jabagram.messages import Messages
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
    TelegramAttachment,
    UnbridgeEvent,
)


class TelegramApiError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"Telegram API error ({code}): {desc}")
        self.code = code
        self.desc = desc


class TelegramApi():
    def __init__(self, token):
        self.__token = token
        self.__session = aiohttp.ClientSession(
            # Fix issues with long timeouts between messages
            # https://github.com/aiogram/aiogram/issues/1500
            connector=TCPConnector(ttl_dns_cache=3600)
        )
        self.__logger = logging.getLogger(__class__.__name__)

    async def __aenter__(self) -> "TelegramApi":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        _: Optional[TracebackType]
    ) -> None:
        if exc_value and exc_type:
            self.__logger.error(
                "Unhandled exception occured: %s - %s",
                exc_type,
                exc_value
            )

        await self.close()

    async def close(self) -> None:
        await self.__session.close()

    def __getattr__(self, method):
        async def wrapper(*file, **kwargs):
            url = f"https://api.telegram.org/bot{self.__token}/{method}"
            params = {
                "params": kwargs,
                "data": file[0] if file else None
            }
            retry_attempts = 5

            while retry_attempts > 0:
                if retry_attempts != 5:
                    self.__logger.info(
                        "Retry to send request, attempts left: %s",
                        retry_attempts
                    )

                retry_attempts = retry_attempts - 1
                try:
                    async with self.__session.post(
                        url=url,
                        **params
                    ) as response:
                        # If an unknown error occurred and the response
                        # does not represent a valid TelegramApi error,
                        # ClientResponseError is raised
                        results = await response.json()

                        if results.get("ok"):
                            return results['result']

                        description = results.get('description')
                        paramaters = results.get("parameters")

                        if response.status == 429 and paramaters:
                            timeout = paramaters.get("retry_after")
                            self.__logger.warning(
                                "Too many requests, request "
                                "will be executed again in: %d secs",
                                timeout
                            )
                            await asyncio.sleep(timeout)
                        else:
                            raise TelegramApiError(
                                response.status, description
                            )

                except ClientConnectionError as error:
                    self.__logger.error(
                        "Failed to execute the request: %s", error
                    )
                except ClientResponseError as error:
                    self.__logger.error(
                        "Failed to get Telegram response: %s", error
                    )
                except asyncio.TimeoutError:
                    continue

            raise TelegramApiError(
                -1, "The number of retry attempts has been exhausted"
            )

        return wrapper


class TelegramChatHandler(ChatHandler):
    def __init__(
        self,
        address: str,
        api: TelegramApi,
        cache: Cache,
        messages: Messages
    ) -> None:
        super().__init__(address)
        self.__cache = cache
        self.__logger = logging.getLogger(f"TelegramChatHandler ({address})")
        self.__api = api
        self.__messages = messages

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
                params["text"] = (
                    f"{formatted_reply}\n"
                    f"{message.sender}: {message.content}"
                )
                params["entities"] = self.__make_bold_entity(
                    message.sender, len(formatted_reply) + 1
                )

        try:
            response = await self.__api.sendMessage(**params)
            self.__cache.reply_map.add(message.content, response['message_id'])
            self.__cache.message_ids.add(
                message.event_id, response['message_id']
            )
        except TelegramApiError as error:
            self.__logger.error("Error sending a message: %s", error)

    async def send_attachment(self, attachment: Attachment) -> None:
        url = await attachment.url_callback()

        try:
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
                        'file', resp.content, filename=attachment.content,
                        content_type=mime
                    )

                    method = self.__api.sendDocument
                    params = {
                        "chat_id": self.address,
                        "caption": f"{attachment.sender}: ",
                        "caption_entities": self.__make_bold_entity(
                            attachment.sender, offset=0
                        )
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
                        self.__cache.reply_map.add(
                            attachment.content, message['message_id'])
                    except TelegramApiError as error:
                        try:
                            await self.__api.sendMessage(
                                chat_id=self.address,
                                text=(
                                    "Couldn't transfer file"
                                    f"{attachment.content} "
                                    f"from {attachment.sender}"
                                )
                            )
                        except TelegramApiError as send_message_error:
                            self.__logger.error(
                                "Failed to send error message: %s",
                                send_message_error
                            )

                        self.__logger.error(
                            "Failed to send file to telegram: %s", error
                        )
        except ClientConnectionError as error:
            self.__logger.error("Failed to upload attachment: %s", error)

    async def edit_message(self, message: Message) -> None:
        telegram_id: str | None = self.__cache.message_ids.get(
            message.event_id)

        if not telegram_id:
            self.__logger.info(
                "Failed to found telegram message id for event: %d",
                message.event_id
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
                params["text"] = (
                    f"{formatted_reply}\n"
                    f"{message.sender}: {message.content}"
                )
                params["entities"] = self.__make_bold_entity(
                    message.sender, len(formatted_reply) + 1
                )
        try:
            response = await self.__api.editMessageText(**params)
            self.__cache.reply_map.add(message.content, response['message_id'])
        except TelegramApiError as error:
            self.__logger.error("Error while editing a message: %s", error)

    async def send_event(self, event: Event) -> None:
        try:
            await self.__api.sendMessage(
                chat_id=self.address,
                text=event.content
            )
        except TelegramApiError as error:
            self.__logger.error(
                "Failed to send event: %s", error
            )

    async def unbridge(self) -> None:
        try:
            await self.__api.sendMessage(
                chat_id=self.address,
                text=self.__messages.unbridge_telegram
            )
            await self.__api.leaveChat(chat_id=self.address)
        except TelegramApiError as error:
            self.__logger.error(
                "Failed to unbridge chat: %s", error
            )


class TelegramClient(ChatHandlerFactory):
    def __init__(
        self,
        token: str,
        jid: str,
        service: ChatService,
        dispatcher: MessageDispatcher,
        messages: Messages
    ) -> None:
        self.__api: TelegramApi = TelegramApi(token)
        self.__token: str = token
        self.__jid: str = jid
        self.__logger = logging.getLogger(__class__.__name__)
        self.__disptacher: MessageDispatcher = dispatcher
        self.__service: ChatService = service
        self.__service.register_factory(self)
        self.__messages = messages

    async def create_handler(
        self,
        address: str,
        muc: str,
        cache: Cache,
    ) -> None:
        handler = TelegramChatHandler(
            address,
            self.__api,
            cache,
            self.__messages
        )
        self.__disptacher.add_handler(muc, handler)

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
                match update:
                    case {"message": {"chat": {
                        "type": "group" | "supergroup", "id": chat
                    }} as message} if self.__disptacher.is_bound(str(chat)):
                        await self.__process_message(message)
                    case {"message": {"text": text, "chat": {
                        "type": "group" | "supergroup", "id": chat
                    }}} if not self.__disptacher.is_bound(str(chat)) \
                            and text.startswith("/jabagram"):
                        await self.__bridge_command(str(chat), text)
                    case {"edited_message": {"chat": {
                        "type": "group" | "supergroup", "id": chat
                    }} as message} if self.__disptacher.is_bound(str(chat)):
                        await self.__process_message(message, edit=True)
                    case {"my_chat_member": {"chat": {
                        "type": "group" | "supergroup", "id": chat
                    }} as member} if self.__disptacher.is_bound(str(chat)):
                        await self.__process_kick_event(member)

            params["offset"] = updates[len(updates) - 1]['update_id'] + 1

    async def __bridge_command(self, chat_id: str, cmd: str) -> None:
        try:
            try:
                muc_address = cmd.split(" ")[1]

                # Check that MUC jid is valid
                JID(muc_address)

                self.__service.pending(muc_address, chat_id)

                await self.__api.sendMessage(
                    chat_id=chat_id,
                    text=self.__messages.queueing_message.format(self.__jid)
                )
            except IndexError:
                await self.__api.sendMessage(
                    chat_id=chat_id,
                    text=self.__messages.missing_muc_jid
                )
            except InvalidJID:
                await self.__api.sendMessage(
                    "sendMessage", chat_id=chat_id,
                    text=self.__messages.invalid_jid
                )
        except TelegramApiError as error:
            self.__logger.error(
                "Error processing the bridge command: %s", error
            )

    def __extract_attachment(
            self,
            sender: str,
            message: dict
    ) -> TelegramAttachment | None:
        match message:
            # We do not send animated stickers because they are in TGS format,
            # which cannot be properly rendered in XMPP clients.
            case {"sticker": sticker} if not sticker.get("is_animated"):
                extension = "webm" if sticker.get("is_video") else "webp"
                emoji = sticker['emoji'] if sticker.get("emoji") else ""
                return TelegramAttachment(
                    is_cacheable=True,
                    fname=f"Sticker {emoji} from {sender}.{extension}",
                    file_id=sticker['file_id'],
                    file_unique_id=sticker['file_unique_id'],
                    fsize=sticker.get("file_size"),
                    mime="image/webm" if sticker.get(
                        "is_video") else "video/webp"
                )
            case {"photo": [*_, photo]}:
                return TelegramAttachment(
                    fname=f"Photo from {sender}.jpg",
                    file_id=photo['file_id'],
                    file_unique_id=photo['file_unique_id'],
                    fsize=photo.get("file_size"),
                    mime="image/jpeg"
                )
            case {"video": video} | {"video_note": video} | \
                    {"animation": video}:
                mime = video.get("mime_type")
                extension = mimetypes.guess_extension(mime or "video/mp4")
                return TelegramAttachment(
                    fname=video.get("file_name") or (
                        f"Video from {sender}.{extension}"
                    ),
                    file_id=video['file_id'],
                    file_unique_id=video['file_unique_id'],
                    fsize=video.get("file_size"),
                    mime=mime
                )
            case {"voice": voice}:
                return TelegramAttachment(
                    fname=f"Voice message from {sender}.ogg",
                    file_id=voice['file_id'],
                    file_unique_id=voice['file_unique_id'],
                    fsize=voice.get("file_size"),
                    mime="audio/ogg"
                )
            case {"audio": audio}:
                mime = audio.get("mime_type")
                extension = mimetypes.guess_extension(mime or "audio/mpeg")
                return TelegramAttachment(
                    fname=audio.get("file_name") or (
                        f"Audio from {sender}.{extension}"
                    ),
                    file_id=audio['file_id'],
                    file_unique_id=audio['file_unique_id'],
                    fsize=audio.get("file_size"),
                    mime=mime
                )
            case {"document": document}:
                mime = document.get("mime_type")
                extension = "." + \
                    (mimetypes.guess_extension(mime) or "") if mime else ""
                return TelegramAttachment(
                    fname=document.get("file_name") or (
                        f"Document from {sender}{extension}"
                    ),
                    file_id=document['file_id'],
                    file_unique_id=document['file_unique_id'],
                    fsize=document.get("file_size"),
                    mime=mime
                )

        return None

    def __get_reply(self, message: dict) -> str | None:
        reply: dict | None = message.get("reply_to_message")
        if not reply:
            return None

        sender: str = self.__get_full_name(reply['from'])
        attachment = self.__extract_attachment(sender, reply)
        reply_body = reply.get("text") or reply.get("caption")

        if not reply_body and attachment:
            reply_body = attachment.fname

        return reply_body

    async def __process_message(self, raw_message: dict, edit=False) -> None:
        chat_id = str(raw_message['chat']['id'])
        sender: str = self.__get_full_name(raw_message['from'])
        message_id = str(raw_message['message_id'])
        text: str | None = raw_message.get(
            "text") or raw_message.get("caption")
        reply: str | None = self.__get_reply(raw_message)
        attachment: TelegramAttachment | None = self.__extract_attachment(
            sender, raw_message
        )

        if attachment:
            async def url_callback():
                try:
                    file = await self.__api.getFile(file_id=attachment.file_id)
                    file_path = file['file_path']
                    url = (
                        f"https://api.telegram.org/file/bot"
                        f"{self.__token}/{file_path}"
                    )
                    return url
                except TelegramApiError as error:
                    self.__logger.error(
                        "Failed to get url of attachment: %s", error
                    )

            # Right now we can cache only stickers
            if attachment.is_cacheable:
                await self.__disptacher.send(
                    Sticker(
                        event_id=message_id,
                        content=attachment.fname,
                        address=chat_id,
                        sender=sender,
                        file_id=attachment.file_unique_id,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback
                    )
                )
            else:
                await self.__disptacher.send(
                    Attachment(
                        event_id=message_id,
                        address=chat_id,
                        sender=sender,
                        content=attachment.fname,
                        # if we have text, reply should be nested
                        # in the message below
                        reply=None if text else reply,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback
                    )
                )

        if text:
            await self.__disptacher.send(
                Message(
                    event_id=message_id,
                    address=chat_id,
                    content=text,
                    sender=sender,
                    reply=reply,
                    edit=edit
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
