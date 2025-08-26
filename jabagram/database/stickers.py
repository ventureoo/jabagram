#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2025 Vasiliy Stelmachenok <ventureo@yandex.ru>
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
from jabagram.database.base import SqliteTable

class StickerCache(SqliteTable):
    def __init__(self, path):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS "
                "stickers(file_id PRIMARY KEY, xmpp_url NOT NULL)"
            )
        ) is None:
            return False

        return True

    def add(self, file_id: str, xmpp_url: str) -> None:
        self._execute(
            file_id,
            xmpp_url,
            statement=(
                "INSERT INTO stickers(file_id, xmpp_url) VALUES (?, ?) ON "
                "CONFLICT (file_id) DO UPDATE SET xmpp_url = excluded.xmpp_url"
            ),
            on_error_message="Failed to add sticker"
        )

    def get(self, file_id: str) -> str | None:
        stickers = self._execute(
            file_id,
            statement="SELECT xmpp_url FROM stickers WHERE file_id = ?",
            on_error_message="Failed to get sticker"
        )

        if not stickers:
            self.__logger.error("Can not get stickers")
            return None

        for entry in stickers:
            return entry[0]

        self.__logger.info("Cache miss for: %s", file_id)

        return None

