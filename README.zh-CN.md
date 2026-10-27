# Context Hub

[![CI](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**一个本地、只读的 AI 会话归档与"共享事项"层，带 MCP 检索与署名笔记。**

[English](README.md) · 简体中文

如果你同时使用多个终端 AI 代理（OpenAI Codex CLI、ZCode），每个代理都有自己独立的会话历史，彼此不可见。交接上下文只能靠复制摘要或重新解释。Context Hub 在本地解决这个问题：

1. **只读适配器**把两个代理的会话数据库和 JSONL 日志归档进一个版本化 SQLite 库——绝不修改源数据。
2. **事项（matter）** 取代"一个文件夹一个项目"：一个事项可以横跨多个目录、多个会话、多个代理；一个会话也可以属于多个事项。
3. **MCP 服务器**提供有边界的工具，任何接入的代理都能按自然语言名称恢复事项、分页读取证据、写回署名笔记——不同代理的分歧并列保留，绝不自动合并。

归档与索引在本地运行，无模型调用和遥测。默认 MCP 使用标准输入输出；可选文件通道保持本地，可选带认证 HTTP 入口仅监听本机，远程接入需另外配置 HTTPS。

## Muse 与以后加入的 Agent（0.6.1）

新 Agent 通过独立身份、显式事项授权和固定署名参与协作。支持共享讨论、引用回复、任务状态及摘要，保留不同意见。查询与写回无需新增会话格式适配器；完整聊天自动归档是另一项能力。

| 客户端已有能力 | 接入方式 |
|---|---|
| 本地 MCP 或已授权本机命令 | 生成的 MCP 配置或 `context-hub-agent` |
| 已授权本机写文件工具，没有命令能力 | `context-hub-file-bridge` 文件请求/回复 |
| 仅云端工具 | 单独配置可达 HTTPS 与受支持认证 |

主人运行 `context-hub agents add <英文标识> --matter <事项名称> --write` 登记。源码提供 `python agent-setup.py` 和 Mac 双击向导 `接入新Agent.command`，可创建首个事项、选权限、启用文件通道。文件通道需要后台持续运行。

Muse 已在一台 Mac 上完成真实查询、署名写回及另一端读回验收；每台机器仍需独立核验权限。文件通道要求已有写文件工具，仅生成配置或看到心跳不算连通。[完整接入说明](docs/AGENT-ENROLLMENT.md)

## 工作原理

```
Codex CLI (SQLite + JSONL) ──┐
                             ├─► 只读适配器 ──► 版本化归档 (SQLite)
ZCode (SQLite) ──────────────┘      （不可变修订；日志游标带前缀哈希改写检测）
                                              │
                                              ▼
                          事项库 + 本地全文索引（FTS5，中文二元分词）
                                              │
                                              ▼
                                  MCP stdio 服务器 ◄── 你的代理
                                              │
                                              ▼
                        署名笔记（作者、证据编号、状态历史）
```

关键性质：

- **版本化**：每次采集到的内容变化都是一条新的不可变修订；被删除的源记录保留在档案中。
- **抗改写采集**：日志以 inode/大小/mtime 指纹加 SHA-256 前缀哈希跟踪——截断、轮转、改写都会被检出并重读；不完整的末行延迟收录，绝不吞半条。保留文件属性的等长改写由每日重验发现。
- **事件身份 ≠ 内容身份**：相同正文的不同消息保留为独立事件（按位置/ordinal 键）；内容哈希决定修订。
- **事务性**：记录与游标同事务提交，失败整体回滚。
- **不可信设计**：归档的对话内容一律视为数据而非指令；工具输出有边界、可分页。

## 安装

```sh
pip install git+https://github.com/Panwenzhuo0211/local-context-hub.git
```

需要 Python 3.11+，支持 macOS / Linux / Windows，零第三方依赖。也可以用源码内 `./hub <命令>` 直接运行。

## 快速开始

```sh
context-hub init          # 建数据目录和默认配置
context-hub sync          # 只读归档两端当前会话
context-hub projects      # 列出已归档项目
context-hub context "论文修改"   # 按自然语言解析事项
```

默认数据目录：macOS `~/Library/Application Support/ContextHub`；Windows `%LOCALAPPDATA%\ContextHub`；Linux `$XDG_DATA_HOME/context-hub`。可用 `CONTEXT_HUB_DATA_DIR` 覆盖。

### 接入代理（MCP）

`context-hub-integrate` 会备份并修改两端客户端配置（可逆），找不到的客户端自动跳过——只装一个客户端也能用：

```sh
context-hub-integrate --dry-run    # 预览将写入的文件
context-hub-integrate              # 安装（--no-rules 跳过 AGENTS.md 规则注入）
context-hub-integrate --uninstall  # 完整移除；归档保留
```

macOS 上同时注册 launchd 服务（约每 180 秒归档）；Linux/Windows 请自行调度 `python3 -m context_hub.background`（cron/systemd/任务计划程序均可）。

手写配置示例见 [README.md](README.md#connect-an-agent-mcp)。

## 工具清单

`continue_matter`（按名称恢复事项）、`list_matters`、`search_context`（全文检索）、`read_record`（按证据编号分页精读）、`read_notes`、`discussion`（署名讨论与回复）、`remember`（写回署名笔记）、`create_matter`、`link_context`（关联/拒绝误关联）、`relate_matters`、`register_memory_source`、`context_status`、`refresh_context`、`handoff_session`、`prepare_resume`。

笔记默认 `proposed`，不会被静默提升为已确认决策；`done` 的任务重新打开时历史完整保留。

## 隐私与安全边界

- 归档无模型调用和遥测；默认 MCP 走标准输入输出，可选 HTTP 服务仅监听本机，远程连接需显式配置。
- 采集源库以只读方式打开（`mode=ro` + `query_only`）。接续会在目标端新建带 Hub 来源的镜像并保护已有会话；拒绝归档目录与采集源重叠或放进云盘。
- 创建的文件与目录在 POSIX 上为 `0600`/`0700`。
- 数据**不做静态加密**，**尚无密钥脱敏**——代理会话里出现过的内容都会原文落盘。请把数据目录当敏感数据处理。
- 工具返回给代理的内容会进入该模型的处理管线；本地保存不等于模型处理离线。
- ChatGPT 桥（官方 Secure MCP Tunnel）为**实验性**功能，限定显式事项白名单——见 [CHATGPT-SETUP.md](CHATGPT-SETUP.md)。

## 已知限制

- 检索是词法级（FTS5 + 中文二元分词），不是语义检索；同义表达请补别名。
- 标题自动关联可能误判：结果标 `auto`、附理由，可拒绝且拒绝后不被覆盖。
- 单条记录索引正文上限 200,000 字符（完整正文仍可用 `read_record` 分页读取）。
- 超过256 KiB的部分取证 raw 载荷会保留哈希和前缀，主对话正文另存；不保证所有附件、工具原始载荷逐字保全。
- Muse/GPT等客户端的完整聊天自动归档未实现；模型是否主动检索和保存摘要依赖客户端行为，不保证每条消息自动写回。
- 多个源库之间没有原子快照；未落盘内容不在覆盖范围。
- 无 ordinal 的 JSONL 日志退化为字节偏移身份：文件前部插入会导致事件重新键入（旧修订保留，陈旧键可能残留）。当前 Codex 日志始终携带 ordinal。详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 开发

```sh
python -m unittest discover -s tests
```

CI 在 macOS、Linux、Windows × Python 3.11/3.14 上运行同一测试套件。参见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
