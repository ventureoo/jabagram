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
import aiohttp
import hashlib

from abc import ABC, abstractmethod
from aiohttp import ClientSession
from collections import OrderedDict
from functools import lru_cache
from jabagram.model import Sender
from pathlib import Path
from slixmpp import ClientXMPP, JID, BaseXMPP
from slixmpp.componentxmpp import ComponentXMPP
from slixmpp.exceptions import IqError, IqTimeout, PresenceError
from slixmpp.stanza import Message
from slixmpp.types import PresenceArgs
from typing import IO, override
from unidecode import unidecode

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

XEPS = (
    'xep_0030', # Service Discovery (Disco)
    'xep_0045', # Multi-User Chat (MUC)
    'xep_0066', # Out of Band Data
    'xep_0071', # XHTML
    'xep_0199', # XMPP Ping
    'xep_0249', # Direct MUC Invitations
    'xep_0308', # Last Message Correction
    'xep_0363', # HTTP File Upload
)

class XmppActor(ABC):
    def __init__(
        self,
        client: BaseXMPP,
        user: Sender,
        upload_domain: str | None
    ):
        self.__client = client
        self.__logger = logging.getLogger(
            f"{__class__.__name__}/{user.id}"
        )
        self.__id = user.id
        self.__name = self.__validate_name(user.name)
        self._rooms: list[str] = []
        self.__upload_domain = upload_domain

        for xep in XEPS:
            self.__client.register_plugin(xep)

        self._start_event = asyncio.Event()
        self.__client.add_event_handler("groupchat_message_error", self.__process_errors)
        self.__client.add_event_handler("session_start", self._session_start)
        self.__client.add_event_handler("connected", self.__on_connected)

    async def _session_start(self, _):
        self.__client.send_presence(pfrom=self.get_from_value())
        self._start_event.set()

    async def __on_connected(self, _):
        self.__logger.info("Successfully connected.")

    async def upload_file(
        self,
        filename: Path,
        size: int,
        content_type: str | None = None,
        input_file: IO[bytes] | None = None
    ) -> str | None:
        xep_0363 = self.__client.plugin['xep_0363']

        info_iq = await xep_0363.find_upload_service(
            domain=JID(self.__upload_domain) if self.__upload_domain else None
        )

        if info_iq is None:
            self.__logger.error(
                "Failed to get upload domain"
            )
            return None

        service = info_iq['from']
        for form in info_iq['disco_info'].iterables:
            values = form['values']
            if values['FORM_TYPE'] == ['urn:xmpp:http:upload:0']:
                try:
                    if size >= int(values['max-file-size']):
                        self.__logger.error(
                            "Max file size exceeded: %s", size
                        )
                        return None
                except (TypeError, ValueError):
                    self.__logger.error(
                        "Invalid max size received from HTTP File Upload service"
                    )
                break

        slot_iq = await xep_0363.request_slot(
            service,
            filename,
            size,
            content_type,
            ifrom=self.get_from_value()
        )
        slot = slot_iq['http_upload_slot']

        headers = {
            'Content-Length': str(size),
            'Content-Type': content_type or "application/octet-stream",
            **{header['name']: header['value'] for header in slot['put']['headers']}
        }

        url = None
        async with ClientSession(
            headers={'User-Agent': 'jabagram'}
        ) as session:
            response = await session.put(
                    slot['put']['url'],
                    data=input_file,
                    headers=headers,
            )
            if response.status >= 400:
                self.__logger.error(
                    "Failed to upload file: %s",
                    response.status
                )
                return None

            response.close()
            url = slot['get']['url']

        return url

    def get_jid(
        self,
        room: JID,
        nick: str,
    ) -> JID | None:
        jid = self.__client.plugin['xep_0045'].get_jid_property(
            room=room,
            nick=nick,
            jid_property="jid",
        )
        return jid

    async def __process_errors(self, message: Message):
        room: str = message['from'].bare
        if message['error']['text'] == XMPP_OCCUPANT_ERROR and room in self.__rooms:
            _ = await self.join(room)

    async def join(self, muc: str) -> bool:
        self.__logger.info(
            "Trying to join %s room...", muc
        )

        if self.__client.is_component:
            presence_options = PresenceArgs(
                pfrom=str(self.get_from_value())
            )
        else:
            presence_options = None

        for _ in range(5):
            try:
                await self.__client.plugin['xep_0045'].join_muc_wait(
                    room=JID(muc),
                    nick=self.__name,
                    maxstanzas=0,
                    timeout=5,
                    presence_options=presence_options
                )
                break
            except TimeoutError:
                self.__logger.error("Failed to join muc: max time exceeded")
            except PresenceError as error:
                self.__logger.error("Failed to join muc: %s", error.text)
        else:
            self.__logger.error("Max number of join attempts exceeded")
            return False

        self.__logger.info(
            "Successfully joined to the room %s", muc
        )

        if muc not in self._rooms:
            self._rooms.append(muc)

        return True

    def leave(self, muc: str):
        if muc in self._rooms:
            self.__client.plugin['xep_0045'].leave_muc(
                room=JID(muc),
                nick=self.__name,
                pfrom=self.get_from_value()
            )
            self._rooms.remove(muc)

    async def start(self):
        await asyncio.wait_for(self._start_event.wait(), 15)

    async def destroy(self):
        self.__client.disconnect()

    def make_message(self, *args, **kwargs) -> Message:
        kwargs["mfrom"] = self.get_from_value()

        message = self.__client.make_message(
            *args,
            **kwargs,
        )
        return message

    @abstractmethod
    def get_from_value(self) -> JID | None:
        pass

    @abstractmethod
    def client(self) -> BaseXMPP:
        pass

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

class XmppReconnectableActor(XmppActor, ABC):
    def __init__(
        self,
        user: Sender,
        client: BaseXMPP
    ):
        super().__init__(
            client=client,
            user=user,
            upload_domain=None
        )
        self._reconnecting: None | bool = None
        self.__client = client
        self.__logger = logging.getLogger(
            f"{__class__.__name__}/{user.id}"
        )
        self.__client.add_event_handler("disconnected", self.__on_connection_reset)

    @override
    async def join(self, muc: str) -> bool:
        if muc in self._rooms and not self._reconnecting:
            return True

        return await super().join(muc)

    @override
    async def _session_start(self, _):
        await super()._session_start(_)

        if self._reconnecting:
            self.__logger.info("Trying to rejoining to rooms...")
            for room in self._rooms:
                await self.join(room)


    @override
    async def start(self):
        self.__client.connect()
        await super().start()

    @override
    async def destroy(self):
        self._reconnecting = False
        await super().destroy()

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

        self.__client.connect()


class XmppUserActor(XmppReconnectableActor):
    def __init__(
        self,
        jid: str,
        password: str,
        user: Sender,
    ):
        client = ClientXMPP(
            jid=f'{jid}/{user.id}',
            password=password
        )
        super().__init__(
            client=client,
            user=user,
        )

    @override
    def get_from_value(self) -> JID | None:
        return None

    @override
    def client(self) -> BaseXMPP:
        return self.__client

class XmppComponentActor(XmppActor):
    def __init__(
        self,
        client: ComponentXMPP,
        user: Sender,
        upload_domain: str | None
    ):
        super().__init__(
            client=client,
            user=user,
            upload_domain=upload_domain
        )
        self.__user = user
        self.__client = client
        self.__logger = logging.getLogger(__class__.__name__)
        self.__client.register_plugin('xep_0153') # vCard-Based Avatars

    async def __set_avatar(self):
        if not self.__user.avatar_callback:
            return None

        url: str | None = await self.__user.avatar_callback()

        if not url:
            return None

        disco = self.__client.plugin["xep_0030"]
        vcard_temp = self.__client.plugin["xep_0054"]
        vcard_avatar = self.__client.plugin["xep_0153"]

        avatar = None
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status >= 400:
                    self.__logger.error("Failed to download avatar: %s", response.status)
                    return None

                avatar = await response.read()

        if not avatar:
            self.__logger.error("Failed to get avatar")
            return

        hash = hashlib.sha1(avatar).hexdigest()
        jid = self.get_from_value()

        try:
            vcard = vcard_temp.make_vcard()
            vcard['PHOTO']['TYPE'] = "image/jpeg"
            vcard['PHOTO']['BINVAL'] = avatar

            await vcard_temp.publish_vcard(vcard=vcard, jid=jid)
            await vcard_avatar.api["set_hash"](jid, args=hash)

            disco.add_identity(
                category='gateway',
                itype='telegram',
                name='jabagram'
            )
            disco.add_feature('http://jabber.org/protocol/disco#info')
            disco.add_feature('vcard-temp')
            disco.add_feature('jabber:iq:last')
            self.__logger.info(f"Avatar set for {self.__user}")
        except (IqError, IqTimeout) as err:
            self.__logger.error(
                "Failed to set avatar for %s: %s",
                self.__user,
                err
            )

    @override
    def get_from_value(self) -> JID | None:
        return JID(f"{self.__user.id}@{self.__client.boundjid.bare}")

    @override
    def client(self) -> ComponentXMPP:
        return self.__client

    @override
    async def start(self):
        await self.__set_avatar()

class XmppActorFactory():
    def __init__(
        self,
        listener: XmppActor,
        upload_domain: str | None,
        pool_size_limit: int = 16,
    ):
        self.__actors_pool: OrderedDict[str, XmppActor] = OrderedDict()
        self.__pool_size_limit = pool_size_limit
        self.__logger = logging.getLogger(__class__.__name__)
        self.__listener = listener
        self.__client = self.__listener.client()
        self.__jid = self.__client.boundjid
        self.__locks: dict[str, asyncio.Lock] = {}
        self.__upload_domain = upload_domain

    async def get_actor(
        self,
        user: Sender | None,
        muc: str
    ) -> XmppActor:
        if not user:
            return self.__listener

        if user.id in self.__actors_pool.keys():
            self.__actors_pool.move_to_end(user.id)
            actor = self.__actors_pool[user.id]
            return actor

        lock = self.__locks.get(user.id)
        if not lock:
            lock = asyncio.Lock()
            self.__locks[user.id] = lock

        async with lock:
            self.__logger.info(
                f"Trying to create actor with {self.__jid}/{user.id}"
            )

            if isinstance(self.__client, ClientXMPP):
                actor = XmppUserActor(
                    jid=self.__client.jid,
                    password=self.__client.password,
                    user=user,
                )

                await actor.start()

            elif isinstance(self.__client, ComponentXMPP):
                actor = XmppComponentActor(
                    client=self.__client,
                    user=user,
                    upload_domain=self.__upload_domain,
                )
                await actor.start()
            else:
                return self.__listener

            if not (await actor.join(muc)):
                await actor.destroy()
                return self.__listener

            self.__actors_pool[user.id] = actor
            self.__actors_pool.move_to_end(user.id)


            if len(self.__actors_pool) > self.__pool_size_limit:
                (_, removed) = self.__actors_pool.popitem(last=False)
                await removed.destroy()

            del self.__locks[user.id]

        return actor

    def leave(self, muc: str):
        # TODO: Optimize it
        self.__listener.leave(muc)

        for _, actor in self.__actors_pool.items():
            actor.leave(muc)
