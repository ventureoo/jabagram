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

from datetime import datetime
from jabagram.cache import Cache, StickerCache
from jabagram.database import ChatService
from jabagram.dispatcher import MessageDispatcher
from jabagram.messages import Messages
from jabagram.model import (
    Attachment,
    ChatHandlerFactory,
    Message,
)
from jabagram.xmpp.handler import XmppRoomHandler
from slixmpp import ClientXMPP, JID
from slixmpp.exceptions import PresenceError

BRIDGE_DEFAULT_NAME = "Telegram Bridge"
XMPP_OCCUPANT_ERROR = "Only occupants are allowed to send messages to the conference"


class XmppClient(ClientXMPP, ChatHandlerFactory):
    def __init__(
        self,
        jid: str,
        password: str,
        service: ChatService,
        disptacher: MessageDispatcher,
        sticker_cache: StickerCache,
        messages: Messages
    ) -> None:
        ClientXMPP.__init__(self, jid, password)
        self.__service = service
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.__dispatcher = disptacher
        self.__sticker_cache = sticker_cache
        self.__mucs = []

        # Used XEPs
        xeps = ('xep_0030', 'xep_0249', 'xep_0071', 'xep_0363',
                'xep_0308', 'xep_0045', 'xep_0066', 'xep_0199')
        list(map(self.register_plugin, xeps))

        # Common event handlers
        self.add_event_handler("session_start", self.__session_start)
        self.add_event_handler("groupchat_message", self.__process_message)
        self.add_event_handler(
            "groupchat_message_error", self.__process_errors
        )
        self.add_event_handler(
            'groupchat_direct_invite', self.__invite_callback
        )
        self.add_event_handler("disconnected", self.__on_connection_reset)
        self.add_event_handler("connected", self.__on_connected)
        self.__reconnecting = False
        self.__service.register_factory(self)
        self.__messages = messages

    async def start(self):
        self.connect()

    async def create_handler(
        self,
        address: str,
        muc: str,
        cache: Cache,
    ) -> None:
        handler = XmppRoomHandler(
            muc,
            self,
            cache,
            self.__sticker_cache,
            self.__messages
        )

        try:
            await self.plugin['xep_0045'].join_muc_wait(
                JID(muc),
                BRIDGE_DEFAULT_NAME,
                maxstanzas=0
            )
            self.__mucs.append(muc)
            self.__dispatcher.add_handler(address, handler)
        except PresenceError as error:
            self.__logger.error("Failed to join muc: %s", error)


    async def __on_connection_reset(self, event):
        self.__logger.warning(
            "Connection reset: %s. Attempting to reconnect...", event)
        self.__reconnecting = True

        # Wait for synchronous handlers
        await asyncio.sleep(5)

        self.connect()

    async def __session_start(self, _):
        await self.get_roster()
        self.send_presence()

        if self.__reconnecting:
            for muc in self.__mucs:
                try:
                    self.__logger.info(
                        "Trying to rejoin %s room...", muc
                    )
                    await self.plugin['xep_0045'].join_muc_wait(
                        JID(muc),
                        BRIDGE_DEFAULT_NAME,
                        maxstanzas=0
                    )
                    self.__logger.info(
                        "Successfully rejoined to the room %s"
                    )
                except PresenceError as error:
                    self.__logger.error(
                        "Failed to re-join into MUC: %s", error
                    )
        else:
            await self.__service.load_chats()

    async def __on_connected(self, _):
        self.__logger.info("Successfully connected.")

    async def __invite_callback(self, invite):
        muc = str(invite['groupchat_invite']['jid'])
        key = invite['groupchat_invite']['reason']
        await self.__service.bind(muc, key)

    async def __process_errors(self, message):
        room = message['from'].bare
        if message['error']['text'] == XMPP_OCCUPANT_ERROR \
                and self.__dispatcher.is_bound(room):
            await self.plugin['xep_0045'].join_muc_wait(
                message['from'],
                BRIDGE_DEFAULT_NAME,
                maxstanzas=0
            )

    async def __process_message(self, message):
        sender = message['mucnick']
        message_id = message['id']
        muc = message['mucroom']
        text = message['body'].strip()

        if not self.__dispatcher.is_bound(muc):
            return

        if sender.endswith("(Telegram)") or \
                sender == BRIDGE_DEFAULT_NAME:
            return

        if message['oob']['url']:
            url = message['oob']['url']

            async def url_callback():
                return url

            fname = url.split("/")[-1]
            attachment = Attachment(
                event_id=message_id,
                address=muc,
                sender=sender,
                url_callback=url_callback,
                content=fname,
                mime=None,
                fsize=None
            )
            await self.__dispatcher.send(attachment)
        else:
            content = text
            is_edit = False

            if message['replace']['id']:
                message_id = message['replace']['id']
                is_edit = True

            reply, body = self.__parse_reply(text)

            if reply and body:
                content = body

            message = Message(
                address=muc,
                content=content,
                reply=reply,
                edit=is_edit,
                sender=sender,
                event_id=message_id
            )
            await self.__dispatcher.send(message)

    def __parse_reply(self, message: str) -> tuple[str | None, str | None]:
        def _safe_get(line: str, index: int):
            try:
                return line[index]
            except IndexError:
                return None

        replies = []
        parts = []

        for line in message.splitlines():
            if _safe_get(line, 0) == ">":
                # Ignore brackets not followed by space
                if _safe_get(line, 1) != " ":
                    continue

                # Ignore nested replies
                if _safe_get(line, 2) == ">":
                    continue

                line = line.replace("> ", "").strip()

                # Attempt to detect a replies format of some mobile clients
                # that add time and sender name of the message sent
                try:
                    datetime.strptime(line, '%Y-%m-%d  %H:%M (GMT%z)')

                    # Remove sender name of message being replied to
                    replies.pop()
                except ValueError:
                    replies.append(line)
            else:
                parts.append(line)

        reply = "\n".join(replies)
        body = "\n".join(parts)

        return reply, body
