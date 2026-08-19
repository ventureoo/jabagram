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

from dataclasses import dataclass
import logging

from gettext import gettext as _
from datetime import datetime
from typing import override

from slixmpp import JID, ClientXMPP
from slixmpp.componentxmpp import ComponentXMPP
from jabagram.command import UserCommandHandler
from jabagram.database.messages import MessageStorage
from jabagram.database.stickers import StickerCache
from jabagram.dispatcher import MessageDispatcher
from jabagram.service import ChatService
from jabagram.model import (
    Attachment,
    Chat,
    Realm,
    ChatHandler,
    ChatHandlerFactory,
    Sender,
    Message,
)
from jabagram.xmpp.actor import XmppActorFactory, XmppActor
from jabagram.xmpp.handler import XmppRoomHandler

BRIDGE_DEAFAULT_ID = "listener"
BRIDGE_DEAFAULT_NAME = "Telegram Bridge"

@dataclass
class XmppConnectionSettings():
    jid: str
    secret: str
    host: str | None
    port: int | None

class XmppListener(XmppActor, ChatHandlerFactory):
    def __init__(
        self,
        settings: XmppConnectionSettings,
        chat_service: ChatService,
        command_handler: UserCommandHandler,
        disptacher: MessageDispatcher,
        sticker_cache: StickerCache,
        message_storage: MessageStorage,
        actors_pool_size_limit: int,
        upload_domain: str | None,
    ) -> None:
        if settings.port and settings.host:
            self.__client = ComponentXMPP(
                jid=settings.jid,
                secret=settings.secret,
                host=settings.host,
                port=settings.port,
            )
        else:
            self.__client = ClientXMPP(
                jid=settings.jid,
                password=settings.secret
            )
        super().__init__(
            client=self.__client,
            upload_domain=upload_domain,
            user=Sender(name=BRIDGE_DEAFAULT_NAME, id=BRIDGE_DEAFAULT_ID)
        )
        self.__chat_service = chat_service
        self.__dispatcher = disptacher
        self.__sticker_cache = sticker_cache
        self.__command_handler = command_handler
        self.__message_storage = message_storage
        self.__actor_factory = XmppActorFactory(
            pool_size_limit=actors_pool_size_limit,
            listener=self,
            upload_domain=upload_domain,
        )
        self.__logger = logging.getLogger(self.__class__.__name__)
        self.__client.add_event_handler("groupchat_direct_invite", self.__invite_callback)
        self.__client.add_event_handler("message", self.__process_user_command)
        self.__client.add_event_handler("groupchat_message", self.__process_muc_message)

    @override
    def get_from_value(self) -> JID | None:
        if self.__client.is_component:
            return JID(f"{BRIDGE_DEAFAULT_ID}@{self.__client.boundjid.bare}")

        return None

    @override
    def realm(self):
        return Realm.XMPP

    @override
    def client(self) -> ClientXMPP | ComponentXMPP:
        return self.__client

    @override
    async def create_handler(
        self,
        address: str,
    ) -> ChatHandler | None:
        handler = XmppRoomHandler(
            chat=Chat(address=address, realm=Realm.XMPP),
            sticker_cache=self.__sticker_cache,
            message_storage=self.__message_storage,
            actor_factory=self.__actor_factory
        )

        if (await self.join(address)):
            return handler

        return None

    @override
    async def _session_start(self, _):
        _ = await super()._session_start(_)

        if not self._reconnecting:
            self.__chat_service.register_factory(Realm.XMPP, self)
            await self.__chat_service.load_chats()

    async def __invite_callback(self, invite):
        muc = str(invite['groupchat_invite']['jid'])
        await self.join(muc)


    # TODO: Rewrite using XEP-0050: Ad-Hoc Commands
    async def __process_user_command(self, message):
        if message['type'] != 'chat':
            return

        command: str = message['body']
        sender: str = message['from'].bare

        if not command.startswith("!jabagram"):
            return

        response = self.__command_handler.handle_direct_user_command(
            Realm.XMPP,
            command=command,
            user=sender,
        )
        message.reply(response).send()

    async def __process_muc_message(self, message):
        sender: str = message['mucnick']
        message_id: str = message['id']
        muc: str = message['mucroom']
        body: str = message['body'].strip()

        user_id = self.get_from_value()
        if user_id and message.get('to') != user_id:
            return

        if body.startswith("!jabagram"):
            jid = self.get_jid(
                room=JID(muc),
                nick=sender,
            )

            if not jid:
                self.make_message(
                    mto=muc,
                    mtype="groupchat",
                    mbody=_(
                      "Bridge can’t get your JID to verify permissions. "
                      "Please make bridge chat admin so that it can see other users' JIDs."
                    )
                ).send()
                return

            response = (await self.__command_handler.handle_chat_user_command(
                source_chat=Chat(address=muc, realm=Realm.XMPP),
                command=body,
                user=jid.bare,
            ))
            self.make_message(
                mto=muc,
                mtype="groupchat",
                mbody=response
            ).send()
            return

        if not self.__dispatcher.is_paired(muc):
            return

        if sender.endswith("(Telegram)") or sender == BRIDGE_DEAFAULT_NAME:
            return

        if message['oob']['url']:
            url = message['oob']['url']

            async def url_callback():
                return url

            caption = None
            url_start = body.rfind(url)

            if url_start > 0:
                caption = body[:url_start].strip()

            fname: str = url.split("/")[-1]
            attachment = Attachment(
                id=message_id,
                chat=Chat(realm=Realm.XMPP, address=str(muc)),
                sender=Sender(name=sender, id=""),
                url_callback=url_callback,
                fname=fname,
                text=caption if caption else "",
                mime=None,
                fsize=None,
            )
            await self.__dispatcher.send(attachment)
        else:
            is_edit = False

            if message['replace']['id']:
                message_id: str = message['replace']['id']
                is_edit = True

            reply, text = self.__parse_reply(body)

            message = Message(
                id=message_id,
                chat=Chat(realm=Realm.XMPP, address=muc),
                sender=Sender(name=sender, id=""),
                text=text if reply and text else body,
                reply=reply,
                edit=is_edit
            )
            await self.__dispatcher.send(message)

    def __parse_reply(self, message: str) -> tuple[str | None, str | None]:
        def _safe_get(line: str, index: int):
            try:
                return line[index]
            except IndexError:
                return None

        replies: list[str] = []
        parts: list[str] = []

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
                    _ = datetime.strptime(line, '%Y-%m-%d  %H:%M (GMT%z)')

                    # Remove sender name of message being replied to
                    _ = replies.pop()
                except ValueError:
                    replies.append(line)
            else:
                parts.append(line)

        reply = "\n".join(replies)
        body = "\n".join(parts)

        return reply, body
