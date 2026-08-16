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

from jabagram.dispatcher import MessageDispatcher
from jabagram.model import ChatHandlerFactory, Realm, Chat, UnbridgeEvent
from jabagram.database.chats import ChatStorage

class ChatService():
    def __init__(
        self,
        dispatcher: MessageDispatcher,
        storage: ChatStorage,
    ) -> None:
        self.__storage = storage
        self.__dispatcher = dispatcher
        self.__pending_chats: dict[Chat, Chat] = {}
        self.__factories: dict[Realm, ChatHandlerFactory] = {}
        self.__logger = logging.getLogger(__class__.__name__)

    async def pair(self, source: Chat) -> bool:
        target: Chat | None = self.__pending_chats.get(source)

        if target is None:
            return False

        if not (await self.__spawn_handlers(source, target)):
            return False

        self.__storage.add(source, target)

        return True

    async def unpair(self, source: Chat) -> None:
        await self.__dispatcher.send(UnbridgeEvent(chat=source))

    async def __spawn_handlers(self, source: Chat, target: Chat) -> bool:
        handlers = self.__dispatcher.get_handlers(source.address)

        target_factory = self.__factories[target.realm]
        target_handler = (await target_factory.create_handler(target.address))

        if not target_handler:
            return False

        if handlers:
            for handler in handlers:
                self.__dispatcher.add_handler(handler.chat.address, target_handler)
                self.__dispatcher.add_handler(target.address, handler)

        else:
            source_factory = self.__factories[source.realm]
            source_handler = (await source_factory.create_handler(source.address))

            if source_handler and target_handler:
                self.__dispatcher.add_handler(source.address, target_handler)
                self.__dispatcher.add_handler(target.address, source_handler)

        del self.__pending_chats[source]
        del self.__pending_chats[target]

        return True

    def register_factory(self, realm: Realm, factory: ChatHandlerFactory) -> None:
        self.__logger.info(f"New chat factory for {realm.name} registred")
        self.__factories[realm] = factory

    async def load_chats(self) -> None:
        self.__logger.info("Loading chats from database...")
        pairs = self.__storage.get() or []

        for pair in pairs:
            source, target = pair

            if self.pending(source, target):
                await self.__spawn_handlers(source, target)

    def is_paired(
            self,
            source: Chat,
            target: Chat | None = None
    ) -> bool:
        if not target:
            return self.__dispatcher.is_paired(source.address)

        handlers = self.__dispatcher.get_handlers(target.address)

        if handlers:
            for handler in handlers:
                if handler.chat.address == source.address:
                    return True

        return False

    def pending(self, source: Chat, target: Chat) -> bool:
        old_target = self.__pending_chats.get(source)
        old_source = self.__pending_chats.get(target)

        if old_source:
            return False

        if old_target:
            del self.__pending_chats[old_target]

        self.__logger.info(
            "The chats are staged for confirmation: %s - %s",
            source,
            target
        )

        self.__pending_chats[source] = target
        self.__pending_chats[target] = source
        return True
