# ADR-007: 权威分层 —— Vault 与 SQLite 的职责边界 (Authority Split)

- **状态**: Accepted
- **日期**: 2026-09-16
- **决策者**: 用户 & Sol (GPT-5.6 Sol Pro Extended)
- **评审来源**: Oracle Job `2ecfbf15-5e89-49ba-a839-77e52acd0544`（Q3 / Q4 裁决）
- **不可逆属性**: **不可逆决策**

## 背景与问题

论文工作台需要持久化五类数据：Paper 身份与绑定、PaperSource、PaperNote、Annotation、阅读状态与阅读现场。

这些数据的**写入频率、设备局部性、Obsidian 可见性需求**截然不同：

- Paper 身份与绑定：低频变更，必须随文件夹移动，必须被 Obsidian 与 P1 AI 读取；
- 笔记正文与标签：用户手工编辑，天然属于 Obsidian 知识资产；
- Annotation：用户产生的知识，需随论文移动；
- 阅读状态与阅读现场：**高频变更**（滚动即变），设备局部，与知识内容无关。

若全部写 Vault，会出现：每次打开论文都触发文件写入、首次自动转 READING 时与 Obsidian 编辑产生无意义冲突、大量备份与文件系统事件、未来多设备双向合并问题。

若全部写 SQLite，会出现：Paper 身份与用户的 Obsidian 知识资产脱节，P1 AI 与 Obsidian 无法读取，违背「Paper 作为一级实体、天然兼容 Obsidian」的核心原则。

## 决策内容

采用**严格边界**的混合权威模型。每个字段只有一个权威源，**禁止双写**。

| 数据 | 权威位置 | SQLite 保存什么 |
|---|---|---|
| Paper 身份 (`paper_id`)、标题覆盖、来源绑定、Paper tags、external_ids | **Vault manifest** | 无（只读镜像用于查询加速） |
| 当前 folder / category、binding state | — | **SQLite 派生索引** |
| 翻译文件解析出的 arXiv / 方向 / 推荐度 | — | **SQLite，且必须标记为 `derived` provenance** |
| PaperNote 正文、note tags | **Vault Markdown** | 路径、hash、索引字段镜像 |
| Annotation 集合 | **Vault JSON sidecar** | 仅可重建索引 |
| 阅读状态 `UNREAD/READING/COMPLETED` | — | **SQLite 权威** |
| `firstOpenedAt` / `lastOpenedAt` / `completedAt` | — | **SQLite 权威** |
| WorkspaceState（页码、滚动、光标） | — | **SQLite 权威** |
| Panel 宽度与折叠状态 | — | 均不在以上两者：**localStorage**（浏览器 UI preference） |

### 关键约束一：阅读状态不写入 Vault

**P0 明确接受：阅读状态不在 Obsidian 中原生可见。**

理由：
- 状态与阅读时间是高频、设备局部的操作状态；
- 写进 `notes.md` 或 manifest 会导致每次打开论文都触发 Vault 写入；
- 首次打开自动 `UNREAD → READING` 会与用户正在 Obsidian 中编辑的同一文件产生无意义冲突；
- 状态修改会产生大量备份与文件系统事件；
- 未来多设备时出现双向合并问题。

**明确禁止**：不要同时在 SQLite 和 frontmatter 保存 `status`，也不要做「尽力同步」。这会制造两个权威源，是后续一切数据不一致的根源。

### 关键约束二：Tag 的双层边界

| 类型 | 含义 | 权威位置 |
|---|---|---|
| `paper_tags` | 论文主题分类（如 `#Transformer`、`#RAG`） | Vault manifest |
| `note_tags` | 这份笔记自己的分类 | `notes.md` frontmatter / 正文 |

方向目录（如 `04-Harness执行框架`）是 `category_path`，**不是**自动写入的 tag。

从翻译 blockquote 推导出的方向与推荐度**必须标记为 `derived`**，用户确认前不得转为显式 tag。

### 关键约束三：状态机规则

```
UNREAD --[首个可读来源成功加载]--> READING
READING --[仅用户手动触发]--> COMPLETED
```

- `UNREAD → READING` 的触发条件是「首个可读来源**成功加载**」，**不能在用户仅点击列表项时触发**；
- `READING → COMPLETED` **只能用户手动触发**，不做自动判断；
- 打开 `COMPLETED` Paper **不自动降级**；
- 所有时间统一 UTC ISO-8601。

### 关键约束四：SQLite 不再是纯缓存，必须按状态库对待

由于 SQLite 承载权威状态，它不再是可以随时重建的缓存。必须：

- 使用 **WAL** 模式；
- `PRAGMA foreign_keys=ON`；
- 配置 `busy_timeout`；
- 使用 **SQLite backup API** 做周期快照 —— **不能只复制运行中的 `.db` 文件**（WAL 模式下会得到不一致备份）；
- 数据库迁移使用 `user_version` 或独立 migration 表。

建议附带 **append-only `paper_status_events`** 表，用于未来统计与回退审计。

### 关键约束五：WorkspaceState 的字段与写入策略

**不保存原始 pixel scrollTop**，改存可跨窗口尺寸恢复的比例值。

```typescript
WorkspaceState {
  paper_id: string
  active_pdf_source_id?: string
  active_markdown_source_id?: string
  active_pane: "PDF" | "MARKDOWN" | "NOTE"

  // 每个来源各自的位置（一个 Paper 可能有多个翻译来源）
  source_positions: map<source_id, Position>

  note_id?: string
  note_cursor_start?: number
  note_cursor_end?: number
  note_content_sha256?: string

  last_opened_at: datetime
  updated_at: datetime
  state_version: number
}

// Position 按来源类型区分
PDF_Position {
  page_index: number          // 内部固定 0-based
  page_offset_ratio: number   // 0..1
  scale: number
  rotation: number
  source_version: number
}
MARKDOWN_Position {
  heading_path: string[]
  block_id: string
  scroll_ratio: number        // 0..1
  source_version: number
}
```

**写入策略**：
- 滚动事件**只更新前端内存**；
- trailing debounce **约 750ms**；
- 最长落库间隔**约 5 秒**；
- **切换 Paper 时强制 flush**；
- `visibilitychange → hidden` 时**强制 flush**；
- **不依赖 `beforeunload`** 作为唯一保存机制；
- **不把阅读位置写 localStorage**。

最后几秒状态在浏览器崩溃时丢失是**可接受的**；为了零丢失而每个 scroll event 写 SQLite 不值得。

**源文件变化时的恢复规则**：

| 情况 | 行为 |
|---|---|
| source version 相同 | 完整恢复（页码 + offset + scale） |
| source hash/version 已变化 | 只恢复到相同页或顶部 heading，**不恢复细粒度 offset** |
| note SHA 不匹配 | **不恢复旧 cursor offset**（避免跳入错误文本） |

### 关键约束六：多设备语义

**P0 明确：`workspace_scope = LOCAL_ONLY`。不做合并、不做同步、不声称跨设备一致。**

## 收益与代价

**收益**
- 每个字段只有一个权威源，消除数据不一致的根本来源；
- 知识资产（身份/绑定/标签/笔记/标注）随 Vault 移动、可备份、Obsidian 与 P1 AI 可读；
- 高频操作状态不污染 Vault，不产生无意义的文件系统事件与编辑冲突。

**代价**
- **阅读状态不在 Obsidian 中可见** —— 这是明确接受的取舍；需要提供工作台内的状态视图作为补偿；
- 需要维护 SQLite 与 Vault 的装配逻辑（API 返回的是二者装配后的 aggregate）；
- SQLite 必须以状态库标准运维（WAL + backup API + migration），运维要求高于纯缓存。

## 相关契约

- `backend/app/paper/schemas/paper.workbench.schema.json`
- ADR-006（身份与 manifest）、ADR-008（Annotation 存储与 anchor）
- **ADR-010（跨介质一致性）** —— 本 ADR 定义了两个权威源各自管辖什么，但没有定义「两者暂时不一致时怎么办」。ADR-010 补上了这一环：检测 + 收敛，收敛方向恒为 Manifest → SQLite。
