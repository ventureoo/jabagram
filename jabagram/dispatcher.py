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
from typing import Dict

from .database import Database
from .model import (
    Attachment,
    ChatHandler,
    Event,
    Forwardable,
    Message,
    Sticker,
    UnbridgeEvent,
)


class MessageDispatcher():
    """ A class that dispatches messages/events/etc between chat handlers """

    def __init__(self, database: Database):
        self.__loop = asyncio.get_event_loop()
        self.__chat_map: Dict[str, ChatHandler] = {}
        self.__event_queue: Queue[Forwardable] = Queue(maxsize=100)
        self.__database = database
        self.__logger = logging.getLogger(self.__class__.__name__)

    async def start(self):
        """
        Start event loop for infinite processing of queued events
        """
        while True:
            forwardable: Forwardable = await self.__event_queue.get()
            handler: ChatHandler | None = self.__chat_map.get(
                forwardable.address
            )
            self.__logger.info("Received event: %s", forwardable)
            if not handler:
                self.__logger.error(
                    "Unhandled event for chat: %s", forwardable.address
                )
                continue

            match forwardable:
                case Sticker():
                    self.__loop.create_task(
                        handler.send_sticker(
                            forwardable)
                    )
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
                    del self.__chat_map[forwardable.address]
                    del self.__chat_map[handler.address]
                    self.__database.remove(handler.address)

    async def send(self, forwardable: Forwardable):
        """Put event inside event queue"""
        await self.__event_queue.put(forwardable)

    def add_handler(self, address: str, handler: ChatHandler):
        """Add chat handler that recieves events"""
        self.__chat_map[address] = handler

    def is_bound(self, chat: str):
        """Check if the chat is inside the chat handlers map"""
        return chat in self.__chat_map

    def remove_handler(self, address: str):
        """Remove chat handler from chat handlers map"""
        del self.__chat_map[address]
