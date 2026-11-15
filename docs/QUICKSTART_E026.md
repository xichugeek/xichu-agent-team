# 第026期快速上手：电报派活，看板追踪，手机查看

本期固定版本 **v0.2.1**。第024期旧Claude模板固定在 `98548e5`；v0.2.0没有本期新增的LAN查看。这里的路径、地址、工单和账号都是教学示例，换成你自己的值。

## 1. 下载并先看界面

从本仓库 v0.2.1 Release 下载搭建包，解压到自己的目录。需要 Python 3.12+；本包不含安装器或账号认证。先在目录里执行：

```powershell
python demo_dashboard.py --open
```

也可双击 `start_demo.bat`。地址是 `http://127.0.0.1:18766/`。页面明确标为教学示例，工单、账号和额度均为虚构数据；它不调用Codex、不连Telegram、不读真实数据库。关掉控制台即停止。这个Demo与真实服务端口18765分开。

## 2. 准备自己的真实环境

- 在自己的电脑安装并登录Codex。模板使用自己的订阅登录和已有CLI/桌面运行时；实际工具、模型和电脑操作权限以你的安装为准。
- 在Telegram的BotFather创建自己的 `pm/video/site` 三个机器人，再放进自己的私群。
- 先用一个账号；B/C独立账号是可选项，见 [ACCOUNTS.md](ACCOUNTS.md)。不要复制作者或别人的auth。
- Token、本人/群ID只填本机私密配置。不要在群聊或公开视频里贴出。

## 3. 创建本地配置和自检

```powershell
Copy-Item config.local.example.json config.local.json
Copy-Item secrets.example.json secrets.local.json
python -m pip install -r requirements-dashboard.txt
python -m xichu_agent discover
```

在 `secrets.local.json` 填自己的三个Token，在 `config.local.json` 填discover返回的本人/群ID及本机实际工具路径。先运行 `python -m xichu_agent doctor`，根据真实检查补齐Codex、FFmpeg/FFprobe等环境，再 `python -m xichu_agent run`。也可使用 `first_setup.bat`、`doctor.bat`、`start_agent.bat`。

公开配置中的 `C:/Projects/YourStudio` 等是占位目录，必须替换。`doctor` 的缺项应实际解决；不是下载后一定全部通过。

## 4. 接上真实看板

在另一控制台运行：

```powershell
Copy-Item control_center.integration.example.json control_center.integration.json
python dashboard.py --open
```

电脑地址 `http://127.0.0.1:18765/`。默认integration的 `agent_root` 为当前仓库；真实Studio在外部目录时，使用 `--studio-root` 并在那个根目录配置integration，`agent_root`填写本仓库绝对路径。没有真实工单/Episodes时显示空状态，不冒充已经制作完成。

在自己的群发一张小任务，例如 `/video 整理我指定脚本的剪辑准备清单，只生成本地文件，不上传发布`。接单后使用真实工单号查看 `/status`、`/continue`，并在看板筛选同一号码。需要原视频/官网工作区能力时，读取并接入你自己的技能、工具、素材和授权；默认synthetic/draft只是合成/草稿，不能当成真实影片制作或网站部署。

## 5. 手机同局域网查看

先在电脑查看地址：

```powershell
Get-NetIPConfiguration
```

选择正在连接Wi-Fi/有线局域网的真实IPv4和子网前缀，避开断开/虚拟网卡。假设教学接口为 `192.168.50.10/24`：

```powershell
python dashboard.py --enable-lan 192.168.50.10/24 --open
```

已有服务需先关闭再启动。电脑控制台显示用户名 `viewer` 和自动随机密码；手机连同一个可信局域网，用控制台给出的地址登录。手机只能查看；电脑127.0.0.1仍可执行原有手动操作。密码保存在被忽略的 `output/control_center/lan-access.local.json`，不要上传。

手机127.0.0.1指手机自己。若拒绝或超时，核对地址、服务是否启动、两边子网和Wi-Fi隔离/防火墙；本工具不会替你更改代理、防火墙或系统网络设置。必须在自己的手机真实打开后才算通过，不是电脑HTTP200就算手机验收。

## 6. 成功标准和资料边界

- Demo：五个tab可看，始终有教学标识；不执行任务。
- 真实工作流：本人指定群发任务→机器人接单编号→实际产物→同号回群和看板记录。
- 手机：验证登录、刷新、查看；重置和手动检查按钮不可提交。
- 工单完成与整期影片完成不同，制作百分比不是后台执行时间。
- 本包不含作者的声音参考/声纹、视频工程、素材、登录文件、服务器配置或真实聊天数据库。

源码、本站和网盘搭建包应核对各自标注的版本/哈希；本站和网盘提供同一个自定义ZIP，GitHub自动源码ZIP打包格式可能不同，不应直接混用SHA256。
