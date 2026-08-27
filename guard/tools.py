# -*- coding: utf-8 -*-
"""qwenpaw-guard 工具：供任意 agent（尤其飞书侧 agent）调用。

- guard_status      ：列出所有 agent 与活动会话（文本摘要）
- guard_resolve     ：结清一条飞书审批（允许/拒绝）
- guard_configure   ：配置通知频道目标（channel/target_user/target_session）
- guard_send        ：向配置的通知频道发送文本
这些工具在敏感规则里被豁免，不会触发自我拦截。
"""
from __future__ import annotations

import logging

from agentscope.tool._response import ToolChunk, ToolResultState
from agentscope.message import TextBlock

from . import monitoring
from .constants import DEFAULT_CHANNEL
from .notifier import send_text
from .store import store

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.tools")


def _tool_text_response(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.SUCCESS,
        content=[TextBlock(type="text", text=text)],
    )


def _err(text: str) -> ToolChunk:
    return ToolChunk(
        is_last=True,
        state=ToolResultState.ERROR,
        content=[TextBlock(type="text", text=text)],
    )


def _format_status() -> str:
    data = monitoring.collect_status()
    lines = [
        f"🧭 智能体守护状态（共 {data['total_agents']} 个 agent / "
        f"{data['total_sessions']} 个会话 / {data['total_active']} 个活动会话）\n"
    ]
    for a in data["agents"]:
        lines.append(
            f"· {a['id']}（{a['name']}）— 会话 {a['session_count']}，"
            f"活动 {a['active_count']}，运行中 {a['running_count']}"
            f"{'【已停用】' if not a['enabled'] else ''}"
        )
        for s in a["sessions"]:
            if not s["active"]:
                continue
            flag = "▶️" if s["status"] == "running" else "🟢"
            lines.append(
                f"    {flag} [{s['channel']}] {s['name']}"
                f"（{s['session_id']}）"
            )

    pending = store.list_pending(10)
    if pending:
        lines.append("")
        lines.append(f"⏳ 待审批 {store.pending_count()} 条：")
        for p in pending:
            lines.append(
                f"    · {p['request_id'][:8]} {p['agent_id']} -> "
                f"{p['tool_name']}（{p['reason']}）"
            )
    else:
        lines.append("")
        lines.append("⏳ 当前无待审批。")
    return "\n".join(lines)


async def guard_status() -> ToolChunk:
    """列出所有 agent 与活动会话，以及当前待审批。"""
    return _tool_text_response(_format_status())


async def guard_resolve(request_id: str, decision: str) -> ToolChunk:
    """结清一条飞书审批。decision ∈ {允许/approve, 拒绝/reject}。

    通常来自飞书会话里的审批回复；request_id 取审批消息里的请求 ID（可只给前 8 位）。
    """
    if not request_id:
        return _err("request_id 不能为空")
    req_id = _match_pending(request_id)
    if req_id is None:
        return _err(f"找不到待审批：{request_id}（可能已结清）")
    decision_l = (decision or "").strip().lower()
    if decision_l in ("allow", "approve", "允许", "同意", "yes", "y", "ok"):
        ok = store.resolve_pending(req_id, "approve", "飞书审批：允许")
        return _tool_text_response(f"✅ 已允许审批 {req_id[:8]}") if ok else _err(f"审批 {req_id[:8]} 不需要/无法结清")
    if decision_l in ("reject", "deny", "拒绝", "不同意", "禁止", "no", "n"):
        ok = store.resolve_pending(req_id, "reject", "飞书审批：拒绝")
        return _tool_text_response(f"❌ 已拒绝审批 {req_id[:8]}") if ok else _err(f"审批 {req_id[:8]} 不需要/无法结清")
    return _err("decision 需为允许/approve 或 拒绝/reject")


def _match_pending(request_id: str) -> str | None:
    """匹配待审批：精确或前 8 位前缀。"""
    request_id = request_id.strip()
    for p in store.list_pending(500):
        pid = p["request_id"]
        if pid == request_id or pid.startswith(request_id) or pid[:8] == request_id:
            return pid
    return None


async def guard_configure(
    target_user: str = "",
    target_session: str = "",
    channel: str = "",
    feishu_topic_chat_id: str = "",
    enabled: bool | None = None,
    notify_stop: bool | None = None,
) -> ToolChunk:
    """配置守护通知频道目标。target_user / target_session 取自 qwenpaw chats list --channel feishu。"""
    updates: dict = {}
    if target_user:
        updates["target_user"] = target_user
    if target_session:
        updates["target_session"] = target_session
    if channel:
        updates["channel"] = channel
    if feishu_topic_chat_id:
        updates["feishu_topic_chat_id"] = feishu_topic_chat_id
    if enabled is not None:
        updates["enabled"] = bool(enabled)
    if notify_stop is not None:
        updates["notify_stop"] = bool(notify_stop)
    cfg = store.set_config(updates)
    return _tool_text_response(
        "🛡️ 守护配置已更新：\n"
        f"- channel：{cfg.get('channel', DEFAULT_CHANNEL)}\n"
        f"- target_user：{cfg.get('target_user') or '(未设置)'}\n"
        f"- target_session：{cfg.get('target_session') or '(未设置)'}\n"
        f"- feishu_topic_chat_id：{cfg.get('feishu_topic_chat_id') or '(未设置)'}\n"
        f"- enabled：{cfg.get('enabled')}\n"
        f"- notify_stop：{cfg.get('notify_stop')}"
    )


async def guard_send(text: str) -> ToolChunk:
    """向配置的通知频道（飞书）发送一条文本。"""
    if not text:
        return _err("text 不能为空")
    result = await send_text(text)
    if result.get("ok"):
        return _tool_text_response("✅ 已发送到通知频道。")
    return _err(f"发送失败：{result.get('message')}")
