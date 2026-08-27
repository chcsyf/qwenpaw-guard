# -*- coding: utf-8 -*-
"""qwenpaw-guard 监控：列出所有 agent 及各自的活动会话。

读取 config profiles 得到 agent 清单；每 agent 读取其 workspace 下的
chats.json 得到会话（不启动 agent 工作区，轻量）。

判活口径（2026-08-27 调整）：
- running  = status == "running"（正在执行）
- recent   = updated_at 距今 < RECENT_WINDOW_SECS（默认 2 天，供「最近两天」过滤）
- active   = running 或 recent（看板「活动会话」数用 active）
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qwenpaw.constant import WORKING_DIR

from .constants import ACTIVE_WINDOW_SECS

RECENT_WINDOW_SECS = int(
    os.environ.get("QWENPAW_GUARD_RECENT_WINDOW", 2 * 24 * 3600)
)

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard.monitoring")


def _iso_to_ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def load_agents() -> list[dict[str, Any]]:
    """从配置读取所有 agent 元数据（不启动工作区）。"""
    try:
        from qwenpaw.config.utils import load_config

        config = load_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("load_agents: load_config failed: %s", exc)
        return []
    profiles = getattr(config.agents, "profiles", {}) or {}
    out = []
    for agent_id, ref in profiles.items():
        out.append(
            {
                "id": agent_id,
                "name": getattr(ref, "name", agent_id) or agent_id,
                "description": getattr(ref, "description", ""),
                "enabled": getattr(ref, "enabled", True),
                "workspace_dir": str(getattr(ref, "workspace_dir", "")),
            }
        )
    return out


def _read_chats(workspace_dir: str) -> list[dict[str, Any]]:
    """读取 agent workspace 下的 chats.json（若无则空）。"""
    if not workspace_dir:
        return []
    path = Path(workspace_dir) / "chats.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("read chats.json failed (%s): %s", path, exc)
        return []
    if isinstance(data, dict):
        return data.get("chats", []) or []
    return []


def resolve_session_name(session_id: str, agent_id: str = "") -> str:
    """返回会话的人类可读名称（来自该 agent workspace 的 chats.json）。

    用于飞书通知里替代难懂的 session_id：
    - 命中 → chat["name"]（如「ok」），让通知一眼看出是哪个会话；
    - 名称缺失/默认 New Chat → 回退 session_id（保证非空可辨识）；
    - 找不到 → 回退 session_id。
    """
    if not session_id:
        return ""
    for a in load_agents():
        if agent_id and a.get("id") != agent_id:
            continue
        for c in _read_chats(a.get("workspace_dir", "")):
            if (c.get("session_id") or "") == session_id:
                name = (c.get("name") or "").strip()
                if name and name != "New Chat":
                    return name
                return session_id
        # 指定了 agent 且已在该 agent 下搜索无果 → 不必再遍历其它 agent
        if agent_id and a.get("id") == agent_id:
            break
    return session_id


def _session_view(chat: dict, agent_id: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc).timestamp()
    updated_ts = _iso_to_ts(chat.get("updated_at"))
    status = chat.get("status", "idle")
    running = status == "running"
    recent = bool(updated_ts and (now - updated_ts) < RECENT_WINDOW_SECS)
    # 活动会话 = 只检测正在运行的（2026-08-27 用户要求）
    active = running
    archived = False
    if agent_id:
        try:
            from .store import store
            archived = store.is_archived(chat.get("session_id", ""), agent_id)
        except Exception:  # noqa: BLE001
            archived = False
    return {
        "id": chat.get("id"),
        "name": chat.get("name", "New Chat"),
        "session_id": chat.get("session_id", ""),
        "user_id": chat.get("user_id", ""),
        "channel": chat.get("channel", ""),
        "status": status,
        "updated_at": chat.get("updated_at", ""),
        "active": active,
        "recent": recent,
        "running": running,
        "archived": archived,
    }


def collect_status() -> dict[str, Any]:
    """返回所有 agent 及其会话的结构化视图。"""
    agents = load_agents()
    result = []
    for a in agents:
        chats = _read_chats(a.get("workspace_dir", ""))
        sessions = [_session_view(c, a["id"]) for c in chats]
        active_count = sum(1 for s in sessions if s["active"])
        recent_count = sum(1 for s in sessions if s["recent"])
        running_count = sum(1 for s in sessions if s["running"])
        archived_count = sum(1 for s in sessions if s["archived"])
        result.append(
            {
                "id": a["id"],
                "name": a["name"],
                "description": a["description"],
                "enabled": a["enabled"],
                "session_count": len(sessions),
                "active_count": active_count,
                "recent_count": recent_count,
                "running_count": running_count,
                "archived_count": archived_count,
                "sessions": sessions,
            }
        )
    return {
        "agents": result,
        "total_agents": len(result),
        "total_sessions": sum(a["session_count"] for a in result),
        "total_active": sum(a["active_count"] for a in result),
    }


def delete_session_chat(workspace_dir: str, session_id: str) -> bool:
    """从 chats.json 移除一条会话记录，并把会话状态文件移入回收站。

    返回是否找到并处理了对应记录。
    """
    if not workspace_dir or not session_id:
        return False
    path = Path(workspace_dir) / "chats.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        chats = data.get("chats", []) if isinstance(data, dict) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: read chats failed (%s): %s", path, exc)
        return False
    kept = [c for c in chats if c.get("session_id") != session_id]
    if len(kept) == len(chats):
        return False  # 未找到该会话
    # 回写 chats.json
    try:
        if isinstance(data, dict):
            data["chats"] = kept
        else:
            data = {"chats": kept}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_session: write chats failed: %s", exc)
        return False
    # 会话状态文件移入回收站（可恢复而非永久删除）
    trash_dir = Path(WORKING_DIR) / "plugin_data" / "qwenpaw-guard" / "trash"
    sem_dir = Path(workspace_dir) / "sessions"
    if sem_dir.exists():
        for f in sem_dir.rglob("*.json"):
            if f.name.endswith("_" + session_id + ".json") or session_id in f.name:
                try:
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    dest = trash_dir / f.name
                    if not dest.exists():
                        f.replace(dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("delete_session: trash move failed: %s", exc)
    return True


def rename_session_chat(workspace_dir: str, session_id: str, name: str) -> bool:
    """重命名 chats.json 中一条会话的 name 字段。"""
    if not workspace_dir or not session_id or not name:
        return False
    path = Path(workspace_dir) / "chats.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        chats = data.get("chats", []) if isinstance(data, dict) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("rename_session: read failed: %s", exc)
        return False
    found = False
    for c in chats:
        if c.get("session_id") == session_id:
            c["name"] = name
            found = True
    if not found:
        return False
    try:
        if isinstance(data, dict):
            data["chats"] = chats
        else:
            data = {"chats": chats}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("rename_session: write failed: %s", exc)
        return False
    return True


def list_notify_chats() -> list[dict[str, Any]]:
    """扫描所有 agent 的通知渠道（飞书等）会话，供发现目标。"""
    seen: dict[str, dict[str, Any]] = {}
    for a in load_agents():
        chats = _read_chats(a.get("workspace_dir", ""))
        for c in chats:
            ch = (c.get("channel") or "").lower()
            if ch not in ("feishu", "lark"):
                continue
            sid = c.get("session_id", "")
            if not sid:
                continue
            seen[sid] = {
                "session_id": sid,
                "user_id": c.get("user_id", ""),
                "name": c.get("name", ""),
                "channel": c.get("channel", ""),
                "agent_id": a["id"],
                "updated_at": c.get("updated_at", ""),
            }
    return list(seen.values())


# 兼容旧名
# 发现飞书通知渠道请直接使用 list_notify_chats()
