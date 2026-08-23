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
        self.__pending_chats: dict[str, Chat] = {}
        self.__factories: dict[Realm, ChatHandlerFactory] = {}
        self.__logger = logging.getLogger(__class__.__name__)

    async def pair(self, source: Chat) -> bool:
        target: Chat | None = self.__pending_chats.get(source.address)

        if target is None:
            self.__logger.warning("Target is not found for %s", source)
            return False

        if not (await self.__spawn_handlers(source, target)):
            self.__logger.error(
                "Failed to spawn targets for %s - %s",
                source,
                target
            )
            return False

        self.__storage.add(target, source)

        return True

    async def unpair(self, source: Chat) -> None:
        await self.__dispatcher.send(UnbridgeEvent(chat=source))

    async def __spawn_handlers(self, source: Chat, target: Chat) -> bool:
        target_handlers = self.__dispatcher.get_handlers(target.address)
        source_handlers = self.__dispatcher.get_handlers(source.address)

        if not source_handlers and not target_handlers:
            target_factory = self.__factories[target.realm]
            target_handler = (await target_factory.create_handler(target.address))

            source_factory = self.__factories[source.realm]
            source_handler = (await source_factory.create_handler(source.address))

            if source_handler and target_handler:
                source_handlers.append(target_handler)
                target_handlers.append(source_handler)
        else:
            existed_handlers = target_handlers or source_handlers
            already_paired_chat = None

            if target_handlers:
                existed_handlers = target_handlers
                already_paired_chat = target
                source_factory = self.__factories[source.realm]
                new_handler = (await source_factory.create_handler(source.address))
            else:
                existed_handlers = source_handlers
                already_paired_chat = source
                target_factory = self.__factories[target.realm]
                new_handler = (await target_factory.create_handler(target.address))

            if not new_handler:
                self.__logger.error(
                    "Failed to create new handler"
                )
                return False

            already_paired_handler = None
            new_handlers = self.__dispatcher.get_handlers(new_handler.chat.address)
            for handler in existed_handlers:
                alt_handlers = self.__dispatcher.get_handlers(handler.chat.address)
                if not already_paired_handler:
                    for alt_handler in alt_handlers:
                        if alt_handler.chat.realm == already_paired_chat.realm:
                            already_paired_handler = alt_handler
                            new_handlers.append(already_paired_handler)
                            break

                alt_handlers.append(new_handler)
                new_handlers.append(handler)

            existed_handlers.append(new_handler)

        del self.__pending_chats[source.address]
        del self.__pending_chats[target.address]

        return True

    def register_factory(self, realm: Realm, factory: ChatHandlerFactory) -> None:
        self.__logger.info(f"New chat factory for {realm.name.lower()} registred")
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
        old_target = self.__pending_chats.get(source.address)
        old_source = self.__pending_chats.get(target.address)

        if old_source:
            return False

        if old_target:
            del self.__pending_chats[old_target.address]

        self.__logger.info(
            "The chats are staged for confirmation: %s - %s",
            source,
            target,
        )

        self.__pending_chats[source.address] = target
        self.__pending_chats[target.address] = source
        return True
