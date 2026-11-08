"""AstrBot port of the PoE2 Trade Announcer polling workflow."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, register


LOGGER = logging.getLogger("astrbot_plugin_poe2_trade_announcer")
TRADE_HISTORY_LIMIT = 100
SEEN_TRADE_LIMIT = 5000


def _item_name_key(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _load_traditional_item_names() -> dict[str, str]:
    try:
        payload = json.loads(
            Path(__file__).with_name("traditional_item_names.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        LOGGER.exception("讀取繁體物品名稱表失敗")
        return {}
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {
        str(key): str(value).strip()
        for key, value in rows.items()
        if key and isinstance(value, str) and value.strip()
    }


TRADITIONAL_ITEM_NAMES = _load_traditional_item_names()
TRADITIONAL_CURRENCY_ALIASES = {
    alias: TRADITIONAL_ITEM_NAMES.get(_item_name_key(full_name), "")
    for alias, full_name in {
        "annulment": "Annulment Orb",
        "chaos": "Chaos Orb",
        "divine": "Divine Orb",
        "exalted": "Exalted Orb",
        "mirror": "Mirror of Kalandra",
        "regal": "Regal Orb",
        "vaal": "Vaal Orb",
    }.items()
}


@register(
    "astrbot_plugin_poe2_trade_announcer",
    "xiaoliz1",
    "PoE2 交易提醒",
    "0.2.1",
)
class Poe2TradeAnnouncer(Star):
    """Poll separate PoE2 trade histories for users who explicitly subscribe."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._poll_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self._legacy_state = self._new_account_state()
        self._user_states: dict[str, dict[str, Any]] = {}
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        """Stop the polling loop when AstrBot unloads the plugin."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)

    @staticmethod
    def _new_account_state() -> dict[str, Any]:
        return {
            "session_id": "",
            "owner_name": "",
            "subscribers": [],
            "seen_trade_ids": [],
            "trade_history": [],
            "baseline_ready": False,
            "last_success_at": None,
            "last_error": None,
        }

    @classmethod
    def _load_account_state(
        cls, payload: Any, include_session_id: bool
    ) -> dict[str, Any]:
        state = cls._new_account_state()
        if not isinstance(payload, dict):
            return state
        if include_session_id:
            state["session_id"] = str(payload.get("session_id", "") or "").strip()
        state["owner_name"] = str(payload.get("owner_name", "") or "").strip()
        subscribers = payload.get("subscribers", [])
        if isinstance(subscribers, list):
            state["subscribers"] = sorted(
                {str(umo).strip() for umo in subscribers if str(umo).strip()}
            )
        ids = payload.get("seen_trade_ids", [])
        if isinstance(ids, list):
            state["seen_trade_ids"] = [str(value) for value in ids if value][
                :SEEN_TRADE_LIMIT
            ]
        trades = payload.get("trade_history", [])
        if isinstance(trades, list):
            state["trade_history"] = [
                trade for trade in trades if isinstance(trade, dict)
            ][:TRADE_HISTORY_LIMIT]
        state["baseline_ready"] = bool(payload.get("baseline_ready"))
        last_success = payload.get("last_success_at")
        state["last_success_at"] = str(last_success) if last_success else None
        last_error = payload.get("last_error")
        state["last_error"] = str(last_error) if last_error else None
        return state

    @staticmethod
    def _serialize_account_state(
        state: dict[str, Any], include_session_id: bool
    ) -> dict[str, Any]:
        payload = {
            "owner_name": state.get("owner_name", ""),
            "subscribers": sorted(set(state.get("subscribers", []))),
            "seen_trade_ids": state.get("seen_trade_ids", [])[:SEEN_TRADE_LIMIT],
            "trade_history": state.get("trade_history", [])[:TRADE_HISTORY_LIMIT],
            "baseline_ready": bool(state.get("baseline_ready")),
            "last_success_at": state.get("last_success_at"),
            "last_error": state.get("last_error"),
        }
        if include_session_id:
            payload["session_id"] = state.get("session_id", "")
        return payload

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        async with self._state_lock:
            if self._state_loaded:
                return
            try:
                saved_subscribers = await self.get_kv_data("subscribers", [])
                if isinstance(saved_subscribers, list):
                    self._legacy_state["subscribers"] = sorted(
                        {
                            str(umo).strip()
                            for umo in saved_subscribers
                            if str(umo).strip()
                        }
                    )

                saved_state = await self.get_kv_data("trade_state", {})
                if isinstance(saved_state, dict):
                    legacy_trade_state = self._load_account_state(
                        saved_state, include_session_id=False
                    )
                    # Older releases stored subscribers separately from trade_state.
                    if "subscribers" not in saved_state:
                        legacy_trade_state["subscribers"] = self._legacy_state[
                            "subscribers"
                        ]
                    self._legacy_state.update(
                        legacy_trade_state
                    )

                saved_users = await self.get_kv_data("user_trade_states", {})
                if isinstance(saved_users, dict):
                    self._user_states = {
                        str(account_key): self._load_account_state(
                            state, include_session_id=True
                        )
                        for account_key, state in saved_users.items()
                        if str(account_key).strip() and isinstance(state, dict)
                    }
            except Exception:
                LOGGER.exception("读取交易提醒持久化状态失败")
            finally:
                self._state_loaded = True

    async def _save_legacy_state_locked(self) -> None:
        try:
            state = self._serialize_account_state(
                self._legacy_state, include_session_id=False
            )
            await self.put_kv_data("subscribers", state["subscribers"])
            await self.put_kv_data("trade_state", state)
        except Exception:
            LOGGER.exception("保存交易提醒持久化状态失败")

    async def _save_user_states_locked(self) -> bool:
        try:
            payload = {
                account_key: self._serialize_account_state(
                    state, include_session_id=True
                )
                for account_key, state in self._user_states.items()
            }
            await self.put_kv_data("user_trade_states", payload)
        except Exception:
            LOGGER.exception("保存使用者交易提醒状态失败")
            return False
        return True

    async def _save_account_state(self, account_key: str | None) -> None:
        async with self._state_lock:
            if account_key is None:
                await self._save_legacy_state_locked()
            else:
                await self._save_user_states_locked()

    def _enabled(self) -> bool:
        value = self.config.get("monitor_enabled", False)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _session_id(self) -> str:
        """Read the optional legacy administrator session configured in WebUI."""
        return str(self.config.get("poe2_session_id", "") or "").strip()

    def _league_name(self) -> str:
        return str(self.config.get("league_name", "") or "").strip()

    def _poll_interval_seconds(self) -> int:
        try:
            minutes = int(self.config.get("poll_interval_minutes", 15))
        except (TypeError, ValueError):
            minutes = 15
        return max(15, min(60, minutes)) * 60

    async def _poll_loop(self) -> None:
        await self._ensure_state_loaded()
        not_ready_signature: tuple[bool, bool] | None = None
        while True:
            try:
                ready = (self._enabled(), bool(self._league_name()))
                if all(ready):
                    not_ready_signature = None
                    legacy_session = self._session_id()
                    if legacy_session and self._legacy_state["subscribers"]:
                        await self._poll_account(None, legacy_session)

                    async with self._state_lock:
                        configured_users = [
                            (account_key, str(state.get("session_id", "")))
                            for account_key, state in self._user_states.items()
                            if state.get("session_id") and state.get("subscribers")
                        ]
                    for account_key, session_id in configured_users:
                        await self._poll_account(account_key, session_id)
                elif ready != not_ready_signature:
                    not_ready_signature = ready
                    LOGGER.info(
                        "交易监控暫未執行：請在外掛設定中啟用監控並填寫目前賽季名稱。"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("交易歷史輪詢流程失敗：%s", str(exc)[:300])
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_account(
        self, account_key: str | None, session_id: str
    ) -> None:
        league = self._league_name()
        endpoint = (
            "https://www.pathofexile.com/api/trade2/history/"
            f"{quote(league, safe='')}"
        )
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "Accept": "application/json",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "User-Agent": "AstrBotPoE2TradeAnnouncer/0.2.1",
            "Cookie": f"POESESSID={session_id}",
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(endpoint) as response:
                    body = await response.text()
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        await self._set_account_error(
                            account_key, session_id, "交易 API 返回了无法解析的响应。"
                        )
                        return

                    if response.status == 429:
                        await self._set_account_error(
                            account_key, session_id, "交易 API 限流，將於下次輪詢重試。"
                        )
                        LOGGER.warning("交易 API 限流，帳號鍵：%s", account_key or "legacy")
                        return
                    if response.status >= 400:
                        await self._set_account_error(
                            account_key,
                            session_id,
                            f"交易 API 請求失敗（HTTP {response.status}）。",
                        )
                        LOGGER.warning(
                            "交易 API 請求失敗（HTTP %s），帳號鍵：%s",
                            response.status,
                            account_key or "legacy",
                        )
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._set_account_error(
                account_key, session_id, "連線交易 API 失敗，將於下次輪詢重試。"
            )
            LOGGER.warning(
                "交易 API 輪詢失敗，帳號鍵：%s，錯誤：%s",
                account_key or "legacy",
                str(exc)[:300],
            )
            return

        if not isinstance(payload, dict):
            await self._set_account_error(
                account_key, session_id, "交易 API 返回了意外的数据結構。"
            )
            return
        if not isinstance(payload.get("result"), list):
            await self._set_account_error(
                account_key, session_id, "未取得交易紀錄，請檢查 Session ID 和賽季名稱。"
            )
            LOGGER.warning("交易 API 未返回交易列表，帳號鍵：%s", account_key or "legacy")
            return

        trades = [trade for trade in payload["result"] if isinstance(trade, dict)]
        await self._record_snapshot(account_key, session_id, trades)
        await self._mark_account_success(account_key, session_id)

    async def _set_account_error(
        self, account_key: str | None, session_id: str, message: str
    ) -> None:
        async with self._state_lock:
            state = (
                self._legacy_state
                if account_key is None
                else self._user_states.get(account_key)
            )
            if not state:
                return
            if account_key is None:
                if self._session_id() != session_id:
                    return
            elif state.get("session_id") != session_id:
                return
            state["last_error"] = message
            if account_key is None:
                await self._save_legacy_state_locked()
            else:
                await self._save_user_states_locked()

    async def _mark_account_success(
        self, account_key: str | None, session_id: str
    ) -> None:
        async with self._state_lock:
            state = (
                self._legacy_state
                if account_key is None
                else self._user_states.get(account_key)
            )
            if not state:
                return
            if account_key is None:
                if self._session_id() != session_id:
                    return
            elif state.get("session_id") != session_id:
                return
            state["last_success_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            state["last_error"] = None
            if account_key is None:
                await self._save_legacy_state_locked()
            else:
                await self._save_user_states_locked()

    async def _record_snapshot(
        self,
        account_key: str | None,
        session_id: str,
        trades: list[dict[str, Any]],
    ) -> None:
        await self._ensure_state_loaded()
        current: list[tuple[str, dict[str, Any]]] = []
        for trade in trades:
            trade_id = self._trade_id(trade)
            if trade_id:
                current.append((trade_id, trade))

        new_trades: list[tuple[str, dict[str, Any]]] = []
        recipients: list[str] = []
        owner_name = "管理員帳號" if account_key is None else "群友"
        async with self._state_lock:
            state = (
                self._legacy_state
                if account_key is None
                else self._user_states.get(account_key)
            )
            if not state:
                return
            if account_key is None:
                if self._session_id() != session_id:
                    return
            elif state.get("session_id") != session_id:
                return

            existing_ids = set(state["seen_trade_ids"])
            if not state["baseline_ready"]:
                state["seen_trade_ids"] = [trade_id for trade_id, _ in current][
                    :SEEN_TRADE_LIMIT
                ]
                state["trade_history"] = [trade for _, trade in current][
                    :TRADE_HISTORY_LIMIT
                ]
                state["baseline_ready"] = True
                LOGGER.info(
                    "交易历史初始基线已记录 %s 笔，不会把历史成交作为新通知发送。",
                    len(current),
                )
            else:
                new_trades = [
                    (trade_id, trade)
                    for trade_id, trade in current
                    if trade_id not in existing_ids
                ]
                if new_trades:
                    current_ids = [trade_id for trade_id, _ in current]
                    state["seen_trade_ids"] = list(
                        dict.fromkeys(current_ids + state["seen_trade_ids"])
                    )[:SEEN_TRADE_LIMIT]
                    new_history = [trade for _, trade in new_trades]
                    new_history_ids = {
                        self._trade_id(trade) for trade in new_history
                    }
                    state["trade_history"] = (
                        new_history
                        + [
                            trade
                            for trade in state["trade_history"]
                            if self._trade_id(trade) not in new_history_ids
                        ]
                    )[:TRADE_HISTORY_LIMIT]
                    recipients = list(state["subscribers"])
                    owner_name = str(state.get("owner_name", "") or "群友")

        await self._save_account_state(account_key)
        if not recipients:
            if new_trades:
                LOGGER.info(
                    "發現 %s 筆新成交，但帳號沒有已訂閱對話。帳號鍵：%s",
                    len(new_trades),
                    account_key or "legacy",
                )
            return

        for _, trade in reversed(new_trades):
            await self._send_trade_to_recipients(
                trade, recipients, owner_name, account_key
            )

    async def _send_trade_to_recipients(
        self,
        trade: dict[str, Any],
        recipients: list[str],
        owner_name: str,
        account_key: str | None,
    ) -> None:
        item = trade.get("item") if isinstance(trade.get("item"), dict) else {}
        safe_owner_name = " ".join(str(owner_name or "群友").split())[:40]
        text = self._format_trade(trade, item)
        if account_key is None:
            text = f"👤 管理員帳號\n{text}"
        sender_id = account_key.rsplit(":", 1)[-1] if account_key else ""
        icon = str(item.get("icon", "") or "").strip()
        for umo in recipients:
            mention_components: list[Any] = []
            should_mention = bool(
                sender_id and self._is_group_umo(umo)
            )
            if should_mention:
                mention_components.extend(
                    [At(qq=sender_id, name=safe_owner_name), Plain("\n")]
                )
            text_components = [*mention_components, Plain(text)]
            components = list(text_components)
            if icon.startswith(("https://", "http://")):
                components.append(Image.fromURL(icon))
            try:
                sent = await self.context.send_message(umo, MessageChain(components))
                if not sent and len(components) > len(text_components):
                    sent = await self.context.send_message(
                        umo, MessageChain(text_components)
                    )
                if not sent:
                    LOGGER.warning("向已订阅会话发送成交提醒失败：%s", umo)
            except Exception:
                if len(components) > len(text_components):
                    try:
                        sent = await self.context.send_message(
                            umo, MessageChain(text_components)
                        )
                        if sent:
                            continue
                    except Exception:
                        pass
                LOGGER.exception("向已订阅会话发送成交提醒失败：%s", umo)

    @staticmethod
    def _trade_id(trade: dict[str, Any]) -> str:
        candidates = [
            trade.get("id"),
            trade.get("item_id"),
            (trade.get("item") or {}).get("id")
            if isinstance(trade.get("item"), dict)
            else None,
        ]
        for candidate in candidates:
            if candidate:
                return str(candidate)
        stable_data = json.dumps(trade, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(stable_data.encode("utf-8")).hexdigest()

    @staticmethod
    def _api_error_message(payload: Any) -> str:
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or error.get("name")
        elif isinstance(error, str):
            message = error
        else:
            message = None
        return str(message or "响应中没有交易列表").replace("\n", " ")[:300]

    @staticmethod
    def _traditional_item_name(item: dict[str, Any]) -> str:
        candidates = [
            str(item.get(field, "") or "").strip()
            for field in ("name", "typeLine", "baseType")
        ]
        for candidate in candidates:
            traditional = TRADITIONAL_ITEM_NAMES.get(_item_name_key(candidate))
            if traditional:
                return traditional
        return "未收錄物品"

    @staticmethod
    def _traditional_price(price: dict[str, Any]) -> str:
        amount = price.get("amount")
        currency = str(price.get("currency", "") or "").strip()
        if amount is None:
            return "價格未提供"
        normalized = _item_name_key(currency)
        traditional = (
            TRADITIONAL_ITEM_NAMES.get(normalized)
            or TRADITIONAL_CURRENCY_ALIASES.get(normalized)
        )
        return f"{amount} {traditional or '未收錄通貨'}"

    @staticmethod
    def _format_trade(trade: dict[str, Any], item: dict[str, Any]) -> str:
        name = Poe2TradeAnnouncer._traditional_item_name(item)
        price = trade.get("price") if isinstance(trade.get("price"), dict) else {}
        price_text = Poe2TradeAnnouncer._traditional_price(price)
        when = trade.get("time") or "時間未提供"
        return (
            "💰 PoE2 成交通知\n"
            f"物品：{name}\n"
            f"價格：{price_text}\n"
            f"成交時間：{when}"
        )

    @staticmethod
    def _current_umo(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    @staticmethod
    def _is_group_umo(umo: str) -> bool:
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return False
        message_type = parts[1].replace("_", "").casefold()
        return message_type == "groupmessage"

    @staticmethod
    def _account_key(event: AstrMessageEvent) -> str:
        platform_id = str(event.get_platform_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if not platform_id or not sender_id:
            return ""
        return f"{platform_id}:{sender_id}"

    @staticmethod
    def _event_command_argument(event: AstrMessageEvent) -> str:
        try:
            message = str(event.get_message_str() or "")
        except Exception:
            message = str(getattr(event, "message_str", "") or "")
        parts = message.strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @classmethod
    def _parse_session_id(cls, event: AstrMessageEvent) -> str:
        value = cls._event_command_argument(event)
        if value.casefold().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        value = value.split(";", 1)[0].strip()
        if value.casefold().startswith("poesessid="):
            value = value.split("=", 1)[1].strip()
        if (
            not value
            or len(value) > 1024
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            return ""
        return value

    @staticmethod
    def _sender_name(event: AstrMessageEvent) -> str:
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            name = ""
        return " ".join(name.split())[:40] or "群友"

    @staticmethod
    def _format_recent_history(trades: list[dict[str, Any]]) -> str:
        if not trades:
            return "目前尚無交易紀錄；首次成功檢查會建立歷史基線。"
        lines = ["最近 PoE2 成交紀錄："]
        for trade in trades[:10]:
            item = trade.get("item") if isinstance(trade.get("item"), dict) else {}
            price = trade.get("price") if isinstance(trade.get("price"), dict) else {}
            name = Poe2TradeAnnouncer._traditional_item_name(item)
            value = Poe2TradeAnnouncer._traditional_price(price)
            when = trade.get("time") or "時間未提供"
            lines.append(f"- {name}｜{value}｜{when}")
        return "\n".join(lines)

    @filter.command("poe2交易帮助")
    async def help(self, event: AstrMessageEvent):
        """说明用户如何安全绑定自己的 PoE 账号并订阅通知。"""
        yield event.plain_result(
            "PoE2 交易通知使用方法：\n"
            "1. 登录 pathofexile.com，在浏览器按 F12，打开 Application/应用程序 → Cookies → https://www.pathofexile.com，复制 POESESSID 的 Value。\n"
            "2. 只在与机器人的私聊中发送：/poe2交易设置 <POESESSID>。\n"
            "3. 到需要接收通知的群发送：/poe2交易订阅。每位群友绑定和收到的成交通知互相独立；有新成交时群里会 @ 你。\n"
            "其他指令：/poe2交易状态、/poe2交易历史、/poe2交易退订、/poe2交易清除。\n"
            "POESESSID 等同账号登录凭据，绝不要发到群里；它会保存在 AstrBot 本机插件数据中，管理员及主机备份可能可以访问。/poe2交易清除只删除本插件保存的副本。"
        )

    @filter.command("poe2交易设置")
    async def set_session(self, event: AstrMessageEvent):
        """仅在私聊中保存当前用户自己的 POESESSID。"""
        if not event.is_private_chat():
            yield event.plain_result(
                "為保護帳號，Session ID 只能私聊設定；若已發到群裡，請立即撤回並在 Path of Exile 官網使該登入會話失效。"
            )
            return

        account_key = self._account_key(event)
        session_id = self._parse_session_id(event)
        if not account_key:
            yield event.plain_result("無法識別你的平台帳號，Session ID 未保存。")
            return
        if not session_id:
            yield event.plain_result(
                "格式：/poe2交易设置 <POESESSID>；也可填写 POESESSID=<值>。請只在私聊中傳送。"
            )
            return

        await self._ensure_state_loaded()
        async with self._state_lock:
            previous = copy.deepcopy(self._user_states.get(account_key))
            state = copy.deepcopy(previous) if previous else self._new_account_state()
            if state.get("session_id") != session_id:
                state["seen_trade_ids"] = []
                state["trade_history"] = []
                state["baseline_ready"] = False
                state["last_success_at"] = None
                state["last_error"] = None
            state["session_id"] = session_id
            state["owner_name"] = self._sender_name(event)
            self._user_states[account_key] = state
            saved = await self._save_user_states_locked()
            if not saved:
                if previous is None:
                    self._user_states.pop(account_key, None)
                else:
                    self._user_states[account_key] = previous

        if not saved:
            yield event.plain_result("保存失敗，請稍後重試；Session ID 未確認保存。")
            return
        yield event.plain_result(
            "你的 Session ID 已單獨保存。請到要接收通知的群發送 /poe2交易订阅；不要在群裡重複傳送 Session ID。"
        )

    @filter.command("poe2交易清除")
    async def clear_session(self, event: AstrMessageEvent):
        """仅清除当前私聊用户在插件中的会话凭据和个人数据。"""
        if not event.is_private_chat():
            yield event.plain_result("請私聊機器人使用 /poe2交易清除。")
            return
        account_key = self._account_key(event)
        if not account_key:
            yield event.plain_result("無法識別你的平台帳號。")
            return

        await self._ensure_state_loaded()
        async with self._state_lock:
            previous = copy.deepcopy(self._user_states.get(account_key))
            existed = previous is not None
            self._user_states.pop(account_key, None)
            saved = await self._save_user_states_locked()
            if not saved and previous is not None:
                self._user_states[account_key] = previous

        if not saved:
            yield event.plain_result("清除失敗，請稍後重試；原有資料仍保留。")
        elif existed:
            yield event.plain_result(
                "已清除本插件保存的 Session ID、個人歷史和所有群訂閱；此操作不會讓 Path of Exile 官網上的登入會話失效。"
            )
        else:
            yield event.plain_result("目前沒有保存你的個人交易資料。")

    @filter.command("poe2交易订阅")
    async def subscribe(self, event: AstrMessageEvent):
        """将当前会话加入当前用户自己的 PoE2 成交通知。"""
        await self._ensure_state_loaded()
        account_key = self._account_key(event)
        umo = self._current_umo(event)
        if not account_key or not umo:
            yield event.plain_result("無法識別目前對話，訂閱未儲存。")
            return

        async with self._state_lock:
            previous = copy.deepcopy(self._user_states.get(account_key))
            state = copy.deepcopy(previous) if previous else self._new_account_state()
            if not state.get("session_id"):
                saved = None
            else:
                state["owner_name"] = self._sender_name(event)
                state["subscribers"] = sorted(set(state["subscribers"]) | {umo})
                self._user_states[account_key] = state
                saved = await self._save_user_states_locked()
                if not saved:
                    if previous is None:
                        self._user_states.pop(account_key, None)
                    else:
                        self._user_states[account_key] = previous

        if saved is None:
            yield event.plain_result(
                "請先私聊機器人發送 /poe2交易设置 <POESESSID>，再回到群裡訂閱。"
            )
        elif not saved:
            yield event.plain_result("保存失敗，請稍後重試；本次訂閱未確認保存。")
        elif self._enabled() and self._league_name():
            yield event.plain_result("已訂閱此對話；你的新成交通知會在群裡 @ 你。")
        else:
            yield event.plain_result(
                "已保存你的訂閱；機器人管理員還需啟用監控並設定目前賽季。"
            )

    @filter.command("poe2交易退订")
    async def unsubscribe(self, event: AstrMessageEvent):
        """仅取消当前用户在当前会话中的 PoE2 成交提醒。"""
        await self._ensure_state_loaded()
        account_key = self._account_key(event)
        umo = self._current_umo(event)
        if not account_key or not umo:
            yield event.plain_result("無法識別目前對話。")
            return
        async with self._state_lock:
            previous = copy.deepcopy(self._user_states.get(account_key))
            if previous is None:
                existed = False
                saved = True
            else:
                state = copy.deepcopy(previous)
                existed = umo in state["subscribers"]
                state["subscribers"] = [item for item in state["subscribers"] if item != umo]
                self._user_states[account_key] = state
                saved = await self._save_user_states_locked()
                if not saved:
                    self._user_states[account_key] = previous
        if not saved:
            yield event.plain_result("保存失敗，請稍後重試；本次退訂未確認保存。")
        else:
            message = "已取消你在此對話的成交通知。" if existed else "你尚未訂閱此對話的成交通知。"
            yield event.plain_result(message)

    @filter.command("poe2交易状态")
    async def status(self, event: AstrMessageEvent):
        """仅查看当前用户自己的交易提醒状态，不显示 Session ID。"""
        await self._ensure_state_loaded()
        account_key = self._account_key(event)
        if not account_key:
            yield event.plain_result("無法識別你的平台帳號。")
            return
        async with self._state_lock:
            state = copy.deepcopy(
                self._user_states.get(account_key, self._new_account_state())
            )
        active = bool(
            self._enabled()
            and self._league_name()
            and state.get("session_id")
            and state["subscribers"]
        )
        chat_subscribed = self._current_umo(event) in state["subscribers"]
        lines = [
            f"你的通知監控：{'執行中' if active else '未啟用／尚未完成設定'}",
            f"目前賽季：{self._league_name() or '未設定'}",
            f"你的 Session ID：{'已設定' if state.get('session_id') else '未設定'}",
            f"目前對話：{'你已訂閱' if chat_subscribed else '你未訂閱'}",
            f"你的訂閱對話數：{len(state['subscribers'])}",
            f"輪詢間隔：{self._poll_interval_seconds() // 60} 分鐘",
            f"最近成功檢查：{state['last_success_at'] or '尚無'}",
        ]
        if state["last_error"]:
            lines.append(f"最近錯誤：{state['last_error']}")
        yield event.plain_result("\n".join(lines))

    @filter.command("poe2交易历史")
    async def history(self, event: AstrMessageEvent):
        """查看当前用户自己的最近成交记录。"""
        await self._ensure_state_loaded()
        account_key = self._account_key(event)
        umo = self._current_umo(event)
        if not account_key:
            yield event.plain_result("無法識別你的平台帳號。")
            return
        async with self._state_lock:
            state = copy.deepcopy(self._user_states.get(account_key))
        if not event.is_private_chat() and (
            state is None or umo not in state["subscribers"]
        ):
            yield event.plain_result("請先在此群發送 /poe2交易订阅，再查看你的成交紀錄。")
            return
        yield event.plain_result(
            self._format_recent_history(state["trade_history"] if state else [])
        )
