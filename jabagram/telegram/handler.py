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
from jabagram.telegram.api import TelegramApi, TelegramApiError
from jabagram.model import ChatHandler, Event, Message, Attachment
from jabagram.messages import Messages
from jabagram.cache import Cache

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
        cache: Cache,
        messages: Messages
    ) -> None:
        super().__init__(address)
        self.__cache = cache
        self.__logger = logging.getLogger(f"TelegramChatHandler ({address})")
        self.__api = api
        self.__messages = messages
        self.__residence_map: dict[str, TopicTimeoutEntry] = {}
        self.__topic_ids_cache: dict[str, int] = {}

    def __make_bold_sender_name(self, text: str):
        message_entities = [
            {
                "type": "bold",
                "offset": 0,
                "length": len(text)
            }
        ]
        return dumps(message_entities)

    def __is_time_left(self, time: datetime, value: int) -> float:
        return (datetime.now() - time).total_seconds() < value

    def add_topic_id(self, message_id: str, topic_id: int):
        self.__topic_ids_cache[message_id] = topic_id

    async def send_message(self, message: Message) -> None:
        params: dict[str, Any] = {
            "text": f"{message.sender}: {message.content}",
            "chat_id": self.address,
            "entities": self.__make_bold_sender_name(message.sender)
        }

        if message.reply:
            telegram_id: str | None = self.__cache.reply_map.get(message.reply)
            if telegram_id:
                params["text"] = f"{message.sender}: {message.content}"
                params["reply_to_message_id"] = telegram_id

                entry: TopicTimeoutEntry | None = self.__residence_map.get(
                    message.sender
                )

                topic_id = self.__topic_ids_cache.get(telegram_id)
                if topic_id:
                    params["message_thread_id"] = topic_id
                    if entry:
                        entry.time = datetime.now()
                        entry.topic_id = topic_id
                else:
                    if entry and self.__is_time_left(entry.time, TELEGRAM_TOPIC_TIMEOUT):
                        params["message_thread_id"] = entry.topic_id
                        entry.time = datetime.now()
            else:
                params["text"] = (
                    f"{message.reply}\n"
                    f"{message.sender}: {message.content}"
                )
                format = [
                    {
                        "type": "blockquote",
                        "offset": 0,
                        "length": len(message.reply)
                    },
                    {
                        "type": "bold",
                        "offset": len(message.reply) + 1,
                        "length": len(message.sender)
                    }
                ]
                params["entities"] = dumps(format)
        else:
            entry: TopicTimeoutEntry | None = self.__residence_map.get(
                message.sender
            )
            if entry and self.__is_time_left(entry.time, TELEGRAM_TOPIC_TIMEOUT):
                params["message_thread_id"] = entry.topic_id
                entry.time = datetime.now()

        try:
            response = await self.__api.sendMessage(**params)
            self.__cache.reply_map.add(
                message.content, response['message_id']
            )
            self.__cache.message_ids.add(
                message.event_id, response['message_id']
            )
            thread_id = response.get("message_thread_id")
            if thread_id:
                self.__topic_ids_cache[response['message_id']] = thread_id
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
                    params: dict[str, Any] = {
                        "chat_id": self.address,
                        "caption": f"{attachment.sender}: ",
                        "caption_entities": self.__make_bold_sender_name(
                            attachment.sender
                        ),
                    }

                    entry: TopicTimeoutEntry | None = self.__residence_map.get(
                        attachment.sender
                    )
                    if entry and self.__is_time_left(
                        entry.time, TELEGRAM_TOPIC_TIMEOUT
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
                        self.__cache.reply_map.add(
                            attachment.content, response['message_id']
                        )
                        thread_id = response.get("message_thread_id")
                        if thread_id:
                            self.__topic_ids_cache[response['message_id']] = thread_id
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
            message.event_id
        )

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
            "entities": self.__make_bold_sender_name(message.sender)
        }

        if message.reply:
            # Be sure that replies to messages was sent as native in Telegram
            if self.__cache.reply_map.get(message.reply):
                params["text"] = f"{message.sender}: {message.content}"
            else:
                params["text"] = (
                    f"{message.reply}\n"
                    f"{message.sender}: {message.content}"
                )
                format = [
                    {
                        "type": "blockquote",
                        "offset": 0,
                        "length": len(message.reply)
                    },
                    {
                        "type": "bold",
                        "offset": len(message.reply) + 1,
                        "length": len(message.sender)
                    }
                ]
                params["entities"] = dumps(format)
        try:
            response = await self.__api.editMessageText(**params)
            self.__cache.reply_map.add(message.content, message.event_id)

            thread_id = response.get("message_thread_id")
            if thread_id:
                self.__topic_ids_cache[response['message_id']] = thread_id
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
