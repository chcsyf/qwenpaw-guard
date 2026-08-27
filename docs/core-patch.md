# 核心扩展点补丁

`qwenpaw-guard` 依赖 QwenPaw 核心 `feishu` channel 的两个**通用扩展点**，用于实现
「多守护会话分别回复」与「话题回复转投 Console」。这两个扩展点**不接受任何业务逻辑**，
纯粹是可插拔的钩子（无人注册时，飞书收发行为与原来完全一致）。

> 先确认你的核心是否已含这些扩展点：

```bash
grep -cE "add_thread_resolver|add_redirect_hook" \
  /path/to/qwenpaw/app/channels/feishu/channel.py
```

- 结果 **≥2**：核心扩展点已就绪，**跳过本文档**，直接装插件即可。
- 结果 **0**：按本文档给核心打补丁。文件位置随安装目录而定（常见 `qwenpaw/app/channels/feishu/channel.py`）。

---

## 1. `__init__` 末尾追加字段

在 `FeishuChannel.__init__` 中找到这一行：

```python
        self._thread_resolver_lock = asyncio.Lock()
```

在其**之后**追加：

```python
        # 通用、可插拔的「转投钩子」扩展点：允许外部（通常是插件）注册回调，
        # 在收到话题帖守护回复时把整条消息转投到另一个渠道会话（如 console），
        # 由目标 channel 身份处理、回复走目标渠道。
        # callable(session_id, thread_id, resolver_text, native) -> Optional[dict]；
        # 返回 {"session_id": ..., "agent_id": ...} 表示转投到该会话的 console，
        # 返回 None 表示按原渠道（飞书）正常处理。无人注册时行为不变。
        self._redirect_hooks: list = []
        self._redirect_hook_lock = asyncio.Lock()
```

> 若你的核心**已有** `self._thread_resolvers` / `self._thread_resolver_lock`，
> 说明 thread-resolver 扩展点已存在，仅需保留下面第 2 步里的 **redirect-hook**
> 相关方法与字段。

---

## 2. 在类内新增方法

以下方法放在类内任意合适位置（例如 `resolve_session_id` 之后）。全部为通用钩子，
**不**包含任何 guard 业务逻辑，且**绝不向外抛异常**。

### 2.1 thread-resolver 注册/注销

```python
    def add_thread_resolver(
        self,
        resolver: Callable[[str, str], Optional[str]],
    ) -> None:
        """注册一个话题帖会话解析回调（通用扩展点，供插件注入）。

        resolver(thread_id, session_id) -> 目标 session_id | None。
        收到话题帖回复时被调用；返回非 None 则覆盖该回复的会话 id。

        防御性：回调不可调用时忽略；channel 旧实例缺少该字段时自动容错
        初始化，绝不向上抛异常。
        """
        try:
            if not callable(resolver):
                logger.warning(
                    "feishu add_thread_resolver: resolver not callable, ignored"
                )
                return
            resolvers = getattr(self, "_thread_resolvers", None)
            if resolvers is None:
                self._thread_resolvers = resolvers = []
            if resolver not in resolvers:
                resolvers.append(resolver)
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu add_thread_resolver failed",
                exc_info=True,
            )

    def remove_thread_resolver(
        self,
        resolver: Callable[[str, str], Optional[str]],
    ) -> None:
        """注销一个话题帖会话解析回调（卸载插件时还原默认行为）。"""
        try:
            resolvers = getattr(self, "_thread_resolvers", None)
            if not resolvers:
                return
            if resolver in resolvers:
                resolvers.remove(resolver)
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu remove_thread_resolver failed",
                exc_info=True,
            )
```

### 2.2 redirect-hook 注册/注销

```python
    def add_redirect_hook(
        self,
        hook: Callable[..., Optional[dict]],
    ) -> None:
        """注册一个「转投钩子」（通用扩展点，供插件注入）。

        hook(session_id, thread_id, resolver_text, native) -> Optional[dict]。
        返回非 None（含 session_id 的 dict）则把整条消息转投到对应会话的
        console 渠道，由 console 身份处理、回复走控制台；返回 None 则按
        飞书渠道正常处理。

        防御性：回调不可调用时忽略；旧实例缺字段时自动容错初始化。
        """
        try:
            if not callable(hook):
                logger.warning(
                    "feishu add_redirect_hook: hook not callable, ignored"
                )
                return
            hooks = getattr(self, "_redirect_hooks", None)
            if hooks is None:
                self._redirect_hooks = hooks = []
            if hook not in hooks:
                hooks.append(hook)
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu add_redirect_hook failed",
                exc_info=True,
            )

    def remove_redirect_hook(
        self,
        hook: Callable[..., Optional[dict]],
    ) -> None:
        """注销一个转投钩子（卸载插件时还原默认行为）。"""
        try:
            hooks = getattr(self, "_redirect_hooks", None)
            if not hooks:
                return
            if hook in hooks:
                hooks.remove(hook)
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu remove_redirect_hook failed",
                exc_info=True,
            )
```

### 2.3 话题帖会话解析（覆盖 session_id）

```python
    async def _apply_thread_resolvers(
        self,
        thread_id: str,
        session_id: str,
        resolver_text: str = "",
    ) -> str:
        """依次调用已注册的话题帖会话解析回调，返回最终 session_id。

        无回调命中时返回原 session_id（默认行为不变）。``resolver_text``
        仅供回调解析会话名称用，不会注入上下文。本方法绝不向外抛异常。
        """
        try:
            resolvers = getattr(self, "_thread_resolvers", None) or []
            if not thread_id or not resolvers:
                return session_id
            for resolver in list(resolvers):
                try:
                    try:
                        mapped = resolver(thread_id, session_id, resolver_text)
                    except TypeError:
                        mapped = resolver(thread_id, session_id)
                    if inspect.isawaitable(mapped):
                        mapped = await mapped
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "feishu thread resolver failed: %s",
                        getattr(resolver, "__name__", resolver),
                        exc_info=True,
                    )
                    continue
                if mapped:
                    if isinstance(mapped, dict):
                        mapped = mapped.get("session_id") or session_id
                    session_id = mapped
                    break
            return session_id
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu _apply_thread_resolvers error",
                exc_info=True,
            )
            return session_id

    async def _thread_resolvers_match(
        self,
        thread_id: str,
        resolver_text: str = "",
    ) -> bool:
        """判断是否有 thread resolver 会命中该话题帖。

        命中即视为"干净的论坛式回复"，跳过被引用文本注入。绝不抛异常。
        """
        try:
            resolvers = getattr(self, "_thread_resolvers", None) or []
            if not thread_id and not resolver_text:
                return False
            for resolver in list(resolvers):
                try:
                    try:
                        mapped = resolver(thread_id, "", resolver_text)
                    except TypeError:
                        mapped = resolver(thread_id, "")
                    if inspect.isawaitable(mapped):
                        mapped = await mapped
                except Exception:  # noqa: BLE001
                    continue
                if mapped:
                    return True
            return False
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu _thread_resolvers_match error",
                exc_info=True,
            )
            return False
```

### 2.4 转投钩子应用（返回目标、转投到 console）

```python
    async def _apply_redirect_hooks(
        self,
        session_id: str,
        thread_id: str,
        resolver_text: str,
        native: dict,
    ) -> Optional[dict]:
        """依次调用已注册的转投钩子，返回首个非 None 的转投目标 dict。

        无人命中时返回 None（按飞书渠道正常处理）。绝不抛异常。
        """
        try:
            hooks = getattr(self, "_redirect_hooks", None) or []
            if not hooks:
                return None
            for hook in list(hooks):
                try:
                    try:
                        target = hook(
                            session_id, thread_id or "", resolver_text, native
                        )
                    except TypeError:
                        target = hook(session_id, thread_id or "")
                    if inspect.isawaitable(target):
                        target = await target
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "feishu redirect hook failed: %s",
                        getattr(hook, "__name__", hook),
                        exc_info=True,
                    )
                    continue
                if isinstance(target, dict) and target.get("session_id"):
                    return target
            return None
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu _apply_redirect_hooks error",
                exc_info=True,
            )
            return None

    async def _redirect_to_console(
        self,
        native: dict,
        redirect: dict,
    ) -> bool:
        """把飞书消息转投到 Console 渠道会话，由 console 身份处理。

        复用 console channel 的 build_agent_request_from_native +
        _consume_with_tracker，以 console 身份注入目标会话并流式处理，
        回复走控制台（不写回飞书话题）。返回 True 表示已转投；False 表示
        转投失败（调用方回退到正常飞书处理）。绝不抛异常。
        """
        workspace = getattr(self, "_workspace", None)
        if workspace is None:
            logger.warning("feishu _redirect_to_console: no workspace")
            return False
        try:
            channel_manager = getattr(workspace, "channel_manager", None)
            if channel_manager is None or not hasattr(
                channel_manager, "get_channel"
            ):
                return False
            console_ch = await channel_manager.get_channel("console")
            if console_ch is None:
                return False
            target_session = redirect.get("session_id") or ""
            if not target_session:
                return False
            target_user = (
                redirect.get("user_id")
                or redirect.get("agent_id")
                or native.get("user_id")
                or native.get("sender_id")
                or "console"
            )
            content_parts = native.get("content_parts") or []
            if not content_parts:
                return False
            console_native = {
                "channel_id": "console",
                "sender_id": target_user,
                "user_id": target_user,
                "session_id": target_session,
                "content_parts": content_parts,
                "meta": {
                    **(native.get("meta") or {}),
                    "session_id": target_session,
                    "feishu_redirected": True,
                    "redirected_from": "feishu",
                },
                "message_metadata": {
                    **(native.get("meta") or {}),
                    "redirected": True,
                },
            }
            build = getattr(console_ch, "build_agent_request_from_native", None)
            if not callable(build):
                return False
            request = build(console_native)
            consume = getattr(console_ch, "_consume_with_tracker", None)
            if not callable(consume):
                return False
            asyncio.ensure_future(consume(request, console_native))
            return True
        except Exception:  # noqa: BLE001
            logger.warning(
                "feishu _redirect_to_console error",
                exc_info=True,
            )
            return False
```

> 若你的核心**没有** `_thread_resolvers` 字段，还需在 `__init__` 里补：
> ```python
> self._thread_resolvers: list = []
> self._thread_resolver_lock = asyncio.Lock()
> ```

---

## 3. 接线到 `_on_message`

在 `FeishuChannel._on_message` 中对三处做小改动（都在该方法内，按注释位置找）：

### 3.1 用 thread-resolver 覆盖 session_id

在 `session_id = self.resolve_session_id(sender_id, meta)` **之后**、构建 `native`
**之前**，加入：

```python
            try:
                session_id = await self._apply_thread_resolvers(
                    thread_id or "",
                    session_id,
                    resolver_text,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "feishu _apply_thread_resolvers call failed, "
                    "keeping default session",
                    exc_info=True,
                )
```

> `thread_id` 在方法内此前已提取；`resolver_text` 在解析被引用消息时已构造。

### 3.2 守护话题回帖跳过引用文本注入

在调用 `_process_quoted_message(parent_id, ...)` **之前**，加入（命中则跳过引用注入）：

```python
            _thread_resolved = False
            try:
                _thread_resolved = await self._thread_resolvers_match(
                    str(getattr(message, "thread_id", "") or "").strip(),
                    resolver_text,
                )
            except Exception:  # noqa: BLE001
                _thread_resolved = False
            if parent_id and not _thread_resolved:
                await self._process_quoted_message(
                    parent_id, text_parts, content_parts,
                )
```

### 3.3 转投到 console

在 `self._enqueue(native)` **之前**加入：

```python
            _redirected = False
            try:
                _redirect = await self._apply_redirect_hooks(
                    session_id, thread_id or "", resolver_text, native,
                )
                if _redirect:
                    _redirected = await self._redirect_to_console(
                        native, _redirect,
                    )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "feishu redirect dispatch failed, keeping feishu path",
                    exc_info=True,
                )
                _redirected = False
            if self._enqueue is not None and not _redirected:
                self._enqueue(native)
```

---

## 4. 验证

打完补丁后：

```bash
python -m py_compile /path/to/qwenpaw/app/channels/feishu/channel.py   # 应无输出
```

重启 QwenPaw，加载插件后日志应出现（插件启动钩子打印）：

```
qwenpaw-guard: feishu thread resolver injected
qwenpaw-guard: feishu redirect hook injected
```

若核心缺少字段、扩展点缺失，插件会**安全降级**（仅不启用对应能力，不崩主流程），
日志会给出 `thread-redirect disabled` / `console-redirect disabled` 之类的提示。
