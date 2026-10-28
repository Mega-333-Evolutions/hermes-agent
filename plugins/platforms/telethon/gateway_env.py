"""Telethon env -> gateway config bridge (``PlatformEntry.env_overrides``)."""

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_env import EnvCred, csv_list, getenv, home_step, reply_mode_step


def _fallback_ips(config: GatewayConfig) -> None:
    # Telethon uses mtproto (long-lived socket), not the Bot API HTTP endpoints,
    # so no HTTP fallback-IP transport is needed.
    pass


def _proxy_step(config: GatewayConfig) -> None:
    if proxy := getenv("TELETHON_PROXY"):
        config.platforms.setdefault(Platform("telethon"), PlatformConfig()).extra["proxy"] = proxy


ENV_ENABLE_CREDENTIALS = ("TELETHON_BOT_TOKEN",)
ENV_OVERRIDES = (
    EnvCred(Platform("telethon"), ("TELETHON_BOT_TOKEN",), token="TELETHON_BOT_TOKEN"),
    reply_mode_step(Platform("telethon"), "TELETHON_REPLY_TO_MODE"),
    _fallback_ips,
    home_step(Platform("telethon"), "TELETHON_HOME_CHANNEL"),
    _proxy_step,
)

# Authorization env names (mirrors the PTB plugin's shape, different env vars).
AUTHZ = {
    "allowed_users_env": "TELETHON_ALLOWED_USERS",
    "allow_all_env": "TELETHON_ALLOW_ALL_USERS",
    "allow_bots_env": "TELETHON_ALLOW_BOTS",
    "group_allowed_chats_env": "TELETHON_GROUP_ALLOWED_CHATS",
    "allowlist_suppresses_pairing": True,
}
