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
from collections.abc import Coroutine
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, override

class Realm(Enum):
    TELEGRAM = 1
    XMPP = 2
    MATRIX = 3

@dataclass(kw_only=True, frozen=True)
class Chat():
    address: str
    realm: Realm
    topic_id: int | None = None

    @override
    def __hash__(self):
        return hash(self.address)

@dataclass(kw_only=True)
class Forwardable():
    chat: Chat

@dataclass(kw_only=True)
class UnbridgeEvent(Forwardable):
    pass

@dataclass(kw_only=True)
class Sender():
    name: str
    id: str
    avatar_callback: Callable[[], Coroutine[None, None, str | None]] | None = field(repr=False)

@dataclass(kw_only=True)
class Event(Forwardable):
    id: str
    text: str = field(repr=False)

@dataclass(kw_only=True)
class Reply():
    id: str | None
    body: str | None

@dataclass(kw_only=True)
class Message(Event):
    sender: Sender
    reply: Reply | None = field(repr=False, default=None)
    edit: bool | None = False

@dataclass(kw_only=True)
class Attachment(Message):
    url_callback: Callable[[], Coroutine[None, None, str | None]] = field(repr=False)
    fname: str | None = None
    mime: str | None = "application/octet-stream"
    fsize: int | None = None

@dataclass(kw_only=True)
class Sticker(Attachment):
    file_id: str

class ChatHandler(ABC):
    def __init__(self, chat: Chat) -> None:
        self.__chat = chat

    @abstractmethod
    async def send_message(self, origin: Message) -> None:
        pass

    @abstractmethod
    async def edit_message(self, edited: Message) -> None:
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
    def chat(self):
        return self.__chat

class ChatHandlerFactory(ABC):
    @abstractmethod
    async def create_handler(
        self,
        address: str,
    ) -> ChatHandler | None:
        pass

    @abstractmethod
    def realm(self) -> Realm:
        pass
