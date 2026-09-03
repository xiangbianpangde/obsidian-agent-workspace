# AgentsView 接入技术方案 v0.1 (第二个 P0)

> 项目：个人工作台 (Personal AI Workspace) · 模块：AI 会话中心 (AgentsView Adapter)  
> 日期：2026-09-03 · 状态：待 Sol 架构评审

---

## 1. 目标与定位

根据用户 P0 需求：
> **接入 agentsview 项目，通过 agentsview 项目得到我的 ai 会话情况。**

### 1.1 用户实际场景
- 用户使用 **pi**、**codex**、**claude** 等多个 coding agents 开发程序；
- 机器上已安装 `agentsview v0.40.1`，其核心数据存储在本地 SQLite：`~/.agentsview/sessions.db`（约 1.1GB，包含 1700+ sessions、43000+ messages，覆盖 pi、codex、claude、hermes、grok 等 12 种 Agent）；
- 核心目标：**在个人工作台中直接查看各 Agent 的会话全景、活跃度、各项目会话历史，并能深入回溯消息流与工具调用。**

---

## 2. 接入架构设计

```text
               个人工作台 前端界面 (Web UI)
                             │
       ┌─────────────────────┴─────────────────────┐
       ▼                                           ▼
  Obsidian 知识层 (原模块)               AI 会话中心 (新增模块)
  文件树 / 编辑器 / 状态中心             全景概览 / Agent 矩阵 / 会话列表 / 详情回溯
       │                                           │
       ▼                                           ▼
FastAPI 后端 (/api/files, /api/tags...)     FastAPI 后端 (/api/agentsview/...)
       │                                           │
       ▼ (读写受控)                                ▼ (严格只读)
本地 Obsidian Vault                     ~/.agentsview/sessions.db
```

### 2.1 架构原则
1. **严格只读 (Read-Only / Immutable)**：
   - 工作台只读查询 `~/.agentsview/sessions.db`；
   - 采用 SQLite URI 只读模式：`file:~/.agentsview/sessions.db?immutable=1&mode=ro`；
   - 绝不在连接上执行任何 INSERT/UPDATE/DELETE/ALTER，不加写锁，与 agentsview 原生后台 daemon 和平共存。
2. **单机本地优先 (Local-First)**：
   - 会话与 token 隐私数据完全留在本机，不上传任何云端；
   - 配置化路径：在 `config.yaml` 中新增 `agentsview.database` 配置项，默认指向 `~/.agentsview/sessions.db`。

---

## 3. 数据层建模 (只读适配)

针对 `sessions.db` 中的核心表进行轻量只读建模：
- `sessions`：
  - `id`: 会话唯一标识
  - `project`: 所属项目名称
  - `agent`: Agent 引擎类型 (`pi`, `codex`, `claude`, `hermes`, `grok` 等)
  - `first_message` / `display_name` / `session_name`: 会话标题与首问
  - `started_at` / `ended_at`: 起止时间
  - `message_count` / `user_message_count`: 交互轮次
  - `file_size` / `file_mtime`: 原转录文件元数据
- `messages`：
  - `session_id`, `ordinal`, `role` (user/assistant/system), `content`, `created_at`
- `tool_calls` / `usage_events` (用于会话工具详情与 Token 消耗回溯)

---

## 4. 后端 API 契约设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/agentsview/status` | 检查 agentsview 数据库连通性、总会话数、总消息数、最新同步时间 |
| GET | `/api/agentsview/overview` | 会话全景看板：按 Agent 统计分布、按 Project 统计排行、时间趋势统计 |
| GET | `/api/agentsview/sessions` | 会话列表查询，支持过滤：`agent`、`project`、`search`、`limit`、`offset` |
| GET | `/api/agentsview/session/{id}` | 会话元数据详情、Token 消耗估计 |
| GET | `/api/agentsview/session/{id}/messages` | 会话消息流回溯（按 ordinal 排序，含用户提问与 Agent 回复） |

---

## 5. 前端交互设计 (工作台界面)

在现有的工作台顶栏或主布局中增加工作模式切换：
- **【知识工作区 (Obsidian)】**：原有文件树、编辑器、渲染器、状态中心；
- **【AI 会话中心 (AgentsView)】**：
  - **顶部指标卡**：
    - `总会话数`（如 1700+）
    - `Agent 矩阵`（Pi 537 · Claude 486 · Codex 395 · Hermes 91 · Grok 80）
    - `活跃项目数`（如 50+）
  - **筛选栏**：
    - Agent 单选胶囊（All / Pi / Codex / Claude / Hermes...）
    - 项目下拉选择器（按项目快速下钻）
    - 会话标题关键词搜索框
  - **会话双栏浏览区**：
    - 会话列表卡片流（显示标题、Agent 图标、所属项目、消息轮数、时间、相对耗时）；
    - 点击任一会话，右侧展开**沉浸式消息流回溯面板**，清晰查看历史上下文与代码对话。

---

## 6. 安全边界与验收指标

### 6.1 安全边界
1. `sessions.db` 必须以只读 URI 打开（`immutable=1` 或 `mode=ro`），代码层无写权限；
2. 即使 agentsview 后台 daemon 正在批量写库，只读连接不引发 lock 冲突；
3. 会话中提取的文本输出到前端时，100% 经过 `escapeHtml()`，杜绝 Stored XSS。

### 6.2 验收指标 (可测)
1. ✅ 自动探测并接入 `~/.agentsview/sessions.db`，读取到真实 1700+ 篇会话；
2. ✅ 正确统计 12 种 Agent 的会话数量与分布；
3. ✅ 支持按 Agent 与 Project 过滤筛选会话；
4. ✅ 打开具体会话可顺畅拉取完整的问答消息流；
5. ✅ 只读隔离验证：尝试在 agentsview 连接上执行写操作必定被底层拒绝。
