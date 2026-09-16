# ADR-008: Annotation 存储与 Anchor 定稿 (Annotation Storage & Anchor Schema)

- **状态**: Accepted
- **日期**: 2026-09-16
- **决策者**: 用户 & Sol (GPT-5.6 Sol Pro Extended)
- **评审来源**: Oracle Job `2ecfbf15-5e89-49ba-a839-77e52acd0544`（Q2 裁决）
- **不可逆属性**: **不可逆决策** —— Anchor schema 是 Annotation 中重构成本最高的部分

## 背景与问题

论文工作台需要持久化三类用户标注：PDF 高亮与批注、Markdown 翻译高亮与批注、以及结构化的感想/创新点/疑问。

候选存储位置有三个：

- (a) 每篇论文一个 JSON sidecar（Vault 内）；
- (b) 写入 `notes.md` 的 frontmatter 或结构化区块；
- (c) 统一放 `~/.personal-ai-workspace/papers/papers.db`（SQLite）。

同时必须定义 **anchor（锚点）格式** —— 标注如何定位到原文的具体位置。这是整个 Annotation 体系中重构成本最高的部分：一旦落地错误，所有历史标注在 PDF 换版或 Markdown 重渲染后会集体失效。

## 决策内容

### 1. 权威存储位置裁决

| 候选 | 裁决 |
|---|---|
| **(a) Vault JSON sidecar** | ✅ **权威存储** |
| **(c) SQLite** | ✅ 派生索引与查询缓存 |
| **(b) notes.md frontmatter/结构化区块** | ❌ **禁止作为 Annotation 数据库** |

默认文件名：**`paper.annotations.json`**，位于论文夹内。

**为什么必须选 (a)**

标注、高亮、感想是用户产生的**知识资产**，不应只困在私有数据库中。JSON sidecar 可以随论文夹移动、进入 Vault 备份、保留稳定 ID 与 source locator、被 P1 的 AIContext 直接装配。

**为什么禁止 (b)**

- 每次标注都要改写用户**正在编辑**的笔记；
- 与 Obsidian 并发冲突显著增加；
- frontmatter 迅速膨胀；
- Markdown 结构被机器区块绑死；
- 用户手工移动章节后难以保持结构完整。

**SQLite 的地位**：可以对 sidecar 建索引，但**数据库删除后必须能仅凭 Vault 重建 Annotation 索引**。

### 2. sidecar 文件格式

```json
{
  "schema_version": 1,
  "paper_id": "pw_...",
  "annotations": [
    {
      "annotation_id": "ann_...",
      "source_id": "src_...",
      "kind": "HIGHLIGHT",
      "body_markdown": "",
      "selected_text": "…",
      "anchor_schema_version": 1,
      "anchor": {},
      "source_sha256": "…",
      "source_version": 3,
      "created_at": "…",
      "updated_at": "…",
      "deleted_at": null,
      "orphaned_at": null,
      "revision": 1
    }
  ]
}
```

`kind` 取值：`HIGHLIGHT` / `COMMENT` / `THOUGHT` / `INNOVATION` / `QUESTION` / `CONCLUSION`。

### 3. 删除语义（继承零删除铁律）

删除标注**必须**写 `deleted_at`，**不得从数组中物理移除**。

```
"deleted_at": "2026-09-16T00:00:00Z"
```

### 4. PDF Anchor 定稿

```json
{
  "type": "PDF_TEXT",
  "page_index": 0,
  "page_label": "1",
  "rotation": 0,
  "quad_points_normalized": [],
  "text_quote": {
    "exact": "...",
    "prefix": "...",
    "suffix": "..."
  }
}
```

**硬性规则**

| 规则 | 说明 |
|---|---|
| `page_index` 固定 **0-based** | UI 页码使用 `page_label`；内部与 API 一律 0-based |
| 坐标**必须**相对于 PDF crop box **标准化** | 禁止存 CSS 像素 —— 缩放、窗口尺寸、屏幕 DPI 变化都会使像素坐标失效 |
| **必须**同时保存 `text_quote` | 坐标丢失或重排后，Text Quote 是唯一的重新定位后备 |

### 5. Markdown Anchor 定稿

```json
{
  "type": "MARKDOWN_TEXT",
  "heading_path": ["3 Method", "3.2 Training"],
  "block_fingerprint": "...",
  "text_position": { "start": 318, "end": 371 },
  "text_quote": {
    "exact": "...",
    "prefix": "...",
    "suffix": "..."
  }
}
```

**硬性规则**

- **禁止只保存 DOM selector** —— Markdown 重新渲染、KaTeX 公式、表格与代码块都会改变 DOM 结构；
- 必须保存 `heading_path`（结构性定位）+ `block_fingerprint`（内容定位）+ `text_quote`（后备定位）三重保障。

### 6. 源文件变化时的 orphan 处理

标注必须携带 `source_sha256` 与 `source_version`。

| 情况 | 行为 |
|---|---|
| source hash 变化 | Annotation 进入 **`ORPHANED` / `NEEDS_REANCHOR`** 状态 |
| 禁止行为 | **禁止仍在旧坐标显示** —— 会产生假关联，让用户误以为标注仍指向原文 |

即：同一路径 PDF 被替换为新版本后，旧 page 5 的坐标**可能仍然能显示**，但对应文本已经不同。必须靠 hash 检测并标记。

### 7. 写入并发保护

Annotation 写入**必须携带 sidecar 当前 SHA256**，不匹配时返回 409 并停止写入（继承 ADR-002 的乐观锁不变量）。

API 用 **operation** 写入（单条增删改），**不允许前端随意上传未经校验的整份 JSON** —— 整份上传会绕过并发检查并可能静默丢弃他处新增的标注。

### 8. 与 Obsidian 的关系

JSON 不会原生显示为 Obsidian 笔记。补偿方案：

- **P0**：工作台内提供 Annotation 列表视图；
- **P0.5**：提供「提升到笔记」或显式导出为 Markdown。

**「提升到笔记」是一次复制，不能形成双向同步。**

## 收益与代价

**收益**
- 标注作为知识资产随 Vault 移动、可备份、可被 P1 AI 直接装配；
- 不干扰用户正在编辑的笔记，不产生 frontmatter 膨胀；
- Anchor 同时携带坐标与 Text Quote，在重排与缩放后仍可恢复；
- hash 变化时显式标记 orphan，避免假关联。

**代价**
- JSON 在 Obsidian 中不可直接阅读，需 P0.5 提供导出；
- 需实现双重 anchor 校验逻辑（坐标匹配失败时回退 Text Quote）；
- sidecar 与 SQLite 索引需要一致性维护（以 Vault 为准，SQLite 可重建）。

## 相关契约

- `backend/app/paper/schemas/paper.annotations.schema.json`
- ADR-002（零删除）、ADR-006（身份与 manifest）、ADR-007（权威分层）
