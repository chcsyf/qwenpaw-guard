# -*- coding: utf-8 -*-
"""qwenpaw-guard REST API（挂载 /api/qwenpaw-guard/）。

所有端点入口都会把 request.app 缓存进 notifier（set_app），供审批/stop
钩子在 agent 忙碌期间进程内直调 channel_manager 发送通知——该场景下
HTTP 自呼本机 API 会因事件循环过载而超时（2026-08-26 排障结论）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from qwenpaw.loop.gates import StopHandlerRegistration
from qwenpaw.runtime.phases import Phase

from . import monitoring
from .constants import DEFAULT_CHANNEL, PLUGIN_ID, PLUGIN_NAME, PLUGIN_VERSION
from .hooks import GuardErrorNotifyHook, agent_stop_handler
from .notifier import (
    send_text,
    send_test_message,
    set_app,
    _ensure_session_topic,
    list_topic_chats,
)
from .store import store

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.router")

router = APIRouter()


def _mask(value: str) -> str:
    """掩码敏感串：仅保留首尾少许字符。"""
    if not value:
        return ""
    if len(value) <= 6:
        return value[0] + "****"
    return value[:4] + "****" + value[-2:]


def _resolve_workspace_dir(agent_id: str) -> str:
    """从配置解析 agent 的 workspace_dir；失败时回退默认路径。"""
    try:
        from qwenpaw.config.utils import load_config

        config = load_config()
        profiles = getattr(config.agents, "profiles", {}) or {}
        ref = profiles.get(agent_id)
        ws = str(getattr(ref, "workspace_dir", "") or "")
        if ws:
            return ws
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve workspace_dir failed: %s", exc)
    return f"/app/working/workspaces/{agent_id or 'default'}"


class ConfigBody(BaseModel):
    channel: str = ""
    target_user: str = ""
    target_session: str = ""
    feishu_topic_chat_id: str = ""
    send_mode: str = "reply"  # reply=回帖(复用专属主题帖) | new_topic=新话题
    enabled: bool | None = None
    notify_stop: bool | None = None
    notify_warn: bool | None = None
    timeout_secs: int | None = None


class ResolveBody(BaseModel):
    action: str  # approve | reject
    note: str = ""


class SessionActionBody(BaseModel):
    agent_id: str = ""
    session_id: str = ""
    channel: str = ""
    name: str = ""
    guarded: bool = True
    archived: bool = True


@router.get("/status")
async def get_status(request: Request) -> dict:
    """列表：所有 agent + 活动会话 + 待审批 + 配置 + 钩子装配诊断。"""
    set_app(request.app)
    data = monitoring.collect_status()
    data["config"] = store.get_config()
    data["pending"] = store.list_pending(50)
    data["guarded"] = store.list_guarded()
    data["archived"] = store.list_archived()
    data["plugin"] = {
        "id": PLUGIN_ID,
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
    }
    # 通知渠道检测：channel=feishu 时读取 agent.json 的 feishu 配置状态
    try:
        import json as _json
        from pathlib import Path as _P

        data["channel_status"] = {}
        cfg = data["config"]
        ch = (cfg.get("channel") or "").lower()
        data["channel_status"]["channel"] = ch
        data["channel_status"]["feishu_topic_chat_id"] = (
            cfg.get("feishu_topic_chat_id") or ""
        )
        if ch in ("feishu", "lark"):
            acf = _P(
                "/app/working/workspaces/default/agent.json"
            )
            if acf.exists():
                acfn = _json.loads(acf.read_text(encoding="utf-8"))
                fs = (acfn.get("channels") or {}).get("feishu", {})
                data["channel_status"]["configured"] = bool(
                    fs.get("app_id") and fs.get("app_secret")
                )
                data["channel_status"]["enabled"] = bool(fs.get("enabled"))
                data["channel_status"]["app_id"] = fs.get("app_id", "")
                data["channel_status"]["app_secret_masked"] = _mask(
                    fs.get("app_secret", "")
                )
            else:
                data["channel_status"]["configured"] = False
                data["channel_status"]["enabled"] = False
        else:
            data["channel_status"]["configured"] = bool(
                cfg.get("target_user") and cfg.get("target_session")
            )
            data["channel_status"]["enabled"] = cfg.get("enabled", False)
    except Exception as exc:  # noqa: BLE001
        data["channel_status"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 钩子装配诊断：stop_handlers / runtime hooks 是否真的挂上 workspace
    try:
        from qwenpaw.plugins.registry import PluginRegistry

        preg = PluginRegistry()
        mam = request.app.state.multi_agent_manager
        diag: dict = {
            "_workspace_manager_set": preg.get_workspace_manager()
            is not None,
        }
        for aid, ws in getattr(mam, "agents", {}).items():
            plugs = getattr(ws, "plugins", None)
            shs = (
                list(getattr(plugs, "stop_handlers", []) or [])
                if plugs
                else []
            )
            hr = getattr(plugs, "hook_registry", None) if plugs else None
            hooks_by_phase: dict = {}
            if hr is not None:
                by_phase = getattr(hr, "_by_phase", {}) or {}
                for phase, lst in by_phase.items():
                    hooks_by_phase[str(phase)] = [
                        getattr(h, "name", type(h).__name__)
                        for h in (lst or [])
                    ]
            diag[aid] = {
                "stop_handlers": [
                    getattr(h, "name", "?") for h in shs
                ],
                "runtime_hooks": hooks_by_phase,
            }
        data["diag"] = diag
    except Exception as exc:  # noqa: BLE001
        data["diag_error"] = f"{type(exc).__name__}: {exc}"
    return data


@router.post("/admin/rearm")
async def rearm(request: Request) -> dict:
    """手动把 guard 的 stop handler 与 ON_ERROR 钩子挂到当前所有 workspace。

    平台对热安装插件的 startup-hook 装配不总是生效（2026-08-26 排障），
    本端点直接操作 workspace.plugins 完成装配，幂等可重复调用。
    """
    set_app(request.app)
    reg_stop = StopHandlerRegistration(
        plugin_id=PLUGIN_ID,
        handler=agent_stop_handler,
        priority=110,
        name="qwenpaw_guard_stop_notify",
    )
    attached: dict = {}
    try:
        mam = request.app.state.multi_agent_manager
        for aid, ws in getattr(mam, "agents", {}).items():
            plugs = getattr(ws, "plugins", None)
            if plugs is None:
                attached[aid] = "no plugins object"
                continue
            # 1) stop handler（去重后追加）
            shs = [
                h
                for h in (getattr(plugs, "stop_handlers", []) or [])
                if getattr(h, "name", "") != "qwenpaw_guard_stop_notify"
            ]
            shs.append(reg_stop)
            plugs.stop_handlers = shs
            # 2) runtime ON_ERROR hook（去重后注册）
            hr = getattr(plugs, "hook_registry", None)
            hook_state = "no hook_registry"
            if hr is not None:
                by_phase = getattr(hr, "_by_phase", {}) or {}
                names = [
                    getattr(h, "name", "")
                    for h in by_phase.get(Phase.ON_ERROR, [])
                ]
                if "qwenpaw_guard_error_notify" not in names:
                    hr.register(GuardErrorNotifyHook())
                    hook_state = "registered"
                else:
                    hook_state = "already-registered"
            attached[aid] = {
                "stop_handler": "attached",
                "error_hook": hook_state,
            }
        return {"ok": True, "attached": attached}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "attached": attached}


@router.get("/config")
async def get_config(request: Request) -> dict:
    set_app(request.app)
    return {"ok": True, "config": store.get_config()}


@router.post("/config")
async def set_config(body: ConfigBody, request: Request) -> dict:
    set_app(request.app)
    updates: dict = {}
    for k in ("channel", "target_user", "target_session", "feishu_topic_chat_id", "send_mode"):
        v = getattr(body, k)
        if v:
            updates[k] = v
    for k in ("enabled", "notify_stop", "notify_warn", "timeout_secs"):
        v = getattr(body, k)
        if v is not None:
            updates[k] = v
    cfg = store.set_config(updates)
    return {"ok": True, "config": cfg}


@router.get("/discover")
async def discover(request: Request) -> dict:
    """发现通知渠道已有的会话，供配置 target_user / target_session。"""
    set_app(request.app)
    chats = monitoring.list_notify_chats()
    return {"ok": True, "channel": DEFAULT_CHANNEL, "chats": chats}


@router.get("/discover-topics")
async def discover_topics(request: Request) -> dict:
    """发现机器人已加入的全部**话题群**，供看板自动检测 feishu_topic_chat_id。

    插件要分发，feishu_topic_chat_id 默认为空，由这个接口自动探测、
    再经看板一键填入（首选唯一值）或下拉选择。
    """
    set_app(request.app)
    ok = True
    try:
        topics = await list_topic_chats()
    except Exception as exc:  # noqa: BLE001
        ok = False
        topics = []
        logger.warning("discover-topics failed: %s", exc)
    return {
        "ok": ok,
        "topics": topics,
        "current": store.get_config().get("feishu_topic_chat_id", ""),
    }


@router.post("/sessions/guard")
async def guard_session(body: SessionActionBody, request: Request) -> dict:
    """标记/取消守护一个会话。守护的会话状态会推通知。"""
    set_app(request.app)
    if not body.session_id or not body.agent_id:
        return {"ok": False, "error": "缺 session_id / agent_id"}
    meta = {
        "agent_id": body.agent_id,
        "session_id": body.session_id,
        "channel": body.channel,
        "name": body.name or "",
    }
    store.set_guarded(body.session_id, body.guarded, meta)
    store.log_event(
        {
            "kind": "session-guard",
            "agent_id": body.agent_id,
            "session_id": body.session_id,
            "guarded": body.guarded,
        }
    )
    # 首次守护时自动在飞书话题群开专属主题帖（best-effort，不阻塞/不报错）。
    # 仅在新守护（guarded=True）时创建；已存在的主题帖会在通知时复用。
    topic_msg = ""
    if body.guarded:
        try:
            tmeta = await _ensure_session_topic(body.agent_id, body.session_id)
            if tmeta:
                topic_msg = (
                    f"已开主题帖 thread={tmeta.get('feishu_thread_id', '')[:24]}"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "guard topic eager-create failed: %s", exc
            )
    return {"ok": True, "guarded": store.list_guarded(), "topic": topic_msg}


@router.post("/sessions/archive")
async def archive_session(body: SessionActionBody, request: Request) -> dict:
    """归档/取消归档一个会话。归档的会话从活动列表隐藏。"""
    set_app(request.app)
    if not body.session_id or not body.agent_id:
        return {"ok": False, "error": "缺 session_id / agent_id"}
    store.set_archived(
        body.session_id,
        body.archived,
        {
            "agent_id": body.agent_id,
            "session_id": body.session_id,
            "channel": body.channel,
            "name": body.name or "",
        },
    )
    store.log_event(
        {
            "kind": "session-archive",
            "agent_id": body.agent_id,
            "session_id": body.session_id,
            "archived": body.archived,
        }
    )
    return {"ok": True, "archived": store.list_archived()}


@router.post("/sessions/rename")
async def rename_session(body: SessionActionBody, request: Request) -> dict:
    """重命名一个会话（改 chats.json 里的 name）。"""
    set_app(request.app)
    if not body.session_id or not body.name:
        return {"ok": False, "error": "缺 session_id / name"}
    workspace_dir = _resolve_workspace_dir(body.agent_id)
    ok = monitoring.rename_session_chat(workspace_dir, body.session_id, body.name)
    if not ok:
        return {"ok": False, "error": "未找到该会话或重命名失败"}
    store.log_event(
        {
            "kind": "session-rename",
            "agent_id": body.agent_id,
            "session_id": body.session_id,
            "name": body.name,
        }
    )
    return {"ok": True}


@router.post("/sessions/delete")
async def delete_session(body: SessionActionBody, request: Request) -> dict:
    """删除一个会话：从 chats.json 移除 + 会话状态文件移入回收站。"""
    set_app(request.app)
    if not body.session_id:
        return {"ok": False, "error": "缺 session_id"}
    workspace_dir = _resolve_workspace_dir(body.agent_id)
    ok = monitoring.delete_session_chat(workspace_dir, body.session_id)
    if not ok:
        return {"ok": False, "error": "未找到该会话或删除失败"}
    # 同时取消该会话的守护/归档标记
    store.set_guarded(body.session_id, False)
    store.set_archived(body.session_id, False)
    store.log_event(
        {"kind": "session-delete", "agent_id": body.agent_id, "session_id": body.session_id}
    )
    return {"ok": True}


@router.get("/pending")
async def pending(request: Request, limit: int = 50) -> dict:
    set_app(request.app)
    return {"ok": True, "count": store.pending_count(), "pending": store.list_pending(limit)}


@router.post("/approvals/{request_id}/resolve")
async def resolve_approval(request_id: str, body: ResolveBody, request: Request) -> dict:
    """结清一条审批：body.action ∈ {approve, reject}（兼容中文 允许/拒绝）。"""
    set_app(request.app)
    action = (body.action or "").strip().lower()
    if action in ("approve", "allow", "允许", "同意", "yes"):
        decision = "approve"
    elif action in ("reject", "deny", "拒绝", "不同意", "禁止", "no"):
        decision = "reject"
    else:
        return {"ok": False, "error": "action 需为 approve/reject"}
    ok = store.resolve_pending(request_id, decision, body.note or "手动结清")
    if not ok:
        return {"ok": False, "error": "审批不存在或已结清"}
    return {"ok": True, "decision": decision}


@router.get("/admin/diag-stop")
async def diag_stop(request: Request) -> dict:
    """深度诊断：handler 注册表实况 + 直接执行 guard stop handler。"""
    set_app(request.app)
    from qwenpaw.plugins.registry import PluginRegistry
    from qwenpaw.app.agent_context import get_current_agent_id

    out: dict = {"current_agent_id": get_current_agent_id()}
    for aid in ("default", None):
        hs = PluginRegistry.get_stop_handlers(agent_id=aid)
        out[f"registry_handlers[{aid}]"] = [
            getattr(h, "name", "?") for h in hs
        ]

    class _Msg:  # 最小 final_msg 桩
        content = "diag-stop 测试消息"

    try:
        res = await agent_stop_handler(
            {
                "agent": None,
                "final_msg": _Msg(),
                "has_tool_calls": False,
                "iteration": 1,
            }
        )
        out["guard_handler_direct_run"] = repr(res)
    except Exception as exc:  # noqa: BLE001
        out["guard_handler_direct_run"] = f"EXC {type(exc).__name__}: {exc}"
    return out


@router.get("/events")
async def events(request: Request, limit: int = 100) -> dict:
    set_app(request.app)
    return {"ok": True, "events": store.recent_events(limit)}


@router.post("/test-send")
async def test_send(body: dict | None = None, request: Request = None) -> dict:
    """发送一条测试消息，验证通知连通性。

    若已配置话题群（feishu_topic_chat_id），消息会发进**话题群**（飞书自动开
    主题），以验证「多守护会话分别回复」链路可达；未配置话题群时退回通用
    固定目标单聊。
    """
    if request is not None:
        set_app(request.app)
    text = (body or {}).get("text") or "🛡️ 这是来自 qwenpaw-guard 的连通性测试消息。"
    result = await send_test_message(text)
    return result
