# ADR-006: Paper 身份与版本化 manifest (Paper Identity & Versioned Manifest)

- **状态**: Accepted
- **日期**: 2026-09-16
- **决策者**: 用户 & Sol (GPT-5.6 Sol Pro Extended)
- **评审来源**: Oracle Job `2ecfbf15-5e89-49ba-a839-77e52acd0544`（Q1 裁决）
- **不可逆属性**: **不可逆决策** —— 一旦实现错误，P1 接入时重构成本极高

## 背景与问题

论文工作台 P0 的首要问题是：**如何建立稳定的 Paper 一级实体**。

需求文档假设的固定三件套（`paper.pdf` / `translation.md` / `notes.md`）在真实 Vault 上被证伪：

- `02. 🟡 归类 Arrange/论文/` 下存在 **253 个论文夹、620 个 PDF、414 个翻译文件、582 个 md**；
- 结构为**三层**（论文根 / 8 个方向分类 / 论文夹），而非需求的单层 `Papers/`；
- 文件名高度异构：`{标题}_翻译导读.md`、`{标题}_全文翻译.md`、`{标题}_中文翻译.md`、MinerU 的 `full.md`、以及完全自定义名（`元上下文学习.md`、`d4ea515293dd7bfd.md`）；
- **85 个论文夹含 MinerU 嵌套同名子目录**（`full.md` + `_origin.pdf` + `_layout.pdf` + JSON），`_layout.pdf` 单文件达 23MB；
- **`notes.md` 完全不存在** —— 它是需要新建的概念。

因此必须回答：Paper 的身份从何而来？目录重命名、分类移动、PDF 换版之后，身份如何保持稳定？

## 决策内容

### 1. Paper / Source / Note 身份采用随机稳定 UUID

```
paper_id  = "pw_"   + UUIDv4
source_id = "src_"  + UUIDv4
note_id   = "note_" + UUIDv4
```

ID 写入论文夹内的 manifest，**绝不**由路径、标题或 PDF 内容哈希派生。

**理由（为什么不能用路径/标题/哈希派生 ID）：**

| 候选派生源 | 失效场景 |
|---|---|
| 路径 | 论文在分类间移动、夹名重命名、大小写或 Unicode 形式变化 |
| 标题 | 用户修改中文译名；同一论文在不同分类下标题不一致 |
| PDF SHA256 | 同一 PDF 可能被复制到不同方向并有独立笔记与阅读状态；PDF 会被替换为出版社版或 arXiv 新版；一篇论文可能有多个版本 |
| DOI / arXiv ID | 并非每篇都有；同一论文可能存在多个版本 ID |

PDF SHA256 **仅用于**：来源版本识别、重命名恢复、重复候选提示。

### 2. manifest 文件格式与位置

文件名：**`paper.workbench.json`**，位于论文夹根目录。

```json
{
  "schema_version": 1,
  "paper_id": "pw_3d9e...",
  "title_override": null,
  "sources": [
    {
      "source_id": "src_a12f...",
      "role": "ORIGINAL_PDF",
      "path": "Agent橙皮书.pdf",
      "primary": true,
      "active": true
    },
    {
      "source_id": "src_810c...",
      "role": "TRANSLATION_GUIDE",
      "path": "Agent橙皮书_翻译导读.md",
      "primary": false,
      "active": true
    },
    {
      "source_id": "src_c908...",
      "role": "TRANSLATION_FULL",
      "path": "Agent橙皮书_全文翻译.md",
      "primary": true,
      "active": true
    }
  ],
  "note": { "note_id": "note_f8cd...", "path": "notes.md" },
  "annotation_store": "paper.annotations.json",
  "tags": [],
  "created_at": "2026-09-16T00:00:00Z",
  "updated_at": "2026-09-16T00:00:00Z",
  "inactive_at": null
}
```

**路径规则**：所有 `path` 必须是**相对于 manifest 所在论文夹**的路径，**禁止 `..`**。这样整个论文夹在分类间移动或重命名时，绑定关系不失效。

### 3. 为什么不用 `.paper.yml` 或 `00-paper.md`

| 候选 | 否决理由 |
|---|---|
| `.paper.yml` | 点文件在不同同步、备份、文件浏览工具中行为不一致；YAML 类型推断与 round-trip 易制造格式噪声；项目当前无可靠的保注释 YAML 写回基础 |
| `00-paper.md` | 会进入现有 Markdown 索引；与分类级 `00-索引.md`、翻译、笔记混杂；253 个文件将明显污染 Obsidian 文件树 |

### 4. 发现 / 采纳两阶段（Discovery–Adoption）

**绝不在普通扫描过程中静默批量写 253 个 manifest。**

- **扫描阶段**：只产生 `DISCOVERED` 候选，写入 SQLite 派生索引，**不触碰 Vault**；
- **采纳阶段**：Paper 首次产生依赖状态时才创建 manifest ——
  - 首次成功打开并要从 `UNREAD` 转为 `READING`；或
  - 添加 Paper tag；或
  - 创建笔记；或
  - 创建标注；或
  - 手动确认来源绑定。
- manifest 创建成功后，Paper 进入 `ADOPTED` 状态。

**收益**：未打开的论文不会被无声写入文件系统；一旦产生状态、笔记或标注，身份又已被稳定锚定。

### 5. 合法 Paper 的最低条件

最低合法条件是「**至少有一个可读来源**」，而不是三件套齐全：

| 形态 | 是否合法 |
|---|---|
| PDF-only | ✅ 合法 |
| PDF + 导读 | ✅ 合法 |
| PDF + 多个翻译 | ✅ 合法 |
| 无笔记 | ✅ 合法 |
| 只有自定义 Markdown、无 PDF | ⚠️ P0 不自动采纳，允许后续手动建立 |

### 6. 复制论文夹导致的 ID 冲突必须 fail closed

用户复制整个文件夹时会连 manifest 一起复制，产生两个相同 `paper_id`。

**决策**：全局唯一约束；扫描时两个位置同时出现即进入 `DUPLICATE_ID_CONFLICT`，**不自动选胜者**。用户确认后对副本执行「fork identity」，生成新 ID。

## 收益与代价

**收益**
- Paper 身份在重命名、跨分类移动、PDF 换版后保持不变，保证 Annotation 与 WorkspaceState 的引用完整性；
- 一个 Paper 可合法拥有多个 PDF 与多个翻译来源，真实 Vault 形态被正确建模；
- 扫描阶段零写入，避免对用户 Vault 造成意外污染。

**代价**
- 每个被采纳的论文夹会多出一个 `paper.workbench.json`（253 篇上限，且仅在被采纳时产生）；
- 需要实现发现/采纳两阶段状态机与冲突检测，而非一次性批量写入。

## 相关契约

- `backend/app/paper/schemas/paper.workbench.schema.json`
- ADR-007（权威分层）、ADR-008（Annotation 存储与 anchor）
