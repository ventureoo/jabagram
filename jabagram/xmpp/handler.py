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
import aiohttp
import logging

from gettext import gettext as _
from jabagram.database.messages import MessageStorage
from jabagram.database.stickers import StickerCache
from jabagram.model import (
    Attachment,
    ChatHandler,
    Event,
    Message,
    Sticker,
)
from jabagram.xmpp.actor import XmppActorFactory, XmppActor

from pathlib import Path
from slixmpp.exceptions import IqTimeout
from slixmpp.jid import JID
from slixmpp.plugins.xep_0363.http_upload import HTTPError

class XmppRoomHandler(ChatHandler):
    def __init__(
        self,
        address: str,
        main_actor: XmppActor,
        actor_factory: XmppActorFactory,
        message_storage: MessageStorage,
        sticker_cache: StickerCache,
    ) -> None:
        super().__init__(address)
        self.__main_actor = main_actor
        self.__actor_factory = actor_factory
        self.__muc = JID(address)
        self.__message_storage = message_storage
        self.__sticker_cache = sticker_cache
        self.__logger = logging.getLogger(f"XmppRoomHandler {address}")

    async def send_message(self, origin: Message) -> None:
        self.__logger.info("Sending message with id: %s", origin.id)

        mbody = origin.content

        if origin.reply:
            reply_body = "> " + origin.reply.replace("\n", "\n> ")
            mbody = f"{reply_body}\n{origin.content}"

        actor = await self.__actor_factory.get_actor(
            origin.sender.id,
            origin.sender.name,
            str(self.__muc)
        )
        message = actor.make_message(
            mto=self.__muc,
            mtype="groupchat",
            mbody=mbody
        )
        message.send()

        self.__message_storage.add(
            chat_id=int(origin.chat.address),
            muc=str(self.__muc),
            stanza_id=message['id'],
            telegram_id=origin.id,
            body=origin.content,
            topic_id=origin.chat.topic_id
        )

    async def send_attachment(self, attachment: Attachment) -> None:
        url = None
        if isinstance(attachment, Sticker):
            self.__logger.info("Sending sticker with id: %s", attachment.file_id)
            url = self.__sticker_cache.get(attachment.file_id)

            # Reupload file if XMPP server deleted it after some time.
            if url:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.head(url) as resp:
                            if resp.status == 404:
                                url = None
                                self.__logger.info(
                                    "Cache miss for a file: %s",
                                    attachment.file_id
                                )
                except aiohttp.ClientConnectionError as error:
                    self.__logger.error(
                        "Cannot do head request for file: %s", error
                    )

        else:
            self.__logger.info(
                "Sending attachment with name: %s", attachment.content
            )

        if not url:
            url = await attachment.url_callback()

        actor = await self.__actor_factory.get_actor(
            attachment.sender.id,
            attachment.sender.name,
            str(self.__muc)
        )

        upload_file = actor.plugin['xep_0363'].upload_file

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    url = await upload_file(
                        filename=Path(attachment.content),
                        size=attachment.fsize or resp.content_length,
                        content_type=attachment.mime or resp.content_type,
                        input_file=resp.content # type: ignore
                    )
                    if isinstance(attachment, Sticker):
                        self.__sticker_cache.add(
                            attachment.file_id, url
                        )

            except (HTTPError, aiohttp.ClientConnectionError, IqTimeout) as error:
                self.__logger.error("Cannot upload file: %s", error)
                return

        if attachment.reply:
            reply_body = "> " + attachment.reply.replace("\n", "\n> ")
            actor.send_message(
                mto=self.__muc,
                mbody=reply_body,
                mtype="groupchat"
            )

        html = (
            f'<body xmlns="http://www.w3.org/1999/xhtml">'
            f'<a href="{url}">{url}</a></body>'
        )
        message = actor.make_message(
            mbody=url,
            mto=self.__muc,
            mtype='groupchat',
            mhtml=html
        )
        message['oob']['url'] = url
        message.send()

    async def edit_message(self, edited: Message) -> None:
        result = self.__message_storage.get_by_id(
            chat_id=int(edited.chat.address),
            muc=str(self.__muc),
            topic_id=edited.chat.topic_id,
            message_id=edited.id
        )

        if not result:
            self.__logger.info(
                "Failed to found stanza for event: %s",
                edited.id
            )
            return

        mbody: str = edited.content
        actor = await self.__actor_factory.get_actor(
            edited.sender.id,
            edited.sender.name,
            str(self.__muc)
        )

        if edited.reply:
            reply_body = "> " + edited.reply.replace("\n", "\n> ")
            mbody = f"{reply_body}\n{edited.content}"

        message = actor.make_message(
            mto=self.__muc,
            mtype="groupchat",
            mbody=mbody
        )
        message['replace']['id'] = result.stanza_id
        message.send()

    async def send_event(self, event: Event) -> None:
        self.__main_actor.send_message(
            mto=self.__muc,
            mbody=event.content,
            mtype="groupchat"
        )

    async def unbridge(self) -> None:
        self.__main_actor.send_message(
            mto=self.__muc,
            mbody=_(
                "This chat was automatically unbridged "
                "due to a bot kick in Telegram."
            ),
            mtype="groupchat"
        )
        self.__actor_factory.leave(str(self.__muc))
