"""Telegram platform adapter (Telethon): inbound messages/media/commands, outbound replies.

Parallel transport to the python-telegram-bot (PTB) adapter at
plugins/platforms/telegram/. This adapter is intentionally independent: it never
imports or touches PTB files, and it must be configured with a SEPARATE bot token
(a different @BotFather bot) because one Telegram token cannot hold two
simultaneous connections (the second gets 409 Conflict).
"""

import asyncio
import logging
from typing import Any, Optional

import aiohttp
import socks

from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

try:
    from telethon import TelegramClient, events
    from telethon.tl.custom.button import Button
    from telethon.tl.types import (
        User, Chat, Channel, InputBotInlineResult, InputBotInlineMessageText,
        UpdateBotGuestChatQuery,
    )
    from telethon.tl.functions.messages import SetBotGuestChatResultRequest
    _TELETHON_AVAILABLE = True
except Exception:  # telethon not installed in this environment
    TelegramClient = None  # type: ignore[assignment]
    events = None  # type: ignore[assignment]
    User = Chat = Channel = None  # type: ignore[assignment]
    _TELETHON_AVAILABLE = False

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096  # Telegram message length limit


class TelethonAdapter(BasePlatformAdapter):
    """Telegram adapter built on Telethon (TelegramClient + mtproto)."""

    def __init__(self, config):
        super().__init__(config, Platform("telethon"))
        extra = getattr(config, "extra", {}) or {}
        self._api_id = _get_scoped_secret("TELETHON_API_ID") or extra.get("api_id")
        self._api_hash = _get_scoped_secret("TELETHON_API_HASH") or extra.get("api_hash")
        self._bot_token = _get_scoped_secret("TELETHON_BOT_TOKEN") or extra.get("bot_token")
        self._session_name = str(extra.get("session_name", "hermes_telethon_bot"))
        self._proxy = self._parse_proxy(
            _get_scoped_secret("TELETHON_PROXY") or extra.get("proxy")
        )
        self._allowed_users: set[str] = set()
        self._allow_all = False
        # Populate allowlist from env so inline queries and other adapter-side
        # checks honor TELETHON_ALLOWED_USERS / TELETHON_ALLOW_ALL_USERS.
        allowed_users_raw = _get_scoped_secret("TELETHON_ALLOWED_USERS") or extra.get("allowed_users", "")
        if allowed_users_raw:
            self._allowed_users = {u.strip() for u in str(allowed_users_raw).split(",") if u.strip()}
        allow_all_raw = _get_scoped_secret("TELETHON_ALLOW_ALL_USERS") or extra.get("allow_all_users", "")
        self._allow_all = str(allow_all_raw).strip().lower() in {"1", "true", "yes", "on"}
        self._home_channel: Optional[str] = None
        self._client: Optional[Any] = None
        self._connected = False
        self._bot_id: Optional[str] = None
        self._bot_username: Optional[str] = None
        # Inline-keyboard picker state (mirrors PTB's _model_picker_state / _choice_picker_state /
        # _clarify_state / _slash_confirm_state / _approval_state). Keyed by chat_id / confirm_id /
        # clarify_id / approval_id so a callback tap can look up its session_key and resolver.
        self._model_picker_state: dict[str, dict] = {}
        self._choice_picker_state: dict[str, dict] = {}
        self._clarify_state: dict[str, str] = {}  # clarify_id -> session_key
        self._clarify_choices: dict[str, list] = {}  # clarify_id -> choices list
        self._slash_confirm_state: dict[str, str] = {}  # confirm_id -> session_key
        self._approval_state: dict[int, str] = {}  # approval_id -> session_key

    @staticmethod
    def _parse_proxy(proxy_str: Optional[str]) -> Optional[tuple]:
        """Parse ``host:port:user:pass`` into a Telethon ``(type, addr, port, username, password)`` tuple.

        Returns None when no proxy is configured.
        """
        if not proxy_str:
            return None
        parts = str(proxy_str).strip().split(":")
        if len(parts) != 4:
            logger.warning("[Telethon] Invalid TELETHON_PROXY format (expected host:port:user:pass), ignoring")
            return None
        host, port, user, pwd = parts
        try:
            port = int(port)
        except ValueError:
            logger.warning("[Telethon] Invalid TELETHON_PROXY port %r, ignoring", port)
            return None
        return (socks.SOCKS5, host, port, True, user, pwd)

    # ── requirements / config ──────────────────────────────────────────────

    def check_requirements(self) -> bool:
        return _TELETHON_AVAILABLE

    # ── connect / disconnect ───────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not _TELETHON_AVAILABLE:
            logger.error("[Telethon] telethon library not installed in the gateway venv")
            return False
        if not self._api_id or not self._api_hash or not self._bot_token:
            logger.error(
                "[Telethon] TELETHON_API_ID / TELETHON_API_HASH / TELETHON_BOT_TOKEN not configured")
            return False
        try:
            self._client = TelegramClient(
                self._session_name, int(self._api_id), self._api_hash,
                proxy=self._proxy)
            await self._client.start(bot_token=self._bot_token)

            me = await self._client.get_me()
            self._bot_id = str(me.id)
            self._bot_username = getattr(me, "username", None) or ""

            self._client.add_event_handler(self._on_new_message, events.NewMessage)

            await self._register_command_menu()

            self._client.add_event_handler(self._handle_inline_query, events.InlineQuery)
            self._client.add_event_handler(self._handle_callback_query, events.CallbackQuery)
            self._client.add_event_handler(
                self._handle_guest_chat_query, events.Raw(UpdateBotGuestChatQuery))

            self._connected = True
            logger.info("[Telethon] Connected as @%s (id=%s)", self._bot_username, self._bot_id)
            return True
        except Exception:
            logger.exception("[Telethon] connect failed")
            self._connected = False
            self._client = None
            return False

    async def _register_command_menu(self) -> None:
        """Push the slash-command menu via the Bot API setMyCommands endpoint.

        Telethon is mtproto-based and has no set_my_commands, so we call the
        Telegram Bot API directly over HTTP — same menu PTB registers.
        """
        try:
            from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands
        except Exception:
            logger.debug("[Telethon] commands_platforms unavailable; skipping command menu")
            return

        try:
            max_commands = telegram_menu_max_commands()
        except Exception:
            max_commands = 60

        try:
            menu_pairs, hidden = await asyncio.to_thread(
                telegram_menu_commands, max_commands=max_commands)
        except Exception:
            logger.exception("[Telethon] failed to collect command menu")
            return

        commands = [
            {"command": name, "description": str(desc or "")[:256]}
            for name, desc in menu_pairs
        ]
        if not commands:
            return

        url = f"https://api.telegram.org/bot{self._bot_token}/setMyCommands"
        payload = {"commands": commands, "scope": {"type": "default"}}

        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=2)) as session:
                async with session.post(url, json=payload, timeout=15) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        logger.info(
                            "[Telethon] setMyCommands OK (%d cmds, %d hidden)",
                            len(commands), hidden)
                    else:
                        logger.warning(
                            "[Telethon] setMyCommands FAILED (HTTP %s): %s",
                            resp.status, text[:200])
        except Exception:
            logger.exception("[Telethon] setMyCommands request failed")

    async def _handle_inline_query(self, event) -> None:
        """Answer ``@botname <query>`` with a searchable command/skill picker (Telethon inline query).

        Mirrors PTB's inline_picker: results computed per keystroke, 50 per page; tapping sends
        ``/cmd`` text as the user through the normal command path. Inline queries arrive from ANY chat,
        so unauthorized users get an empty list (the skill catalog is not leaked).
        """
        try:
            query = getattr(event, "query", "") or ""
            offset = getattr(event, "offset", "") or ""
            user_id = str(getattr(event, "user_id", "") or "").strip()
        except Exception:
            logger.debug("[Telethon] inline query: cannot read event fields", exc_info=True)
            return

        # Authorize on user identity alone (no chat context on inline queries).
        try:
            from plugins.platforms.telethon.inline_picker import (
                build_inline_results, collect_inline_catalog,
                CACHE_TIME_SECONDS as _CACHE,
            )
        except Exception:
            logger.debug("[Telethon] inline picker unavailable", exc_info=True)
            return

        authorized = bool(user_id) and user_id in self._allowed_users
        if not self._allow_all and not authorized:
            try:
                await event.answer(
                    [], cache_time=_CACHE, is_personal=True)
            except Exception:
                logger.debug("[Telethon] inline query: empty answer failed", exc_info=True)
            return

        try:
            results, next_offset = await asyncio.to_thread(
                build_inline_results, query, offset=offset)

            inline_results = [
                InputBotInlineResult(
                    id=r["id"],
                    type="article",
                    title=r["title"],
                    description=r["description"],
                    send_message=InputBotInlineMessageText(message=r["message_text"]),
                )
                for r in results
            ]
            await event.answer(
                inline_results, cache_time=_CACHE, is_personal=True,
                next_offset=next_offset or "")
            logger.debug("[Telethon] inline query answered (%d results)", len(inline_results))
        except Exception:
            logger.exception("[Telethon] inline query handler failed")

    async def _handle_guest_chat_query(self, event) -> None:
        """Handle guest chat queries (@botname mention in groups where bot isn't a member).

        Builds inline results using the same picker as inline queries, then posts
        the result directly into the chat via messages.setBotGuestChatResult.
        Requires bot_guestchat flag set on the bot via @BotFather.
        """
        try:
            query_id = event.query_id
            message = event.message
            query = getattr(message, "message", "") or ""
            sender = getattr(message, "from_id", None)
            user_id = str(getattr(sender, "user_id", "") or "") if sender else ""
        except Exception:
            logger.debug("[Telethon] guest chat query: cannot read event fields", exc_info=True)
            return

        # Authorize on user identity (same as inline queries)
        try:
            from plugins.platforms.telethon.inline_picker import (
                build_inline_results, collect_inline_catalog,
                CACHE_TIME_SECONDS as _CACHE,
            )
        except Exception:
            logger.debug("[Telethon] inline picker unavailable for guest chat", exc_info=True)
            return

        authorized = bool(user_id) and user_id in self._allowed_users
        if not self._allow_all and not authorized:
            # Answer with empty result for unauthorized users
            try:
                await self._client(SetBotGuestChatResultRequest(
                    query_id=query_id,
                    result=InputBotInlineResult(
                        id="0",
                        type="article",
                        title="Unauthorized",
                        description="You are not authorized to use this bot.",
                        send_message=InputBotInlineMessageText(
                            message="⚠ You are not authorized to use this bot.",
                        ),
                    ),
                ))
            except Exception:
                pass
            return

        try:
            results, next_offset = await asyncio.to_thread(
                build_inline_results, query, offset=""
            )
            logger.debug(
                "[Telethon] guest chat query='%s' results=%d", query, len(results),
            )

            if not results:
                # Build a helpful intro message for general queries (hi, who are you, help, etc.)
                catalog = collect_inline_catalog()
                examples = [f"/{c['name']}" for c in catalog[:8]]
                hint = "\n".join(examples) if examples else ""

                msg = (
                    f"🤖 Hello! I'm @{self._bot_username or 'hermes_bot'}, your Hermes AI assistant.\n\n"
                    f"I can help you with:\n{hint}\n\n"
                    f"💡 Start a DM with me first to set up your session and choose a model.\n"
                    f"Then come back here and mention me with a command."
                )
                await self._client(SetBotGuestChatResultRequest(
                    query_id=query_id,
                    result=InputBotInlineResult(
                        id="0",
                        type="article",
                        title="Hermes AI Assistant",
                        description="Chat with Hermes in any group",
                        send_message=InputBotInlineMessageText(
                            message=msg,
                        ),
                    ),
                ))
                return

            # Guest chat posts a single result directly into the chat
            r = results[0]
            inline_result = InputBotInlineResult(
                id=r["id"],
                type="article",
                title=r["title"],
                description=r["description"],
                send_message=InputBotInlineMessageText(message=r["message_text"]),
            )

            await self._client(SetBotGuestChatResultRequest(
                query_id=query_id,
                result=inline_result,
            ))
            logger.debug("[Telethon] guest chat query answered (%s)", r.get("title", ""))
        except Exception:
            logger.exception("[Telethon] guest chat query handler failed")

    async def disconnect(self) -> None:
        self._connected = False
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # nosec
                logger.debug("[Telethon] disconnect raised", exc_info=True)
            self._client = None

    # ── inline-keyboard pickers (mirror PTB's InlineKeyboardMarkup buttons) ─────────

    @staticmethod
    def _rows_of_two(buttons: list) -> list:
        """2-per-row layout keeps labels readable on mobile (a 4-button row truncates)."""
        return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]

    @staticmethod
    def _cb(text: str, data: str, style: Optional[str] = None) -> Any:
        """Build a Telethon inline button with optional style (primary/danger/success)."""
        return Button.inline(text, data.encode("utf-8"), style=style)

    async def send_model_picker(
        self, chat_id: str, providers: list, current_model: str, current_provider: str,
        session_key: str, on_model_selected, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an inline-keyboard model picker: provider -> model drill-down.

        Mirrors PTB: paginated provider list (10/page) with nav buttons,
        then paginated model list (8/page) with back/cancel buttons.
        """
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            # Providers: list of {"name": str, "slug": str, "models": [str, ...]}
            provider_buttons = [
                self._cb(
                    p.get("label") or p.get("name") or "provider",
                    f"mp:{p.get('slug') or p.get('name') or ''}",
                    style="success"
                    if p.get("slug") == current_provider or p.get("name") == current_provider
                    else None,
                )
                for p in providers
                if p.get("slug") or p.get("name")
            ]
            if not provider_buttons:
                return SendResult(success=False, error="No providers")
            keyboard = self._paged_provider_keyboard(provider_buttons, page=0, page_size=10)
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            msg = await self._client.send_message(
                entity,
                f"🤖 Select a provider for /model (current: {current_provider}):",
                buttons=keyboard,
            )
            self._model_picker_state[str(chat_id)] = {
                "msg_id": msg.id,
                "providers": providers,
                "provider_buttons": provider_buttons,
                "provider_page": 0,
                "provider_page_size": 10,
                "session_key": session_key,
                "on_model_selected": on_model_selected,
                "current_model": current_model,
                "current_provider": current_provider,
                "model_list": [],
                "model_page": 0,
                "selected_provider": "",
            }
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            logger.exception("[Telethon] send_model_picker failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    # ── model picker helpers (mirror PTB's paged keyboards) ─────────────────

    @staticmethod
    def _nav_row(page: int, total_pages: int, prefix: str) -> list:
        """``◀ Prev | n/N | Next ▶`` navigation row."""
        nav: list = []
        if page > 0:
            nav.append(TelethonAdapter._cb("◀ Prev", f"{prefix}:{page - 1}"))
        nav.append(TelethonAdapter._cb(f"{page + 1}/{total_pages}", "mx:noop"))
        if page < total_pages - 1:
            nav.append(TelethonAdapter._cb("Next ▶", f"{prefix}:{page + 1}"))
        return nav

    @staticmethod
    def _back_cancel_row() -> list:
        return [TelethonAdapter._cb("⬅ Back", "mb"), TelethonAdapter._cb("✕ Cancel", "mx")]

    def _provider_page(self, buttons: list, page: int, page_size: int) -> tuple:
        """Slice provider buttons into a page; return (page_buttons, total_pages, page)."""
        total_pages = max(1, (len(buttons) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start, end = page * page_size, min(page * page_size + page_size, len(buttons))
        return buttons[start:end], total_pages, page

    def _paged_provider_keyboard(self, buttons: list, page: int, page_size: int) -> list:
        """Build provider keyboard page: button rows + nav row + cancel row."""
        page_buttons, total_pages, page = self._provider_page(buttons, page, page_size)
        rows = self._rows_of_two(page_buttons)
        if total_pages > 1:
            rows.append(self._nav_row(page, total_pages, "mp"))
        rows.append([self._cb("✕ Cancel", "mx")])
        return rows

    def _paged_model_keyboard(self, models: list, page: int, page_size: int) -> tuple:
        """Build model keyboard page: button rows + nav row + back/cancel row.
        Returns (keyboard_rows, total_pages, page)."""
        total_pages = max(1, (len(models) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start, end = page * page_size, min(page * page_size + page_size, len(models))
        page_models = models[start:end]
        button_rows = []
        for i, m in enumerate(page_models):
            button_rows.append([self._cb(m, f"mm:{start + i}")])
        if total_pages > 1:
            button_rows.append(self._nav_row(page, total_pages, "mg"))
        button_rows.append(self._back_cancel_row())
        return button_rows, total_pages, page

    # ── model picker callback ───────────────────────────────────────────────

    async def _handle_model_picker_callback(self, event, data: str, chat_id: str) -> None:
        """``mp:<slug>`` → show models; ``mp:<page>`` → provider page nav;
        ``mg:<page>`` → model page nav; ``mm:<index>`` → select model;
        ``mb`` → back to providers; ``mx`` → cancel."""
        parts = data.split(":", 2)
        if parts[0] == "mp":
            if len(parts) == 2:
                arg = parts[1]
                # Try page navigation first
                try:
                    page = int(arg)
                    state = self._model_picker_state.get(chat_id)
                    if not state:
                        await event.answer(message="Picker expired.", cache_time=5)
                        return
                    buttons = state["provider_buttons"]
                    page_size = state.get("provider_page_size", 10)
                    page_buttons, total_pages, page = self._provider_page(buttons, page, page_size)
                    state["provider_page"] = page
                    rows = self._rows_of_two(page_buttons)
                    if total_pages > 1:
                        rows.append(self._nav_row(page, total_pages, "mp"))
                    rows.append([self._cb("✕ Cancel", "mx")])
                    try:
                        await event.edit(
                            f"🤖 Select a provider for /model (current: {state.get('current_provider','')}):",
                            buttons=rows,
                        )
                    except Exception:
                        pass
                    await event.answer(message=f"Page {page + 1}/{total_pages}", cache_time=5)
                    return
                except ValueError:
                    # Not a page number — it's a provider slug
                    provider_slug = arg
                    state = self._model_picker_state.get(chat_id)
                    if not state:
                        await event.answer(message="Picker expired.", cache_time=5)
                        return
                    providers = state["providers"]
                    selected = next(
                        (p for p in providers if p.get("slug") == provider_slug or p.get("name") == provider_slug),
                        None,
                    )
                    if not selected:
                        await event.answer(message="Unknown provider.", cache_time=5)
                        return
                    models = list(selected.get("models", []))
                    if not models:
                        await event.answer(message="No models for this provider.", cache_time=5)
                        return
                    state["model_list"] = models
                    state["selected_provider"] = provider_slug
                    state["model_page"] = 0
                    keyboard, total_pages, page = self._paged_model_keyboard(models, 0, 8)
                    try:
                        await event.edit(
                            f"🤖 Provider: {selected.get('label') or provider_slug}\nSelect a model:",
                            buttons=keyboard,
                        )
                    except Exception:
                        pass
                    await event.answer(message=f"Provider: {provider_slug}", cache_time=5)
                    return

            if len(parts) == 3 and parts[0] == "mp" and parts[1] == "noop":
                await event.answer(message="", cache_time=1)
                return

        if parts[0] == "mg" and len(parts) == 2:
            # Model page navigation
            try:
                page = int(parts[1])
            except ValueError:
                await event.answer(message="Invalid page.", cache_time=5)
                return
            state = self._model_picker_state.get(chat_id)
            if not state:
                await event.answer(message="Picker expired.", cache_time=5)
                return
            models = state.get("model_list", [])
            keyboard, total_pages, page = self._paged_model_keyboard(models, page, 8)
            state["model_page"] = page
            selected_provider = state.get("selected_provider", "")
            try:
                await event.edit(
                    f"🤖 Provider: {selected_provider}\nSelect a model:",
                    buttons=keyboard,
                )
            except Exception:
                pass
            await event.answer(message=f"Page {page + 1}/{total_pages}", cache_time=5)
            return

        if parts[0] == "mm" and len(parts) == 2:
            # Select model by index
            try:
                idx = int(parts[1])
            except ValueError:
                await event.answer(message="Invalid model selection.", cache_time=5)
                return
            state = self._model_picker_state.pop(chat_id, None)
            if not state:
                await event.answer(message="Picker expired.", cache_time=5)
                return
            model_list = state.get("model_list", [])
            if idx < 0 or idx >= len(model_list):
                await event.answer(message="Invalid model index.", cache_time=5)
                return
            model_id = model_list[idx]
            provider_slug = state.get("selected_provider", "")
            on_model_selected = state.get("on_model_selected")
            result_text = None
            if on_model_selected:
                try:
                    result_text = await on_model_selected(chat_id, model_id, provider_slug)
                except Exception:
                    logger.exception("[Telethon] on_model_selected failed")
                    result_text = "Model switch failed — see logs."
            try:
                if result_text:
                    await event.edit(result_text, buttons=None)
                else:
                    await event.edit(f"✓ Model set: {provider_slug} / {model_id}", buttons=None)
            except Exception:
                pass
            await event.answer(message="Model selected", cache_time=5)
            return

        if data == "mb":
            # Back to provider list
            state = self._model_picker_state.get(chat_id)
            if not state:
                await event.answer(message="Picker expired.", cache_time=5)
                return
            buttons = state["provider_buttons"]
            page_size = state.get("provider_page_size", 10)
            page_buttons, total_pages, page = self._provider_page(buttons, 0, page_size)
            state["provider_page"] = 0
            state["model_list"] = []
            state["selected_provider"] = ""
            rows = self._rows_of_two(page_buttons)
            if total_pages > 1:
                rows.append(self._nav_row(0, total_pages, "mp"))
            rows.append([self._cb("✕ Cancel", "mx")])
            try:
                await event.edit(
                    f"🤖 Select a provider for /model (current: {state.get('current_provider','')}):",
                    buttons=rows,
                )
            except Exception:
                pass
            await event.answer(message="Back to providers", cache_time=5)
            return

        if data == "mx" or data.startswith("mx:"):
            # Cancel or noop (n/N page indicator)
            if data.startswith("mx:") and data != "mx":
                # "mx:noop" is the n/N page indicator — no-op
                await event.answer(message="", cache_time=1)
                return
            # Cancel
            self._model_picker_state.pop(chat_id, None)
            try:
                await event.edit("Model picker cancelled.", buttons=None)
            except Exception:
                pass
            await event.answer(message="Cancelled", cache_time=5)
            return

    async def send_choice_picker(
        self, chat_id: str, title: str, choices: list, session_key: str,
        on_choice_selected, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Flat inline-keyboard picker (one tap → one value) for /reasoning, /fast, etc.
        Each choice dict: {"value": str, "label": str, "is_current": bool}."""
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            buttons = []
            for i, choice in enumerate(choices):
                label = str(choice.get("label") or choice.get("value") or "")
                if choice.get("is_current"):
                    label = f"✓ {label}"
                    buttons.append(self._cb(label, f"cp:{i}", style="success"))
                else:
                    buttons.append(self._cb(label, f"cp:{i}"))
            if not buttons:
                return SendResult(success=False, error="No choices")
            keyboard = self._rows_of_two(buttons)
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            msg = await self._client.send_message(
                entity, title, buttons=keyboard,
            )
            self._choice_picker_state[str(chat_id)] = {
                "msg_id": msg.id,
                "choices": choices,
                "session_key": session_key,
                "on_choice_selected": on_choice_selected,
            }
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            logger.exception("[Telethon] send_choice_picker failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_clarify(
        self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
        session_key: str, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Clarify prompt: one numbered button per choice plus "Other (type answer)"."""
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            text = f"❓ {question}"
            buttons = []
            if choices:
                for i, c in enumerate(choices):
                    buttons.append(self._cb(str(i + 1), f"cl:{clarify_id}:{i}"))
                text += "\n\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(choices))
                buttons.append(self._cb("✏️ Other", f"cl:{clarify_id}:other", style="danger"))
                text += "\n\nOr type your answer."
            keyboard = self._rows_of_two(buttons) if buttons else None
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            msg = await self._client.send_message(
                entity, text, buttons=keyboard,
            )
            self._clarify_state[clarify_id] = session_key
            self._clarify_choices[clarify_id] = list(choices or [])
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            logger.exception("[Telethon] send_clarify failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        """Inline-keyboard exec-approval prompt; buttons call resolve_gateway_approval()."""
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            import itertools
            if not hasattr(self, "_approval_counter"):
                self._approval_counter = itertools.count(1)
            approval_id = next(self._approval_counter)
            buttons = [
                self._cb(label, f"ea:{choice}:{approval_id}",
                         style="success" if choice in ("once", "session", "always") else "danger")
                for label, choice, _ in prompt.actions
            ]
            keyboard = self._rows_of_two(buttons)
            entity = await self._client.get_entity(self._chat_entity(prompt.chat_id))
            msg = await self._client.send_message(
                entity, prompt.text, buttons=keyboard,
            )
            self._approval_state[approval_id] = prompt.session_key
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            logger.exception("[Telethon] _send_exec_approval_prompt failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_slash_confirm(
        self, chat_id: str, title: str, message: str, session_key: str, confirm_id: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Three-button slash-command confirmation: Approve Once / Always / Cancel."""
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            keyboard = self._rows_of_two([
                self._cb("Approve Once", f"sc:once:{confirm_id}", style="success"),
                self._cb("Always Approve", f"sc:always:{confirm_id}", style="success"),
                self._cb("Cancel", f"sc:cancel:{confirm_id}", style="danger"),
            ])
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            msg = await self._client.send_message(
                entity, f"{title}\n\n{message}", buttons=keyboard,
            )
            self._slash_confirm_state[confirm_id] = session_key
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as exc:
            logger.exception("[Telethon] send_slash_confirm failed")
            return SendResult(success=False, error=str(exc), retryable=True)

    async def _handle_callback_query(self, event) -> None:
        """Route inline-keyboard button taps (callback_data prefixes) to resolvers."""
        try:
            data = event.data.decode("utf-8") if isinstance(event.data, bytes) else str(event.data or "")
            user_id = str(getattr(event, "sender_id", "") or "").strip()
            chat_id = str(getattr(event, "chat_id", "") or "")
            if not chat_id:
                # Fallback: fetch the message to get its chat
                msg = await event.get_message()
                chat_id = str(getattr(msg, "chat_id", "") or "") if msg else ""
        except Exception:
            logger.debug("[Telethon] callback query: cannot read event fields", exc_info=True)
            return

        # --- Model picker / choice picker need chat_id ---
        # Exact-match buttons first (no prefix)
        if data == "mb" or data == "mx" or data.startswith("mg:") or data.startswith("mx:"):
            if chat_id and await self._callback_authorized(event, user_id, chat_id):
                try:
                    await self._handle_model_picker_callback(event, data, chat_id)
                except Exception:
                    logger.exception("[Telethon] handler _handle_model_picker_callback failed")
            return

        for prefix, handler in (
            ("mp:", self._handle_model_picker_callback),
            ("mm:", self._handle_model_picker_callback),
            ("cp:", self._handle_choice_picker_callback),
        ):
            if data.startswith(prefix):
                if chat_id and await self._callback_authorized(event, user_id, chat_id):
                    try:
                        await handler(event, data, chat_id)
                    except Exception:
                        logger.exception("[Telethon] handler %s failed", handler.__name__)
                return

        # --- Executable approval / slash confirm / clarify ---
        for prefix, handler in (
            ("ea:", self._handle_exec_approval_callback),
            ("sc:", self._handle_slash_confirm_callback),
            ("cl:", self._handle_clarify_callback),
        ):
            if data.startswith(prefix):
                if await self._callback_authorized(event, user_id, chat_id):
                    try:
                        await handler(event, data)
                    except Exception:
                        logger.exception("[Telethon] handler %s failed", handler.__name__)
                return

        try:
            await event.answer(message="Unknown button", cache_time=5)
        except Exception:
            pass

    async def _callback_authorized(self, event, user_id: str, chat_id: str) -> bool:
        """Gate a callback tap on the allowlist; answer with a toast if refused."""
        if self._allow_all or user_id in self._allowed_users:
            return True
        try:
            await event.answer(message="Not authorized to use this button.", cache_time=5)
        except Exception:
            pass
        return False

    async def _handle_choice_picker_callback(self, event, data: str, chat_id: str) -> None:
        """``cp:<index>`` → select a choice."""
        parts = data.split(":", 1)
        if len(parts) != 2:
            return
        try:
            idx = int(parts[1])
        except ValueError:
            return
        state = self._choice_picker_state.pop(chat_id, None)
        if not state:
            await event.answer(message="Picker expired.", cache_time=5)
            return
        choices = state["choices"]
        if idx < 0 or idx >= len(choices):
            await event.answer(message="Invalid choice.", cache_time=5)
            return
        choice = choices[idx]
        value = choice.get("value", choice.get("label", ""))
        try:
            await event.edit(f"✓ Selected: {choice.get('label') or value}", buttons=None)
        except Exception:
            pass
        await event.answer(message="Choice selected", cache_time=5)
        on_choice_selected = state.get("on_choice_selected")
        if on_choice_selected:
            try:
                await on_choice_selected(value)
            except Exception:
                logger.exception("[Telethon] on_choice_selected failed")

    async def _handle_exec_approval_callback(self, event, data: str) -> None:
        """``ea:<choice>:<approval_id>`` → resolve a pending exec approval."""
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        choice, approval_id_s = parts[1], parts[2]
        try:
            approval_id = int(approval_id_s)
        except ValueError:
            return
        session_key = self._approval_state.pop(approval_id, None)
        if not session_key:
            await event.answer(message="Approval already resolved or expired.", cache_time=5)
            return
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(session_key, choice)
        except Exception as exc:
            logger.error("Failed to resolve gateway approval from Telethon button: %s", exc)
            count = 0
        label = {"once": "Approved (once)", "session": "Approved (session)", "always": "Approved (always)", "deny": "Denied"}.get(choice, "Resolved")
        if count:
            await event.edit(f"✓ {label}", buttons=None)
        else:
            await event.edit(f"⚠ {label} (no pending command)", buttons=None)
        await event.answer(message=label, cache_time=5)

    async def _handle_slash_confirm_callback(self, event, data: str) -> None:
        """``sc:<choice>:<confirm_id>`` → resolve a slash-command confirmation."""
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        choice, confirm_id = parts[1], parts[2]
        session_key = self._slash_confirm_state.pop(confirm_id, None)
        if not session_key:
            await event.answer(message="Confirmation already resolved or expired.", cache_time=5)
            return
        label = {"once": "Run once", "always": "Always approve", "cancel": "Cancelled"}.get(choice, "Resolved")
        try:
            await event.edit(f"✓ {label}", buttons=None)
        except Exception:
            pass
        await event.answer(message=label, cache_time=5)
        try:
            from tools import slash_confirm as _slash_confirm_mod
            result_text = await _slash_confirm_mod.resolve(session_key, confirm_id, choice)
            if result_text:
                chat_id_str = str(getattr(event, "chat_id", "") or "")
                if chat_id_str:
                    entity = await self._client.get_entity(self._chat_entity(chat_id_str))
                    await self._client.send_message(entity, result_text)
        except Exception as exc:
            logger.exception("[Telethon] slash_confirm resolve failed")

    async def _handle_clarify_callback(self, event, data: str) -> None:
        """``cl:<clarify_id>:<idx>`` or ``cl:<clarify_id>:other`` → resolve a clarify prompt."""
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        clarify_id, idx_s = parts[1], parts[2]
        session_key = self._clarify_state.pop(clarify_id, None)
        if not session_key:
            await event.answer(message="Clarify expired or already answered.", cache_time=5)
            return
        if idx_s == "other":
            await event.answer(message="Type your answer.", cache_time=5)
            try:
                await event.edit("✏️ Waiting for your typed answer…", buttons=None)
            except Exception:
                pass
            from tools.clarify_gateway import mark_awaiting_text
            mark_awaiting_text(clarify_id)
            return
        try:
            idx = int(idx_s)
        except ValueError:
            return
        try:
            from tools.clarify_gateway import resolve_gateway_clarify
            # We don't know the choice text without re-reading state; use the index.
            # Store choices inline via a small sidecar on the clarify id.
            choices = getattr(self, "_clarify_choices", {}).get(clarify_id, [])
            choice_text = choices[idx] if 0 <= idx < len(choices) else str(idx)
            resolve_gateway_clarify(clarify_id, choice_text)
        except Exception as exc:
            logger.exception("[Telethon] clarify resolve failed")
        try:
            await event.edit(f"✓ Clarify answered", buttons=None)
        except Exception:
            pass
        await event.answer(message="Answered", cache_time=5)


    async def _on_new_message(self, event) -> None:
        """Telethon NewMessage handler -> normalize to MessageEvent -> handle_message()."""
        try:
            message = event.message
            if message is None:
                logger.debug("[Telethon] Inbound update with no message, skipping")
                return

            logger.debug("[Telethon] NewMessage update_id=%s chat_id=%s text=%r",
                         getattr(event, "update_id", None),
                         getattr(getattr(event, "message", None), "chat_id", None),
                         (message.message or "")[:80])

            chat = None
            try:
                chat = await event.get_chat()
            except Exception:
                logger.debug("[Telethon] get_chat() failed", exc_info=True)
            sender = None
            try:
                sender = await event.get_sender()
            except Exception:
                logger.debug("[Telethon] get_sender() failed", exc_info=True)

            # Skip our own messages to avoid reply loops.
            if sender is not None and str(getattr(sender, "id", "")) == self._bot_id:
                logger.debug("[Telethon] Skipping own message")
                return

            chat_id = str(getattr(chat, "id", "")) if chat is not None else ""
            if not chat_id and sender is not None:
                chat_id = str(getattr(sender, "id", ""))
            is_channel = isinstance(chat, Channel) or getattr(chat, "megagroup", False)
            chat_type = "group" if is_channel else "dm"
            user_id = str(getattr(sender, "id", "")) if sender is not None else None
            user_name = (
                getattr(sender, "username", None)
                or getattr(sender, "first_name", None)
                or ""
            )

            text = str(message.message or "")
            msg_type = MessageType.TEXT
            if message.media:
                msg_type = MessageType.PHOTO

            # Authorization is handled by Hermes core via TELETHON_ALLOWED_USERS
            # (gateway_env.AUTHZ). No per-message allowlist check here.

            logger.debug("[Telethon] Dispatching inbound chat_id=%s user_id=%s type=%s text=%r",
                         chat_id, user_id, chat_type, text[:80])

            source = self.build_source(
                chat_id=chat_id,
                user_id=user_id or "",
                chat_type=chat_type,
                user_name=user_name,
            )

            ev = MessageEvent(
                text=text,
                message_type=msg_type,
                user_id=user_id,
                user_name=user_name,
                source=source,
                raw_message=message,
                message_id=str(message.id),
                reply_to_message_id=(
                    str(message.reply_to_msg_id)
                    if getattr(message, "reply_to_msg_id", None)
                    else None
                ),
                allow_gateway_control=True,
                reply_expected=True,
            )

            # --- /delete <session_id> interception ---
            if (text or "").strip().startswith("/delete"):
                parts = text.split(None, 1)
                session_id = parts[1].strip() if len(parts) > 1 else ""
                if session_id:
                    await self._delete_session_command(event, chat_id, session_id)
                    return

            await self.handle_message(ev)
            logger.debug("[Telethon] handle_message returned OK")
        except Exception:
            logger.exception("[Telethon] inbound handler failed")

    async def _delete_session_command(self, event, chat_id: str, session_id: str) -> None:
        """Delete a session by session_id via the gateway state DB (Telethon path)."""
        runner = getattr(self, "gateway_runner", None)
        if runner is None:
            await self.send(chat_id, "Cannot delete session: gateway runner unavailable.")
            return
        db = getattr(runner, "_session_db", None)
        if db is None:
            await self.send(chat_id, "Cannot delete session: session DB unavailable.")
            return
        try:
            deleted = await asyncio.to_thread(
                db.delete_session, session_id,
                sessions_dir=None,
                exclude_active_write_guards=True,
            )
            if deleted:
                await self.send(chat_id, f"✓ Session `{session_id}` deleted.")
            else:
                await self.send(chat_id, f"⚠ Session `{session_id}` not found.")
        except Exception as exc:
            logger.exception("[Telethon] /delete session failed")
            await self.send(chat_id, f"✗ Failed to delete session `{session_id}`: {exc}")

    def _chat_entity(self, chat_id: str) -> int | str:
        """Normalize a chat_id string for Telethon get_entity: numeric → int, else str.

        Bare numeric strings (e.g. ``7361622601``) are passed as int so Telethon
        doesn't mistake them for a phone number.
        """
        s = str(chat_id).strip()
        if s.lstrip("-").isdigit():
            return int(s)
        if s.startswith("@"):
            return s[1:]
        return s

    def _is_user_authorized(self, user_id: Optional[str], chat_type: str, chat_id: str) -> bool:
        if self._allow_all:
            return True
        if user_id and user_id in self._allowed_users:
            return True
        return bool(getattr(self, "_allow_all_users", False))

    # ── outbound ───────────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            kwargs = {}
            if reply_to:
                kwargs["reply_to"] = int(reply_to)
            if len(content) > MAX_MESSAGE_LENGTH:
                content = content[: MAX_MESSAGE_LENGTH - 50] + "\n…(truncated)"
            result = await self._client.send_message(entity, content, **kwargs)
            return SendResult(success=True, message_id=str(getattr(result, "id", "")))
        except Exception as exc:
            logger.exception("[Telethon] send failed to %s", chat_id)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        if self._client is None or not self._connected:
            return
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            async with self._client.action(entity, "typing"):
                await asyncio.sleep(0)
        except Exception:  # nosec
            logger.debug("[Telethon] typing indicator failed for %s", chat_id, exc_info=True)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        if self._client is None:
            return {"name": chat_id, "type": "unknown", "chat_id": chat_id}
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            name = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or str(chat_id)
            )
            is_channel = isinstance(entity, Channel) or getattr(entity, "megagroup", False)
            return {
                "name": name,
                "type": "group" if is_channel else "dm",
                "chat_id": str(entity.id),
            }
        except Exception:
            return {"name": chat_id, "type": "unknown", "chat_id": chat_id}

    # ── media senders (override base signatures exactly) ───────────────────

    async def send_image(self, chat_id: str, image_url: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            await self._client.send_file(entity, image_url, caption=caption, **({"reply_to": int(reply_to)} if reply_to else {}))
            return SendResult(success=True)
        except Exception as exc:
            logger.exception("[Telethon] send_image failed to %s", chat_id)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_animation(self, chat_id: str, animation_url: str,
                             caption: Optional[str] = None,
                             reply_to: Optional[str] = None,
                             metadata: Optional[dict[str, Any]] = None) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption=caption,
                                     reply_to=reply_to, metadata=metadata)

    async def send_voice(self, chat_id: str, audio_path: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            await self._client.send_file(entity, audio_path, caption=caption,
                                           **({"reply_to": int(reply_to)} if reply_to else {}))
            return SendResult(success=True)
        except Exception as exc:
            logger.exception("[Telethon] send_voice failed to %s", chat_id)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_video(self, chat_id: str, video_path: str,
                         caption: Optional[str] = None,
                         reply_to: Optional[str] = None,
                         metadata: Optional[dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            await self._client.send_file(entity, video_path, caption=caption,
                                           **({"reply_to": int(reply_to)} if reply_to else {}))
            return SendResult(success=True)
        except Exception as exc:
            logger.exception("[Telethon] send_video failed to %s", chat_id)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_document(self, chat_id: str, file_path: str,
                            caption: Optional[str] = None,
                            file_name: Optional[str] = None,
                            reply_to: Optional[str] = None,
                            metadata: Optional[dict[str, Any]] = None,
                            **kwargs) -> SendResult:
        if self._client is None or not self._connected:
            return SendResult(success=False, error="telethon client not connected", retryable=False)
        try:
            entity = await self._client.get_entity(self._chat_entity(chat_id))
            await self._client.send_file(entity, file_path, caption=caption,
                                           **({"reply_to": int(reply_to)} if reply_to else {}))
            return SendResult(success=True)
        except Exception as exc:
            logger.exception("[Telethon] send_document failed to %s", chat_id)
            return SendResult(success=False, error=str(exc), retryable=True)

    async def send_image_file(self, chat_id: str, image_path: str,
                              caption: Optional[str] = None,
                              reply_to: Optional[str] = None,
                              metadata: Optional[dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self.send_image(chat_id, image_path, caption=caption,
                                     reply_to=reply_to, metadata=metadata)


def check_telethon_requirements() -> bool:
    """Check if this platform's dependencies are available."""
    return _TELETHON_AVAILABLE


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    from .adapter_hint import PLATFORM_HINT
    from .dashboard_card import DASHBOARD_CARD
    from . import gateway_env

    ctx.register_platform(
        name="telethon",
        label="Telethon",
        adapter_factory=TelethonAdapter,
        check_fn=check_telethon_requirements,
        required_env=["TELETHON_BOT_TOKEN"],
        env_overrides=gateway_env.ENV_OVERRIDES,
        env_enable_credentials=gateway_env.ENV_ENABLE_CREDENTIALS,
        **DASHBOARD_CARD,
        platform_hint=PLATFORM_HINT,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="📱",
        allow_update_command=True,
        cron_deliver_env_var="TELETHON_HOME_CHANNEL",
        **gateway_env.AUTHZ,
    )
