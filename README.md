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

## Prerequisites

You need two separate Telegram bot tokens (one token = one connection; PTB and Telethon cannot share):

| Bot | Purpose | Env Var |
|-----|---------|---------|
| PTB bot (from @BotFather) | Primary Telegram transport | `TELEGRAM_BOT_TOKEN` |
| Telethon bot (from @BotFather) | Telethon mtproto transport | `TELETHON_BOT_TOKEN` |

Plus Telethon API credentials from [my.telegram.org](https://my.telegram.org):
- `TELETHON_API_ID` (e.g. `7155513`)
- `TELETHON_API_HASH`

## HF Spaces Setup

1. **Create a new Space** → select **Docker** as the SDK.
2. **Connect your GitHub** repo (`taslimx19/hermes-agent-telethon`) or upload the Dockerfile directly.
3. **In the Space settings, add these Secrets** (not Variables, so they're hidden):
   - `TELEGRAM_BOT_TOKEN` — your PTB bot token
   - `TELETHON_BOT_TOKEN` — your Telethon bot token
   - `TELETHON_API_ID` — from my.telegram.org
   - `TELETHON_API_HASH` — from my.telegram.org
   - `TELETHON_ALLOWED_USERS` — your Telegram user ID (e.g. `7361622601`)
   - `TELETHON_HOME_CHANNEL` — your home channel ID (e.g. `7361622601`)
   - `TELETHON_PROXY` — optional SOCKS5 `host:port:user:pass` if mtproto is blocked
4. **Enable persistent storage** in the Space settings (mounted at `/data`) so `state.db` survives restarts. Without it, sessions are lost on every reboot.
5. **Deploy** — wait for the build to finish.

## What gets deployed

- `nousresearch/hermes-agent:latest` base image
- Telethon 1.45.0 + python-socks installed
- `plugins/platforms/telethon/` — the Telethon adapter plugin
- `hermes_cli/commands.py` — `/delete <session_id>` command registered in the Telegram menu
- `hermes_cli/gateway_setup_wizard.py` — PTB / Telethon / Both transport choice

## Files

| File | Purpose |
|------|---------|
| `Dockerfile.hf` | HF Spaces Dockerfile (rename to `Dockerfile` when pushing to HF) |
| `docker-compose.yml` | Local test equivalent |
| `plugins/platforms/telethon/` | Telethon adapter plugin |

## Local test

```bash
docker build -f Dockerfile.hf -t hermes-telethon .
docker run -d \
  --name hermes-telethon \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELETHON_BOT_TOKEN=... \
  -e TELETHON_API_ID=... \
  -e TELETHON_API_HASH=... \
  -e TELETHON_ALLOWED_USERS=7361622601 \
  -e TELETHON_HOME_CHANNEL=7361622601 \
  -v $(pwd)/data:/data \
  hermes-telethon
```

## Notes

- PTB and Telethon run in parallel on separate tokens — no conflict.
- Telethon needs API ID + API Hash from my.telegram.org (unlike PTB which only needs a bot token).
- If mtproto is blocked from HF's network, set `TELETHON_PROXY` to a SOCKS5 proxy.
- `/delete <session_id>` works on both bots once deployed.
