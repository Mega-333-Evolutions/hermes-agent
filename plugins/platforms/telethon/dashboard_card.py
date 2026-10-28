"""Telethon's Channels-page card (Desktop + web dashboard)."""

import re

_BOT_TOKEN_RE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")
_API_ID_RE = re.compile(r"\d+")
_USER_ID_RE = re.compile(r"\d+")

_ENV_VALUE_RULES = {
    "TELETHON_API_ID": (
        _API_ID_RE.fullmatch,
        "Telethon API ID must be a numeric ID from my.telegram.org.",
    ),
    "TELETHON_API_HASH": (
        lambda v: bool(v.strip()) and len(v.strip()) >= 32,
        "Telethon API hash must be the hash string from my.telegram.org.",
    ),
    "TELETHON_BOT_TOKEN": (
        _BOT_TOKEN_RE.fullmatch,
        "Telethon bot token must be the complete token from @BotFather, such as 123456789:ABC… "
        "(use a DIFFERENT bot than the python-telegram-bot one).",
    ),
    "TELETHON_ALLOWED_USERS": (
        lambda v: all(_USER_ID_RE.fullmatch(u.strip()) for u in v.split(",") if u.strip()),
        "Telethon allowed users must be comma-separated numeric user IDs.",
    ),
}

DASHBOARD_CARD = dict(
    dashboard_description="Run Hermes from Telegram via Telethon. Requires a separate bot token from the PTB adapter.",
    docs_url="https://core.telegram.org/bots/features#botfather",
    dashboard_env_vars=("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_BOT_TOKEN", "TELETHON_ALLOWED_USERS"),
    env_value_rules=_ENV_VALUE_RULES,
)
