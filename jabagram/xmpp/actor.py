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

import asyncio
import logging
import re
import stringprep

from functools import lru_cache
from collections import OrderedDict

from unidecode import unidecode
from slixmpp import ClientXMPP, JID
from slixmpp.stanza import Message
from slixmpp.exceptions import PresenceError

# XMPP does not support all available characters for resourcepart in JIDs, so
# we need to filter a range of characters.
BLACKLIST_USERNAME_CHARS = (
    stringprep.in_table_c12,
    stringprep.in_table_c21,
    stringprep.in_table_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_a1,
    stringprep.in_table_c9
)
RTL_CHAR_PATTERN = re.compile(r'[\u0590-\u05FF\u0600-\u06FF]')
XMPP_OCCUPANT_ERROR = "Only occupants are allowed to send messages to the conference"

class XmppActor(ClientXMPP):
    def __init__(
        self,
        jid: str,
        password: str,
        user_id: str,
        user_name: str
    ):
        ClientXMPP.__init__(self, f'{jid}/{user_id}', password)
        self._reconnecting = None
        self.__logger = logging.getLogger(
            f"{__class__.__name__}/{user_id}"
        )

        self.__name = user_name
        self.__rooms: list[str] = []

        for xep in ('xep_0030', 'xep_0249', 'xep_0071', 'xep_0363',
                    'xep_0308', 'xep_0045', 'xep_0066', 'xep_0199'):
            self.register_plugin(xep)

        self.__start_event = asyncio.Event()
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("groupchat_message_error", self.__process_errors)
        self.add_event_handler("disconnected", self.__on_connection_reset)
        self.add_event_handler("connected", self.__on_connected)

    async def _session_start(self, _):
        await self.get_roster()
        self.send_presence()
        self.__start_event.set()

        if self._reconnecting:
            self.__logger.info("Trying to rejoining to rooms...")
            for room in self.__rooms:
                await self.join(room)

    async def __on_connected(self, _):
        self.__logger.info("Successfully connected.")

    async def __on_connection_reset(self, event):
        if self._reconnecting is False:
            return

        self._reconnecting = True
        self.__logger.warning(
            "Connection reset: %s. Attempting to reconnect...",
            event
        )

        # Wait for synchronous handlers
        await asyncio.sleep(5)

        self.connect()

    async def __process_errors(self, message: Message):
        room: str = message['from'].bare
        if message['error']['text'] == XMPP_OCCUPANT_ERROR and room in self.__rooms:
            _ = await self.join(room)

    async def join(self, muc: str) -> bool:
        if muc in self.__rooms:
            return True

        count = 5
        while count > 0:
            try:
                self.__logger.info(
                    "Trying to join %s room...", muc
                )
                _ = await self.plugin['xep_0045'].join_muc_wait(
                    JID(muc),
                    self.__name,
                    maxstanzas=0,
                    timeout=5
                )
                self.__logger.info(
                    "Successfully joined to the room %s",
                    muc
                )
                self.__rooms.append(muc)
                return True
            except PresenceError as error:
                count = count - 1
                self.__logger.error("Failed to join muc: %s", error.text)
            except TimeoutError:
                count = count - 1
                self.__logger.error("Failed to join muc: max time exceeded")

        return False

    def leave(self, muc: str):
        if muc in self.__rooms:
            self.plugin['xep_0045'].leave_muc(
                room=JID(muc),
                nick=self.__name
            )

    async def start(self):
        self.connect()
        await asyncio.wait_for(self.__start_event.wait(), 15)

    async def destroy(self):
        self._reconnecting = False
        self.disconnect()

class XmppActorFactory():
    def __init__(
        self,
        jid: str,
        password: str,
        pool_size_limit: int,
        fallback: XmppActor
    ):
        self.__jid = jid
        self.__fallback = fallback
        self.__actors_pool: OrderedDict[str, XmppActor] = OrderedDict()
        self.__password = password
        self.__pool_size_limit = pool_size_limit
        self.__logger = logging.getLogger(__class__.__name__)

    async def get_actor(
        self,
        user_id: str,
        user_name: str,
        muc: str
    ) -> XmppActor:
        user_name = self.__validate_name(user_name) + " (Telegram)"

        if user_id in self.__actors_pool.keys():
            self.__actors_pool.move_to_end(user_id)
            actor = self.__actors_pool[user_id]
        else:
            self.__logger.info(
                f"Trying to create actor with {self.__jid}/{user_id}"
            )
            actor = XmppActor(
                jid=self.__jid,
                password=self.__password,
                user_name=user_name,
                user_id=user_id
            )
            self.__actors_pool[user_id] = actor
            self.__actors_pool.move_to_end(user_id)

            if len(self.__actors_pool) > self.__pool_size_limit:
                (_, removed) = self.__actors_pool.popitem(last=False)
                await removed.destroy()

            await actor.start()

        if not (await actor.join(muc)):
            return self.__fallback

        return actor

    def leave(self, muc: str):
        # TODO: Optimize it
        self.__fallback.leave(muc)

        for _, actor in self.__actors_pool.items():
            actor.leave(muc)

    @lru_cache(maxsize=100)
    def __validate_name(self, sender: str) -> str:
        if RTL_CHAR_PATTERN.search(sender):
            sender = unidecode(sender)

        valid: list[str] = []
        for char in sender:
            for check in BLACKLIST_USERNAME_CHARS:
                if check(char):
                    break
            else:
                valid.append(char)

        return "".join(valid)
