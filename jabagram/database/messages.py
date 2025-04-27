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

from typing import NamedTuple, Optional
from jabagram.database.base import SqliteTable

class MessageIdEntry(NamedTuple):
    telegram_id: int
    stanza_id: str

class MessageStorage(SqliteTable):
    def __init__(self, path):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self) -> bool:
        if self._execute(
            statement=(
                "CREATE TABLE IF NOT EXISTS messages"
                "(telegram_id INTEGER UNIQUE NOT NULL, stanza_id TEXT UNIQUE NOT NULL,"
                "body TEXT NOT NULL, chat_id INTEGER NOT NULL, topic_id INTEGER,"
                "muc TEXT NOT NULL)"
            )
        ) is None:
            return False

        return True

    def add(self,
        chat_id: int,
        topic_id: int | None,
        body: str,
        telegram_id: str,
        muc: str,
        stanza_id: str
    ) -> None:
        digest = hashlib.sha256(body.encode()).hexdigest()
        self._execute(
            telegram_id,
            stanza_id,
            digest,
            chat_id,
            topic_id,
            muc,
            statement=(
                'INSERT INTO messages(telegram_id, stanza_id, body, chat_id,'
                'topic_id, muc) VALUES (?, ?, ?, ?, ?, ?)'
            ),
            on_error_message="Failed to insert message in table"
        )

    def get_by_id(
        self,
        chat_id: int,
        topic_id: Optional[int],
        muc: str,
        message_id: str
    ) -> MessageIdEntry | None:
        statement = (
            "SELECT telegram_id, stanza_id FROM messages WHERE"
            " chat_id = ? AND muc = ? AND (stanza_id = ? OR telegram_id = ?)"
        )

        args = (
            chat_id,
            muc,
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
                message_id
            )
            return None

        return MessageIdEntry._make(message[0])

    def get_by_body(
        self,
        chat_id: int,
        topic_id: Optional[int],
        muc: str,
        body: str
    ) -> MessageIdEntry | None:
        statement = (
            "SELECT telegram_id, stanza_id FROM messages WHERE"
            " chat_id = ? AND muc = ? AND body = ?"
        )
        digest = hashlib.sha256(body.encode()).hexdigest()

        args = (
            chat_id,
            muc,
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
                digest
            )
            return None

        return MessageIdEntry._make(message[0])

