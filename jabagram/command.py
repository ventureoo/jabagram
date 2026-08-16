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

import logging

from gettext import gettext as _
from jabagram.model import Chat, Realm
from jabagram.database.admin import AdminStorage
from jabagram.service import ChatService
from slixmpp.jid import InvalidJID, JID

class UserCommandHandler():
    def __init__(
        self,
        secret_key: str,
        chat_service: ChatService,
        admin_storage: AdminStorage
    ) -> None:
        self.__admin_storage = admin_storage
        self.__chat_service = chat_service
        self.__secret_key = secret_key
        self.__logger = logging.getLogger(__class__.__name__)

    def handle_direct_user_command(
        self,
        realm: Realm,
        command: str,
        user: str,
    ):
        parts = command.split()

        if len(parts) < 2:
            return _("Missed subcommand")

        subcommand = parts[1]
        if subcommand == "verify":
            if len(parts) < 3:
                return _("Missed secret key for verification")

            result = self.__admin_storage.get(realm, user)

            if result:
                return _("You already verified user")

            key = parts[2]

            if key != self.__secret_key:
                return _("Wrong secret key is used")

            self.__admin_storage.add(realm, user)
            return _("You are successfully verified")

        return _("Invalid subcommand specified")


    async def handle_chat_user_command(
        self,
        source_chat: Chat,
        command: str,
        user: str,
    ) -> str:
        parts = command.split()

        if len(parts) < 2:
            return _("Missed subcommand")

        subcommand = parts[1]

        if not self.__admin_storage.get(
            realm=source_chat.realm,
            user_id=user
        ):
            return _(
                "You're not verified user to execute any commands"
            )

        if subcommand == "pair":
            if len(parts) < 3:
                return _(
                    "Please specify the another gateway with which "
                    "you want to pair this Telegram chat, it can be: xmpp"
                )

            target = parts[2]

            if len(parts) < 4:
                return _(
                    "Please specify the target address of room "
                    "you want to pair with this Telegram chat."
                )

            target_address = parts[3]

            if target == "xmpp":
                target_realm = Realm.XMPP
                try:
                    # Check that MUC jid is valid
                    JID(target)
                except InvalidJID:
                    return _(
                        "You have specified an incorrect room JID. "
                        "Please try again."
                    )
            else:
                return _(
                    "You have specified an incorrect target gateway. "
                    "Please try again."
                )

            target_chat = Chat(
                address=target_address,
                realm=target_realm
            )

            if self.__chat_service.is_paired(target_chat, source_chat):
                return _(
                    "This chat is already paired with specified room."
                )

            if not self.__chat_service.pending(source_chat, target_chat):
                return _(
                    "Specified room has been already placed on the queue, so it "
                    "can not be paired with this chat again\n"
                )

            return _(
                "Specified room has been successfully placed on the queue. "
                "Please invite this bot to target chat.\n"
                "If you have specified an incorrect room address, simply repeat "
                "the pair command with the corrected address."
            )
        elif subcommand == "confirm":
            if not (await self.__chat_service.pair(source_chat)):
                return _(
                    "There are no pairing requests for this chat."
                )
            else:
                return _(
                    "This chat was successfully paired."
                )
        elif subcommand == "unpair":
            if not self.__chat_service.is_paired(source_chat):
                return _(
                    "This chat is not paired with any other chat."
                )

            await self.__chat_service.unpair(source_chat)
            return _(
                "Chat has been successfully unpaired. You can kick this bot."
            )

        return _("Invalid subcommand specified")

