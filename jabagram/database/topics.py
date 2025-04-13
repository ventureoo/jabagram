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

class TopicNameCache(SqliteTable):
    def __init__(self, path):
        self.__logger = logging.getLogger(__class__.__name__)
        super().__init__(path=path)

    def create(self):
        if self._execute(
            statement="CREATE TABLE IF NOT EXISTS topics(chat_id, topic_id, topic_name NOT NULL)"
        ) is None:
            return False

        return True

    def add(self,
        chat_id: int,
        topic_id: int,
        topic_name: str
    ) -> None:
        self._execute(
            chat_id,
            topic_id,
            topic_name,
            statement=(
                "INSERT INTO topics(chat_id, topic_id, topic_name) VALUES (?, ?, ?)"
            ),
            on_error_message=(
                f"Failed to add topic name {topic_name} for {chat_id}#{topic_id}"
            )
        )

    def get(
        self,
        chat_id: int,
        topic_id: int
    ) -> str | None:
        topics = self._execute(
            chat_id,
            topic_id,
            statement=(
                "SELECT topic_name FROM topics WHERE chat_id = ? and topic_id = ?"
            ),
            on_error_message=(
                f"Failed to get topic name for {chat_id}#{topic_id}"
            )
        )

        if not topics:
            self.__logger.error("Can not get topics")
            return None

        for entry in topics:
            return entry[0]

        self.__logger.info("Cache miss for: %s#%s", chat_id, topic_id)

        return None
