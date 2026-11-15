# 看板

v0.2.1已包含第026期演示的同局域网查看。只看界面可先运行 `python demo_dashboard.py --open`；详见 [快速上手](QUICKSTART_E026.md)。

安装 `requirements-dashboard.txt` 后运行 `python dashboard.py --open`，默认 http://127.0.0.1:18765/ 。也可使用启动脚本。

页面分为总览、工单、账号额度、视频制作、官网运维。没有数据就显示空/未知状态。通过当前 Studio 根目录的 `control_center.integration.json` 指向自己的 agent 仓库绝对路径；该文件不提交。

```powershell
python dashboard.py --studio-root "C:/Projects/YourStudio" --open
```

工单读取 `config.json` + `config.local.json`，不读 Telegram secrets。制作记录读取自己的 Episodes、清单和已有交接；不会复制或上传原片。剪映项目只检查允许目录的metadata，不解析加密草稿。

工单与制作状态独立；制作百分比是台账的辅助解释，旧台账可能滞后于实际交付/发布。公开链接、AI检查或文件存在不会自动产生人工批准。

普通刷新不运行解码或全套测试；手动 Validate 调已有契约引擎及私密路径防护，不做sync、mark、批准或发布。账号额度通过本机CLI读取并缓存，不启动模型推理；重置仅由本人确认，结果持久化并复用请求ID，不能在待确认时删除记录。

退出控制台会停止服务；重启后需重新运行，没有安装系统服务/登录自启。外部Studio的启动/停止使用同一参数：

```powershell
.\start_dashboard.ps1 -StudioRoot "C:/Projects/YourStudio"
.\stop_dashboard.ps1 -StudioRoot "C:/Projects/YourStudio"
```

停止器核对服务标识、工作区和进程，不批量结束其他Python程序。

## 自行启用手机查看

```powershell
python dashboard.py --enable-lan 192.168.50.10/24 --open
# 读取外部Studio时仍使用同一个根目录
python dashboard.py --studio-root "C:/Projects/YourStudio" --enable-lan 192.168.50.10/24 --open
```

这里的 `192.168.50.10/24` 只是教学地址，必须替换成自己的电脑接口与子网前缀。可用 `Get-NetIPConfiguration` 查看，不需要改系统设置。首次显式启用生成随机密码，后续配置沿用该密码；配置位于选定Studio根目录的 `output/control_center/lan-access.local.json`，不提交Git。

手机打开控制台列出的地址，用户名 `viewer`，密码看电脑控制台。默认仍仅本机；开启后绑定所有IPv4接口，但只接受显式配置的RFC1918目标地址和对应子网客户端，其他Host/客户端被拒绝，未登录返回401，错误密码限速。不要设置公网端口转发。

手机只能查看，不能POST重置或Validate，API不向远端发重置令牌；本机127.0.0.1使用方式保留。HTTP仅适用于自己的可信局域网，不提供公网/TLS部署。电脑和服务须保持运行；手机127.0.0.1指手机自己。首次是否被防火墙或Wi-Fi隔离须在本人手机实际验收，本工具不修改这些系统设置。
