# 论文工作台 P0 · 真实环境勘察报告

> 勘察日期：2026-09-09 · 对象：`/Users/xbpd/Documents/xbpd_obsidian`
> 目的：把需求文档的假设与真实 Vault 结构对齐，识别工程风险点。

---

## 一、核心发现：真实结构与需求假设不同

需求文档假设：

```text
Papers/
└── Attention Is All You Need/
    ├── paper.pdf
    ├── translation.md
    └── notes.md
```

**真实结构**（253 个论文夹，620 个 PDF，414 个翻译文件，582 个 md）：

```text
02. 🟡 归类 Arrange/论文/
├── 01-智能体/                    (41 个论文夹)
├── 02-上下文工程/                (69)
├── 03-提示词工程/                (41)
├── 04-Harness执行框架/           (31)
├── 05-循环工程/                  (27)
├── 06-AI医疗/                    (39)
├── 07-Deepsearch/                (3)
└── 其他方向/
    ├── 07-教育AI与知识图谱/       (20)
    └── 08-通用AI与深度学习/       (5)
```

即：**三层结构 = 论文根 / 方向分类 / 论文夹**，而非两层。

### 1.1 单个论文夹的真实形态（三种）

**形态 A — 标准双语对（最常见，约 60%）**

```text
Agent橙皮书/
├── Agent橙皮书.pdf
└── Agent橙皮书_翻译导读.md
```

**形态 B — 标准双语对 + 全文翻译**

```text
Agent橙皮书/
├── Agent橙皮书.pdf
├── Agent橙皮书_翻译导读.md
└── Agent橙皮书_全文翻译.md
```

**形态 C — 含 MinerU 解析产物（80 个论文夹，85 个嵌套同名子目录）**

```text
SkillZipPro：.../
├── SkillZipPro：....pdf                      ← 顶层 PDF（阅读用）
├── SkillZipPro：..._翻译导读.md
├── SkillZipPro：..._全文翻译.md
└── SkillZipPro：.../                         ← 同名嵌套目录
    ├── full.md                               ← MinerU 全文 Markdown
    ├── <uuid>_origin.pdf                     ← 原始 PDF 副本
    ├── <uuid>_layout.pdf                     ← 版面分析 PDF（23MB 级）
    ├── <uuid>_content_list.json
    └── <uuid>_model.json
```

**命名不确定性**：还有少量论文夹使用完全自定义命名（如 `WikiSkill：..._中文翻译.md`、`元上下文学习.md`、`full_paper_zh_apa7.md`、`d3ea515293dd7bfd.md` 等），**不存在 `paper.pdf` / `translation.md` / `notes.md` 固定文件名约定**。

### 1.2 关键结论

| 需求假设 | 真实情况 | 工程影响 |
|---|---|---|
| `Papers/` 单层目录 | 论文根 / 方向分类 / 论文夹 三层 | 扫描器需支持**可配置根目录 + 递归深度** |
| 固定文件名 `paper.pdf` | 无固定名，同名 `{title}.pdf` | 必须**启发式关联 + 显式覆盖** |
| `translation.md` | `{title}_翻译导读.md` / `_全文翻译.md` / `full.md` / 自定义名 | 需**多候选评分**，且可能存在两个翻译候选 |
| `notes.md` | **完全不存在** | `notes.md` 是**新建概念**，需定义创建策略 |
| 无 frontmatter | 翻译文件用 blockquote 元数据（`> 方向判定`、`> arXiv 链接`） | 可解析 arXiv/方向/推荐度作为 Paper metadata 补充源 |
| — | 存在 `00-索引.md`（分类级索引） | 可作为分类元数据源，不可当作论文 |

---

## 二、现有代码基础

### 2.1 后端

- FastAPI，路由：`files` / `tags` / `templates` / `agentsview` / `im` / `schedule` / `assignment`
- 安全边界 `backend/app/security/path_guard.py`：
  - `_ALLOWED_ASSET_EXTS = {.png,.jpg,.jpeg,.webp,.gif,.bmp,.ico}` — **显式禁止 PDF**（原为「禁止内联嵌入主动内容」）
  - `resolve_for_asset_read()` 强制 vault 内 + 排除区 + 拒绝 symlink 逃逸
- Vault 扫描器 `backend/app/scanner/vault_scanner.py`：`extension_ignore` 忽略 `.pdf`
- 写入路径：`files.py` 的 `file_save` 已有 **SHA256 乐观锁（409）+ 备份 + 原子写**

**结论**：PDF 服务需新增**专用只读端点**（Range 支持 + `Content-Disposition: inline` + CSP），不能复用 `_ALLOWED_ASSET_EXTS` 白名单（那是为了阻止内联主动内容，与「专用 PDF 阅读器」语义不同）。

### 2.2 前端

- **单文件** `frontend/dist/index.html`（3,865 行 / 200KB），**无构建系统**，直接编辑
- CDN 依赖：Tailwind / lucide / marked / highlight.js / KaTeX / DOMPurify
- 多核心导航：`#nav-mode-obsidian|agentsview|im|schedule|assignment`
- 已有三栏布局 + 右栏折叠 + 状态中心，可复用其面板模式

**结论**：PDF 渲染需引入 pdf.js（CDN 或本地 vendor），且要处理 worker 配置。

### 2.3 数据存储现状

| 子系统 | 存储位置 | 是否在 Vault 内 |
|---|---|---|
| 知识索引 | `data/vault.db` (SQLite) | 否 |
| IM Journal | `~/.personal-ai-workspace/im/im_hub.db` | 否 |
| 课表 | `~/.personal-ai-workspace/schedule/schedule.db` | 否 |
| 作业平台 | SQLite (assignment) | 否 |

**既有模式**：**私有的、派生的、易变的运行时状态一律放 `~/.personal-ai-workspace/`，绝不污染 Vault**；Vault 只存知识资产。

---

## 三、需要 Sol 裁决的架构问题

### Q1 Paper 实体发现与三方关联

真实文件名高度异构，且未来持续新增（用户会不断加论文）。如何建立稳定的 `Paper` 实体？

- 纯启发式（同目录同名匹配 + 后缀评分）？命中率上限多少？
- 是否需要显式元数据覆盖机制（如论文夹内 `.paper.yml` 或 `00-paper.md`）？
- 关联失败时的降级策略（PDF-only Paper 是否合法）？
- **Paper ID 稳定性**：目录重命名 / 标题改名后 ID 是否必须保持稳定？（影响 annotations / workspace state 的引用完整性）

### Q2 标注（Annotation）存储位置与格式

PDF 高亮、批注、Markdown 高亮需要持久化。候选：

- (a) 每篇论文一个 JSON sidecar（如 `annotations.json`）放论文夹内
- (b) 写入 `notes.md` 的 frontmatter / 结构化区块
- (c) 统一放 `~/.personal-ai-workspace/papers/papers.db`（SQLite）

约束：Obsidian 兼容性、P1 AI 上下文可读性、`notes.md` 被用户手改时的冲突处理。**哪个是正确的？**

### Q3 阅读状态（status / tags）的权威存储

需求要求 `PaperStatus`（unread/reading/completed）+ `tags` + `PaperReadingMetadata`。

- 若放 Vault（frontmatter）→ Obsidian 可见，但每次状态变更都要改文件（乐观锁 + 备份开销），且与 Obsidian 手动编辑存在双写冲突
- 若放 SQLite → 快，但 Obsidian 看不到状态，且与「Paper 作为一级实体、天然兼容 Obsidian」的原则有张力

**权衡后的推荐？** 是否采用「Vault 存知识（notes/tags），SQLite 存运行时状态（status/reading metadata/workspace state）」的混合模型？

### Q4 WorkspaceState（阅读现场）持久化

`pdfPage` / `pdfScrollPosition` / `markdownScrollPosition` / `noteCursorPosition` 高频变更（滚动即变）。

- 写入频率与持久化策略（防抖 + 批量？仅在论文切换/关闭时落库？）
- 存储位置是否等同 SQLite（非 Vault）？
- **多设备语义**：本机阅读位置是否需要同步？（当前单机，应明确不做）

### Q5 PDF 阅读器实现路线

无构建系统的单文件前端下，pdf.js 的三种接入方式：

- (a) **pdf.js 官方 `viewer.html` iframe**：功能最全（含自带高亮/批注 UI），但与工作台联动弱、样式不可控、跨 iframe 通信复杂
- (b) **pdf.js 自定义 canvas 渲染 + 自建文本层**：完全可控，可与翻译栏双向联动，但工作量最大
- (c) 第三方轻量查看器（如 embed 浏览器内置 PDF 插件）：**最省事但无法实现高亮/批注持久化**

P0 应选哪条？若选 (b)，P0 是否可接受「先只做渲染 + 选择 + 页码/缩放 + 单页高亮，不做全文搜索」？

### Q6 面板系统与状态持久化

四栏可折叠 + 可拖拽 + 尺寸持久化。当前前端无组件框架。

- 直接手写 splitter（指针事件 + CSS）是否足够？
- pane size 存 `localStorage` 还是后端配置？（多核心共用一套 UI 状态）
- 折叠后中央区自适应扩展的实现约束？

### Q7 写入安全边界（必须继承的既有不变量）

工作台写 Vault 已有严格规则：**禁止物理删除**（ADR-002）、SHA256 乐观锁、原子写 + 备份、路径隔离。

论文工作台新增写操作包括：创建 `notes.md`、保存笔记、写入标注、更新 frontmatter。**请确认这些写操作必须继承的全部不变量清单**，以及是否有新增风险（例如：用户同时在 Obsidian 打开同一 `notes.md`）。

### Q8 分期与验收边界

需求给了 P0-1 ~ P0-7。但真实数据下：

- 253 个论文夹、620 个 PDF、414 个翻译文件 —— **首次扫描与 PDF 加载性能**是否需要懒加载/分页？
- P0 是否应**先只支持一个方向分类**（如 `04-Harness执行框架` 31 篇）做试点验收，再全量放开？

---

## 四、待确认的工程约束

1. **零删除铁律**适用于 Vault 知识资产；`notes.md` 创建后不可物理删除（只能软标记）
2. **私有性**：阅读状态/笔记中含个人学习痕迹，端点必须 `Cache-Control: no-store`
3. **PDF 大文件**：单个 PDF 达 23MB，必须支持 HTTP Range，否则跳页卡顿
4. **路径安全**：论文夹名含中文、空格、emoji、`：` 全角冒号，URL 编码必须正确
5. **P1 AI 预留**：Paper 作为一级实体，`AIContext` 需能一次性装配（paper + 当前页 + 选择 + 笔记 + 标注）
