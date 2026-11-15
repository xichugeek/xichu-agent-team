# 账号池

默认只启用 A，三个岗位共用。B/C 必须是自己的独立订阅登录，不把同一 auth 复制三份。

在 `config.local.json` 填 B/C 的真实绝对目录后，运行对应 login 脚本或 `python -m xichu_agent login-codex codex_b`。浏览器授权由本人核对邮箱，doctor 检查启用账号是否重复。

```json
{
  "executors": {
    "codex_b": {"codex_home": "C:/CodexAccounts/B", "enabled": true},
    "codex_c": {"codex_home": "C:/CodexAccounts/C", "enabled": true}
  },
  "roles": {
    "pm": {"pool": ["codex_c", "codex_a", "codex_b"]},
    "video": {"pool": ["codex_a", "codex_c", "codex_b"]},
    "site": {"pool": ["codex_b", "codex_c", "codex_a"]}
  }
}
```

繁忙或真实额度限制时选择池内可用账号；原工作区/桌面任务串行，避免争用窗口和文件。额度来自官方返回，不换算固定任务次数。手动重置使用官方机会，不购买、不把余额写成100%。
