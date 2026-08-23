#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2026 Vasiliy Stelmachenok <ventureo@yandex.ru>
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

from gettext import gettext as _
from io import BytesIO
from PIL import Image, UnidentifiedImageError
from typing import Any, override
from nio import AsyncClient, RoomSendError
from nio.responses import (
    RoomLeaveResponse,
    RoomSendResponse,
    UploadResponse
)

from jabagram.database.avatars import AvatarCache
from jabagram.database.messages import MessageStorage
from jabagram.model import (
    Attachment,
    Chat,
    Realm,
    ChatHandler,
    Event,
    Message,
    Sender
)

class MatrixChatHandler(ChatHandler):
    def __init__(
        self,
        chat: Chat,
        client: AsyncClient,
        message_storage: MessageStorage,
        avatar_cache: AvatarCache
    ) -> None:
        super().__init__(chat)
        self.__chat = chat
        self.__message_storage = message_storage
        self.__client = client
        self.__avatar_cache = avatar_cache
        self.__logger = logging.getLogger(f"MatrixChatHandler ({chat.address})")

    @override
    async def send_event(self, event: Event) -> None:
        content: dict[str, Any] = {
            "body": event.text,
            "msgtype": "m.text",
        }

        response = await self.__client.room_send(
            room_id=self.__chat.address,
            message_type="m.room.message",
            content=content
        )

        if not isinstance(response, RoomSendResponse):
            self.__logger.error(
                "Error when sending a event: %s",
                response
            )

    async def __get_avatar(self, user: Sender):
        avatar_url = self.__avatar_cache.get(user.id)

        if avatar_url:
            return avatar_url

        if not user.avatar_callback:
            return None

        url: str | None = await user.avatar_callback()

        if not url:
            return None

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                data = await response.read()
                response, _ = await self.__client.upload(
                    data_provider=BytesIO(data),
                    content_type=response.content_type,
                    filename=f"Avatar of {user.name}",
                    filesize=response.content_length
                )

                if not isinstance(response, UploadResponse):
                    self.__logger.error(
                        "Failed to upload user avatar: %s",
                        response
                    )
                    return None

                avatar_url = response.content_uri
                self.__avatar_cache.add(user.id, avatar_url)

        return avatar_url


    async def __make_content_message(self, message: Message):
        content: dict[str, Any] = {
            "body": f"{message.sender.name}: {message.text}",
            "msgtype": "m.text",
            "format": "org.matrix.custom.html",
            "formatted_body": f"<strong data-mx-profile-fallback>{message.sender.name}: </strong>{message.text}",
            "com.beeper.per_message_profile": {
                "id": message.sender.id,
                "displayname": message.sender.name,
                "has_fallback": True
            }
        }

        avatar = await self.__get_avatar(message.sender)
        if avatar:
            content['com.beeper.per_message_profile']['avatar_url'] = avatar

        if message.reply:
            result = None
            if message.reply.id:
                result = self.__message_storage.get_by_id(
                    source=message.chat.address,
                    target=self.__chat.address,
                    topic_id=message.chat.topic_id,
                    message_id=message.reply.id,
                )

            if message.reply.body and not result:
                result = self.__message_storage.get_by_body(
                    target=self.__chat.address,
                    target_realm=self.__chat.realm,
                    topic_id=message.chat.topic_id,
                    body=message.reply.body,
                )

            if result:
                event_id = result.source_id if result.source_realm == Realm.MATRIX.value else result.target_id
                content['m.relates_to'] = {
                    "m.in_reply_to": {
                        "event_id": event_id
                    }
                }
        return content

    @override
    async def send_message(self, origin: Message) -> None:
        content = await self.__make_content_message(origin)
        response = await self.__client.room_send(
            room_id=self.__chat.address,
            message_type="m.room.message",
            content=content
        )

        if isinstance(response, RoomSendError):
            self.__logger.error("Failed to send message: %s", response)
            return

        self.__message_storage.add(
            target=self.__chat,
            source=origin.chat,
            source_message_id=origin.id,
            target_message_id=response.event_id,
            body=origin.text,
            topic_id=origin.chat.topic_id
        )

    @override
    async def edit_message(self, edited: Message) -> None:
        result = self.__message_storage.get_by_id(
            target=self.__chat.address,
            topic_id=edited.chat.topic_id,
            source=edited.chat.address,
            message_id=edited.id
        )

        if not result:
            self.__logger.info(
                "Failed to found Matrix message id for event: %s",
                edited.id
            )
            return

        event_id = result.source_id if result.source_realm == Realm.MATRIX.value else result.target_id

        content = await self.__make_content_message(edited)
        content["m.relates_to"] = {
            "rel_type": "m.replace",
            "event_id": event_id
        }
        content["m.new_content"] = content.copy()

        response = await self.__client.room_send(
            room_id=self.__chat.address,
            message_type="m.room.message",
            content=content
        )

        if isinstance(response, RoomSendError):
            self.__logger.error("Failed to edit message: %s", response)
            return

    @override
    async def send_attachment(self, attachment: Attachment) -> None:
        url: str | None = await attachment.url_callback()
        if not url:
            self.__logger.error(
                "Failed to get URL for: %s", attachment
            )
            return

        content = await self.__make_content_message(attachment)
        content["body"] = attachment.fname
        content["info"] = {
            "size": attachment.fsize,
            "mimetype": attachment.mime,
        }
        content["org.matrix.msc1767.caption"] = {
            "org.matrix.msc1767.text": content['body']
        }

        content_type = attachment.mime

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as raw:
                data = await raw.read()

                if not content_type:
                    content_type = raw.content_type

                ftype = content_type.split("/")[0]

                if ftype == "image":
                    try:
                        img = Image.open(BytesIO(data))
                        content["msgtype"] = "m.image"
                        content["info"]["w"] = img.size[0]
                        content["info"]["h"] = img.size[1]
                    except UnidentifiedImageError as error:
                        self.__logger.error(
                            "Failed to detect image: %s",
                            error
                        )

                response, _ = await self.__client.upload(
                    data_provider=BytesIO(data),
                    content_type=content_type,
                    filename=attachment.fname,
                    filesize=attachment.fsize
                )

        if ftype == "video":
            content["msgtype"] = "m.video"
        elif ftype == "audio":
            content["msgtype"] = "m.audio"
        elif not ftype:
            content["msgtype"] = "m.file"

        if not isinstance(response, UploadResponse):
            self.__logger.error(
                "Failed to upload attachment: %s",
                response
            )
            return

        content["url"] = response.content_uri

        response = await self.__client.room_send(
            room_id=self.__chat.address,
            message_type="m.room.message",
            content=(await self.__make_content_message(attachment))
        )

        if not isinstance(response, RoomSendResponse):
            self.__logger.error(
                "Error when sending a message: %s",
                response
            )

        response = await self.__client.room_send(
            self.__chat.address,
            message_type="m.room.message",
            content=content
        )

        if not isinstance(response, RoomSendResponse):
            self.__logger.error(
                "Error when sending a attachment: %s",
                response
            )


    @override
    async def unbridge(self) -> None:
        content: dict[str, Any] = {
            "body": _(
                "This chat was automatically unbridged "
                "due to a bot kick or via command."
            ),
            "msgtype": "m.text",
        }

        response = await self.__client.room_send(
            room_id=self.__chat.address,
            message_type="m.room.message",
            content=content
        )

        if not isinstance(response, RoomSendResponse):
            self.__logger.error(
                "Error when sending a message: %s",
                response
            )

        response = await self.__client.room_leave(self.__chat.address)
        if not isinstance(response, RoomLeaveResponse):
            self.__logger.error(
                "Error when leaving a room: %s",
                response
            )
