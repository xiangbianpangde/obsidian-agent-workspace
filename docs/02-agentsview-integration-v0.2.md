# AgentsView 接入技术方案 v0.2 (第二个 P0 · 开发合同)

> 修订基线：v0.1 + Sol 架构评审修订清单 (2026-09-03)  
> 状态：架构终审版 (Ready for Implementation)

---

## 1. 目标与定位

根据用户 P0 需求：
> **接入 agentsview 项目，通过 agentsview 项目得到我的 ai 会话情况。**

### 1.1 系统定位
- **双核工作台**：
  - **Obsidian 知识中心**：负责个人笔记、学习记录、学术研究资产（Create/Read/Update 受控，Delete 严禁）；
  - **AI 会话中心 (AgentsView)**：负责开发者与 AI Agent（Pi、Codex、Claude、Hermes 等）的交互流感知与历史回溯（严格只读，Read-Through Only）；
- **非目标 (Non-Goals)**：
  - 不替代 AgentsView，不重复实现会话转录解析器与费用算法；
  - 严禁工作台启动第二个 AgentsView 写守护进程（避免多 Writer 竞争）；
  - 不侵入修改 `~/.agentsview/sessions.db`，严禁对其执行 `CREATE INDEX`、`ALTER` 或任何写事务。

---

## 2. 系统整体架构

```text
                  个人工作台 前端界面 (Web UI)
                               │
       ┌───────────────────────┴───────────────────────┐
       ▼                                               ▼
  Obsidian 知识中心                              AI 会话中心
  文件树 / 编辑器 / 状态中心                 全景概览 / Agent 矩阵 / 会话列表 / 详情流
       │                                               │
       ▼                                               ▼
FastAPI 核心路由 (/api/files, /api/tags)      FastAPI 适配路由 (/api/agentsview/*)
       │                                               │
       ▼                                               ▼
  Vault Adapter                               AgentsView Adapter
                                              (Authority: Session API DTO)
                                                       │
                                            ┌──────────┴──────────┐
                                            ▼                     ▼
                                       CLI Transport         SQLite-RO Transport
                                       (P0 默认基线)          (低延迟直接查询回退)
                                            │                     │
                                            └──────────┬──────────┘
                                                       ▼
                                            ~/.agentsview/sessions.db
                                            (只读，mode=ro，严禁 immutable=1)
```

---

## 3. 数据契约与传输适配层 (Adapter Design)

### 3.1 权威契约
工作台不以 `sessions.db` 内部私有表结构为公共契约，而是以 **AgentsView 官方稳定的 Session API DTO** 作为上层交互标准：

- **Session DTO (`SessionSummary`)**：
  - `id`: string (会话唯一标识)
  - `project`: string (所属项目名称)
  - `agent`: string (`pi`, `codex`, `claude`, `hermes`, `grok` 等)
  - `title`: string (首条提问或展示标题)
  - `started_at`: ISO datetime
  - `ended_at`: ISO datetime
  - `message_count`: integer
  - `user_message_count`: integer
  - `machine`: string (如 `local`)
  - `cwd`: string (工作区绝对路径)
  - `git_branch`: string (Git 分支，P1 关联预留)

- **Message DTO (`SessionMessage`)**：
  - `session_id`: string
  - `ordinal`: integer (序号)
  - `role`: string (`user`, `assistant`, `system`, `tool`)
  - `content`: string (文本正文)
  - `created_at`: ISO datetime

- **Tool Call DTO (`ToolCall`)**：
  - `id`: string
  - `tool_name`: string
  - `arguments`: string / object
  - `result_summary`: string
  - `created_at`: ISO datetime

### 3.2 传输实现
- **CLI Transport (推荐主路径)**：
  - 调用 `/Users/xbpd/.local/bin/agentsview session ... --json`；
  - 天然复用 AgentsView 官方对 daemon 运行状态的自动判断（daemon 在线走 HTTP，不在线走只读 SQLite）；
- **SQLite-RO Transport (低延迟只读直连回退)**：
  - 路径：`Path("~/.agentsview/sessions.db").expanduser().resolve()`；
  - 连接 URI：`file:<path>?mode=ro`（**坚决删除 `immutable=1`**，避免 live DB 脏读）；
  - PRAGMA 强化：`PRAGMA query_only=ON; PRAGMA busy_timeout=2000;`；
  - 请求即连，查询即关，绝不在内存长期持有只读事务。

---

## 4. API 契约设计

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/agentsview/status` | 无 | 探测连通性、版本号、传输通道、总会话数、总消息数、最新活跃时间 |
| GET | `/api/agentsview/overview` | 无 | **工作流全景**：Agent 使用矩阵分布、活跃项目排行榜、最近 24h / 7d 会话数、最近 10 条活跃会话 |
| GET | `/api/agentsview/sessions` | `agent?`, `project?`, `q?`, `limit=50`, `offset=0` | 会话列表有界查询 |
| GET | `/api/agentsview/session/{id}` | 路径参数 | 会话元数据、上下文路径、Token 估计 |
| GET | `/api/agentsview/session/{id}/messages` | `from=0`, `limit=50` | **有界分页消息流**（禁止全量 dump） |
| GET | `/api/agentsview/session/{id}/tool-calls` | 无 | 会话工具调用明细回溯 |

---

## 5. 安全与隐私防护边界 (P0 硬边界)

1. **绝对只读，绝不写库**：
   - 连接使用 `mode=ro` 和 `PRAGMA query_only=ON`，工作台代码无写权限；
2. **会话内容只读直通 (Read-Through Only)**：
   - 会话对话正文、代码片段绝不保存到工作台自己的 SQLite 数据库中；
   - 会话内容不打印到后端 Application 日志；
   - 会话 API 统一注入响应头：`Cache-Control: no-store`；
3. **输出层 XSS 清洗**：
   - 会话中的用户提问、Agent 回复、工具调用结果在前端统一经过 `DOMPurify.sanitize()` 与 `escapeHtml()` 处理，代码块通过 Highlight.js 格式化。

---

## 6. 前端交互设计

在工作台顶栏提供一级切换：
- **【知识中心 (Obsidian)】** ↔ **【AI 会话中心 (AgentsView)】**

AI 会话中心界面结构：
1. **顶部工作流活跃度指标看板**：
   - 展现总会话、近 24 小时、近 7 天活跃指标；
   - **Agent 矩阵筛选胶囊**：`全部`、`Pi (537)`、`Claude (486)`、`Codex (395)`、`Hermes (91)`、`Grok (80)` 等，点击瞬间过滤；
2. **会话过滤工具栏**：
   - 项目下拉选择器（快速定位如 `个人工作台`、`医学问诊助手`、`xbpd` 等特定工程）；
   - 标题与关键词即时检索框；
3. **左列表 · 右沉浸详情双栏布局**：
   - 左侧会话卡片流（显示标题、所属项目标签、Agent 图标、交互轮次、时间）；
   - 右侧会话回溯抽屉/面板：
     - 上方：会话元数据（工作目录 `cwd`、Git 分支、起止耗时）；
     - 中部：Tab 切换【消息对话流 (Messages)】与【工具调用明细 (Tool Calls)】；
     - 消息对话流以清晰气泡排版用户提问与 Agent 代码答复，支持代码高亮与公式渲染。

---

## 7. 验收指标 (可测)

1. ✅ 自动探测并接入 `~/.agentsview/sessions.db` 与 CLI，正确返回系统状态与版本信息；
2. ✅ Overview 正确呈现 12 种 Agent 分布矩阵、项目排行及近 7 天工作活跃度；
3. ✅ 会话列表支持按 Agent（如仅看 Pi 或仅看 Codex）及项目精准筛选；
4. ✅ 打开具体会话，消息流支持有界分页拉取并清晰渲染用户提问与 Agent 答复；
5. ✅ 支持回溯该会话中的工具调用（Tool Calls）记录；
6. ✅ 安全底线：底层严禁执行任何写入，会话接口携带 `Cache-Control: no-store`，输出经 DOMPurify 严密清洗。
