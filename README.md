# AI 员工团队（Telegram 私群派活 · Claude Code + Codex）

在自己电脑上跑一个小调度器：你在 Telegram 私人群里 @ 一个机器人说需求，项目经理（Claude Code）判断派给谁，员工（Codex）干活，做完由项目经理独立质检，进度和结果都回复在你那条消息下面。

- 只用 Python 标准库，不需要公网服务器。
- 用 Claude 和 ChatGPT 的**订阅登录**，不接按量付费的 API；启动前会去掉 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等环境变量，并检查是不是订阅登录，不是就拒绝执行。
- 只听你本人在指定私群里的消息；私聊、别的群、别人、编辑过的消息一律不执行。
- 产物只写在本机 `runtime\work\` 下（程序员岗位可以另外配置一个项目目录）；发布、上传、付款、删除之类的事一律不做。

> 由 [西楚极客](https://xichugeek.com/) 制作，配套视频：《让 Claude 帮你搭一支 AI 员工团队》。

## 群里的机器人（默认三个岗位，可在 config.json 增删改）

| 岗位 | 默认执行器 | 职责 |
| --- | --- | --- |
| 项目经理(Claude) `pm` | Claude Code | 接需求、派给合适的员工（拿不准就问你一个问题）；员工交付后做独立质检，不合格自动退回返工一轮 |
| 写手(Codex) `writer` | Codex | 写文档、文章、周报、方案等文字草稿 |
| 程序员(Codex) `coder` | Codex | 在指定项目目录里写代码、改 bug、跑测试（不提交、不推送） |

想换成自己的岗位（客服、写周报、盯服务器……）：改 `config.json` 的 `roles`，在 `prompts\` 里写对应的提示词；或者把 `docs\给ClaudeCode的需求说明.md` 交给 Claude Code，让它帮你改。

## 准备

1. Windows 电脑，安装 Python 3.10 或更新版本（安装时勾选 “Add python.exe to PATH”）。
2. Claude 付费订阅，安装 Claude Code，在终端运行 `claude`，用 `/login` 登录订阅账号。
3. ChatGPT 付费订阅，安装 Codex CLI，运行 `codex login` 用 ChatGPT 账号登录。
4. 终端里 `claude --version`、`codex --version` 都能输出版本号。
5. 一个 Telegram 账号。

## 第一步：建机器人和群

1. 在 Telegram 搜索 **@BotFather**，发 `/newbot`，按提示起名，拿到一串 token。**token 相当于机器人的密码，不要发给别人、不要截图外传。**
2. 一共建三个：项目经理、写手、程序员。
3. 双击 `first_setup.bat`，在打开的记事本里把三个 token 分别粘到 `pm`、`writer`、`coder` 后面，保存。
4. 新建一个只有你自己的群，把三个机器人拉进来。
5. **项目经理必须关闭隐私模式**：BotFather 发 `/setprivacy` → 选项目经理 → `Disable`。然后把项目经理**移出群再重新拉回来**，设置才生效。不关的话，你在群里 @ 它，它收不到。
6. 三个机器人都关掉“可被拉进其他群”：BotFather 发 `/setjoingroups` → 选机器人 → `Disable`。

## 第二步：填你的用户 ID 和群 ID

1. 在群里随便发一句话。
2. 双击 `discover.bat`，它会列出 `chat_id`（群，负数）和 `from_id`（你）。
3. 打开 `config.json`，把 `owner_user_id` 改成你的 `from_id`，`group_chat_id` 改成群的 `chat_id`。

## 第三步：自检和启动

- `doctor.bat`：检查机器人是否都在群里、项目经理隐私模式、Claude Code 和 Codex 是否订阅登录。全部 OK 再启动。
- `start_agent.bat`：启动调度器，窗口保持打开。
- `stop_agent.bat`：正常停止；正在运行的任务下次启动会重新排队。
- `run_tests.bat`：跑自动化测试（不需要联网、不调用模型）。

## 群里怎么用

（下面的 @项目经理 指在群里 @ 项目经理机器人的用户名，输入 @ 后从列表里选；也可以用“写手：需求”这种中文前缀写法）

```
@项目经理 需求          由项目经理判断派给谁（不清楚会先问你）
@写手 需求 / @程序员 需求  直接派给该员工，做完由项目经理质检
/writer 需求  /coder 需求  /pm 需求   同上，命令写法
/review 内容            只让项目经理做一次质检
/status [T-0001]  /retry T-0001  /cancel T-0001  /help
```

- 同一任务只发一次；重复的消息、内容相同的未结工单都只回复“重复派单”。
- 员工每完成一步写一行进度，调度器每 2 分钟把新进度转发到群里。
- 失败时群里直接告诉你原因和工作目录，修好后 `/retry`。
- 订阅额度用完时不会硬跑：该执行器冷却（默认 60 分钟），工单排队，群里告诉你大约几点恢复。

## 程序员岗位接自己的项目

在 `config.json` 的 `roles.coder.project_dir` 填项目目录（例如 `"D:/Projects/my-app"`）。建议这个目录是 Git 仓库，并且先提交一次，改坏了随时能退回。程序员只改文件、跑测试，不会 `git commit` / `git push`。

## 常见坑

| 现象 | 原因和处理 |
| --- | --- |
| @项目经理 没反应 | 没关隐私模式，或关了没移出群再拉回。`doctor.bat` 会提示。 |
| Codex 报只读、拒绝执行命令 | 调度器用独立配置启动 Codex，Windows 沙盒要显式设成 `elevated`（`config.json` 已默认设好）。 |
| doctor 提示不是订阅登录 | 电脑里设过 API Key 环境变量，或 Claude Code / Codex 用 API Key 登录了。改成订阅账号登录。 |
| 群被升级成超级群后没反应 | 群 ID 变了，重新运行 `discover.bat`，把新 ID 填回 `config.json`。 |

## 目录

```
agent_team/    调度器代码（scheduler、store、telegram、executors、workflows …）
prompts/       项目经理和员工的提示词（triage / review / work_*.md）
docs/          给 Claude Code 的需求说明
tests/         自动化测试（伪 Telegram 服务、伪 codex/claude 命令）
runtime/       数据库、日志、每个工单的工作目录（自动生成，不提交）
```

## 许可

MIT。使用 Claude Code 和 Codex 时请遵守各自的服务条款。
