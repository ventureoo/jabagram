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
    Chat,
    ChatHandler,
    Event,
    Sender,
    Message,
    Sticker,
)
from jabagram.xmpp.actor import XmppActorFactory

from aiohttp import ClientConnectionError
from pathlib import Path
from slixmpp.exceptions import IqTimeout, IqError
from slixmpp.jid import JID
from slixmpp.plugins.xep_0363.http_upload import HTTPError

class XmppRoomHandler(ChatHandler):
    def __init__(
        self,
        chat: Chat,
        actor_factory: XmppActorFactory,
        message_storage: MessageStorage,
        sticker_cache: StickerCache,
    ) -> None:
        super().__init__(chat)
        self.__chat = chat
        self.__actor_factory = actor_factory
        self.__muc = JID(chat.address)
        self.__message_storage = message_storage
        self.__sticker_cache = sticker_cache
        self.__logger = logging.getLogger(f"XmppRoomHandler {chat.address}")

    async def send_message(self, origin: Message) -> None:
        self.__logger.info("Sending message with id: %s", origin.id)

        mbody = origin.text

        if origin.reply and origin.reply.body:
            reply_body = "> " + origin.reply.body.replace("\n", "\n> ")
            mbody = f"{reply_body}\n{origin.text}"

        actor = await self.__actor_factory.get_actor(
            user=Sender(
                id=origin.sender.id,
                name=origin.sender.name,
                avatar_callback=origin.sender.avatar_callback,
            ),
            muc=str(self.__muc)
        )
        message = actor.make_message(
            mto=self.__muc,
            mtype="groupchat",
            mbody=mbody
        )
        message.send()

        self.__message_storage.add(
            source=origin.chat,
            target=self.__chat,
            target_message_id=message['id'],
            source_message_id=origin.id,
            body=origin.text,
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
                "Sending attachment with name: %s", attachment.fname
            )

        if not url:
            url = await attachment.url_callback()

            if not url:
                self.__logger.error(
                    "Failed to get URL for: %s",
                    attachment
                )
                return

        actor = await self.__actor_factory.get_actor(
            user=Sender(
                id=attachment.sender.id,
                name=attachment.sender.name,
                avatar_callback=attachment.sender.avatar_callback
            ),
            muc=str(self.__muc)
        )

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    url = await actor.upload_file(
                        filename=Path(attachment.fname or f"File from {attachment.sender.name}"),
                        size=attachment.fsize or resp.content_length,
                        content_type=attachment.mime or resp.content_type,
                        input_file=resp.content # type: ignore
                    )

                    if not url:
                        self.__logger.error(
                            "Failed to upload attachment: %s",
                            attachment
                        )
                        return

                    if isinstance(attachment, Sticker) and url:
                        self.__sticker_cache.add(
                            attachment.file_id, url
                        )

            except(
                HTTPError,
                ClientConnectionError,
                IqTimeout,
                IqError
            ) as error:
                self.__logger.error("Cannot upload file: %s", error)
                return

        body = url
        if attachment.text:
            body = f"{attachment.text}\n{body}"

        if attachment.reply and attachment.reply.body:
            reply = "> " + attachment.reply.body.replace("\n", "\n> ")
            body = f"{reply}\n{body}"

        message = actor.make_message(
            mbody=body,
            mto=self.__muc,
            mtype='groupchat',
        )
        message['oob']['url'] = url
        self.__logger.info("Attachment message: %s", message)
        message.send()

        if body:
            self.__message_storage.add(
                source=attachment.chat,
                target=self.__chat,
                target_message_id=message['id'],
                source_message_id=attachment.id,
                body=body,
                topic_id=attachment.chat.topic_id
            )

    async def edit_message(self, edited: Message) -> None:
        result = self.__message_storage.get_by_id(
            source=edited.chat.address,
            target=str(self.__muc),
            topic_id=edited.chat.topic_id,
            message_id=edited.id
        )

        if not result:
            self.__logger.info(
                "Failed to found stanza for event: %s",
                edited.id
            )
            return

        mbody = edited.text
        actor = await self.__actor_factory.get_actor(
            user=Sender(
                id=edited.sender.id,
                name=edited.sender.name,
                avatar_callback=edited.sender.avatar_callback,
            ),
            muc=str(self.__muc)
        )

        if edited.reply and edited.reply.body:
            reply_body = "> " + edited.reply.body.replace("\n", "\n> ")
            mbody = f"{reply_body}\n{mbody}"

        message = actor.make_message(
            mto=self.__muc,
            mtype="groupchat",
            mbody=mbody
        )
        message['replace']['id'] = result.target_id
        message.send()

    async def send_event(self, event: Event) -> None:
        actor = await self.__actor_factory.get_actor(
            user=None,
            muc=str(self.__muc)
        )

        message = actor.make_message(
            mto=self.__muc,
            mbody=event.text,
            mtype="groupchat"
        )
        message.send()

    async def unbridge(self) -> None:
        actor = await self.__actor_factory.get_actor(
            user=None,
            muc=str(self.__muc)
        )

        message = actor.make_message(
            mto=self.__muc,
            mbody=_(
                "This chat was automatically unbridged "
                "due to a bot kick in Telegram."
            ),
            mtype="groupchat"
        )
        message.send()
        self.__actor_factory.leave(str(self.__muc))
