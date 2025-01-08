# jabagram

A lightweight and fast, full-featured bridge between XMPP and Telegram. Was
originally written for personal use, so may not be very flexible in
configuration.

<p align="center" width="100%">
<img width="35%" src="https://github.com/ventureoo/jabagram/assets/92667539/91646d1d-bee8-40e0-ad9a-071d2d4431b9"> 
<img width="35%" src="https://github.com/ventureoo/jabagram/assets/92667539/1d74e64f-541d-4aa2-8913-2f43fcf06182"> 
</p>

## Features

This bridge has the following features and supports all the basic capabilities:

- [x] Lightweight and asynchronous, with support for adding new chats in runtime, without config editing
- [x] Unlike other bridges, it doesn't require hosting your own XMPP server
- [x] Forwarding between multiple pairs of chats at once instance (see below for usage)
- [x] Forwarding of plain text messages
- [x] Forwarding of attachments (videos, images, audio, etc)
- [x] Stickers (from Telegram -> XMPP)
- [x] Native replies to messages in Telegram
- [x] Round-trip message edit changes
- [x] Forwards events between bridged chats, such as mebmers join/exit, etc.


## Installation

### With pip

Jabagram stable releases can now be installed via pip. This is the recommended
way if you don't want to use Docker or unable to install jabagram dependencies
to your system paths.

```
pip install --user jabagram
```

You can also install in a virtual environment if you don't want to clutter your
system with all the jabagram dependencies.

### Manual

When manually installing to make the bridge work, you need to use Python 3.10+,
and install all the dependencies specified in the ``requirements.txt``(slixmpp,
aiohttp) file:

```
git clone https://github.com/ventureoo/jabagram
cd jabagram
pip install -r requirements.txt
```

## Deploy

Before you start the bridge, you need to do some basic configuration. An
example configuration is given in the form of the file ``config.example.ini``.
Rename it to ``config.ini`` and specify the following data:

1) Create a bot in Telegram via @botfather on behalf of which messages will be
forwarded and specify its token in the ``token`` field of the ``[telegram]``
section.
2) Create an XMPP account on behalf of which the bridge will forward messages
in MUC rooms, specify its JID (login) and password in the ``login`` and
``password`` fields in the ``[xmpp]`` section respectively.
3) Come up with a secret key that will be used when linking new Telegram and
XMPP chats. This key must be passed as a reason to invite a bot in XMPP to the
MUC room, otherwise the bot will not accept the invitation. This is a security
measure if you don't want your bridge instance to be used to bind other chats.
Otherwise, leave this field blank.
4) If you have done everything correctly, the bridge should start without
errors or exceptions and will create a database ``jabagram.db`` inside data
folder, which will store information about the "bound" chats in Telegram and XMPP.
See Usage further below.

An example of a config file that is given as ``config.example.ini``:

```
# Telegram bot token, get it from @botfather
[telegram]
token=XXXXXXXXXX:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

# Login and password from the bot account in XMPP
[xmpp]
login=your_xmpp_user@example.org
password=YoUr_SEcReT_PaSs

# The secret key specified in the user's invitation to the MUC for linking two
# chats. PLEASE change it from the default key if you don't want your bridge
# instance to be used by whoever you want. Otherwise, leave it blank.
[general]
key=SUxvdmVYTVBQ

# Further on is the service messages that are sent by a bot
[messages.missing_muc_jid]
line = Please specify the MUC address of room you want to pair with this Telegram chat.

[messages.invalid_jid]
line = You have specified an incorrect room JID. Please try again.

[messages.queueing_message]
line.1 = Specified room has been successfully placed on the queue.
line.2 = Please invite this {} bot to your XMPP room, and as the reason for the invitation specify the secret key that is specified in bot's config or ask the owner of this bridge instance for it.
line.3 = If you have specified an incorrect room address, simply repeat
line.4 = the pair command (/jabagram) with the corrected address.

[messages.unbridge_telegram]
line.1 = This chat was automatically unbridged due to a bot kick in XMPP.
line.2 = If you want to bridge it again, invite this bot to this chat again and use the /jabagram command.

[messages.unbridge_xmpp]
line.1 = This chat was automatically unbridged due to a bot kick in Telegram.
```

### Via Docker

You can also use Docker to deploy the bridge. Here you don't need to
pre-install dependencies on the host system, just build a container image:

```
git clone https://github.com/ventureoo/
cd jabagram
docker build -t jabagram .
```

The following command is used to start it:

```
docker run --restart always -d --name jabagram -v "$(pwd)/data:/app/data:rw" jabagram
```

Note about the ``-v`` key. It specifies that the database file be stored
outside the container environment, i.e. on your host system. The ``--restart``
option is needed if you want the bridge to automatically restart on critical
errors or when your system is powered on.

## Usage

Once you have successfully started your own bridge instance or found an
existing one, you can perform chat binding using the following algorithm:

1. Invite your bot to the Telegram chat you want to bridge.
2. Run the ``/jabagram`` command with the MUC address of the room you want to
   bridge.
   1. If the address was entered incorrectly, simply repeat the command with
      the corrected address.
3. The bot will queue your chats and then wait for you to invite the bot into
   the XMPP room.
4. Invite the XMPP bot into the room and as a reason, specify the secret key
   that you previously specified in the config or received from the admin of an
   existing instance. For example, in the Gajim client, this is done with the
   command: ``/invite <XMPP_JID> <SECRET_KEY>``
5. Once the bot successfully joins an XMPP room, messages should be sent
   between your room and the chat room in Telegram.

If you want to "unbridge" chats, just kick the bot from your Telegram chat or
XMPP room. It will automatically remove the entry from the database. Note that
losing the ``./data/jabagram.db`` file will unbridge all chats and you will need to
re-bridge them.

To re-bridge chats follow the steps above.

### Limitations

This bridge has some limitation that will probably never be properly fixed.

#### OMEMO encryption

This bridge cannot forward messages that have been encrypted using OMEMO or
OpenPGP. This will never be implemented because:

1) slixmpp does not support encryption via OMEMO 
2) Forwarding encrypted messages is probably a bad idea in the first place, you
are actually passing the body of your message unencrypted to the Telegram API,
it will be visible to all Telegram chat members to see it. Which calls into
question the whole point of encrypting messages.

#### Deleting messages

Bridge cannot delete a forwarded message in XMPP if it has been deleted in
Telegram. The Telegram API doesn't allow the bot to receive any updates about
deleted messages, so this simply can't be implemented. In the case of XMPP,
message deletion is a feature that can only be implemented on the client side,
not at the protocol or XEP level. Use editing of message with a stub as a
workaround.

#### Animated stickers from Telegram

Telegram has a proprietary format for animated stickers that probably can't be
properly previewed by XMPP clients. The bridge will not forward them to XMPP
because they cannot be properly rendered in any XMPP client.

#### Forwarding private messages

Sorry, but at the moment I only want to support forwarding messages from group
chats or MUCs. But this may be implemented in the future.

#### Mobile XMPP clients don't show nicknames set by the bot in MUC

Some mobile clients like Conversations and Blabber don't show nicknames in MUC
for users you have in your contacts [1]. Please, after inviting a bot to XMPP,
make sure you remove it from your contacts, otherwise it will not accept
Telegram sender nicknames for you.

[1] - https://github.com/iNPUTmice/Conversations/commit/ef1429c9a6983c101da41a277bd9353374dc89e7

## License

Licensed under GPL 3.0. If you'd like to help with improvements or fixes to this shitty code, I'd be happy to consider your patches ;D

## Feedback

If you have any issues with how the bridge works/is configured, please use
"Discussions" tab on the GitHub.
