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

from datetime import datetime
from jabagram.database.messages import MessageStorage
from jabagram.database.stickers import StickerCache
from jabagram.dispatcher import MessageDispatcher
from jabagram.service import ChatService
from jabagram.model import (
    Attachment,
    Chat,
    ChatHandlerFactory,
    Sender,
    Message,
)
from jabagram.xmpp.actor import XmppActor, XmppActorFactory
from jabagram.xmpp.handler import XmppRoomHandler

BRIDGE_DEAFAULT_ID = "listener"
BRIDGE_DEAFAULT_NAME = "Telegram Bridge"

class XmppClient(XmppActor, ChatHandlerFactory):
    def __init__(
        self,
        jid: str,
        password: str,
        service: ChatService,
        disptacher: MessageDispatcher,
        sticker_cache: StickerCache,
        message_storage: MessageStorage,
        actors_pool_size_limit: int
    ) -> None:
        super().__init__(
            jid=jid,
            password=password,
            user_id=BRIDGE_DEAFAULT_ID,
            user_name=BRIDGE_DEAFAULT_NAME
        )
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.__service = service
        self.__dispatcher = disptacher
        self.__sticker_cache = sticker_cache
        self.__message_storage = message_storage
        self.__actor_factory = XmppActorFactory(
            jid=jid,
            password=password,
            pool_size_limit=actors_pool_size_limit,
            fallback=self
        )

        self.add_event_handler("groupchat_message", self.__process_message)
        self.add_event_handler("groupchat_direct_invite", self.__invite_callback)
        self.__service.register_factory(self)

    async def create_handler(
        self,
        address: str,
        muc: str,
    ) -> None:
        handler = XmppRoomHandler(
            main_actor=self,
            address=muc,
            sticker_cache=self.__sticker_cache,
            message_storage=self.__message_storage,
            actor_factory=self.__actor_factory
        )

        if (await self.join(muc)):
            self.__dispatcher.add_handler(address, handler)

    async def _session_start(self, _):
        _ = await super()._session_start(_)

        if not self._reconnecting:
            await self.__service.load_chats()

    async def __invite_callback(self, invite):
        muc = str(invite['groupchat_invite']['jid'])
        key: str = invite['groupchat_invite']['reason']
        await self.__service.bind(muc, key)


    async def __process_message(self, message):
        sender: str = message['mucnick']
        message_id: str = message['id']
        muc: str = message['mucroom']
        text: str = message['body'].strip()

        if not self.__dispatcher.is_bound(muc):
            return

        if sender.endswith("(Telegram)") or sender == BRIDGE_DEAFAULT_NAME:
            return

        if message['oob']['url']:
            url = message['oob']['url']

            async def url_callback():
                return url

            fname: str = url.split("/")[-1]
            attachment = Attachment(
                id=message_id,
                chat=Chat(address=str(muc)),
                sender=Sender(name=sender, id=""),
                url_callback=url_callback,
                content=fname,
                mime=None,
                fsize=None,
            )
            await self.__dispatcher.send(attachment)
        else:
            content = text
            is_edit = False

            if message['replace']['id']:
                message_id: str = message['replace']['id']
                is_edit = True

            reply, body = self.__parse_reply(text)

            if reply and body:
                content = body

            message = Message(
                id=message_id,
                chat=Chat(address=muc),
                sender=Sender(name=sender, id=""),
                content=content,
                reply=reply,
                edit=is_edit
            )
            await self.__dispatcher.send(message)

    def __parse_reply(self, message: str) -> tuple[str | None, str | None]:
        def _safe_get(line: str, index: int):
            try:
                return line[index]
            except IndexError:
                return None

        replies: list[str] = []
        parts: list[str] = []

        for line in message.splitlines():
            if _safe_get(line, 0) == ">":
                # Ignore brackets not followed by space
                if _safe_get(line, 1) != " ":
                    continue

                # Ignore nested replies
                if _safe_get(line, 2) == ">":
                    continue

                line = line.replace("> ", "").strip()

                # Attempt to detect a replies format of some mobile clients
                # that add time and sender name of the message sent
                try:
                    _ = datetime.strptime(line, '%Y-%m-%d  %H:%M (GMT%z)')

                    # Remove sender name of message being replied to
                    _ = replies.pop()
                except ValueError:
                    replies.append(line)
            else:
                parts.append(line)

        reply = "\n".join(replies)
        body = "\n".join(parts)

        return reply, body
