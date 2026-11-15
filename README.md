# AI 员工团队 · Codex Edition

在 Telegram 私群派视频制作和官网运维任务，由本机 Codex 执行、保留交接并回复结果。支持三个岗位、独立 A/B/C 账号池、工单历史和本地看板。

> v0.2.0 改用 Codex。旧视频对应的 Claude 版保留在提交 `98548e5`；升级前阅读 [升级说明](docs/UPGRADE.md)。

第026期视频：[Codex AI员工团队升级｜Telegram群里派活，手机看进度](https://www.youtube.com/watch?v=uN6oqo7wdIM)。**本期复现版本为 v0.2.1**，包含密码保护的同局域网手机只读查看。

第一次使用先看 [第026期快速上手](docs/QUICKSTART_E026.md) 和 [可复制教学提示词](docs/E026_PROMPTS.md)。只看界面可以运行 `python demo_dashboard.py --open` 或双击 `start_demo.bat`；教学Demo无需账号、Token或真实工单，不执行真实任务。

## 功能

- 项目经理、视频制作、官网运维三个机器人。视频/官网可直接执行，不必先经项目经理。
- 独立 Codex 账号池：主账号繁忙或额度受限时换可用账号，全部不可用则排队。
- 自动生成有界事实摘要，旧要求、失败和交接按需分页检索；原文与人工批准凭据保留，不将整个群聊当上下文。
- 默认轻量流程：简短回群，不自动追加 AI 审核/返工或上传长报告；需要复核时 `/review`。
- 附件接收、完整性缓存、跨机器人消息去重、重试保留旧产物。
- 本地看板：总览、工单、账号额度、视频制作、官网运维；默认仅本机，可自行启用带密码的同局域网手机只读访问。
- 额度从已登录 Codex CLI 读取；重置只在本人确认后使用官方返回的机会，不购买或伪造余额。

调度器使用 Python 标准库，看板另需 PyYAML。使用 ChatGPT 订阅登录，不使用额外 API Key 计费。实际模型和电脑操作工具取决于自己的 Codex 安装与权限。

## 准备与配置

需要 Python **3.12+**、已通过 ChatGPT 登录的 Codex CLI/桌面版，以及自己的 Telegram 私群和三个机器人。需要接收普通 @消息时，在 BotFather 关闭项目经理隐私模式，并按 Telegram 要求重新加群。

运行 `first_setup.bat`，或复制：

```powershell
Copy-Item config.local.example.json config.local.json
Copy-Item secrets.example.json secrets.local.json
```

在 `secrets.local.json` 填 `pm/video/site` Token。运行 `python -m xichu_agent discover` 获取本人/群 ID，填入 `config.local.json`。

配置优先级：公开 `config.json` → 被忽略的 `config.local.json` → `secrets.local.json`。真实 ID、路径与账号目录放本地文件。默认只启用 A，三个岗位可共用；B/C 需独立登录后启用，见 [账号配置](docs/ACCOUNTS.md)。不复制他人的 auth。

```powershell
python -m xichu_agent doctor
python -m xichu_agent run
```

也可双击 `doctor.bat`、`start_agent.bat`。同一时刻只允许一个调度器实例；停止用 `stop_agent.bat`。

## 群里使用

```text
/video 制作需求               直接交给视频制作
/site 官网运维需求            直接交给官网运维
/pm 需求                      按需让项目经理拆解
/review 要检查的内容           按需独立复核
/continue T-0001 补充要求      接续原工单和产物
/status T-0001
/retry T-0001
/cancel T-0001
```

只执行配置的本人在指定私群发出的任务，其他群、私聊、其他人和编辑消息不执行。需要人工关卡时先交付具体目标及 SHA256，本人再 `/approve T-0001 关卡名`；摘要或 AI 复核不产生批准，文件变化使旧确认失效。

## 接自己的工作区

默认 `video_mode=synthetic`、`site_mode=draft`，只是合成素材/草稿流程，不能冒充真实影片制作或网站上线。在本地配置自己的 `studio_root/site_repo/forge_root`、技能和工具，才启用 `studio/workspace` 原工作区模式。

**本仓库不包含作者的声音参考/特征、影片素材、真实剪映工程、账号、服务器凭据或私服部署脚本。** 适配器使用自己的原工作区能力，并非克隆仓库就能获得这些私有资源。电脑工具按官方方式安装并由本人授权；原项目的必要人工审片和发布批准保留。

剪映工程保持不透明；验收优先关闭后完整备份、通过原生界面复制，在副本保存/重开/播放，并单独核对源文件。打开原件本身可能自动保存。

## 本地看板

```powershell
python -m pip install -r requirements-dashboard.txt
Copy-Item control_center.integration.example.json control_center.integration.json
python dashboard.py --open
# 或读取自己的外部Studio
python dashboard.py --studio-root "C:/Projects/YourStudio" --open
```

地址：**http://127.0.0.1:18765/**。在选定 Studio 根目录配置被忽略的 integration 文件，将 `agent_root` 指向本仓库绝对路径。无数据时显示空/未知状态，不附个人工单。详见 [看板说明](docs/CONTROL_CENTER.md)。

关闭控制台或关机将停止服务；此版本不安装登录自启或 Windows 服务，下次使用重新启动即可。

## 手机查看（v0.2.1）

手机与电脑连同一个受信任的局域网。将下面的教学地址换成电脑实际 IPv4 和子网前缀：

```powershell
python dashboard.py --enable-lan 192.168.50.10/24 --open
```

启动器会生成仅保存在 `output/control_center/lan-access.local.json` 的随机密码，并在电脑控制台显示。手机打开控制台列出的局域网地址，用户名 `viewer`，输入该密码。手机端不能重置额度、运行手动检查或执行写操作。这个功能不会配置代理、防火墙或公网转发；HTTP只用于自己的可信局域网。端口被防火墙或路由器隔离时需本人按环境处理，不能凭电脑自测称手机已连通。

普通重启会沿用配置与密码。关闭服务后将上述本地JSON的 `enabled` 改为 `false`，再启动即可恢复仅本机访问。真实密码、地址和账号目录不要提交Git。

## 测试与隐私

```powershell
python -m unittest discover -s tests -t . -v
```

测试使用临时合成数据和伪 Telegram/CLI/RPC；媒体测试需本机 FFmpeg，缺少时明确跳过。请阅读 [隐私说明](docs/PRIVACY.md)，不要把运行数据或 Codex Home 当源码上传。

MIT License。使用各平台工具时遵守其条款及资源所有者授权。
