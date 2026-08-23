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

from typing import NamedTuple
from jabagram.model import Realm
from jabagram.database.base import SqliteTable

class AdminEntry(NamedTuple):
    realm: Realm
    user_id: str

class AdminStorage(SqliteTable):
    def __init__(self, path: str):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS admins"
                "(realm INTEGER NOT NULL CHECK ( realm IN (1, 2, 3) ),"
                "user_id TEXT NOT NULL)"
            )
        ) is None:
            return False

        return True

    def add(self,
        realm: Realm,
        user_id: str,
    ) -> None:
        self._execute(
            realm.value,
            user_id,
            statement=(
                'INSERT INTO admins(realm, user_id) VALUES (?, ?)'
            ),
            on_error_message="Failed to insert admin in table"
        )

    def get(
        self,
        realm: Realm,
        user_id: str,
    ) -> AdminEntry | None:
        statement = (
            "SELECT realm, user_id FROM admins WHERE realm = ? AND user_id = ?"
        )

        args = (
            realm.value,
            user_id,
        )

        admin = self._execute(
            *args,
            statement=statement,
            on_error_message="Failed to get admin"
        )

        if not admin:
            return None

        return AdminEntry._make(admin[0])
