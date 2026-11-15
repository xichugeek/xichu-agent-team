## 本机网络与代理规则（站长要求）
- Codex 是 MSIX 应用。禁止修改代理或网络设置，包括 HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings 下的 ProxyEnable、ProxyServer、AutoConfigURL、Connections。
- 不设置 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 环境变量，不执行 netsh winhttp set proxy，不启动或停止代理客户端的系统代理。
- 需要切换代理时，请站长在 v2rayN 里自行切换系统代理或 TUN 模式。
- 单条命令需要测试代理时，只在该命令中临时指定代理（curl -x 或 Invoke-WebRequest -Proxy），不改全局配置。
