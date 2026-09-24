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
import asyncio

class StanzaEntry():
    def __init__(self):
        self.__event = asyncio.Event()
        self.__stanza_id = None

    @property
    def event(self):
        return self.__event

    @property
    def stanza_id(self):
        return self.__stanza_id

    @stanza_id.setter
    def stanza_id(self, stanza_id: str):
        self.__stanza_id = stanza_id

class StanzaManager():
    def __init__(self):
        self.__stanzas: dict[str, StanzaEntry] = {}

    async def wait(self, message_id: str) -> str | None:
        entry = self.__stanzas.get(message_id)

        if not entry:
            entry = StanzaEntry()
            self.__stanzas[message_id] = entry

        _ = await asyncio.wait_for(entry.event.wait(), 15)

        stanza_id = entry.stanza_id
        del self.__stanzas[message_id]

        return stanza_id

    def deliver(self, message_id: str, stanza_id: str):
        entry = self.__stanzas.get(message_id)

        if not entry:
            return

        entry.stanza_id = stanza_id
        entry.event.set()
