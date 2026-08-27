# -*- coding: utf-8 -*-
"""qwenpaw-guard 通知：向配置的消息频道（默认飞书）推送文本。

发送策略（2026-08-26 排障结论）：
1. 首选**进程内直调** workspace.channel_manager.send_text —— 与
   /api/messages/send handler 内部完全同一函数。审批/stop 场景下
   default agent 正忙，本进程 HTTP 入站无法被处理（事件循环过载，
   参见 qwenpaw-team/notify_queue.py 同款结论），HTTP 自呼必超时；
   进程内协程直调不受影响。
2. app 引用不可得时（尚未有任何请求缓存）退回 HTTP httpx 异步调用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from .constants import DEFAULT_CHANNEL, SENDER_AGENT_ID
from .store import store

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.notifier")

# 由 router 在处理请求时缓存（request.app），供后台协程直调使用
_app_ref = None

# 飞书应用凭证：读取 default 工作区的 agent.json（与 router channel_status 同源）
_FEISHU_AGENT_JSON = Path("/app/working/workspaces/default/agent.json")
_token_cache = {"token": "", "exp": 0.0}


def set_app(app) -> None:
    """router 各端点在请求入口调用，缓存 FastAPI app 供直调。"""
    global _app_ref
    if _app_ref is not None:
        return
    _app_ref = app
    logger.info("notifier: app reference cached (direct-call enabled)")


# ---------------------------------------------------------------------------
# 飞书话题群 · 主题帖（多守护会话分别回复）
# ---------------------------------------------------------------------------


def _feishu_credentials() -> tuple[str, str]:
    """读取飞书 app_id/app_secret（default 工作区 agent.json）。"""
    try:
        if _FEISHU_AGENT_JSON.exists():
            data = json.loads(_FEISHU_AGENT_JSON.read_text(encoding="utf-8"))
            fs = (data.get("channels") or {}).get("feishu", {})
            app_id = (fs.get("app_id") or "").strip()
            app_secret = (fs.get("app_secret") or "").strip()
            if app_id and app_secret:
                return app_id, app_secret
    except Exception as exc:  # noqa: BLE001
        logger.debug("feishu credentials read failed: %s", exc)
    return "", ""


async def _get_tenant_token() -> str:
    """获取（并缓存）tenant_access_token。"""
    now = time.time()
    if _token_cache["token"] and _token_cache["exp"] > now:
        return _token_cache["token"]
    app_id, app_secret = _feishu_credentials()
    if not app_id or not app_secret:
        logger.warning("feishu token: 未配置 app_id/app_secret")
        return ""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/auth/v3/"
                "tenant_access_token/internal",
                data={"app_id": app_id, "app_secret": app_secret},
            )
            res = resp.json()
        if res.get("code") != 0:
            logger.warning("feishu token refused: %s", res.get("msg"))
            return ""
        tok = res.get("tenant_access_token") or ""
        exp = int(res.get("expire", 7200))
        _token_cache.update({"token": tok, "exp": now + exp - 60})
        return tok
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu token error: %s", exc)
        return ""


async def _feishu_create_topic(chat_id: str, text: str) -> tuple[str, str]:
    """往话题群发一条普通消息，由飞书自动开主题帖。

    返回 (message_id, thread_id)；失败返回 ("", "")。
    """
    token = await _get_tenant_token()
    if not token or not chat_id:
        return "", ""
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://open.feishu.cn/open-apis/im/v1/messages"
                "?receive_id_type=chat_id",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            res = resp.json()
        if res.get("code") != 0:
            logger.warning("feishu create topic failed: %s", res.get("msg"))
            return "", ""
        d = res.get("data") or {}
        return d.get("message_id") or "", d.get("thread_id") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu create topic error: %s", exc)
        return "", ""


async def list_topic_chats(max_pages: int = 5) -> list[dict]:
    """列出机器人已加入的所有**话题群**（chat_mode == 'topic'）。

    返回 ``[{chat_id, name}]``。该接口供看板「通知配置」卡片做**自动检测**，
    使用户免手填 ``feishu_topic_chat_id``（插件要分发，故不留硬编码默认值）。
    无凭证 / 调用失败返回 ``[]``。
    """
    token = await _get_tenant_token()
    if not token:
        return []
    items: list[dict] = []
    page_token = ""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            for _ in range(max(max_pages, 1)):
                params: dict = {"page_size": 100}
                if page_token:
                    params["page_token"] = page_token
                resp = await client.get(
                    "https://open.feishu.cn/open-apis/im/v1/chats",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                res = resp.json()
                if res.get("code") != 0:
                    logger.warning(
                        "feishu list chats failed: %s", res.get("msg")
                    )
                    break
                d = res.get("data") or {}
                for it in d.get("items", []):
                    if (it.get("chat_mode") or "").lower() == "topic":
                        items.append(
                            {
                                "chat_id": it.get("chat_id") or "",
                                "name": it.get("name") or "",
                            }
                        )
                page_token = d.get("page_token") or ""
                if not d.get("has_more") or not page_token:
                    break
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu list chats error: %s", exc)
        return items


def _thread_meta(topic_chat_id: str, thread_id: str, message_id: str) -> dict:
    """构造让飞书 channel 走「主题回帖」的 send meta。"""
    return {
        "feishu_receive_id": topic_chat_id,
        "feishu_receive_id_type": "chat_id",
        "feishu_thread_id": thread_id,
        "feishu_message_id": message_id,
    }


async def _feishu_reply(root_message_id: str, text: str) -> tuple[bool, str]:
    """回复进话题群**已有主题**（复用语主题帖根，不新建）。

    走飞书原生 reply 接口，确保通知落进守护会话的固定主题帖（用户可收到）。
    返回 (ok, message)。
    """
    token = await _get_tenant_token()
    if not token or not root_message_id:
        return False, "缺少 token 或主题根 message_id"
    url = (
        "https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{root_message_id}/reply"
    )
    payload = {
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "msg_type": "text",
        "reply_in_thread": True,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
            res = resp.json()
        if res.get("code") != 0:
            logger.warning("feishu reply failed: %s", res.get("msg"))
            return False, f"回复失败：{res.get('msg')}"
        return True, "已回复到话题群主题"
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu reply error: %s", exc)
        return False, str(exc)


async def _ensure_session_topic(agent_id: str, session_id: str) -> dict | None:
    """确保该守护会话有一个固定主题帖，返回 send meta 或 None。

    - 已存在 → 复用（存于 guarded 记录）；
    - 不存在 → 首次往话题群开主题，记录 thread_id + 根 message_id；
    - 未配置话题群 / 无飞书凭证 → 返回 None（交由通用固定目标兜底）。
    """
    if not agent_id or not session_id:
        return None
    cfg = store.get_config()
    topic_chat_id = (cfg.get("feishu_topic_chat_id") or "").strip()
    if not topic_chat_id:
        return None
    # 已有主题帖则直接复用
    rec = store.get_guarded(session_id, agent_id)
    if rec:
        mid = (rec.get("feishu_message_id") or "").strip()
        tid = (rec.get("thread_id") or "").strip()
        if mid and tid:
            return _thread_meta(topic_chat_id, tid, mid)
    # 首次：自动开主题（只建一次）
    # 用会话名称替代难懂的 session_id（守卫通知一眼可辨哪个会话）。
    try:
        from .monitoring import resolve_session_name

        sname = resolve_session_name(session_id, agent_id)
    except Exception:  # noqa: BLE001
        sname = session_id
    sname = sname or session_id
    label = f"{agent_id}「{sname}」"
    mid, tid = await _feishu_create_topic(
        topic_chat_id,
        f"🛡️【守护主题】会话 {label} 的守护通知将汇聚于此。\n"
        "在这里回复，可继续处理该会话。",
    )
    if not mid or not tid:
        logger.warning("session topic 创建失败：%s", label)
        return None
    store.update_guarded_topic(session_id, agent_id, tid, mid)
    logger.info(
        "session topic 已创建：%s thread=%s message=%s",
        label,
        tid[:20],
        mid[:24],
    )
    return _thread_meta(topic_chat_id, tid, mid)


async def send_test_message(text: str) -> dict:
    """发送一条测试消息到**话题群**（飞书自动开主题）。

    仅话题群通道；未配置话题群则返失败（不再退回固定单聊）。
    用于看板「测试发送」验证话题群链路。
    """
    cfg = store.get_config()
    topic_chat_id = (cfg.get("feishu_topic_chat_id") or "").strip()
    if not topic_chat_id:
        return {
            "ok": False,
            "message": "未配置飞书话题群（feishu_topic_chat_id），无法发送",
        }
    mid, tid = await _feishu_create_topic(topic_chat_id, text)
    if mid:
        return {
            "ok": True,
            "message": f"已发送到话题群（thread_id={tid or ''}）。"
            "在该主题下回复可测试守护续会话。",
        }
    return {
        "ok": False,
        "message": "话题群发送失败，请检查 feishu_topic_chat_id 是否有效、"
        "机器人是否在群内。",
    }


async def send_text(
    text: str,
    *,
    channel: str | None = None,
    channel_manager=None,
    session_id: str | None = None,
    agent_id: str | None = None,
    **_,
) -> dict:
    """向**飞书话题群**发送一条文本（统一话题群通道，不回退单聊）。

    - 带 session_id + agent_id（守护会话）：复用该会话在话题群里的专属主题帖
      （`_feishu_reply` 回复进主题根），实现多守护会话分别回复、且可收到。
    - 否则（通用 / guard_send / 测试）：`_feishu_create_topic` 直发话题群，
      由飞书自动开主题。
    - 未配置 ``feishu_topic_chat_id``：直接失败（不再向固定单聊兜底）。
    """
    cfg = store.get_config()
    if not cfg.get("enabled", True):
        return {"ok": False, "message": "guard 总开关关闭"}
    topic_chat_id = (cfg.get("feishu_topic_chat_id") or "").strip()
    if not topic_chat_id:
        return {
            "ok": False,
            "message": "未配置飞书话题群（feishu_topic_chat_id），无法发送",
        }

    # --- 守护会话 → 通知模式（回帖 reply / 新话题 new_topic）---
    if session_id and agent_id:
        mode = (cfg.get("send_mode") or "reply").strip()
        # 「新话题」模式：每次直发话题群，由飞书自动开主题（不 reply）。
        if mode == "new_topic":
            mid, tid = await _feishu_create_topic(topic_chat_id, text)
            if mid:
                return {
                    "ok": True,
                    "message": f"已作为新话题发送（thread_id={tid or ''}）",
                    "mode": "new_topic",
                }
            return {"ok": False, "message": "话题群发送失败"}
        # 「回帖」模式（默认）：复用该会话的专属主题帖（主题根 reply）。
        topic = await _ensure_session_topic(agent_id, session_id)
        if topic:
            root_mid = (topic.get("feishu_message_id") or "").strip()
            if root_mid:
                ok, msg = await _feishu_reply(root_mid, text)
                if ok:
                    return {"ok": True, "message": msg, "mode": "reply"}
                logger.warning(
                    "guard reply into session topic failed: %s", msg
                )
        # 回帖失败（主题根可能被删）→ 自动降级为新话题重建主题并写回记录，
        # 之后该会话的通知将复用新主题根，避免再次失败。
        mid, tid = await _feishu_create_topic(topic_chat_id, text)
        if mid:
            try:
                store.update_guarded_topic(session_id, agent_id, tid, mid)
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": True,
                "message": f"原主题已失效，已作为新话题重建（thread_id={tid or ''}）",
                "mode": "new_topic",
            }
        return {"ok": False, "message": "话题群发送失败"}

    # --- 通用：直发话题群，自动开主题 ---
    mid, tid = await _feishu_create_topic(topic_chat_id, text)
    if mid:
        return {"ok": True, "message": f"已发送到话题群（thread_id={tid or ''}）"}
    return {"ok": False, "message": "话题群发送失败"}


def format_approval_message(
    request_id: str,
    agent_id: str,
    tool_name: str,
    tool_input: str,
    rule: dict,
) -> str:
    """构造审批请求的飞书消息文本。"""
    params = tool_input if len(tool_input) <= 400 else tool_input[:400] + "…"
    return (
        "⚠️ 【需要你的审批】\n"
        f"- 会话/agent：{agent_id}\n"
        f"- 工具：{tool_name}\n"
        f"- 参数：{params}\n"
        f"- 命中规则：{rule.get('name', '?')}（{rule.get('reason', '?')}）\n"
        f"- 请求ID：`{request_id}`\n\n"
        "回复「允许」或「拒绝」，可附一句理由；如需直接放行请回复"
        f"「允许 {request_id[:8]}」。超时为默认拒绝（安全优先）。"
    )
