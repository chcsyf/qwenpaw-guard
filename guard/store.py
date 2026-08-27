# -*- coding: utf-8 -*-
"""qwenpaw-guard 存储：配置 + 待审批 + 事件日志。

- 配置：plugin_data/qwenpaw-guard/config.json
- 待审批：进程内 dict（含 asyncio.Future，不落盘，仅 log 决策）
- 事件日志：plugin_data/qwenpaw-guard/events.jsonl（追加）
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from qwenpaw.constant import WORKING_DIR

from .constants import APPROVAL_TIMEOUT_SECS, DEFAULT_CHANNEL, PLUGIN_ID

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.store")

_BASE = Path(WORKING_DIR) / "plugin_data" / PLUGIN_ID
_CONFIG_PATH = _BASE / "config.json"
_EVENTS_PATH = _BASE / "events.jsonl"
_GUARDED_PATH = _BASE / "guarded.json"
_ARCHIVED_PATH = _BASE / "archived.json"

_DEFAULT_CONFIG = {
    "channel": DEFAULT_CHANNEL,
    "target_user": "",
    "target_session": "",
    "timeout_secs": APPROVAL_TIMEOUT_SECS,
    "enabled": True,          # 总开关：False = 关闭审批拦截与通知
    "notify_stop": True,      # 会话结束时是否通知
    "notify_warn": True,      # warn 级副作用是否通知（默认只记日志）
    # 飞书「话题群」chat_id（oc_xxx）：开启后每个守护会话的通知都会进
    # 各自专有的主题帖，用户可在对应主题下回复分别续任务。
    # 插件要分发，因此**不硬编码默认值**，留空，由看板「自动检测」填入。
    "feishu_topic_chat_id": "",
    # 飞书通知发送模式（仅对「守护会话」通知生效）：
    #   reply     —— 回帖：复用该会话的专属主题帖（主题根），通知回进同一主题（默认）
    #   new_topic —— 新话题：每次直发话题群，由飞书自动开一个新主题
    # 说明：若主题根被删除，reply 模式会自动降级为「新话题」重建主题并写回记录。
    "send_mode": "reply",
}

_VALID_SEND_MODES = {"reply", "new_topic"}


class Store:
    """单例存储：线程安全（RLock），待审批用 asyncio Future。"""

    def __init__(self) -> None:
        _BASE.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._config: dict = self._load_config()
        self._guarded: dict[str, dict] = self._load_guarded()
        self._archived: dict[str, dict] = self._load_archived()
        self._events_path = _EVENTS_PATH

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _load_config(self) -> dict:
        try:
            if _CONFIG_PATH.exists():
                data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = dict(_DEFAULT_CONFIG)
                    merged.update(data)
                    return merged
        except Exception as exc:  # noqa: BLE001
            logger.warning("config load failed: %s", exc)
        return dict(_DEFAULT_CONFIG)

    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def set_config(self, updates: dict) -> dict:
        with self._lock:
            for k, v in updates.items():
                if k not in _DEFAULT_CONFIG or v is None:
                    continue
                if k == "send_mode" and v not in _VALID_SEND_MODES:
                    continue
                self._config[k] = v
            try:
                _CONFIG_PATH.write_text(
                    json.dumps(self._config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("config save failed: %s", exc)
            return dict(self._config)

    # ------------------------------------------------------------------
    # 待审批（内存）
    # ------------------------------------------------------------------
    def create_pending(
        self,
        *,
        agent_id: str,
        tool_name: str,
        tool_input: str,
        rule: dict,
    ) -> str:
        """登记一条待审批，返回 request_id。须在 asyncio 运行循环内调用。"""
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        with self._lock:
            self._pending[request_id] = {
                "request_id": request_id,
                "agent_id": agent_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "rule_name": rule.get("name", ""),
                "reason": rule.get("reason", ""),
                "created_at": time.time(),
                "status": "pending",
                "future": loop.create_future(),
                "decision": "",
                "note": "",
            }
        logger.info(
            "guard approval pending: %s agent=%s tool=%s rule=%s",
            request_id[:8],
            agent_id,
            tool_name,
            rule.get("name", "?"),
        )
        return request_id

    def get_pending(self, request_id: str) -> dict | None:
        with self._lock:
            p = self._pending.get(request_id)
            return dict(p) if p else None

    def list_pending(self, limit: int = 50) -> list[dict]:
        with self._lock:
            items = sorted(
                self._pending.values(),
                key=lambda p: p["created_at"],
                reverse=True,
            )
            out = []
            for p in items:
                d = dict(p)
                d.pop("future", None)
                out.append(d)
            return out[:limit]

    def resolve_pending(
        self,
        request_id: str,
        decision: str,
        note: str = "",
    ) -> bool:
        """结清一条待审批。decision ∈ {approve, reject, timeout}。

        返回是否成功（找不到/已结清返回 False）。
        """
        with self._lock:
            p = self._pending.get(request_id)
            if p is None or p["status"] != "pending":
                return False
            p["status"] = "resolved"
            p["decision"] = decision
            p["note"] = note
            p["resolved_at"] = time.time()
            future = p.get("future")
        if future and not future.done():
            future.set_result(decision)
        self.log_event(
            {
                "kind": "approval-resolved",
                "request_id": request_id,
                "agent_id": p["agent_id"],
                "tool": p["tool_name"],
                "decision": decision,
                "note": note,
            }
        )
        logger.info("guard approval resolved: %s -> %s", request_id[:8], decision)
        return True

    async def wait_pending(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        """等待审批结果，返回 (decision, note)；超时自动置为 timeout 默认拒绝。"""
        p = self.get_pending(request_id)
        if p is None:
            return "reject", "审批请求不存在"
        fut = None
        with self._lock:
            rec = self._pending.get(request_id)
            if rec:
                fut = rec.get("future")
        if fut is None:
            return "reject", "审批请求无 future"
        timeout = timeout or self._config.get("timeout_secs", APPROVAL_TIMEOUT_SECS)
        try:
            decision = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.resolve_pending(request_id, "timeout", "审批超时，默认拒绝")
            return "reject", "审批超时，默认拒绝"
        note = ""
        with self._lock:
            note = (self._pending.get(request_id) or {}).get("note", "")
        if decision == "approve":
            return "allow", note or "(允许)"
        return "reject", note or "(未回复/已拒绝)"

    def cancel_all_pending(self) -> int:
        """取消全部待审批（例如停止/卸载时）。"""
        ids = list(self._pending.keys())
        n = 0
        for rid in ids:
            if self.resolve_pending(rid, "timeout", "取消待审批"):
                n += 1
        return n

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._pending.values() if p["status"] == "pending")

    # ------------------------------------------------------------------
    # 守护会话（持久化）
    # ------------------------------------------------------------------
    def _load_guarded(self) -> dict[str, dict]:
        try:
            if _GUARDED_PATH.exists():
                data = json.loads(_GUARDED_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("guarded load failed: %s", exc)
        return {}

    def _save_guarded(self) -> None:
        try:
            _GUARDED_PATH.write_text(
                json.dumps(self._guarded, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("guarded save failed: %s", exc)

    def set_guarded(
        self,
        session_id: str,
        guarded: bool,
        meta: dict | None = None,
    ) -> dict:
        """标记/取消守护一个会话。返回更新后的 guarded 集合。

        meta 提供 agent_id/channel/user_id/name 用于展示与通知。
        """
        key = (meta or {}).get("agent_id", "") + ":" + session_id
        with self._lock:
            if guarded:
                if key in self._guarded:
                    self._guarded[key].update(meta or {})
                else:
                    self._guarded[key] = dict(meta or {})
                self._guarded[key].update(
                    {"session_id": session_id, "guarded": True}
                )
            else:
                # 取消守护：按 session_id 匹配清除（兼容未携带 agent_id 的调用）
                self._guarded.pop(key, None)
                if meta and meta.get("agent_id"):
                    self._guarded.pop(meta["agent_id"] + ":" + session_id, None)
                for _k in [k for k, v in self._guarded.items() if v.get("session_id") == session_id]:
                    self._guarded.pop(_k, None)
            self._save_guarded()
            return dict(self._guarded)

    def is_guarded(self, session_id: str, agent_id: str = "") -> bool:
        with self._lock:
            if agent_id and (agent_id + ":" + session_id) in self._guarded:
                return True
            return any(
                v.get("session_id") == session_id for v in self._guarded.values()
            )

    def get_guarded(self, session_id: str, agent_id: str = "") -> dict | None:
        """取某会话的守护记录（含 thread_id / feishu_message_id 等）。"""
        with self._lock:
            if agent_id:
                rec = self._guarded.get(agent_id + ":" + session_id)
                if rec:
                    return dict(rec)
            for v in self._guarded.values():
                if v.get("session_id") == session_id:
                    return dict(v)
        return None

    def update_guarded_topic(
        self,
        session_id: str,
        agent_id: str,
        thread_id: str,
        message_id: str,
    ) -> dict:
        """记录某守护会话的飞书主题帖（thread_id + 主题根 message_id）。

        只创建/更新一次，之后该会话的通知都复用这个固定主题。
        """
        key = agent_id + ":" + session_id
        with self._lock:
            rec = self._guarded.get(key)
            if rec is None:
                rec = {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "guarded": True,
                }
                self._guarded[key] = rec
            rec["thread_id"] = thread_id
            rec["feishu_message_id"] = message_id
            self._save_guarded()
            logger.info(
                "guarded topic recorded: %s thread=%s message=%s",
                key,
                (thread_id or "")[:24],
                (message_id or "")[:24],
            )
            return dict(rec)

    def resolve_thread_session(self, thread_id: str) -> tuple[str, str] | None:
        """按飞书话题 thread_id 反查被守护的原始会话。

        用于把「话题主题帖里的回复」重定向回它对应的守护会话
        （session_id, agent_id），从而让该会话的历史上下文被加载，
        实现「多守护会话分别回复」。
        """
        tids = (thread_id or "").strip()
        if not tids:
            return None
        # 归一化：截取尾部作为宽松匹配（飞书 thread_id 可能整段或短码）
        tail = tids[-10:].lower()
        with self._lock:
            for _k, rec in self._guarded.items():
                r_tid = (rec.get("thread_id") or "").strip()
                if not r_tid:
                    continue
                if r_tid == tids or (tail and r_tid[-10:].lower() == tail):
                    sid = rec.get("session_id") or ""
                    aid = rec.get("agent_id") or ""
                    if sid:
                        return (sid, aid)
        return None

    def resolve_session_by_name(self, name: str, agent_id: str = "") -> tuple[str, str] | None:
        """按会话名称反查被守护的原始会话。

        会话名称是稳定、用户可读的标识（如「Simple Addition」），
        不随飞书话题帖 thread_id 重建而漂移，作为 thread_id 之外
        的可靠映射依据。返回 (session_id, agent_id) 或 None。
        """
        nm = (name or "").strip().lower()
        if not nm:
            return None
        with self._lock:
            for _k, rec in list(self._guarded.items()):
                r_name = (rec.get("name") or "").strip().lower()
                if not r_name:
                    continue
                if r_name == nm:
                    sid = rec.get("session_id") or ""
                    if sid:
                        return (sid, rec.get("agent_id") or "")
        return None

    def list_guarded(self) -> list[dict]:
        with self._lock:
            return list(self._guarded.values())

    # ------------------------------------------------------------------
    # 归档会话（持久化）
    # ------------------------------------------------------------------
    def _load_archived(self) -> dict[str, dict]:
        try:
            if _ARCHIVED_PATH.exists():
                data = json.loads(_ARCHIVED_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("archived load failed: %s", exc)
        return {}

    def _save_archived(self) -> None:
        try:
            _ARCHIVED_PATH.write_text(
                json.dumps(self._archived, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("archived save failed: %s", exc)

    def set_archived(
        self,
        session_id: str,
        archived: bool,
        meta: dict | None = None,
    ) -> dict:
        """标记/取消归档一个会话。返回更新后的 archived 集合。"""
        key = (meta or {}).get("agent_id", "") + ":" + session_id
        with self._lock:
            if archived:
                if key in self._archived:
                    self._archived[key].update(meta or {})
                else:
                    self._archived[key] = dict(meta or {})
                self._archived[key].update(
                    {"session_id": session_id, "archived": True}
                )
            else:
                self._archived.pop(key, None)
                if meta and meta.get("agent_id"):
                    self._archived.pop(meta["agent_id"] + ":" + session_id, None)
                for _k in [
                    k
                    for k, v in self._archived.items()
                    if v.get("session_id") == session_id
                ]:
                    self._archived.pop(_k, None)
            self._save_archived()
            return dict(self._archived)

    def is_archived(self, session_id: str, agent_id: str = "") -> bool:
        with self._lock:
            if agent_id and (agent_id + ":" + session_id) in self._archived:
                return True
            return any(
                v.get("session_id") == session_id for v in self._archived.values()
            )

    def list_archived(self) -> list[dict]:
        with self._lock:
            return list(self._archived.values())

    # ------------------------------------------------------------------
    # 事件日志
    # ------------------------------------------------------------------
    def log_event(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        try:
            with self._events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning("event log failed: %s", exc)

    def recent_events(self, limit: int = 100) -> list[dict]:
        if not self._events_path.exists():
            return []
        try:
            lines = self._events_path.read_text(encoding="utf-8").splitlines()
        except Exception:  # noqa: BLE001
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        return list(reversed(out))


# 模块级单例
store = Store()
