# -*- coding: utf-8 -*-
"""qwenpaw-guard 常量：配置默认值、敏感操作规则、审批判定词。"""

PLUGIN_ID = "qwenpaw-guard"
PLUGIN_NAME = "AI 守护者"
PLUGIN_VERSION = "0.0.1"

# 通知/审批频道：默认飞书（Open API 主动推送，无被动回复窗口限制）。
# 可经 config 接口或环境变量覆盖
DEFAULT_CHANNEL = "feishu"
# 发送消息时使用的 agent 身份（X-Agent-Id 头）
SENDER_AGENT_ID = "default"

# 审批超时（秒）：超时未在飞书回复 = 默认拒绝（安全优先）
APPROVAL_TIMEOUT_SECS = int(__import__("os").environ.get("QWENPAW_GUARD_APPROVAL_TIMEOUT", 300))

# 审批判定词（拒绝词优先，安全优先）
APPROVE_KEYWORDS = ("允许", "同意", "放行", "批准", "approve", "yes", "y", "ok", "allow")
REJECT_KEYWORDS = ("拒绝", "不同意", "禁止", "不予", "不准", "reject", "no", "n", "deny", "block")

# 会话判活窗口：updated_at 距今在该秒数内，视为「活动会话」
ACTIVE_WINDOW_SECS = int(__import__("os").environ.get("QWENPAW_GUARD_ACTIVE_WINDOW", 1800))

# 不参与监督/审批的噪音工具（避免插件自身的辅助工具被误判）
LOG_IGNORE_TOOLS = (
    "guard_status",
    "guard_resolve",
    "guard_configure",
    "guard_send",
    "list_agents",
    "team_status",
    "team_check_task",
)

# 敏感操作规则（照搬/精简自 qwenpaw-team）：
#   level=block：拦截并唤起飞书审批；warn：放行但记日志
#   tools：专用工具名关键词（子串匹配）
#   arg_keywords：参数里出现的关键词（对 shell 类工具生效）
#   pattern：参数正则，命中才触发（shell = 关键词 + pattern 双重匹配）
SENSITIVE_RULES = [
    {
        "name": "recursive_force_delete",
        "level": "block",
        "tools": ("rm", "del", "remove"),
        "arg_keywords": ("rm", "del", "remove"),
        "pattern": r"-rf|--recursive|rmdir",
        "reason": "递归强制删除不可恢复",
    },
    {
        "name": "disk_destroy",
        "level": "block",
        "tools": ("mkfs", "fdisk", "dd", "format", "shutdown", "reboot"),
        "arg_keywords": ("mkfs", "fdisk", "shutdown", "reboot", "format"),
        "pattern": r"",
        "reason": "磁盘/系统级破坏性操作",
    },
    {
        "name": "external_side_effect",
        "level": "warn",
        "tools": ("send_email", "send_message", "post_tweet", "publish", "deploy", "release"),
        "arg_keywords": (),
        "pattern": r"",
        "reason": "可能产生外部副作用（邮件/推送/部署）",
    },
    {
        "name": "force_push",
        "level": "warn",
        "tools": ("git", "push"),
        "arg_keywords": ("git", "push"),
        "pattern": r"--force|-f\b",
        "reason": "git 强制推送会覆盖远端历史",
    },
    {
        "name": "write_other_workspace",
        "level": "warn",
        "tools": ("write_file", "edit_file", "cp", "mv"),
        "arg_keywords": ("cp", "mv"),
        "pattern": r"workspaces/(?!default|guard)",
        "reason": "写入非本人 workspace，疑似越权",
    },
]

# shell 类工具：规则通过参数关键词 + pattern 双重匹配（命令藏在参数里）
SHELL_TOOLS = (
    "execute_shell_command",
    "run_shell_command",
    "bash",
    "zsh",
    "sh",
    "cmd",
    "powershell",
)
