# -*- coding: utf-8 -*-
"""qwenpaw-guard 钩子：会话结束/异常中断时自动向飞书发送通知。

两个互补的通知点：
1. agent_stop_handler（register_agent_stop_handler）：agent 正常完成回复
   （本轮无工具调用、final_msg 非空）时通知，避免中途误报。
2. GuardErrorNotifyHook（register_runtime_hook, ON_ERROR 阶段）：run 异常
   终止（工具取消、管线错误等意外中断）时通知——这类情况走不到 stop
   handler，在 run 异常中断时补发通知，避免漏通知。
守护侧可用 guard_resolve 处理命令。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from qwenpaw.loop.gates.base import StopAction, StopHandlerResult
from qwenpaw.runtime.hooks import HookBase, HookResult
from qwenpaw.runtime.phases import Phase

from .constants import PLUGIN_ID
from .notifier import send_text
from .store import store

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.hooks")

# 忽略的 agent/身份（避免给监控/守护自身的会话也发通知造成刷屏）
_IGNORE_AGENTS = {"guardian", "qwenpaw-guard"}


async def agent_stop_handler(ctx: dict[str, Any]) -> StopHandlerResult:
    """agent 停止钩子：通知飞书 + 记日志，返回默认 TERMINATE。"""
    # 默认：正常停止
    result = StopHandlerResult(action=StopAction.TERMINATE)
    try:
        agent = ctx.get("agent")
        agent_id = _resolve_agent_id(agent)
        logger.info(
            "[qwenpaw-guard] stop handler invoked: agent=%s "
            "has_tool_calls=%r final_msg=%r",
            agent_id,
            ctx.get("has_tool_calls"),
            ctx.get("final_msg") is not None,
        )
        # 本轮有工具调用（还没真正结束）不通知，避免中途误报
        if ctx.get("has_tool_calls") is True:
            return result
        final_msg = ctx.get("final_msg")
        if final_msg is None:
            return result
        if agent_id in _IGNORE_AGENTS:
            return result

        cfg = store.get_config()
        if not cfg.get("enabled", True) or not cfg.get("notify_stop", True):
            return result

        # 只对守护的会话推送（避免无关刷屏），并路由到其专属主题帖。
        session_id = _resolve_session_id(agent)
        if not store.is_guarded(session_id, agent_id):
            return result

        summary = _brief_message(final_msg)
        text = (
            "✅ 【守护者 · 会话结束】\n"
            f"- agent：{agent_id}\n"
            f"- 本轮结论：{summary}\n"
            "（如需要继续/调整，可直接回复命令。）"
        )
        sent = await send_text(text, session_id=session_id, agent_id=agent_id)
        store.log_event(
            {
                "kind": "session-stop",
                "agent_id": agent_id,
                "sent": sent.get("ok", False),
            }
        )
        logger.info("[qwenpaw-guard] session stop notified for %s", agent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[qwenpaw-guard] stop handler error: %s", exc)
    return result


class GuardErrorNotifyHook(HookBase):
    """run 异常终止时向飞书推送通知（runtime ON_ERROR 阶段）。

    agent_stop_handler 只覆盖「正常完成回复」；工具被取消、管线异常等
    意外中断走不到 stop handler，由本钩子兜底（2026-08-26 用户反馈补漏）。
    优先用 ctx.workspace 的 channel_manager 进程内直调，绕开 HTTP 自呼。
    """

    phase = Phase.ON_ERROR
    name = "qwenpaw_guard_error_notify"
    priority = 200

    async def run(self, _ctx: Any) -> HookResult:
        try:
            agent_id = (getattr(_ctx, "agent_id", "") or "?").strip()
            logger.info(
                "[qwenpaw-guard] error hook invoked: agent=%s error=%r",
                agent_id,
                getattr(_ctx, "error", None),
            )
            if agent_id in _IGNORE_AGENTS:
                return HookResult()
            cfg = store.get_config()
            if not cfg.get("enabled", True):
                return HookResult()

            session_id = (getattr(_ctx, "session_id", "") or "")[:60]
            # 仅守护的会话才通知
            if not store.is_guarded(session_id, agent_id):
                return HookResult()

            err = getattr(_ctx, "error", None)
            err_text = f"{type(err).__name__}: {err}" if err else "未知错误"
            if len(err_text) > 300:
                err_text = err_text[:300] + "…"

            # 用会话名称替代难懂的 session_id（通知一眼可辨哪个会话）。
            sname = session_id
            try:
                from .monitoring import resolve_session_name

                sname = resolve_session_name(session_id, agent_id) or session_id
            except Exception:  # noqa: BLE001
                pass

            text = (
                "❌ 【守护者 · 异常中断】\n"
                f"- agent：{agent_id}\n"
                f"- 会话：{sname}\n"
                f"- 错误：{err_text}\n"
                "（可回复命令让 agent 继续处理。）"
            )
            ws = getattr(_ctx, "workspace", None)
            ws_cm = getattr(ws, "channel_manager", None) if ws else None
            sent = await send_text(
                text, session_id=session_id, agent_id=agent_id,
                channel_manager=ws_cm,
            )
            store.log_event(
                {
                    "kind": "session-error",
                    "agent_id": agent_id,
                    "sent": bool(sent.get("ok")),
                    "error": err_text[:120],
                }
            )
            logger.info(
                "[qwenpaw-guard] error notify for %s: ok=%s",
                agent_id,
                sent.get("ok"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[qwenpaw-guard] error-notify hook failed: %s", exc
            )
        return HookResult()


class GuardThreadSessionRedirectHook(HookBase):
    """把飞书话题主题帖里的「回复」重定向回它守护的原始会话。

    飞书话题群里的回复默认按 chat_id 粒度新开一个会话，导致 agent 不知道
    之前被守护会话的上下文。本钩子在 PRE_DISPATCH（session.load 之前、
    slash 分发之前）按 thread_id 反查 guarded 记录，把 session_id 改写为
    被守护的原始会话，从而加载其历史上下文，实现「多守护会话分别回复」。

    仅对「属于某个守护 topic 的回复」生效；普通消息/非守护话题不受影响。
    """

    phase = Phase.PRE_DISPATCH
    name = "qwenpaw_guard_thread_session_redirect"
    priority = 5  # 早于其他 PRE_DISPATCH 钩子（contextvars_setup=10），
    # 这样 session.load / AgentBuilder 读到的 session_id 已是重定向值。

    async def run(self, ctx: Any) -> HookResult:
        try:
            thread_id = _extract_thread_id(ctx)
            if not thread_id:
                return HookResult()
            hit = store.resolve_thread_session(thread_id)
            if not hit:
                return HookResult()
            target_session_id, target_agent_id = hit
            # 重定向 session_id，使后续 session.load / AgentBuilder 加载
            # 该守护会话的历史上下文（历史按 session_id + agent_id 存储）。
            if target_session_id:
                ctx.session_id = target_session_id
                try:
                    ctx.request.session_id = target_session_id
                except Exception:  # noqa: BLE001
                    pass
            logger.info(
                "qwenpaw-guard: redirect thread %s -> session=%s agent=%s",
                (thread_id or "")[:24],
                target_session_id,
                target_agent_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard thread-redirect hook error: %s", exc
            )
        return HookResult()


def _extract_session_name(text: str) -> str:
    """从主题帖标题/文本里解析会话名称。

    守护主题帖标题固定形如「🛡️【守护主题】会话 default「Simple Addition」
    的守护通知将汇聚于此。」，会话名称被「」括起。逐个提取，返回第一个
    形如名称的非空片段；找不到返回空串。
    """
    if not text:
        return ""
    for m in re.finditer(r"「([^」]+)」", text):
        name = m.group(1).strip()
        if name:
            return name
    return ""


def guard_thread_resolver(
    thread_id: str,
    session_id: str,
    resolver_text: str = "",
) -> str | None:
    """飞书「话题帖会话解析」回调：把话题帖里的回复归回它守护的原始会话。

    采用「名称优先 + thread_id 兜底」的稳定映射，避免 thread_id 重建漂移：
      1) 优先从 resolver_text（主题帖标题等）解析会话名称（如「Simple
         Addition」），按名称反查 guarded 记录。
      2) 名称解析不到时，再按 thread_id 反查。
    命中返回被守护的 session_id；否则返回 None（保持飞书默认会话行为）。
    resolver_text 仅在插件内部用于名称解析，不会进入 agent 上下文。
    """
    # 1) 名称优先：名称稳定、用户可读，不随 thread_id 漂移。
    name = _extract_session_name(resolver_text or "")
    if name:
        by_name = store.resolve_session_by_name(name, agent_id="default")
        if by_name:
            logger.info(
                "guard_thread_resolver: name=%r -> HIT session=%s "
                "(target=%s)",
                name,
                session_id,
                by_name[0],
            )
            return by_name[0] or None

    # 2) thread_id 兜底。
    tid = (thread_id or "").strip()
    if not tid:
        return None
    hit = store.resolve_thread_session(tid)
    if hit:
        logger.info(
            "guard_thread_resolver: tid=%s -> HIT session=%s (target=%s)",
            tid,
            session_id,
            hit[0],
        )
        return hit[0] or None

    logger.info(
        "guard_thread_resolver: NO HIT name=%r tid=%s session=%s",
        name or "",
        tid,
        session_id,
    )
    return None


def guard_redirect(
    session_id: str,
    thread_id: str,
    resolver_text: str = "",
    native: dict | None = None,
) -> dict | None:
    """飞书「转投钩子」回调：把守护话题回帖转投到其守护的 Console 会话。

    与 guard_thread_resolver 同判定（名称优先 + thread_id 兜底反查 guarded），
    但目标是「整个消息转投」：命中被守护的会话时返回该会话的 console 目标，
    由飞书 channel 的 _redirect_to_console 以 console 身份处理、回复走控制台，
    消息**不**写回飞书话题。

    行为：飞书守护话题回帖 → 注入被守护的 console 会话 → 用控制台身份续上下文 →
    回复走控制台。目标会话动态反查（飞书回帖所在话题 → guarded 记录），不硬编码、
    不依赖已删除会话。

    命中返回 {"session_id": <守护console会话>, "agent_id": <守护agent>}；
    未命中返回 None（按飞书渠道正常处理，行为不变）。
    """
    # 1) 名称优先：稳定主键，不随 thread_id 漂移。
    name = _extract_session_name(resolver_text or "")
    target = None
    if name:
        by_name = store.resolve_session_by_name(name, agent_id="default")
        if by_name:
            target = by_name
    # 2) thread_id 兜底。
    if not target:
        tid = (thread_id or "").strip()
        if tid:
            target = store.resolve_thread_session(tid)
    if not target:
        logger.info(
            "guard_redirect: NO HIT name=%r tid=%s session=%s",
            name or "",
            thread_id or "",
            session_id,
        )
        return None
    logger.info(
        "guard_redirect HIT: session=%s => target_session=%s agent=%s",
        session_id,
        target[0],
        target[1],
    )
    return {
        "session_id": target[0] or session_id,
        "agent_id": target[1] or "default",
    }


def _extract_thread_id(ctx: Any) -> str:
    """从运行上下文里提取飞书话题 thread_id（优先 channel_meta/meta/metadata）。"""
    request = getattr(ctx, "request", None)
    if request is None:
        return ""
    for attr in ("channel_meta", "meta", "metadata"):
        m = getattr(request, attr, None)
        if isinstance(m, dict):
            tid = m.get("feishu_thread_id")
            if tid:
                return str(tid).strip()
    tid = getattr(request, "feishu_thread_id", None)
    return str(tid or "").strip()


def _resolve_agent_id(agent: Any) -> str:
    if agent is None:
        return "?"
    rc = getattr(agent, "_request_context", None) or {}
    if rc.get("agent_id"):
        return rc["agent_id"]
    return getattr(agent, "name", "?") or "?"


def _resolve_session_id(agent: Any) -> str:
    """解析当前会话 session_id：优先 contextvar，退回 agent._request_context。"""
    try:
        from qwenpaw.app.agent_context import get_current_session_id

        sid = (get_current_session_id() or "").strip()
        if sid:
            return sid
    except Exception:  # noqa: BLE001
        pass
    rc = getattr(agent, "_request_context", None) or {}
    return (rc.get("session_id", "") or "").strip()


def _brief_message(message: Any, limit: int = 200) -> str:
    """从 agent 最终消息里提取少量文本用于通知摘要。"""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        text = content
    else:
        texts: list[str] = []
        for block in content or []:
            block_type = (
                block.get("type")
                if isinstance(block, dict)
                else getattr(block, "type", None)
            )
            if block_type != "text":
                continue
            btext = (
                block.get("text", "")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
            if btext:
                texts.append(str(btext))
        text = "\n".join(texts)
    clean = (text or "").strip().replace("\n", " ")
    return clean if len(clean) <= limit else clean[:limit] + "…"
