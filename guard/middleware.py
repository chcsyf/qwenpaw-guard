# -*- coding: utf-8 -*-
"""qwenpaw-guard 监督中间件：每次工具调用必经的「需要权限」钩子。

与 qwenpaw-team 的硬监督同构，但审批者从监督者 agent 改为「飞书会话」：
- 普通操作 → 放行 + 记事件日志
- warn 级 → 放行 + 记警告日志（外部副作用/强推/越权写）
- block 级 → 自动向飞书发审批请求，等待飞书回复：
    回复「允许」→ 放行；「拒绝」/ 超时 / 未配置飞书 → 默认拒绝（安全优先）
审批结果的结清由飞书侧 agent 调用 guard_resolve 工具完成。
"""
from __future__ import annotations

import logging
import re
from typing import Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolChunk, ToolResultState
from agentscope.message import TextBlock

from .constants import (
    DEFAULT_CHANNEL,
    LOG_IGNORE_TOOLS,
    SENSITIVE_RULES,
    SHELL_TOOLS,
)
from .store import store
from .notifier import format_approval_message, send_text

# 这些 agent 自己的通知跳过，防止自激循环
_IGNORE_REPLY_AGENTS = {"guardian", "qwenpaw-guard"}

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.middleware")


class GuardSupervisionMiddleware(MiddlewareBase):
    """守护监督中间件：记录 + 规则拦截 + 飞书审批路由 + 回合结束通知。"""

    def __init__(self, agent: Any = None) -> None:
        self._agent = agent

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        """回复流结束后的通知点。

        平台注册表里的 agent-stop-gate（QwenPawAgent._reasoning 尾部的
        ``_run_stop_handlers``）在 agentscope「收到最终 Msg 即 return」的
        架构下永远执行不到（2026-08-26 排障确认，属上游死代码），因此
        「会话停止→通知」改在本钩子实现：仅当 reply 流自然耗尽才
        走到这里；被 cancel 的异常场景由 ON_ERROR runtime hook 兜底。

        2026-08-27 调整：只对「守护的会话」通知，且附带最后回复内容。
        """
        async for item in next_handler(**(input_kwargs or {})):
            yield item
        # ---- reply 流正常结束 ----
        try:
            cfg = store.get_config()
            if not cfg.get("enabled", True):
                return
            aid = _resolve_agent_id(agent)
            if aid in _IGNORE_REPLY_AGENTS:
                return
            # 仅守护的会话才推通知
            from qwenpaw.app.agent_context import get_current_channel

            session_id = _resolve_session_id(agent)
            if not store.is_guarded(session_id, aid):
                return
            notify_channel = (
                cfg.get("channel") or DEFAULT_CHANNEL
            ).lower()
            origin = (get_current_channel() or "").lower()
            if not cfg.get("notify_stop", True):
                return
            last = _last_assistant_text(agent)
            tail = f"\n\n📄 最后回复：\n{last}" if last else ""
            store.log_event(
                {
                    "kind": "session-stop",
                    "agent_id": aid,
                    "session_id": session_id,
                    "origin": origin,
                    "sent": True,
                }
            )
            result = await send_text(
                f"✅ 【守护者】{aid}「{_safe_session_name(session_id, aid)}」"
                f" 会话回合已结束{tail}\n\n"
                "📩 如需继续 / 调整 / 澄清，可直接回复命令，我会处理。",
                session_id=session_id,
                agent_id=aid,
            )
            logger.info(
                "[qwenpaw-guard] session stop notified: %s session=%s "
                "send=%s tail_len=%d",
                aid,
                session_id,
                result,
                len(last),
            )
        except GeneratorExit:  # noqa: F821 - 流被外部关闭时不做任何 IO
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[qwenpaw-guard] session stop notify failed: %s",
                exc,
            )

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        tool_call = input_kwargs.get("tool_call")
        if tool_call is None:
            async for item in next_handler():
                yield item
            return

        tool_name = getattr(tool_call, "name", "?") or "?"
        tool_input = getattr(tool_call, "input", "") or ""
        agent_id = _resolve_agent_id(agent)

        verdict = _evaluate(tool_name, tool_input)

        # 记事件（噪音工具跳过）
        if tool_name not in LOG_IGNORE_TOOLS:
            store.log_event(
                {
                    "kind": "tool-call",
                    "agent_id": agent_id,
                    "tool": tool_name,
                    "args": _truncate(tool_input, 300),
                    "level": verdict["level"] if verdict else "info",
                    "rule": verdict["name"] if verdict else "",
                    "reason": verdict["reason"] if verdict else "",
                }
            )

        cfg = store.get_config()
        # 总开关关闭：直接放行（不拦截、不通知）
        if not cfg.get("enabled", True):
            async for item in next_handler():
                yield item
            return

        # 仅守护的会话才触发审批/通知；非守护会话的敏感操作不推飞书。
        session_id = _resolve_session_id(agent)
        guarded = store.is_guarded(session_id, agent_id)

        # block 级：仅守护会话唤起飞书审批；非守护直接拒绝（安全优先）。
        if verdict and verdict["level"] == "block":
            if not guarded:
                store.log_event(
                    {
                        "kind": "approval-skip",
                        "agent_id": agent_id,
                        "tool": tool_name,
                        "session_id": session_id,
                        "reason": "not-guarded",
                    }
                )
                yield ToolChunk(
                    is_last=True,
                    state=ToolResultState.DENIED,
                    content=[TextBlock(
                        type="text",
                        text=(
                            f"❌ 守护者拦截：{tool_name} 命中「{verdict['reason']}」"
                            f"（规则: {verdict['name']}）。\n"
                            "该会话未开启守护，不发起审批，默认拒绝。"
                        ),
                    )],
                )
                return
            decision, note = await _ask_approval(
                agent_id=agent_id,
                tool_name=tool_name,
                tool_input=tool_input,
                rule=verdict,
                session_id=session_id,
            )
            store.log_event(
                {
                    "kind": "approval",
                    "agent_id": agent_id,
                    "tool": tool_name,
                    "decision": decision,
                    "note": note,
                    "rule": verdict["name"],
                }
            )
            if decision == "allow":
                logger.info(
                    "[qwenpaw-guard] %s approved via feishu: %s(%s)",
                    agent_id,
                    tool_name,
                    _truncate(tool_input, 120),
                )
                async for item in next_handler():
                    yield item
                return
            yield ToolChunk(
                is_last=True,
                state=ToolResultState.DENIED,
                content=[TextBlock(
                    type="text",
                    text=(
                        f"❌ 守护者拦截：{tool_name} 命中「{verdict['reason']}」"
                        f"（规则: {verdict['name']}），飞书审批未通过。\n"
                        f"说明：{note}。如确需执行，请在飞书中回复「允许」后重试。"
                    ),
                )],
            )
            return

        # warn 级：放行（可选通知，仅守护会话才提醒）
        if verdict and verdict["level"] == "warn" and guarded and cfg.get("notify_warn", True):
            try:
                await send_text(
                    "⚠️ 【守护者警告】\n"
                    f"- 会话：{_safe_session_name(session_id, agent_id)}"
                    f"（agent：{agent_id}）\n"
                    f"- 工具：{tool_name}\n"
                    f"- 命中：{verdict['name']}（{verdict['reason']}）\n"
                    "（警告类操作已放行，仅记录）",
                    session_id=session_id,
                    agent_id=agent_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("guard warn notify failed: %s", exc)

        async for item in next_handler():
            yield item


# ---------------------------------------------------------------------------
# 飞书审批路由
# ---------------------------------------------------------------------------


async def _ask_approval(
    agent_id: str,
    tool_name: str,
    tool_input: str,
    rule: dict,
    session_id: str = "",
) -> tuple[str, str]:
    """向飞书发审批请求并等待结果。返回 (decision, note)。

    超时 / 飞书未配置 / 异常 → 默认拒绝（安全优先）。
    session_id 用于把审批通知发进该守护会话的专属主题帖。
    """
    request_id = store.create_pending(
        agent_id=agent_id,
        tool_name=tool_name,
        tool_input=tool_input,
        rule=rule,
    )
    msg = format_approval_message(request_id, agent_id, tool_name, tool_input, rule)
    sent = await send_text(msg, session_id=session_id, agent_id=agent_id)
    if not sent.get("ok"):
        store.resolve_pending(request_id, "timeout", "飞书通知发送失败，默认拒绝")
        return "reject", f"飞书通知发送失败（{sent.get('message', '未知')}），默认拒绝"

    decision, note = await store.wait_pending(request_id)
    return decision, note


def _resolve_agent_id(agent: Any) -> str:
    """从 agent 对象解析 agent_id（与 qwenpaw-team 同构）。"""
    if agent is None:
        return "?"
    rc = getattr(agent, "_request_context", None) or {}
    if rc.get("agent_id"):
        return rc["agent_id"]
    return getattr(agent, "name", "?") or "?"


def _safe_session_name(session_id: str, agent_id: str) -> str:
    """取会话的人类可读名称（回退 session_id），供通知文案使用。"""
    if not session_id:
        return session_id
    try:
        from .monitoring import resolve_session_name

        return resolve_session_name(session_id, agent_id) or session_id
    except Exception:  # noqa: BLE001
        return session_id


def _resolve_session_id(agent: Any) -> str:
    """解析当前会话 session_id：优先 contextvar，退回 agent._request_context。

    用于守护会话判定——on_reply / on_acting 阶段 get_current_session_id()
    可能尚未设置或已被消费，直接从 agent 上下文取更可靠。
    """
    try:
        from qwenpaw.app.agent_context import get_current_session_id

        sid = (get_current_session_id() or "").strip()
        if sid:
            return sid
    except Exception:  # noqa: BLE001
        pass
    rc = getattr(agent, "_request_context", None) or {}
    return (rc.get("session_id", "") or "").strip()


def _last_assistant_text(agent: Any) -> str:
    """读取 agent 会话历史里最后一条 assistant 回复的纯文本。"""
    try:
        ctx = getattr(getattr(agent, "state", None), "context", None)
        if not ctx:
            return ""
        msgs = ctx if isinstance(ctx, list) else list(ctx)
        for msg in reversed(msgs):
            if getattr(msg, "role", "") != "assistant":
                continue
            parts: list[str] = []
            for blk in (getattr(msg, "content", None) or []):
                if hasattr(blk, "text"):
                    t = blk.text
                    if t:
                        parts.append(t)
            if parts:
                return "\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[qwenpaw-guard] last_assistant_text failed: %s", exc)
    return ""


def _evaluate(tool_name: str, tool_input: str) -> dict | None:
    """对一次工具调用做敏感规则判定。返回命中规则 dict 或 None。"""
    if tool_name in LOG_IGNORE_TOOLS:
        return None
    lowered = tool_name.lower()
    is_shell = any(t in lowered for t in SHELL_TOOLS)
    for rule in SENSITIVE_RULES:
        pat = rule.get("pattern", "")
        if is_shell:
            keywords = rule.get("arg_keywords", ())
            if not keywords:
                continue
            if not any(k.lower() in tool_input.lower() for k in keywords):
                continue
            if pat:
                try:
                    if not re.search(pat, tool_input):
                        continue
                except re.error:
                    continue
        else:
            if not any(t.lower() in lowered for t in rule["tools"]):
                continue
            if pat:
                try:
                    if not re.search(pat, tool_input):
                        continue
                except re.error:
                    continue
        return rule
    return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
