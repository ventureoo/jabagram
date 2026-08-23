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
import mimetypes

from typing import override

import asyncio
from jabagram.database.avatars import AvatarCache
from jabagram.dispatcher import MessageDispatcher
from jabagram.model import (
    Attachment,
    Chat,
    ChatHandler,
    Sender,
    Reply,
    Message,
    Realm,
    ChatHandlerFactory
)
from jabagram.service import ChatService
from jabagram.command import UserCommandHandler
from jabagram.database.messages import MessageStorage
from jabagram.matrix.handler import MatrixChatHandler
from nio import (
    AsyncClient,
    Event,
    LoginResponse,
    MatrixInvitedRoom,
    MatrixRoom,
    RoomMessageMedia,
    RoomMessageText,
)
from nio.events.invite_events import InviteEvent

class MatrixClient(ChatHandlerFactory):
    def __init__(
        self,
        server: str,
        user: str,
        password: str,
        command_handler: UserCommandHandler,
        dispatcher: MessageDispatcher,
        chat_service: ChatService,
        message_storage: MessageStorage,
        avatar_cache: AvatarCache,
    ):
        self.__password = password
        self.__user = user
        self.__client = AsyncClient(server, user.split(":")[0][1:])
        self.__message_storage = message_storage
        self.__command_handler = command_handler
        self.__dispatcher = dispatcher
        self.__chat_service = chat_service
        self.__start_event = asyncio.Event()
        self.__avatar_cache = avatar_cache
        self.__logger = logging.getLogger(__class__.__name__)

    async def wait_for_start(self):
        await self.__start_event.wait()

    @override
    def realm(self):
        return Realm.MATRIX

    @override
    async def create_handler(
        self,
        address: str,
    ) -> ChatHandler | None:
        self.__logger.info("Creating new handler for %s", address)
        handler = MatrixChatHandler(
            chat=Chat(address=address, realm=Realm.MATRIX),
            client=self.__client,
            message_storage=self.__message_storage,
            avatar_cache=self.__avatar_cache,
        )
        return handler

    async def start(self):
        response = await self.__client.login(self.__password)

        if not isinstance(response, LoginResponse):
            self.__logger.error("Login failed: %s", response)
            return

        self.__chat_service.register_factory(realm=Realm.MATRIX, factory=self)

        # Discard previous messages
        _ = await self.__client.sync(30000)

        self.__client.add_event_callback(self.__message_callback, RoomMessageText)
        self.__client.add_event_callback(self.__attachment_callback, RoomMessageMedia)
        self.__client.add_event_callback(self.__invite_callback, InviteEvent)

        self.__start_event.set()
        await self.__client.sync_forever(timeout=30000)


    async def __reply_to(
        self,
        room: MatrixRoom,
        event: Event,
        response: str
    ):
        _ = await self.__client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": response,
                "m.relates_to": {
                    "m.in_reply_to": {
                        "event_id": event.event_id
                    }
                }
            }
        )

    async def __invite_callback(
        self,
        source: MatrixInvitedRoom,
        _: str,
    ):
        self.__logger.info("Accepting invite to %s", source.room_id)
        await self.__client.join(source.room_id)

    async def __message_callback(
        self,
        room: MatrixRoom,
        event: RoomMessageText
    ) -> None:
        if event.sender == self.__user:
            return

        if not (room.member_count > 2):
            if not event.body.startswith("!jabagram"):
                return

            response = self.__command_handler.handle_direct_user_command(
                Realm.MATRIX,
                command=event.body,
                user=event.sender,
            )
            await self.__reply_to(room, event, response)
            return

        if event.body.startswith("!jabagram"):
            response = await self.__command_handler.handle_chat_user_command(
                source_chat=Chat(address=room.room_id, realm=Realm.MATRIX),
                command=event.body,
                user=event.sender,
            )
            await self.__reply_to(room, event, response)
            return

        if not self.__dispatcher.is_paired(room.room_id):
            return

        relation = event.source.get('content', {}).get("m.relates_to", {})
        relation_id = relation.get("m.in_reply_to", relation).get("event_id", None)
        relation_type = relation.get("rel_type", "reply" if relation_id is not None else None)

        message = Message(
            id=relation_id if relation_type == "m.replace" else event.event_id,
            chat=Chat(realm=Realm.MATRIX, address=room.room_id),
            sender=Sender(
                name=(room.user_name(event.sender) or "Unknown user") + " (Matrix)",
                id=event.sender[1:].replace(':', '_'),
                avatar_callback=None
            ),
            text=event.body,
            reply=Reply(id=relation_id, body=None) if relation_type == "reply" else None,
            edit=(relation_type == "m.replace"),
        )

        await self.__client.room_read_markers(room.room_id, event.event_id, event.event_id)
        await self.__dispatcher.send(message)

    async def __attachment_callback(
        self,
        room: MatrixRoom,
        event: RoomMessageMedia
    ) -> None:
        if event.sender == self.__user:
            return

        if not self.__dispatcher.is_paired(room.room_id):
            return

        async def url_callback():
            url = await self.__client.mxc_to_http(event.url)
            return url

        sender = room.user_name(event.sender) or "Unknown user"
        content = event.source.get("content", {})
        caption = content.get("org.matrix.msc1767.caption", {})
        text = caption.get("org.matrix.msc1767.text")
        fname = content.get("filename")
        mime = content.get("info", {}).get("mimetype")

        if fname and not text:
            text = event.body

        if not fname:
            if event.body:
                fname = event.body
            else:
                fname = f"File from {sender}"

                ext = mimetypes.guess_extension(mime) if mime else ""
                if ext:
                    fname += ext


        attachment = Attachment(
            id=event.event_id,
            chat=Chat(address=room.room_id, realm=Realm.MATRIX, topic_id=None),
            sender=Sender(
                name=sender + " (Matrix)",
                id=event.sender[1:].replace(':', '_'),
                avatar_callback=None
            ),
            url_callback=url_callback,
            text=text,
            fname=fname,
            mime=mime,
            fsize=content.get("info", {}).get("size"),
        )

        await self.__dispatcher.send(attachment)
