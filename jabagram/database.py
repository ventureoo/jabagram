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
import sqlite3
from sqlite3 import Error, connect
from typing import Dict, List

from .cache import Cache
from .model import ChatHandlerFactory

class Database():
    def __init__(self, path):
        self.__path = path
        self.__logger = logging.getLogger(self.__class__.__name__)

    def create(self) -> bool:
        try:
            with connect(self.__path) as con:
                cursor = con.cursor()
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS chats(telegram_id, muc)"
                )
                cursor.execute(
                    "CREATE TABLE IF NOT EXISTS stickers(file_id PRIMARY KEY, xmpp_url NOT NULL)"
                )
                con.commit()

            return True
        except Error as e:
            self.__logger.error("Can't initialize the database: %s", e)

        return False

    def add(self, chat: str, muc: str) -> None:
        try:
            with connect(self.__path) as con:
                cursor = con.cursor()
                cursor.execute(
                    "INSERT INTO chats(telegram_id, muc) VALUES (?, ?)",
                    (chat, muc)
                )
                con.commit()
        except Error as e:
            self.__logger.error("Can not add chats pair: %s", e)

    def get_chats(self) -> List:
        res = []
        try:
            with sqlite3.connect(self.__path) as con:
                cursor = con.cursor()
                for row in cursor.execute("SELECT telegram_id,muc FROM chats"):
                    res.append(row)

        except Error as e:
            self.__logger.error("Can't add chats pair: %s", e)

        return res

    def remove(self, chat: str):
        try:
            with sqlite3.connect(self.__path) as con:
                cursor = con.cursor()
                cursor.execute(
                    "DELETE FROM chats WHERE telegram_id = ? OR muc = ?", (chat,)
                )
        except Error as e:
            self.__logger.error("Can't remove chats: %s", e)

class ChatService():
    def __init__(
        self,
        database: Database,
        key: str
    ) -> None:
        self.__database = database
        self.__pending_chats: Dict[str, str] = {}
        self.__factories: List[ChatHandlerFactory] = []
        self.__key = key
        self.__logger = logging.getLogger(__class__.__name__)

    async def bind(self, muc: str, key: str) -> None:
        telegram_id: str | None = self.__pending_chats.get(muc)

        if telegram_id is None:
            return

        if key != self.__key:
            self.__logger.info("Wrong key was recieved: %s", key)
            return

        self.__logger.info('New chat pair binded: %s - %s', muc, telegram_id)
        self.__database.add(telegram_id, muc)
        del self.__pending_chats[muc]
        await self.__spawn_handlers(telegram_id, muc)

    async def __spawn_handlers(self, telegram_id, muc):
        cache = Cache(100)
        self.__logger.info(
            "Create handlers for chat %s and MUC: %s", telegram_id, muc
        )
        # Notify all factories to create chat message handlers
        for factory in self.__factories:
            await factory.create_handler(telegram_id, muc, cache)

    def register_factory(self, factory: ChatHandlerFactory) -> None:
        self.__factories.append(factory)

    async def load_chats(self) -> None:
        self.__logger.info("Loading chats from database...")
        chats = self.__database.get_chats()

        for chat in chats:
            telegram_id, muc = chat
            await self.__spawn_handlers(str(telegram_id), muc)

    def pending(self, muc: str, chat: str) -> None:
        self.__logger.info(
            "The chats are staged for confirmation: %s - %s", muc, chat
        )
        for room, chat_id in self.__pending_chats.items():
            if chat_id == chat:
                del self.__pending_chats[room]
                break

        self.__pending_chats[muc] = chat
