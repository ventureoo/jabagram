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
import logging
import aiohttp

from json import dumps
from typing import Any

from aiohttp import ClientConnectionError
from datetime import datetime
from jabagram.database.messages import MessageIdEntry, MessageStorage
from jabagram.telegram.api import TelegramApi, TelegramApiError
from jabagram.model import ChatHandler, Event, Message, Attachment
from jabagram.messages import Messages

# When an XMPP user replies to a message coming from a side topic, all of his
# further simple messages (not replies) are forwarded to that Telegram chat
# topic. This constant specifies the time after which simple messages will be
# forwarded to the main chat again.
TELEGRAM_TOPIC_TIMEOUT = 10

class TopicTimeoutEntry():
    def __init__(self, topic_id: int, time: datetime):
        self.__topic_id = topic_id
        self.__time = time

    @property
    def time(self):
        return self.__time

    @time.setter
    def time(self, time: datetime):
        self.__time = time

    @property
    def topic_id(self):
        return self.__topic_id

    @topic_id.setter
    def topic_id(self, topic_id: int):
        self.__topic_id = topic_id

class TelegramChatHandler(ChatHandler):
    def __init__(
        self,
        address: str,
        api: TelegramApi,
        message_storage: MessageStorage,
        messages: Messages
    ) -> None:
        super().__init__(address)
        self.__address = address
        self.__message_storage = message_storage
        self.__logger = logging.getLogger(f"TelegramChatHandler ({address})")
        self.__api = api
        self.__messages = messages
        self.__residence_map: dict[str, TopicTimeoutEntry] = {}

    def __make_bold_sender_name(self, text: str):
        message_entities = [
            {
                "type": "bold",
                "offset": 0,
                "length": len(text)
            }
        ]
        return dumps(message_entities)

    def __is_time_left(self, last: datetime, timeout: int) -> bool:
        return (datetime.now() - last).total_seconds() < timeout

    async def send_message(self, origin: Message) -> None:
        params: dict[str, Any] = {
            "text": f"{origin.sender.name}: {origin.content}",
            "chat_id": self.address,
            "entities": self.__make_bold_sender_name(origin.sender.name)
        }
        entry: TopicTimeoutEntry | None = self.__residence_map.get(origin.sender.name)

        if origin.reply:
            result: MessageIdEntry | None = self.__message_storage.get_by_body(
                chat_id=int(self.__address),
                topic_id=None,
                muc=origin.chat.address,
                body=origin.reply,
            )
            if result:
                params["text"] = f"{origin.sender.name}: {origin.content}"
                params["reply_to_message_id"] = result.telegram_id

                if result.topic_id:
                    params["message_thread_id"] = result.topic_id
                    entry = TopicTimeoutEntry(result.topic_id, datetime.now())
                else:
                    del self.__residence_map[origin.sender.name]
                    entry = None
            else:
                params["text"] = (
                    f"{origin.reply}\n"
                    f"{origin.sender.name}: {origin.content}"
                )
                format = [
                    {
                        "type": "blockquote",
                        "offset": 0,
                        "length": len(origin.reply)
                    },
                    {
                        "type": "bold",
                        "offset": len(origin.reply) + 1,
                        "length": len(origin.sender.name)
                    }
                ]
                params["entities"] = dumps(format)
        else:
            if entry and self.__is_time_left(
                last=entry.time,
                timeout=TELEGRAM_TOPIC_TIMEOUT
            ):
                params["message_thread_id"] = entry.topic_id
                entry.time = datetime.now()

        try:
            response = await self.__api.sendMessage(**params)
            self.__message_storage.add(
                chat_id=int(self.__address),
                muc=origin.chat.address,
                stanza_id=origin.id,
                telegram_id=response['message_id'],
                body=origin.content,
                topic_id=response.get("message_thread_id")
            )

            if entry:
                self.__residence_map[origin.sender.name] = entry
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
                        return None

                    mime = resp.content_type
                    form_data = aiohttp.FormData()
                    form_data.add_field(
                        'file', resp.content, filename=attachment.content,
                        content_type=mime
                    )

                    method = self.__api.sendDocument
                    params: dict[str, Any] = {
                        "chat_id": self.address,
                        "caption": f"{attachment.sender.name}: ",
                        "caption_entities": self.__make_bold_sender_name(
                            attachment.sender.name
                        ),
                    }

                    entry: TopicTimeoutEntry | None = self.__residence_map.get(
                        attachment.sender.name
                    )
                    if entry and self.__is_time_left(
                        last=entry.time,
                        timeout=TELEGRAM_TOPIC_TIMEOUT
                    ):
                        params["message_thread_id"] = entry.topic_id
                        entry.time = datetime.now()

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
                        response = await method(form_data, **params)
                        self.__message_storage.add(
                            chat_id=int(self.__address),
                            muc=attachment.chat.address,
                            stanza_id=attachment.id,
                            telegram_id=response['message_id'],
                            body=attachment.content,
                            topic_id=response.get("message_thread_id")
                        )
                        if entry:
                            self.__residence_map[attachment.sender.name] = entry
                    except TelegramApiError as error:
                        try:
                            await self.__api.sendMessage(
                                chat_id=self.address,
                                text=(
                                    "Couldn't transfer file"
                                    f"{attachment.content} "
                                    f"from {attachment.sender.name}"
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

    async def edit_message(self, edited: Message) -> None:
        result = self.__message_storage.get_by_id(
            chat_id=int(self.__address),
            topic_id=None,
            muc=edited.chat.address,
            message_id=edited.id
        )

        if not result:
            self.__logger.info(
                "Failed to found telegram message id for event: %s",
                edited.id
            )
            return

        params = {
            "chat_id": self.address,
            "text": f"{edited.sender.name}: {edited.content}",
            "message_id": result.telegram_id,
            "entities": self.__make_bold_sender_name(edited.sender.name)
        }

        if edited.reply:
            # Be sure that replies to messages was sent as native in Telegram
            if self.__message_storage.get_by_body(
                chat_id=int(self.__address),
                topic_id=edited.chat.topic_id,
                muc=edited.chat.address,
                body=edited.reply
            ):
                params["text"] = f"{edited.sender.name}: {edited.content}"
            else:
                params["text"] = (
                    f"{edited.reply}\n"
                    f"{edited.sender.name}: {edited.content}"
                )
                format = [
                    {
                        "type": "blockquote",
                        "offset": 0,
                        "length": len(edited.reply)
                    },
                    {
                        "type": "bold",
                        "offset": len(edited.reply) + 1,
                        "length": len(edited.sender.name)
                    }
                ]
                params["entities"] = dumps(format)
        try:
            response = await self.__api.editMessageText(**params)
            self.__message_storage.add(
                chat_id=int(self.__address),
                muc=edited.chat.address,
                stanza_id=edited.id,
                telegram_id=response['message_id'],
                body=edited.content,
                topic_id=response.get("message_thread_id")
            )
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
