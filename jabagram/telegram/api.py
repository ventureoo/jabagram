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
import asyncio
import logging

from aiohttp import (
    ClientSession,
    ClientResponseError,
    ClientConnectionError
)
from types import TracebackType
from typing import Optional, Type


class TelegramApiError(Exception):
    def __init__(self, code, desc):
        super().__init__(f"Telegram API error ({code}): {desc}")
        self.code = code
        self.desc = desc


class TelegramApi():
    def __init__(self, token: str):
        self.__token = token
        self.__session: ClientSession | None = None
        self.__logger = logging.getLogger(__class__.__name__)

    async def __aenter__(self) -> "TelegramApi":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        _: Optional[TracebackType]
    ) -> None:
        if exc_value and exc_type:
            self.__logger.error(
                "Unhandled exception occured: %s - %s",
                exc_type,
                exc_value
            )

        await self.close()

    async def close(self) -> None:
        if self.__session:
            await self.__session.close()

    async def __get_session(self) -> ClientSession:
        if not self.__session or self.__session.closed:
            self.__session = ClientSession()

        return self.__session

    def __getattr__(self, method: str):
        async def wrapper(*file, **kwargs):
            url = f"https://api.telegram.org/bot{self.__token}/{method}"
            params = {
                "params": kwargs,
                "data": file[0] if file else None
            }
            retry_attempts = 5

            while retry_attempts > 0:
                if retry_attempts != 5:
                    self.__logger.info(
                        "Retry to send request, attempts left: %s",
                        retry_attempts
                    )

                retry_attempts = retry_attempts - 1
                try:
                    session = await self.__get_session()
                    async with session.post(
                        url=url,
                        **params
                    ) as response:
                        # If an unknown error occurred and the response
                        # does not represent a valid TelegramApi error,
                        # ClientResponseError is raised
                        results = await response.json()

                        if results.get("ok"):
                            return results['result']

                        description = results.get('description')
                        paramaters = results.get("parameters")

                        if response.status == 429 and paramaters:
                            timeout = paramaters.get("retry_after")
                            self.__logger.warning(
                                "Too many requests, request "
                                "will be executed again in: %d secs",
                                timeout
                            )
                            await asyncio.sleep(timeout)
                        else:
                            raise TelegramApiError(
                                response.status, description
                            )

                except ClientConnectionError as error:
                    self.__logger.error(
                        "Failed to execute the request: %s", error
                    )
                except ClientResponseError as error:
                    self.__logger.error(
                        "Failed to get Telegram response: %s", error
                    )
                except asyncio.TimeoutError:
                    continue

            raise TelegramApiError(
                -1, "The number of retry attempts has been exhausted"
            )

        return wrapper

