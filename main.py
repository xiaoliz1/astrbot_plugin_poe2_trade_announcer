"""AstrBot port of the PoE2 Trade Announcer polling workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register


LOGGER = logging.getLogger("astrbot_plugin_poe2_trade_announcer")
TRADE_HISTORY_LIMIT = 100
SEEN_TRADE_LIMIT = 5000


@register(
    "astrbot_plugin_poe2_trade_announcer",
    "xiaoliz1",
    "PoE2 交易提醒",
    "0.1.0",
)
class Poe2TradeAnnouncer(Star):
    """Poll PoE2 trade history and notify chats that explicitly subscribed."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._poll_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self._subscribers: set[str] = set()
        self._seen_trade_ids: list[str] = []
        self._trade_history: list[dict[str, Any]] = []
        self._baseline_ready = False
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        """Stop the polling loop when AstrBot unloads the plugin."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        async with self._state_lock:
            if self._state_loaded:
                return
            try:
                saved_subscribers = await self.get_kv_data("subscribers", [])
                if isinstance(saved_subscribers, list):
                    self._subscribers = {
                        str(umo).strip() for umo in saved_subscribers if str(umo).strip()
                    }

                saved_state = await self.get_kv_data("trade_state", {})
                if isinstance(saved_state, dict):
                    ids = saved_state.get("seen_trade_ids", [])
                    trades = saved_state.get("trade_history", [])
                    self._seen_trade_ids = [
                        str(value) for value in ids if value
                    ][:SEEN_TRADE_LIMIT] if isinstance(ids, list) else []
                    self._trade_history = [
                        trade for trade in trades if isinstance(trade, dict)
                    ][:TRADE_HISTORY_LIMIT] if isinstance(trades, list) else []
                    self._baseline_ready = bool(saved_state.get("baseline_ready"))
                    last_success = saved_state.get("last_success_at")
                    self._last_success_at = str(last_success) if last_success else None
            except Exception:
                LOGGER.exception("读取交易提醒持久化状态失败")
            finally:
                self._state_loaded = True

    async def _save_state(self) -> None:
        try:
            await self.put_kv_data("trade_state", {
                "seen_trade_ids": self._seen_trade_ids[:SEEN_TRADE_LIMIT],
                "trade_history": self._trade_history[:TRADE_HISTORY_LIMIT],
                "baseline_ready": self._baseline_ready,
                "last_success_at": self._last_success_at,
            })
        except Exception:
            LOGGER.exception("保存交易提醒持久化状态失败")

    def _enabled(self) -> bool:
        value = self.config.get("monitor_enabled", False)
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _session_id(self) -> str:
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
        not_ready_signature: tuple[bool, bool, bool] | None = None
        while True:
            try:
                ready = (self._enabled(), bool(self._session_id()), bool(self._league_name()))
                if all(ready):
                    not_ready_signature = None
                    await self._poll_once()
                elif ready != not_ready_signature:
                    not_ready_signature = ready
                    LOGGER.info(
                        "交易监控暂未运行：请在插件配置中启用监控，并填写 POESESSID 与当前赛季名称。"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = f"轮询请求失败：{str(exc)[:300]}"
                LOGGER.exception("交易历史轮询失败")
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self) -> None:
        league = self._league_name()
        session_id = self._session_id()
        endpoint = (
            "https://www.pathofexile.com/api/trade2/history/"
            f"{quote(league, safe='')}"
        )
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "Accept": "application/json",
            "User-Agent": "AstrBotPoE2TradeAnnouncer/0.1.0",
            "Cookie": f"POESESSID={session_id}",
        }

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(endpoint) as response:
                body = await response.text()
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as exc:
                    self._last_error = f"交易 API 返回了无法解析的响应（HTTP {response.status}）。"
                    raise RuntimeError(self._last_error) from exc

                if response.status == 429:
                    self._last_error = "交易 API 限流；插件会在下个轮询周期重试。"
                    LOGGER.warning(self._last_error)
                    return
                if response.status >= 400:
                    message = self._api_error_message(payload)
                    self._last_error = f"交易 API 请求失败（HTTP {response.status}）：{message}"
                    LOGGER.warning(self._last_error)
                    return

        if not isinstance(payload, dict):
            self._last_error = "交易 API 返回了意外的数据结构。"
            LOGGER.warning(self._last_error)
            return
        if not isinstance(payload.get("result"), list):
            self._last_error = self._api_error_message(payload)
            LOGGER.warning("交易 API 未返回交易列表：%s", self._last_error)
            return

        trades = [trade for trade in payload["result"] if isinstance(trade, dict)]
        await self._record_snapshot(trades)
        self._last_success_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._last_error = None
        await self._save_state()

    async def _record_snapshot(self, trades: list[dict[str, Any]]) -> None:
        await self._ensure_state_loaded()
        current: list[tuple[str, dict[str, Any]]] = []
        for trade in trades:
            trade_id = self._trade_id(trade)
            if trade_id:
                current.append((trade_id, trade))

        async with self._state_lock:
            existing_ids = set(self._seen_trade_ids)
            if not self._baseline_ready:
                self._seen_trade_ids = [trade_id for trade_id, _ in current][
                    :SEEN_TRADE_LIMIT
                ]
                self._trade_history = [trade for _, trade in current][
                    :TRADE_HISTORY_LIMIT
                ]
                self._baseline_ready = True
                LOGGER.info(
                    "交易历史初始基线已记录 %s 笔，不会把历史成交作为新通知发送。",
                    len(current),
                )
                return

            new_trades = [
                (trade_id, trade)
                for trade_id, trade in current
                if trade_id not in existing_ids
            ]
            if not new_trades:
                return

            current_ids = [trade_id for trade_id, _ in current]
            self._seen_trade_ids = list(dict.fromkeys(
                current_ids + self._seen_trade_ids
            ))[:SEEN_TRADE_LIMIT]
            new_history = [trade for _, trade in new_trades]
            new_history_ids = {self._trade_id(trade) for trade in new_history}
            self._trade_history = (
                new_history
                + [
                    trade
                    for trade in self._trade_history
                    if self._trade_id(trade) not in new_history_ids
                ]
            )[:TRADE_HISTORY_LIMIT]
            recipients = list(self._subscribers)

        await self._save_state()
        if not recipients:
            LOGGER.info("发现 %s 笔新成交，但当前没有已订阅会话。", len(new_trades))
            return

        for _, trade in reversed(new_trades):
            await self._send_trade_to_recipients(trade, recipients)

    async def _send_trade_to_recipients(
        self, trade: dict[str, Any], recipients: list[str]
    ) -> None:
        item = trade.get("item") if isinstance(trade.get("item"), dict) else {}
        text = self._format_trade(trade, item)
        icon = str(item.get("icon", "") or "").strip()
        for umo in recipients:
            try:
                components: list[Any] = [Plain(text)]
                if icon.startswith(("https://", "http://")):
                    components.append(Image.fromURL(icon))
                sent = await self.context.send_message(umo, MessageChain(components))
                if not sent and len(components) > 1:
                    sent = await self.context.send_message(
                        umo, MessageChain([Plain(text)])
                    )
                if not sent:
                    LOGGER.warning("向已订阅会话发送成交提醒失败：%s", umo)
            except Exception:
                if icon.startswith(("https://", "http://")):
                    try:
                        sent = await self.context.send_message(
                            umo, MessageChain([Plain(text)])
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
    def _format_trade(trade: dict[str, Any], item: dict[str, Any]) -> str:
        name = item.get("name") or item.get("typeLine") or item.get("baseType") or "未知物品"
        price = trade.get("price") if isinstance(trade.get("price"), dict) else {}
        amount = price.get("amount")
        currency = price.get("currency")
        price_text = f"{amount} {currency}".strip() if amount is not None else "价格未提供"
        league = item.get("league") or "未知赛季"
        when = trade.get("time") or "时间未提供"
        return (
            "💰 PoE2 成交提醒\n"
            f"物品：{name}\n"
            f"价格：{price_text}\n"
            f"赛季：{league}\n"
            f"成交时间：{when}"
        )

    @staticmethod
    def _current_umo(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    @filter.command("poe2交易订阅")
    async def subscribe(self, event: AstrMessageEvent):
        """订阅当前会话的 PoE2 成交提醒。"""
        await self._ensure_state_loaded()
        umo = self._current_umo(event)
        if not umo:
            yield event.plain_result("无法识别当前会话，订阅未保存。")
            return
        async with self._state_lock:
            self._subscribers.add(umo)
            await self.put_kv_data("subscribers", sorted(self._subscribers))
        if self._enabled() and self._session_id() and self._league_name():
            yield event.plain_result("当前会话已订阅 PoE2 成交提醒。")
        else:
            yield event.plain_result(
                "当前会话已订阅；管理员还需在插件配置中启用监控并填写 POESESSID 与赛季名称。"
            )

    @filter.command("poe2交易退订")
    async def unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的 PoE2 成交提醒。"""
        await self._ensure_state_loaded()
        umo = self._current_umo(event)
        async with self._state_lock:
            existed = umo in self._subscribers
            self._subscribers.discard(umo)
            await self.put_kv_data("subscribers", sorted(self._subscribers))
        message = "当前会话已取消成交提醒。" if existed else "当前会话没有订阅成交提醒。"
        yield event.plain_result(message)

    @filter.command("poe2交易状态")
    async def status(self, event: AstrMessageEvent):
        """查看交易提醒监控状态，不显示会话 Cookie。"""
        await self._ensure_state_loaded()
        ready = self._enabled() and bool(self._session_id()) and bool(self._league_name())
        chat_subscribed = self._current_umo(event) in self._subscribers
        lines = [
            f"交易监控：{'运行中' if ready else '未配置或未启用'}",
            f"当前赛季：{self._league_name() or '未设置'}",
            f"POESESSID：{'已配置' if self._session_id() else '未配置'}",
            f"当前会话：{'已订阅' if chat_subscribed else '未订阅'}",
            f"订阅会话数：{len(self._subscribers)}",
            f"轮询间隔：{self._poll_interval_seconds() // 60} 分钟",
            f"最近成功检查：{self._last_success_at or '尚无'}",
        ]
        if self._last_error:
            lines.append(f"最近错误：{self._last_error}")
        yield event.plain_result("\n".join(lines))

    @filter.command("poe2交易历史")
    async def history(self, event: AstrMessageEvent):
        """查看最近成交；仅当前已订阅会话可查看。"""
        await self._ensure_state_loaded()
        umo = self._current_umo(event)
        if umo not in self._subscribers:
            yield event.plain_result("请先发送 /poe2交易订阅，再查看该账号的成交记录。")
            return
        if not self._trade_history:
            yield event.plain_result("目前还没有交易记录；首次成功检查会建立历史基线。")
            return
        lines = ["最近 PoE2 成交记录："]
        for trade in self._trade_history[:10]:
            item = trade.get("item") if isinstance(trade.get("item"), dict) else {}
            price = trade.get("price") if isinstance(trade.get("price"), dict) else {}
            name = item.get("name") or item.get("typeLine") or item.get("baseType") or "未知物品"
            amount = price.get("amount")
            currency = price.get("currency")
            value = f"{amount} {currency}".strip() if amount is not None else "价格未提供"
            when = trade.get("time") or "时间未提供"
            lines.append(f"- {name}｜{value}｜{when}")
        yield event.plain_result("\n".join(lines))
