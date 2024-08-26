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
from collections import OrderedDict
import logging
from sqlite3 import Error, connect

class SimpleLRUCache():
    def __init__(self, size: int):
        self.__size = size
        self.__map = OrderedDict()
        self.__logger = logging.getLogger("Cache")

    def get(self, key: str) -> str | None:
        if key in self.__map.keys():
            self.__map.move_to_end(key)
            return self.__map[key]

        self.__logger.debug("Not found key in cache: %s", key)
        return None

    def add(self, key: str, value: str) -> None:
        self.__logger.debug("Add pair: %s - %s", key, value)
        self.__map[key] = value
        self.__map.move_to_end(key)
        if len(self.__map) > self.__size:
            self.__map.popitem(last=False)

class StickerCache():
    def __init__(self, path):
        self.__path = path
        self.__logger = logging.getLogger(self.__class__.__name__)

    def add(self, file_id, xmpp_url: str) -> None:
        try:
            with connect(self.__path) as con:
                cursor = con.cursor()
                cursor.execute(
                    "INSERT INTO stickers(file_id, xmpp_url) VALUES (?, ?) ON CONFLICT (file_id) DO UPDATE SET xmpp_url = excluded.xmpp_url",
                    (file_id, xmpp_url)
                )
                con.commit()
        except Error as e:
            self.__logger.error("Can not add sticker: %s", e)

    def get(self, file_id: str) -> str | None:
        try:
            with connect(self.__path) as con:
                cursor = con.cursor()
                cursor.execute(
                    "SELECT xmpp_url FROM stickers WHERE file_id = ?", (file_id,)
                )
                row = cursor.fetchone()

                if row:
                    return row[0]

                self.__logger.info("Cache miss for: %s", file_id)
        except Error as e:
            self.__logger.error("Can not get sticker: %s", e)


class Cache():
    def __init__(self, capacity: int):
        self.__reply_cache = SimpleLRUCache(capacity)
        self.__message_ids_cache = SimpleLRUCache(capacity)

    @property
    def reply_map(self):
        return self.__reply_cache

    @property
    def message_ids(self):
        return self.__message_ids_cache
