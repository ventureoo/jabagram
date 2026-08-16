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
from jabagram.database.base import SqliteTable
from jabagram.model import Chat, Realm

class ChatStorage(SqliteTable):
    def __init__(self, path: str):
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS chats"
                "(source TEXT NOT NULL, target TEXT NOT NULL,"
                "source_realm INTEGER NOT NULL CHECK ( source_realm IN (1, 2) ),"
                "target_realm INTEGER NOT NULL CHECK ( target_realm IN (1, 2) ))"
            )
        ) is None:
            return False

        return True

    def add(self, source: Chat, target: Chat) -> None:
        self._execute(
            source.address,
            target.address,
            source.realm.value,
            target.realm.value,
            statement=(
                "INSERT INTO chats(source, target,"
                "source_realm, target_realm) VALUES (?, ?, ?, ?)"),
            on_error_message="Failed to add chats"
        )

    def get(self) -> list[tuple[Chat, Chat]] | None:
        pairs = self._execute(
            statement=(
                "SELECT source, target, source_realm, target_realm FROM chats"
            ),
            on_error_message="Failed to get chats"
        )

        if not pairs:
            return None

        result = []

        for pair in pairs:
            (source, target, source_realm, target_realm) = pair
            result.append(
                (Chat(address=source, realm=Realm(source_realm)),
                 Chat(address=target, realm=Realm(target_realm)))
            )

        return result

    def remove(self, chat: Chat) -> None:
        self._execute(
            chat.address,
            chat.address,
            chat.realm.value,
            chat.realm.value,
            statement=(
                "DELETE FROM chats WHERE (source = ? OR target = ?) AND "
                "(source_realm = ? OR target_realm = ?)"
            ),
            on_error_message="Failed to remove chats"
        )
