# -*- coding: utf-8 -*-
"""qwenpaw-guard 插件入口：AI 守护者（监督看护工具）。

能力（v0.0.1）：
- 列出所有 agent 与活动会话：GET /api/qwenpaw-guard/status、看板 UI
- 会话结束钩子：register_agent_stop_handler → 自动向飞书推送「会话结束」通知
- 需要权限钩子：register_middleware（on_acting）拦截敏感工具调用 →
  向飞书发审批请求，飞书回复（guard_resolve / REST）允许/拒绝，超时默认拒绝
- 飞书侧命令：guard_status / guard_resolve / guard_configure / guard_send 工具，
  供绑定飞书的 agent 使用；也可走 REST 接口
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from qwenpaw.plugins import PluginApi

from .guard.constants import PLUGIN_ID
from .guard.hooks import (
    GuardErrorNotifyHook,
    agent_stop_handler,
    guard_redirect,
    guard_thread_resolver,
)
from .guard.middleware import GuardSupervisionMiddleware
from .guard.router import router as guard_router
from .guard.tools import (
    guard_configure,
    guard_resolve,
    guard_send,
    guard_status,
)

logger = logging.getLogger("qwenpaw").getChild("qwenpaw-guard")


def _start_guard_cleanup() -> None:
    """启动钩子：无操作占位（预留清理待审批）。"""
    # store 为进程级单例，无需清理；真正清理放在 shutdown
    logger.info("qwenpaw-guard started")


class GuardPlugin:
    """qwenpaw-guard 插件：register(api) 由 PluginLoader 调用。"""

    def __init__(self) -> None:
        self.api: PluginApi | None = None

    def register(self, api: PluginApi) -> None:
        self.api = api

        # 会话结束钩子（所有 agent 生效）
        api.register_agent_stop_handler(
            handler=agent_stop_handler,
            priority=110,
            name="qwenpaw_guard_stop_notify",
        )

        # 异常中断钩子：run 以错误终止（工具取消/管线异常）时推飞书。
        # 与 stop handler 互补——异常路径走不到 stop gate。
        api.register_runtime_hook(GuardErrorNotifyHook())

        # 话题帖回复 → 归回守护会话（在飞书 channel 消息源头覆盖 session_id）。
        # 通过「通用 thread-resolver 扩展点」注入：加载插件时注册回调，
        # 卸载插件时注销（uninstall hook），从而可逆、不破坏核心默认行为。
        # 注：旧方案 GuardThreadSessionRedirectHook（PRE_DISPATCH 重定向）来得太晚
        # （Envelope 已用飞书会话 id 建好、消息不落库进守护会话），已弃用。
        api.register_startup_hook(
            hook_name="guard_inject_thread_resolver",
            callback=self._inject_thread_resolver,
            priority=100,
        )
        api.register_uninstall_hook(
            hook_name="guard_remove_thread_resolver",
            callback=self._remove_thread_resolver,
            priority=100,
        )

        # 「转投钩子」：飞书守护话题回帖 → 注入被守护的 console 会话 →
        # 用控制台身份续上下文 → 回复走控制台。
        # 与 thread-resolver 同点注册/注销，可逆、不破坏飞书默认行为。
        api.register_startup_hook(
            hook_name="guard_inject_redirect_hook",
            callback=self._inject_redirect_hook,
            priority=110,
        )
        api.register_uninstall_hook(
            hook_name="guard_remove_redirect_hook",
            callback=self._remove_redirect_hook,
            priority=110,
        )

        # 需要权限钩子：监督中间件（所有 agent 生效）
        api.register_middleware(
            lambda ctx, agent_config: GuardSupervisionMiddleware(),
            priority=50,
        )

        # 飞书侧可用的 guard_* 工具（默认对所有 agent 可见；在敏感规则里被豁免）
        api.register_tool(
            tool_name="guard_status",
            tool_func=guard_status,
            description=(
                "列出所有 agent 与活动会话，以及当前待审批。"
                "用于查看智能体守护状态。"
            ),
            icon="🧭",
            enabled=True,
            tool_type="internal",
        )
        api.register_tool(
            tool_name="guard_resolve",
            tool_func=guard_resolve,
            description=(
                "结清一条飞书审批：request_id 取审批消息里的请求 ID（可只给前 8 位），"
                "decision ∈ 允许/approve 或 拒绝/reject。"
            ),
            icon="✅",
            enabled=True,
            tool_type="internal",
            target_param="request_id",
        )
        api.register_tool(
            tool_name="guard_configure",
            tool_func=guard_configure,
            description=(
                "配置守护通知频道目标：target_user / target_session 取自 "
                "qwenpaw chats list --channel feishu 的结果。"
            ),
            icon="⚙️",
            enabled=True,
            tool_type="internal",
            target_param="target_user",
        )
        api.register_tool(
            tool_name="guard_send",
            tool_func=guard_send,
            description="向配置的通知频道（默认飞书）发送一条文本。",
            icon="📤",
            enabled=True,
            tool_type="internal",
            target_param="text",
        )

        # REST API（prefix 为 /api 下的路径）
        api.register_http_router(guard_router, prefix=f"/{PLUGIN_ID}")

    # ------------------------------------------------------------------
    # 话题帖会话解析扩展点：加载注入 / 卸载还原
    # ------------------------------------------------------------------
    @staticmethod
    async def _get_feishu_channel():
        """获取 default agent workspace 的飞书 channel 实例（供注册解析器）。

        通过插件单例 registry → workspace_manager → agents["default"]
        → channel_manager → get_channel("feishu")（get_channel 是 async，须 await）。
        拿不到则返回 None。
        """
        try:
            from qwenpaw.plugins import PluginRegistry

            mgr = PluginRegistry().get_workspace_manager()
            if mgr is None:
                return None
            workspaces = getattr(mgr, "agents", getattr(mgr, "workspaces", {}))
            ws = workspaces.get("default")
            if ws is None:
                return None
            cm = getattr(ws, "channel_manager", None)
            if cm is None:
                return None
            return await cm.get_channel("feishu")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard: get feishu channel failed: %s", exc
            )
            return None

    async def _inject_thread_resolver(self) -> None:
        """加载/重启插件时：把守护话题帖解析回调注册进飞书 channel。"""
        try:
            feishu = await self._get_feishu_channel()
            if feishu is None or not hasattr(feishu, "add_thread_resolver"):
                logger.warning(
                    "qwenpaw-guard: feishu channel has no add_thread_resolver; "
                    "thread-redirect disabled"
                )
                return
            feishu.add_thread_resolver(guard_thread_resolver)
            logger.info("qwenpaw-guard: feishu thread resolver injected")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard: inject thread resolver failed: %s", exc
            )

    async def _remove_thread_resolver(
        self,
        plugin_id: str | None = None,
        delete_files: bool | None = None,
    ) -> None:
        """卸载插件时：注销话题帖解析回调，恢复飞书默认会话行为。"""
        try:
            feishu = await self._get_feishu_channel()
            if feishu is None or not hasattr(feishu, "remove_thread_resolver"):
                return
            feishu.remove_thread_resolver(guard_thread_resolver)
            logger.info("qwenpaw-guard: feishu thread resolver removed")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard: remove thread resolver failed: %s", exc
            )

    async def _inject_redirect_hook(self) -> None:
        """加载/重启插件时：把「转投钩子」注册进飞书 channel。"""
        try:
            feishu = await self._get_feishu_channel()
            if feishu is None or not hasattr(feishu, "add_redirect_hook"):
                logger.warning(
                    "qwenpaw-guard: feishu channel has no add_redirect_hook; "
                    "console-redirect disabled"
                )
                return
            feishu.add_redirect_hook(guard_redirect)
            logger.info("qwenpaw-guard: feishu redirect hook injected")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard: inject redirect hook failed: %s", exc
            )

    async def _remove_redirect_hook(
        self,
        plugin_id: str | None = None,
        delete_files: bool | None = None,
    ) -> None:
        """卸载插件时：注销转投钩子，恢复飞书默认（不回投 console）。"""
        try:
            feishu = await self._get_feishu_channel()
            if feishu is None or not hasattr(feishu, "remove_redirect_hook"):
                return
            feishu.remove_redirect_hook(guard_redirect)
            logger.info("qwenpaw-guard: feishu redirect hook removed")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qwenpaw-guard: remove redirect hook failed: %s", exc
            )


plugin = GuardPlugin()
