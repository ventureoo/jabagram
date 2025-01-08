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
from datetime import datetime
from functools import lru_cache
import logging
import stringprep
import unicodedata
import re
from unidecode import unidecode

import aiohttp
from aiohttp import ClientConnectionError
from jabagram.messages import Messages
from slixmpp.clientxmpp import ClientXMPP
from slixmpp.exceptions import IqTimeout, PresenceError
from slixmpp.jid import JID
from slixmpp.plugins.xep_0363.http_upload import HTTPError

from .cache import Cache, StickerCache
from .database import ChatService
from .dispatcher import MessageDispatcher
from .model import Attachment, ChatHandler, ChatHandlerFactory, Event, Message, Sticker

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
BRIDGE_DEFAULT_NAME = "Telegram Bridge"
XMPP_OCCUPANT_ERROR = "Only occupants are allowed to send messages to the conference"
RTL_CHAR_PATTERN = re.compile(r'[\u0590-\u05FF\u0600-\u06FF]')


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
            self.add_event_handler(
                f"muc::{muc}::got_online", handler.nick_change_handler
            )
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
        text = message['body']

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
            params = {
                "address": muc,
                "event_id": message_id,
                "content": text,
                "sender": sender,
                "edit": False,
                "reply": None
            }

            if message['replace']['id']:
                params["event_id"] = message['replace']['id']
                params["edit"] = True

            reply, body = self.__parse_reply(text)

            if reply:
                params["content"] = body
                params["reply"] = reply

            message = Message(**params)
            await self.__dispatcher.send(message)

    def __parse_reply(self, message: str) -> tuple[str, str]:
        def _safe_get(line: str, index: int):
            try:
                return line[index]
            except IndexError:
                return None

        replies = []
        parts = []

        for line in message.splitlines():
            if _safe_get(line, 0) == ">":
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


class XmppRoomHandler(ChatHandler):
    def __init__(
        self,
        address: str,
        client: XmppClient,
        cache: Cache,
        sticker_cache: StickerCache,
        messages: Messages
    ) -> None:
        super().__init__(address)
        self.__client = client
        self.__muc = JID(address)
        self.__last_sender = BRIDGE_DEFAULT_NAME
        self.__cache = cache
        self.__logger = logging.getLogger(f"XmppRoomHandler {address}")
        self.__nick_change_event = asyncio.Event()
        self.__sticker_cache = sticker_cache
        self.__messages = messages

    def nick_change_handler(self, presence):
        nick = presence['from'].resource
        if nick == self.__last_sender:
            self.__nick_change_event.set()

    async def __change_nick(self, sender: str):
        sender = self.__validate_name(sender) + " (Telegram)"

        if sender == self.__last_sender:
            return

        self.__logger.debug("Changing nick to %s", sender)
        self.__client.send_presence(
            pto=f"{self.__muc.bare}/{sender}",
            pfrom=self.__client.boundjid.full
        )
        self.__last_sender = sender

        # To avoid getting deadlock if for some reason the nickname has not
        # been changed, even though we have processed its validity in advance.
        await asyncio.wait_for(self.__nick_change_event.wait(), 15)

    async def send_message(self, message: Message) -> None:
        self.__logger.info("Sending message with id: %s", message.event_id)

        params = {
            "mto": self.__muc,
            "mtype": "groupchat",
            "mbody": message.content
        }

        if message.reply:
            reply_body = "> " + message.reply.replace("\n", "\n> ")
            params["mbody"] = f"{reply_body}\n{message.content}"

        await self.__change_nick(message.sender)
        msg = self.__client.make_message(**params)
        self.__cache.reply_map.add(message.content, message.event_id)
        self.__cache.message_ids.add(message.event_id, msg['id'])
        msg.send()

    async def send_attachment(self, attachment: Attachment) -> None:
        url = await attachment.url_callback()

        if not url:
            return

        self.__logger.info(
            "Sending attachment with name: %s", attachment.content
        )

        await self.__change_nick(attachment.sender)

        if attachment.reply:
            reply_body = "> " + attachment.reply.replace("\n", "\n> ")
            self.__client.send_message(
                mto=self.__muc,
                mbody=reply_body,
                mtype="groupchat"
            )

        upload_file = self.__client.plugin['xep_0363'].upload_file

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    url = await upload_file(
                        filename=attachment.content,
                        size=attachment.fsize or resp.length,
                        content_type=attachment.mime or resp.content_type,
                        input_file=resp.content
                    )
                    html = (
                        f'<body xmlns="http://www.w3.org/1999/xhtml">'
                        f'<a href="{url}">{url}</a></body>'
                    )
                    message = self.__client.make_message(
                        mbody=url,
                        mto=self.__muc,
                        mtype='groupchat',
                        mhtml=html
                    )
                    message['oob']['url'] = url
                    message.send()
            except HTTPError as error:
                self.__logger.error("Cannot upload file: %s", error)
            except ClientConnectionError as error:
                self.__logger.error("Cannot upload file: %s", error)
            except IqTimeout as error:
                self.__logger.error("Cannot upload file: %s", error)

    async def send_sticker(self, sticker: Sticker) -> None:
        self.__logger.info("Sending sticker with id: %s", sticker.file_id)
        url = self.__sticker_cache.get(sticker.file_id)

        if url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url) as resp:
                        if resp.status == 404:
                            # Reupload file if XMPP server deleted it after
                            # some time.
                            url = None
                            self.__logger.info(
                                "Cache miss for a file: %s", sticker.file_id
                            )
            except ClientConnectionError as error:
                self.__logger.error(
                    "Cannot do head request for file: %s", error
                )
                return

        if not url:
            upload_file = self.__client.plugin['xep_0363'].upload_file
            attachment_url = await sticker.url_callback()

            if not attachment_url:
                return

            async with aiohttp.ClientSession() as session:
                try:
                    async with session.get(attachment_url) as resp:
                        url = await upload_file(
                            filename=sticker.content,
                            size=sticker.fsize or resp.content_length,
                            content_type=sticker.mime or resp.content_type,
                            input_file=resp.content
                        )
                        self.__sticker_cache.add(
                            sticker.file_id, url
                        )
                except HTTPError as error:
                    self.__logger.error("Cannot upload file: %s", error)
                    return
                except ClientConnectionError as error:
                    self.__logger.error("Cannot upload file: %s", error)
                    return
        else:
            self.__logger.info(
                "Sticker %s was taken from the cache", sticker.file_id
            )

        await self.__change_nick(sticker.sender)

        html = (
            f'<body xmlns="http://www.w3.org/1999/xhtml">'
            f'<a href="{url}">{url}</a></body>'
        )
        message = self.__client.make_message(
            mbody=url,
            mto=self.__muc,
            mtype='groupchat',
            mhtml=html
        )
        message['oob']['url'] = url
        message.send()

    async def edit_message(self, message: Message) -> None:
        stanza = self.__cache.message_ids.get(message.event_id)

        if not stanza:
            self.__logger.info(
                "Failed to found stanza for event: %s", message.event_id
            )
            return

        params = {
            "mto": self.__muc,
            "mtype": "groupchat",
            "mbody": message.content
        }

        if message.reply:
            reply_body = "> " + message.reply.replace("\n", "\n> ")
            params["mbody"] = f"{reply_body}\n{message.content}"

        await self.__change_nick(message.sender)
        msg = self.__client.make_message(**params)
        msg['replace']['id'] = stanza
        msg.send()

    async def send_event(self, event: Event) -> None:
        await self.__change_nick(BRIDGE_DEFAULT_NAME)
        self.__client.send_message(
            mto=self.__muc,
            mbody=event.content,
            mtype="groupchat"
        )

    async def unbridge(self) -> None:
        self.__client.send_message(
            mto=self.__muc,
            mbody=self.__messages.unbridge_xmpp,
            mtype="groupchat"
        )
        self.__client.plugin['xep_0045'].leave_muc(
            self.__muc, self.__last_sender
        )

    @lru_cache(maxsize=100)
    def __validate_name(self, sender: str) -> str:
        if RTL_CHAR_PATTERN.search(sender):
            sender = unidecode(sender)
        valid = []
        for char in sender:
            for check in BLACKLIST_USERNAME_CHARS:
                if check(char):
                    break
            else:
                valid.append(char)

        return "".join(valid)
