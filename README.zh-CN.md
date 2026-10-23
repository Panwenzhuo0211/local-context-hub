# Context Hub

[![CI](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml/badge.svg)](https://github.com/Panwenzhuo0211/local-context-hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**一个本地、只读的 AI 会话归档与"共享事项"层，带 MCP 检索与署名笔记。**

[English](README.md) · 简体中文

如果你同时使用多个终端 AI 代理（OpenAI Codex CLI、ZCode），每个代理都有自己独立的会话历史，彼此不可见。交接上下文只能靠复制摘要或重新解释。Context Hub 在本地解决这个问题：

1. **只读适配器**把两个代理的会话数据库和 JSONL 日志归档进一个版本化 SQLite 库——绝不修改源数据。
2. **事项（matter）** 取代"一个文件夹一个项目"：一个事项可以横跨多个目录、多个会话、多个代理；一个会话也可以属于多个事项。
3. **MCP 服务器**提供有边界的工具，任何接入的代理都能按自然语言名称恢复事项、分页读取证据、写回署名笔记——不同代理的分歧并列保留，绝不自动合并。

全部本地运行：无模型调用、无遥测、无网络监听。

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

- **无损且版本化**：每次内容变化都是一条新的不可变修订；被删除的源记录保留在档案中。
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

`continue_matter`（按名称恢复事项）、`list_matters`、`search_context`（全文检索）、`read_record`（按证据编号分页精读）、`read_notes`、`remember`（写回署名笔记）、`create_matter`、`link_context`（关联/拒绝误关联）、`relate_matters`、`register_memory_source`、`context_status`、`refresh_context`。

笔记默认 `proposed`，不会被静默提升为已确认决策；`done` 的任务重新打开时历史完整保留。

## 隐私与安全边界

- 纯本地：无网络调用、无遥测、无监听端口；MCP 走标准输入输出。
- 源库以只读方式打开（`mode=ro` + `query_only`）；Hub 绝不写代理数据目录，拒绝把数据目录放进云盘同步位置。
- 创建的文件与目录在 POSIX 上为 `0600`/`0700`。
- 数据**不做静态加密**，**尚无密钥脱敏**——代理会话里出现过的内容都会原文落盘。请把数据目录当敏感数据处理。
- 工具返回给代理的内容会进入该模型的处理管线；本地保存不等于模型处理离线。
- ChatGPT 桥（官方 Secure MCP Tunnel）为**实验性**功能，限定显式事项白名单——见 [CHATGPT-SETUP.md](CHATGPT-SETUP.md)。

## 已知限制

- 检索是词法级（FTS5 + 中文二元分词），不是语义检索；同义表达请补别名。
- 标题自动关联可能误判：结果标 `auto`、附理由，可拒绝且拒绝后不被覆盖。
- 单条记录索引正文上限 200,000 字符（完整正文仍可用 `read_record` 分页读取）。
- 多个源库之间没有原子快照；未落盘内容不在覆盖范围。
- 无 ordinal 的 JSONL 日志退化为字节偏移身份：文件前部插入会导致事件重新键入（旧修订保留，陈旧键可能残留）。当前 Codex 日志始终携带 ordinal。详见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。

## 开发

```sh
python -m unittest discover -s tests   # 37 项测试，< 1 秒
```

CI 在 macOS、Linux、Windows × Python 3.11/3.13 上运行同一测试套件。参见 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
