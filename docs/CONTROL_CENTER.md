# 看板

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
