---
title: Hermes Agent + Telethon (Telegram)
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
license: mit
short_description: Hermes agent with Telethon Telegram plugin
---

# Hermes + Telethon on Hugging Face Spaces

This Space runs the [Hermes Agent](https://github.com/NousResearch/hermes-agent) gateway with the **Telethon Telegram platform plugin** as a separate parallel transport alongside PTB.

## Required Secrets (HF Space Settings → Secrets)

Add these as **Secrets** (not Variables):

| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Your PTB bot token from @BotFather |
| `TELETHON_BOT_TOKEN` | Your Telethon bot token from @BotFather (different bot) |
| `TELETHON_API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `TELETHON_API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `TELETHON_ALLOWED_USERS` | Your Telegram user ID (e.g. `7361622601`) |
| `TELETHON_HOME_CHANNEL` | Your home channel ID (e.g. `7361622601`) |
| `TELETHON_PROXY` | Optional SOCKS5 `host:port:user:pass` if mtproto is blocked |

## Persistent Storage (Required)

Enable **Persistent storage** in your Space settings, mounted at `/data`. Without it, `state.db` and all sessions are lost on every restart.

## Deploy

1. Add the Secrets above
2. Enable persistent storage at `/data`
3. Deploy — wait for the Docker build to finish
4. PTB and Telethon bots will run in parallel on separate tokens
