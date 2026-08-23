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
import hashlib

from typing import NamedTuple
from jabagram.model import Chat, Realm
from jabagram.database.base import SqliteTable

class MessageIdEntry(NamedTuple):
    source_id: str
    target_id: str
    topic_id: int | None
    source_realm: int
    target_realm: int

class MessageStorage(SqliteTable):
    def __init__(self, path: str):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS messages"
                "(source_message_id TEXT NOT NULL, target_message_id TEXT NOT NULL,"
                "body TEXT, source TEXT NOT NULL, topic_id INTEGER,"
                "target TEXT NOT NULL, source_realm INTEGER NOT NULL,"
                "target_realm INTEGER NOT NULL)"
            )
        ) is None:
            return False

        return True

    def add(self,
        source: Chat,
        target: Chat,
        source_message_id: str,
        target_message_id: str,
        topic_id: int | None,
        body: str,
    ) -> None:
        digest = hashlib.sha256(body.encode()).hexdigest()
        self._execute(
            source_message_id,
            target_message_id,
            digest,
            source.address,
            topic_id,
            target.address,
            source.realm.value,
            target.realm.value,
            statement=(
                "INSERT INTO messages(source_message_id, target_message_id,"
                "body, source, topic_id, target, source_realm, target_realm)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            on_error_message="Failed to insert message in table"
        )

    def get_by_id(
        self,
        source: str,
        topic_id: int | None,
        target: str,
        message_id: str
    ) -> MessageIdEntry | None:
        statement = (
            "SELECT source_message_id, target_message_id, topic_id, source_realm, target_realm FROM messages WHERE"
            " (source = ? OR source = ?) AND (target = ? OR target = ?) AND (source_message_id = ? OR target_message_id = ?)"
        )

        args = (
            source,
            target,
            source,
            target,
            message_id,
            message_id,
        )

        if topic_id:
            args = (*args, topic_id)
            statement += " AND topic_id = ?"

        message = self._execute(
            *args,
            statement=statement,
            on_error_message="Failed to get message"
        )

        if not message:
            self.__logger.error(
                "Cache miss for message with %s id",
                message_id,
            )
            return None

        return MessageIdEntry._make(message[0])

    def get_by_body(
        self,
        target: str,
        target_realm: Realm,
        topic_id: int | None,
        body: str
    ) -> MessageIdEntry | None:
        statement = (
            "SELECT source_message_id, target_message_id, topic_id, source_realm, target_realm FROM messages WHERE"
            " (source = ? OR target = ?) AND (target_realm = ? OR source_realm = ?) AND body = ?"
        )
        digest = hashlib.sha256(body.encode()).hexdigest()

        args = (
            target,
            target,
            target_realm.value,
            target_realm.value,
            digest,
        )

        if topic_id:
            args = (*args, topic_id)
            statement += " AND topic_id = ?"

        message = self._execute(
            *args,
            statement=statement,
            on_error_message="Failed to get message"
        )

        if not message:
            self.__logger.error(
                "Cache miss for message with %s body",
                digest,
            )
            return None

        return MessageIdEntry._make(message[len(message) - 1])

