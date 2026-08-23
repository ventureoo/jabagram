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
import aiohttp
import logging
import mimetypes

from gettext import gettext as _
from typing import Any, override
from jabagram.command import UserCommandHandler
from jabagram.database.messages import MessageStorage
from jabagram.database.topics import TopicNameCache
from jabagram.dispatcher import MessageDispatcher
from jabagram.service import ChatService
from jabagram.model import (
    Attachment,
    Realm,
    Chat,
    Reply,
    ChatHandler,
    ChatHandlerFactory,
    Sender,
    Message,
    Sticker,
    UnbridgeEvent,
)
from jabagram.telegram.api import TelegramApi, TelegramApiError
from jabagram.telegram.handler import TelegramChatHandler
from jabagram.telegram.model import TelegramAttachment

class TelegramClient(ChatHandlerFactory):
    def __init__(
        self,
        token: str,
        chat_service: ChatService,
        command_handler: UserCommandHandler,
        dispatcher: MessageDispatcher,
        topic_name_cache: TopicNameCache,
        message_storage: MessageStorage,
    ) -> None:
        self.__api = TelegramApi(token)
        self.__token = token
        self.__disptacher = dispatcher
        self.__chat_service = chat_service
        self.__topic_name_cache = topic_name_cache
        self.__message_storage = message_storage
        self.__command_handler = command_handler
        self.__start_event = asyncio.Event()
        self.__logger = logging.getLogger(__class__.__name__)

    @override
    def realm(self):
        return Realm.TELEGRAM

    @override
    async def create_handler(
        self,
        address: str,
    ) -> ChatHandler | None:
        self.__logger.info("Creating new handler for %s", address)
        handler = TelegramChatHandler(
            chat=Chat(address=address, realm=Realm.TELEGRAM),
            api=self.__api,
            message_storage=self.__message_storage,
        )
        return handler

    async def wait_for_start(self):
        await self.__start_event.wait()

    async def start(self):
        params = {
            "allowed_updates": ['message', 'edited_message', 'my_chat_member']
        }
        self.__logger.info("Starting to getting updates from Telegram...")
        self.__chat_service.register_factory(realm=Realm.TELEGRAM, factory=self)

        self.__start_event.set()
        while True:
            updates = None
            try:
                updates = await self.__api.getUpdates(**params)
            except TelegramApiError as error:
                self.__logger.error("Error receiving updates: %s", error)

            if not updates:
                continue

            for update in updates:
                match update:
                    case {
                        "message": {
                            "text": command,
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat_id
                            },
                            "from": {
                                "id": user_id
                            }
                        }
                    } if command.startswith("/jabagram"):
                        await self.__bridge_chat_command(
                            command=command,
                            chat_id=str(chat_id),
                            user_id=str(user_id),
                        )
                    case {
                        "message": {
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            },
                        } as message
                    } if self.__disptacher.is_paired(str(chat)):
                        await self.__process_message(message)
                    case {
                        "message": {
                            "chat": {
                                "type": "private",
                                "id": chat_id
                            },
                            "text": command,
                            "from": {
                                "id": user_id
                            }
                        } as message
                    } if command.startswith("/jabagram"):
                        await self.__bridge_direct_command(
                            command=command,
                            chat_id=str(chat_id),
                            user_id=str(user_id),
                        )
                    case {
                        "edited_message": {
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            },
                        } as message
                    } if self.__disptacher.is_paired(str(chat)):
                        await self.__process_message(message, edit=True)
                    case {
                        "my_chat_member": {
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            }
                        } as member
                    } if self.__disptacher.is_paired(str(chat)):
                        await self.__process_kick_event(member)

            params["offset"] = updates[len(updates) - 1]['update_id'] + 1

    async def __bridge_direct_command(
        self,
        command: str,
        chat_id: str,
        user_id: str,
    ):
        try:
            response = self.__command_handler.handle_direct_user_command(
                realm=Realm.TELEGRAM,
                command=command,
                user=user_id,
            )
            await self.__api.sendMessage(
                chat_id=chat_id,
                text=response
            )
        except TelegramApiError as error:
            self.__logger.error(
                "Error processing the bridge command: %s", error
            )

    async def __bridge_chat_command(
        self,
        command: str,
        chat_id: str,
        user_id: str,
    ) -> None:
        try:
            response = (await self.__command_handler.handle_chat_user_command(
                source_chat=Chat(address=chat_id, realm=Realm.TELEGRAM),
                command=command,
                user=user_id,
            ))
            await self.__api.sendMessage(
                chat_id=chat_id,
                text=response
            )
        except TelegramApiError as error:
            self.__logger.error(
                "Error processing the bridge command: %s", error
            )

    def __extract_attachment(
            self,
            sender: str,
            message: dict[Any, Any]
    ) -> TelegramAttachment | None:
        match message:
            # We do not send animated stickers because they are in TGS format,
            # which cannot be properly rendered in XMPP clients.
            case {"sticker": sticker} if not sticker.get("is_animated"):
                extension = "webm" if sticker.get("is_video") else "webp"
                emoji = sticker['emoji'] if sticker.get("emoji") else ""
                return TelegramAttachment(
                    is_cacheable=True,
                    fname=f"Sticker {emoji} from {sender}.{extension}",
                    file_id=sticker['file_id'],
                    file_unique_id=sticker['file_unique_id'],
                    fsize=sticker.get("file_size"),
                    mime="image/webm" if sticker.get(
                        "is_video") else "video/webp"
                )
            case {"photo": [*_, photo]}:
                return TelegramAttachment(
                    fname=f"Photo from {sender}.jpg",
                    file_id=photo['file_id'],
                    file_unique_id=photo['file_unique_id'],
                    fsize=photo.get("file_size"),
                    mime="image/jpeg"
                )
            case {"video": video} | {"video_note": video} | \
                    {"animation": video}:
                mime = video.get("mime_type")
                extension = mimetypes.guess_extension(mime or "video/mp4")
                return TelegramAttachment(
                    fname=video.get("file_name") or (
                        f"Video from {sender}.{extension}"
                    ),
                    file_id=video['file_id'],
                    file_unique_id=video['file_unique_id'],
                    fsize=video.get("file_size"),
                    mime=mime
                )
            case {"voice": voice}:
                return TelegramAttachment(
                    fname=f"Voice message from {sender}.ogg",
                    file_id=voice['file_id'],
                    file_unique_id=voice['file_unique_id'],
                    fsize=voice.get("file_size"),
                    mime="audio/ogg"
                )
            case {"audio": audio}:
                mime = audio.get("mime_type")
                extension = mimetypes.guess_extension(mime or "audio/mpeg")
                return TelegramAttachment(
                    fname=audio.get("file_name") or (
                        f"Audio from {sender}.{extension}"
                    ),
                    file_id=audio['file_id'],
                    file_unique_id=audio['file_unique_id'],
                    fsize=audio.get("file_size"),
                    mime=mime
                )
            case {"document": document}:
                mime = document.get("mime_type")
                extension = "." + \
                    (mimetypes.guess_extension(mime) or "") if mime else ""
                return TelegramAttachment(
                    fname=document.get("file_name") or (
                        f"Document from {sender}{extension}"
                    ),
                    file_id=document['file_id'],
                    file_unique_id=document['file_unique_id'],
                    fsize=document.get("file_size"),
                    mime=mime
                )

        return None

    def __get_reply(
        self,
        message: dict[Any, Any]
    ) -> tuple[str | None, str] | None:
        reply: dict[Any, Any] | None = message.get("reply_to_message")
        if not reply:
            return None

        sender, _ = self.__get_user(reply['from'])
        attachment = self.__extract_attachment(sender, reply)
        reply_body = reply.get("text") or reply.get("caption")
        reply_id = str(reply.get("message_id"))

        if not reply_body and attachment:
            reply_body = attachment.fname

        return reply_body, reply_id

    async def __process_message(
        self,
        raw_message: dict[Any, Any],
        edit=False
    ) -> None:
        chat_id = str(raw_message['chat']['id'])
        message_id = str(raw_message['message_id'])
        sender, user_id = self.__get_user(raw_message['from'])
        text: str | None = raw_message.get("text")
        reply = self.__get_reply(raw_message)

        reply_body = reply_id = None
        if reply:
            (reply_body, reply_id) = reply

        forward: dict[Any, Any] | None = raw_message.get('forward_origin')
        topic_id = raw_message.get("message_thread_id")
        topic_name = self.__extract_topic_name(raw_message)

        sender_id = str(user_id)
        if topic_name:
            sender += " [" + topic_name + "]"
            sender_id += f"_{topic_id}"

        sender += " (Telegram)"

        async def avatar_callback():
            try:
                profile = await self.__api.getUserProfilePhotos(
                    user_id=user_id,
                    offset=0,
                    limit=1
                )

                avatars = profile['photos']

                if not avatars:
                    return None

                # Pick always the smallest first avatar only
                avatar = avatars[0][0]
                file = await self.__api.getFile(file_id=avatar['file_id'])
                file_path = file['file_path']
                url = (
                    f"https://api.telegram.org/file/bot"
                    f"{self.__token}/{file_path}"
                )
                return url
            except TelegramApiError as error:
                self.__logger.error(
                    "Failed to get avatar of user: %s",
                    error
                )
            except aiohttp.ClientResponseError as error:
                self.__logger.error(
                    "Failed to download user avatar: %s",
                    error
                )

        if text:
            if forward:
                original_sender = "Unknown"

                match forward:
                    case {"chat": chat} | {"sender_chat": chat}:
                        original_sender = chat['title']
                    case {"sender_user": user}:
                        original_sender, _ = self.__get_user(user)
                    case {"sender_user_name": name}:
                        original_sender = name

                text = f"**Message forwarded from {original_sender}**\n\n{text}"

            await self.__disptacher.send(
                Message(
                    id=message_id,
                    chat=Chat(
                        realm=Realm.TELEGRAM,
                        address=chat_id,
                        topic_id=topic_id
                    ),
                    text=text,
                    sender=Sender(
                        name=sender,
                        id=sender_id,
                        avatar_callback=avatar_callback
                    ),
                    reply=Reply(
                        id=reply_id,
                        body=reply_body,
                    ) if reply else None,
                    edit=edit,
                )
            )
        else:
            attachment = self.__extract_attachment(sender, raw_message)

            if not attachment:
                return

            async def url_callback():
                try:
                    file = await self.__api.getFile(file_id=attachment.file_id)
                    file_path = file['file_path']
                    url = (
                        f"https://api.telegram.org/file/bot"
                        f"{self.__token}/{file_path}"
                    )
                    return url
                except TelegramApiError as error:
                    self.__logger.error(
                        "Failed to get url of attachment: %s", error
                    )

            # Right now we can cache only stickers
            if attachment.is_cacheable:
                await self.__disptacher.send(
                    Sticker(
                        id=message_id,
                        fname=attachment.fname,
                        text=raw_message.get("caption") or "",
                        chat=Chat(
                            realm=Realm.TELEGRAM,
                            address=chat_id,
                            topic_id=topic_id
                        ),
                        sender=Sender(
                            name=sender,
                            id=sender_id,
                            avatar_callback=avatar_callback
                        ),
                        file_id=attachment.file_unique_id,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback,
                    )
                )
            else:
                await self.__disptacher.send(
                    Attachment(
                        id=message_id,
                        fname=attachment.fname,
                        text=raw_message.get("caption") or "",
                        chat=Chat(
                            realm=Realm.TELEGRAM,
                            address=chat_id,
                            topic_id=topic_id
                        ),
                        sender=Sender(
                            name=sender,
                            id=sender_id,
                            avatar_callback=avatar_callback
                        ),
                        # if we have text, reply should be nested
                        # in the message below
                        reply=Reply(
                            id=reply_id,
                            body=reply_body,
                        ) if reply and not text else None,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback,
                    )
                )


    async def __process_kick_event(
        self,
        chat_member: dict[Any, Any]
    ) -> None:
        new_state = chat_member.get("new_chat_member")
        if new_state and new_state.get("status") == "left":
            await self.__disptacher.send(
                UnbridgeEvent(
                    chat=Chat(
                        realm=Realm.TELEGRAM,
                        address=str(chat_member['chat']['id'])
                    )
                )
            )

    def __get_user(
        self,
        user: dict[Any, Any]
    ) -> tuple[str, str]:
        user_name: str = user['first_name']
        if user.get("last_name"):
            user_name = user_name + " " + user['last_name']

        user_id = user['id']

        return user_name, user_id

    def __extract_topic_name(
        self,
        message: dict[Any, Any]
    ) -> str | None:
        chat_id = message['chat']['id']
        topic_id = message.get("message_thread_id")

        if topic_id is None:
            return None

        topic_name = self.__topic_name_cache.get(chat_id, topic_id)

        if topic_name:
            return topic_name

        reply_message = message.get('reply_to_message')
        if reply_message:
            topic = reply_message.get("forum_topic_created")
            if topic:
                self.__topic_name_cache.add(
                    chat_id, topic_id, topic.get("name")
                )
                return topic.get("name")
            else:
                if reply_message.get("is_topic_message"):
                    return "Unknown"

        return None

    def get_api(self) -> TelegramApi:
        return self.__api


