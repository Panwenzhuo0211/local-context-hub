# GPT 聊天接入：本地端已准备，账号端待连接

此功能使用 OpenAI Secure MCP Tunnel。它保持本机服务不对公网监听，让 ChatGPT 按需读取已开放事项，并写回署名为 chatgpt 的记忆、任务和观点。数据库留在本机；返回给 GPT 的内容会传到 OpenAI。没有调用 Responses 或其他模型 API，不能据此承诺 ChatGPT 或隧道无限免费。

## 当前状态

- 官方 tunnel-client v0.0.16 已安装并核对 SHA256。
- 专用 MCP 接入端已部署，7 个工具，限制事项范围。
- 首次仅开放一个明确开放的事项；未默认开放其他事项和全部历史记录。
- 本地测试通过；尚未获得账号隧道 ID/运行密钥，未完成 GPT 聊天实际调用验证。

## 一次性账号连接

1. 在 https://platform.openai.com/settings/organization/tunnels 登录与 ChatGPT 相同的账号，创建或选择隧道，并关联目标 ChatGPT 工作区。创建需要 Tunnels Read + Manage；运行需要 Read + Use。
2. 在 Platform 创建限权运行密钥，不要把密钥发到聊天里。
3. 双击本地“连接 GPT 聊天.command”，按提示输入隧道 ID 和运行密钥。密钥不回显，不写入配置和日志；只交给隧道进程环境。
4. 在 ChatGPT 插件页面添加自定义 MCP，连接方式选 Tunnel，选择同一隧道并安装。授予访问前检查只开放的事项范围。
5. 在 GPT 聊天输入 @ 并选择 Context Hub 插件，要求“先恢复已有专利进度，注明记录日期”。验收应包括两件方向、历史文档版本、当时状态及待办。

电脑必须开机且隧道进程正常。重启或隧道停止后需重新运行连接入口并输入运行密钥。此版本尚未实现密钥钥匙串持久化及隧道自动登录启动。

## 双向边界

GPT 可以通过 remember 把关键结论写回本机，ZCode/Codex 下次查询会看到。没有实现 GPT 完整聊天自动下载归档；模型也未必每次主动调用 remember。不同作者和历史日期保留，不覆盖其他 AI 观点。

## 维护

代码：chatgpt-entry.py、context_hub/chatgpt_server.py、connect-chatgpt.py。私有配置 chatgpt-access.json 位于 ContextHub 数据目录，matters 是允许的事项 ID，allow_remember 控制写回。配置每次调用重新读取，删除事项可立即撤销后续访问。停止隧道也会关闭云端读取通道，不删除本地归档。

官方依据：
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://developers.openai.com/api/docs/guides/custom-mcp-server
