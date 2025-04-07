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
import logging
import stringprep
import re

from aiohttp import ClientSession, ClientConnectionError
from functools import lru_cache
from jabagram.cache import Cache, StickerCache
from jabagram.messages import Messages
from jabagram.model import ChatHandler, Event, Message, Attachment, Sticker
from jabagram.xmpp.client import XmppClient
from pathlib import Path
from slixmpp.exceptions import IqTimeout
from slixmpp.jid import JID
from slixmpp.plugins.xep_0363.http_upload import HTTPError
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
BRIDGE_DEFAULT_NAME = "Telegram Bridge"
RTL_CHAR_PATTERN = re.compile(r'[\u0590-\u05FF\u0600-\u06FF]')

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
        self.__cache = cache
        self.__logger = logging.getLogger(f"XmppRoomHandler {address}")
        self.__sticker_cache = sticker_cache
        self.__messages = messages
        self.__muc_handle = client.plugin['xep_0045']

    async def __change_nick(self, sender: str):
        sender = self.__validate_name(sender) + " (Telegram)"

        if sender == self.__muc_handle.get_nick(self.__muc, self.__client.boundjid):
            return

        self.__logger.debug("Changing nick to %s", sender)
        try:
            await self.__muc_handle.set_self_nick(
                room=self.__muc,
                new_nick=sender,
                timeout=10
            )
        except TimeoutError:
            self.__logger.error("Failed to change nickname to: %s", sender)

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

        upload_file = self.__client.plugin['xep_0363'].upload_file

        async with ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    url = await upload_file(
                        filename=Path(attachment.content),
                        size=attachment.fsize or resp.content_length,
                        content_type=attachment.mime or resp.content_type,
                        input_file=resp.content # type: ignore
                    )
            except (HTTPError, ClientConnectionError, IqTimeout) as error:
                self.__logger.error("Cannot upload file: %s", error)
                return

        await self.__change_nick(attachment.sender)

        if attachment.reply:
            reply_body = "> " + attachment.reply.replace("\n", "\n> ")
            self.__client.send_message(
                mto=self.__muc,
                mbody=reply_body,
                mtype="groupchat"
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

    async def send_sticker(self, sticker: Sticker) -> None:
        self.__logger.info("Sending sticker with id: %s", sticker.file_id)
        url = self.__sticker_cache.get(sticker.file_id)

        if url:
            try:
                async with ClientSession() as session:
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

            async with ClientSession() as session:
                try:
                    async with session.get(attachment_url) as resp:
                        url = await upload_file(
                            filename=Path(sticker.content),
                            size=sticker.fsize or resp.content_length,
                            content_type=sticker.mime or resp.content_type,
                            input_file=resp.content  # type: ignore
                        )
                        self.__sticker_cache.add(
                            sticker.file_id, url
                        )
                except (HTTPError, ClientConnectionError) as error:
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
                "Failed to found stanza for event: %s",
                message.event_id
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
        self.__muc_handle.leave_muc(
            room=self.__muc,
            nick=self.__muc_handle.get_nick(
                self.__muc, self.__client.boundjid
            ) or BRIDGE_DEFAULT_NAME
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
