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
import sqlite3
import logging
from abc import abstractmethod
from typing import Optional

class SqliteTable():
    def __init__(self, path):
        self.__path = path
        self.__logger = logging.getLogger(__class__.__name__)

    def _execute(
        self,
        *args,
        statement: str,
        on_error_message: Optional[str] = None
    ) -> list | None:
        try:
            with sqlite3.connect(self.__path) as connection:
                cursor = connection.cursor()
                return [row for row in cursor.execute(statement, (*args,))]
        except sqlite3.Error as error:
            self.__logger.error(
                "%s: %s",
                on_error_message or "Failed to execute statement",
                error,
            )

        return None

    @abstractmethod
    def create(self) -> bool:
        pass
