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
from collections import OrderedDict
from configparser import ConfigParser, NoSectionError

QUEUEING_MESSAGE = """
Specified room has been successfully placed on the queue.
Please invite this {} bot to your XMPP room, and as the reason for the invitation specify the secret key that is specified in bot's config or ask the owner of this bridge instance for it.

If you have specified an incorrect room address, simply repeat
the pair command (/jabagram) with the corrected address.
"""

INVALID_JID_MESSAGE = """
You have specified an incorrect room JID. Please try again.
"""

UNBRIDGE_TELEGRAM_MESSAGE = """
This chat was automatically unbridged due to a bot kick in XMPP.
If you want to bridge it again, invite this bot to this chat again and use the /jabagram command.
"""

MISSING_MUC_JID_MESSAGE = """
Please specify the MUC address of room you want to pair with this Telegram chat.
"""

UNBRIDGE_XMPP_MESSAGE = """
This chat was automatically unbridged due to a bot kick in Telegram.
"""


class Messages():
    def __init__(self, parser: ConfigParser) -> None:
        self.__parser: ConfigParser = parser
        self.__logger = logging.getLogger(__class__.__name__)
        self.__messages: dict = OrderedDict([
            ("messages.missing_muc_jid", MISSING_MUC_JID_MESSAGE),
            ("messages.queueing_message", QUEUEING_MESSAGE),
            ("messages.invalid_jid", INVALID_JID_MESSAGE),
            ("messages.unbridge_telegram", UNBRIDGE_TELEGRAM_MESSAGE),
            ("messages.unbridge_xmpp", UNBRIDGE_XMPP_MESSAGE)
        ])

    def load(self):
        for section in self.__messages.keys():
            lines = []

            try:
                for key, value in self.__parser.items(section):
                    if not isinstance(value, str):
                        self.__logger.warning(
                            "Section %s can have only string values: %s", section, key
                        )
                        continue

                    lines.append(value)
            except NoSectionError:
                self.__logger.error("Section %s not found in config file!", section)
                continue

            multiline = "\n".join(lines)

            if multiline:
                self.__messages[section] = multiline

    @property
    def missing_muc_jid(self) -> str:
        return self.__messages["messages.missing_muc_jid"]

    @property
    def queueing_message(self) -> str:
        return self.__messages["messages.queueing_message"]

    @property
    def invalid_jid(self) -> str:
        return self.__messages["messages.invalid_jid"]

    @property
    def unbridge_telegram(self) -> str:
        return self.__messages["messages.unbridge_telegram"]

    @property
    def unbridge_xmpp(self) -> str:
        return self.__messages["messages.unbridge_xmpp"]
