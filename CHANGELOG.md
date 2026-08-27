# Changelog

## v0.0.1 (2026-08-27)

**通知/审批频道：飞书**

- 通知/审批默认频道为飞书（`DEFAULT_CHANNEL=feishu`，Open API 主动推送，无被动回复窗口限制）
- `notifier.py` 统一走**话题群**通道：`send_text` / `send_test_message` 单一话题群通道，新增 `_ensure_session_topic` / `_feishu_create_topic` / `_feishu_reply`；每个守护会话的通知进入其**专属主题帖**（标题固化会话名称）

**飞书话题群自动检测**

- 新增 `list_topic_chats()` 与 `GET /discover-topics`；「通知配置（飞书）」看板改自动检测：唯一话题群自动填入并保存，多个则下拉选择
- `feishu_topic_chat_id` 不硬编码默认值（由看板填入）

**多守护会话分别回复**

- 核心 feishu channel 新增通用 **thread-resolver 扩展点**（`add/remove_thread_resolver` + `_apply_thread_resolvers` + `_thread_resolvers_match`）
- 插件 `guard_thread_resolver`：按主题帖标题「会话名」反查 `resolve_session_by_name`（稳定主键），`thread_id` 仅兜底（避免话题重建漂移）
- 守护话题回帖跳过被引用文本注入（`_thread_resolvers_match` 命中即不注入引用噪音）

**话题回复转投 Console**

- 核心 feishu channel 新增通用 **redirect-hook 扩展点**（`add/remove_redirect_hook` + `_apply_redirect_hooks` + `_redirect_to_console`）
- 插件 `guard_redirect`：守护话题回帖转投到被守护会话的 console 渠道，由控制台身份续上下文、回复走控制台；目标会话动态反查
- 转投路径复用 console 的 `build_agent_request_from_native` + `_consume_with_tracker`（后台异步消费）

**会话名称显示**

- 新增 `monitoring.resolve_session_name`；通知正文/主题帖标题用可读会话名替代 `session_id`（标题仅创建时固化）

**守护/归档/会话管理 REST**

- 新增 `POST /sessions/guard` / `archive` / `rename` / `delete`，`POST /admin/rearm`

**会话管理健壮性修复**

- 归档按钮根因：`SessionActionBody` 缺 `archived` 字段导致 `body.archived` 抛 `AttributeError` → 路由 500；补齐字段并验证 round-trip
- 删除/重命名改用自定义 `.qgd-modal` 弹窗（移除 `window.confirm` / `prompt`）
- 看板统计卡片改按当前会话列表现算（删除会话后计数自动同步，agent 头标签同改）
- 看板配置按钮布局：保存 + 测试发送移入话题群行、紧跟「自动检测/重新检测」后

**基础能力**

- 新增：列出所有 agent 与活动会话（REST `GET /api/qwenpaw-guard/status` + 看板 UI）
- 新增：会话结束钩子 `register_agent_stop_handler`，自动向飞书推送通知
- 新增：需要权限钩子（`register_middleware` `on_acting`），拦截敏感工具调用并路由到飞书审批
- 新增：异常中断兜底钩子 `GuardErrorNotifyHook`（`Phase.ON_ERROR`）：run 异常终止时按守护会话过滤推送
- 新增：飞书侧命令工具 `guard_status` / `guard_resolve` / `guard_configure` / `guard_send`
- 默认安全：飞书未配置或审批超时时，block 级操作默认拒绝
- 修复：看板 statRow 误用（`stat()` 返回单元素被当数组 push）导致页面崩溃
- 修复：组件 uid 每次渲染重新生成导致 useEffect 无限触发、持续请求 /status
