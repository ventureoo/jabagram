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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .cache import Cache

@dataclass(kw_only=True)
class Forwardable():
    address: str

@dataclass(kw_only=True)
class UnbridgeEvent(Forwardable):
    pass

@dataclass(kw_only=True)
class Event(Forwardable):
    event_id: str
    content: str


@dataclass(kw_only=True)
class Message(Event):
    sender: str
    reply: Optional[str] = None
    edit: Optional[bool] = False

@dataclass(kw_only=True)
class Attachment(Message):
    fname: str
    mime: Optional[str] = None
    fsize: Optional[int] = None


class ChatHandler(ABC):
    def __init__(self, address: str) -> None:
        self.__address = address

    @abstractmethod
    async def send_message(self, message: Message) -> None:
        pass

    @abstractmethod
    async def edit_message(self, message: Message) -> None:
        pass

    @abstractmethod
    async def send_event(self, event: Event) -> None:
        pass

    @abstractmethod
    async def send_attachment(self, attachment: Attachment) -> None:
        pass

    @abstractmethod
    async def unbridge(self) -> None:
        pass

    @property
    def address(self):
        return self.__address

class ChatHandlerFactory(ABC):
    @abstractmethod
    def create_handler(
        self,
        address: str,
        muc: str,
        cache: Cache
    ) -> None:
        pass
