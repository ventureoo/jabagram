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
import mimetypes

from jabagram.cache import Cache
from jabagram.database.topics import TopicNameCache
from jabagram.dispatcher import MessageDispatcher
from jabagram.messages import Messages
from jabagram.service import ChatService
from jabagram.model import (
    Attachment,
    ChatHandlerFactory,
    Message,
    Sticker,
    TelegramAttachment,
    UnbridgeEvent,
)
from jabagram.telegram.api import TelegramApi, TelegramApiError
from jabagram.telegram.handler import TelegramChatHandler
from slixmpp.jid import InvalidJID, JID

class TelegramClient(ChatHandlerFactory):
    def __init__(
        self,
        token: str,
        jid: str,
        service: ChatService,
        dispatcher: MessageDispatcher,
        topic_name_cache: TopicNameCache,
        messages: Messages
    ) -> None:
        self.__api: TelegramApi = TelegramApi(token)
        self.__token: str = token
        self.__jid: str = jid
        self.__logger = logging.getLogger(__class__.__name__)
        self.__disptacher: MessageDispatcher = dispatcher
        self.__service: ChatService = service
        self.__service.register_factory(self)
        self.__messages = messages
        self.__handlers: dict[int, TelegramChatHandler] = {}
        self.__topic_name_cache = topic_name_cache

    async def create_handler(
        self,
        address: str,
        muc: str,
        cache: Cache,
    ) -> None:
        handler = TelegramChatHandler(
            address=address,
            api=self.__api,
            cache=cache,
            messages=self.__messages
        )
        self.__handlers[int(address)] = handler
        self.__disptacher.add_handler(muc, handler)

    async def start(self):
        params = {
            "allowed_updates": ['message', 'edited_message', 'my_chat_member']
        }
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
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            },
                        } as message
                    } if self.__disptacher.is_bound(str(chat)):
                        await self.__process_message(message)
                    case {
                        "message": {
                            "text": text,
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            },
                        }
                    } if not self.__disptacher.is_bound(str(chat)) \
                            and text.startswith("/jabagram"):
                        await self.__bridge_command(str(chat), text)
                    case {
                        "edited_message": {
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            },
                        } as message
                    } if self.__disptacher.is_bound(str(chat)):
                        await self.__process_message(message, edit=True)
                    case {
                        "my_chat_member": {
                            "chat": {
                                "type": "group" | "supergroup",
                                "id": chat
                            }
                        } as member
                    } if self.__disptacher.is_bound(str(chat)):
                        await self.__process_kick_event(member)

            params["offset"] = updates[len(updates) - 1]['update_id'] + 1

    async def __bridge_command(self, chat_id: str, cmd: str) -> None:
        try:
            try:
                muc_address = cmd.split(" ")[1]

                # Check that MUC jid is valid
                JID(muc_address)

                self.__service.pending(muc_address, chat_id)

                await self.__api.sendMessage(
                    chat_id=chat_id,
                    text=self.__messages.queueing_message.format(self.__jid)
                )
            except IndexError:
                await self.__api.sendMessage(
                    chat_id=chat_id,
                    text=self.__messages.missing_muc_jid
                )
            except InvalidJID:
                await self.__api.sendMessage(
                    "sendMessage", chat_id=chat_id,
                    text=self.__messages.invalid_jid
                )
        except TelegramApiError as error:
            self.__logger.error(
                "Error processing the bridge command: %s", error
            )

    def __extract_attachment(
            self,
            sender: str,
            message: dict
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

    def __get_reply(self, message: dict) -> str | None:
        reply: dict | None = message.get("reply_to_message")
        if not reply:
            return None

        sender: str = self.__get_full_name(reply['from'])
        attachment = self.__extract_attachment(sender, reply)
        reply_body = reply.get("text") or reply.get("caption")

        if not reply_body and attachment:
            reply_body = attachment.fname

        return reply_body

    async def __process_message(self, raw_message: dict, edit=False) -> None:
        chat_id = str(raw_message['chat']['id'])
        message_id = str(raw_message['message_id'])
        sender: str = self.__get_full_name(raw_message['from'])
        text: str | None = raw_message.get(
            "text") or raw_message.get("caption")
        reply: str | None = self.__get_reply(raw_message)
        forward: dict | None = raw_message.get('forward_origin')
        attachment: TelegramAttachment | None = self.__extract_attachment(
            sender, raw_message
        )
        topic_name: str | None = self.__extract_topic_name(raw_message)

        if topic_name:
            sender += " [" + topic_name + "]"

        if attachment:
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
                        event_id=message_id,
                        content=attachment.fname,
                        address=chat_id,
                        sender=sender,
                        file_id=attachment.file_unique_id,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback
                    )
                )
            else:
                await self.__disptacher.send(
                    Attachment(
                        event_id=message_id,
                        address=chat_id,
                        sender=sender,
                        content=attachment.fname,
                        # if we have text, reply should be nested
                        # in the message below
                        reply=None if text else reply,
                        mime=attachment.mime,
                        fsize=attachment.fsize,
                        url_callback=url_callback
                    )
                )

        if text:
            if forward:
                original_sender = "Unknown"

                match forward:
                    case {"chat": chat} | {"sender_chat": chat}:
                        original_sender = chat['title']
                    case {"sender_user": user}:
                        original_sender = self.__get_full_name(user)
                    case {"sender_user_name": name}:
                        original_sender = name

                text = f"**Message forwarded from {original_sender}**\n\n{text}"

            await self.__disptacher.send(
                Message(
                    event_id=message_id,
                    address=chat_id,
                    content=text,
                    sender=sender,
                    reply=reply,
                    edit=edit
                )
            )

    async def __process_kick_event(self, chat_member: dict) -> None:
        new_state = chat_member.get("new_chat_member")
        if new_state and new_state.get("status") == "left":
            await self.__disptacher.send(
                UnbridgeEvent(address=str(chat_member['chat']['id']))
            )

    def __get_full_name(self, user: dict) -> str:
        name: str = user['first_name']
        if user.get("last_name"):
            name = name + " " + user['last_name']

        return name

    def __extract_topic_name(
        self,
        message: dict
    ) -> str | None:
        chat_id = message['chat']['id']
        topic_id = message.get("message_thread_id")

        if not topic_id:
            return None

        handler = self.__handlers.get(message['chat']['id'])
        if handler:
            handler.add_topic_id(message['message_id'], topic_id)

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


