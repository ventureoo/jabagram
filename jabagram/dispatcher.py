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
from asyncio import Queue
import logging

from jabagram.database.chats import ChatStorage
from jabagram.model import (
    Attachment,
    ChatHandler,
    Chat,
    Event,
    Forwardable,
    Message,
    UnbridgeEvent,
)


class MessageDispatcher():
    """ A class that dispatches messages/events/etc between chat handlers """

    def __init__(self, storage: ChatStorage):
        self.__loop = asyncio.get_event_loop()
        self.__chat_map: dict[str, list[ChatHandler]] = {}
        self.__event_queue: Queue[Forwardable] = Queue(maxsize=100)
        self.__storage = storage
        self.__logger = logging.getLogger(self.__class__.__name__)

    async def start(self):
        """
        Start event loop for infinite processing of queued events
        """
        while True:
            forwardable = await self.__event_queue.get()
            handlers = self.get_handlers(forwardable.chat.address)
            self.__logger.info("Received event: %s", forwardable)

            if not handlers:
                self.__logger.error(
                    "Unhandled event for chat: %s", forwardable.chat.address
                )
                continue

            for handler in handlers:
                match forwardable:
                    case Attachment():
                        self.__loop.create_task(
                            handler.send_attachment(forwardable)
                        )
                    case Message():
                        if forwardable.edit:
                            self.__loop.create_task(
                                handler.edit_message(forwardable)
                            )
                        else:
                            self.__loop.create_task(
                                handler.send_message(forwardable)
                            )
                    case Event():
                        self.__loop.create_task(
                            handler.send_event(forwardable)
                        )
                    case UnbridgeEvent():
                        await handler.unbridge()
                        self.__unpair(forwardable.chat, handler.chat)

            if isinstance(forwardable, UnbridgeEvent):
                del self.__chat_map[forwardable.chat.address]
                self.__storage.remove(forwardable.chat)

    async def send(self, forwardable: Forwardable):
        """Put event inside event queue"""
        await self.__event_queue.put(forwardable)

    def set_handlers(self, address: str, handlers: list[ChatHandler]):
        """Set chat handlers that recieves events from address"""
        self.__chat_map[address] = handlers

    def get_handlers(
        self,
        address: str
    ) -> list[ChatHandler]:
        """Get handlers for the chat"""
        handlers = self.__chat_map.get(address)
        if not handlers:
            handlers = []
            self.__chat_map[address] = handlers

        return handlers

    def is_paired(self, chat: str) -> bool:
        """Check if the chat is inside the chat handlers map"""
        return chat in self.__chat_map

    def __unpair(self, source: Chat, target: Chat):
        """Remove chat handler from chat handlers map"""
        handlers = self.get_handlers(target.address) or []
        for i in range(len(handlers)):
            if handlers[i].chat.address == source.address:
                handlers.pop(i)
                break

        if not handlers:
            del self.__chat_map[target.address]
