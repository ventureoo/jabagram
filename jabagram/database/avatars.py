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

class AvatarCache(SqliteTable):
    def __init__(self, path: str):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS "
                "avatars(user_id TEXT PRIMARY KEY, data BLOB NOT NULL)"
            )
        ) is None:
            return False

        return True

    def add(self, user_id: str, data: bytes) -> None:
        self._execute(
            user_id,
            data,
            statement=(
                "INSERT INTO avatars(user_id, data) VALUES (?, ?) ON "
                "CONFLICT (user_id) DO UPDATE SET data = excluded.data"
            ),
            on_error_message="Failed to add avatar"
        )

    def get(self, user_id: str) -> bytes | None:
        avatars: list[tuple[bytes]] | None = self._execute(
            user_id,
            statement="SELECT data FROM avatars WHERE user_id = ?",
            on_error_message="Failed to get avatar"
        )

        return avatars[0][0] if avatars else None
