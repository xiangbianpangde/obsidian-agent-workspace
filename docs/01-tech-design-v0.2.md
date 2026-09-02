# Obsidian Agent Workspace 技术方案 v0.2（P0 精简版，修订 1）

> 修订基线：v0.2 rev0（docs/01-tech-design-v0.2.md）+ Sol 审核 5 条 MUST（2026-09-02）
> 状态：已过 Sol 审核（Demo 可开发级），本版为落地版

## 0. 定位（一句话）

**标签驱动的 Obsidian 文件工作台**：让你的本地 Vault（约 2300+ md）在 Web 工作台中可见、可按标签整理、可直接编辑，并能用你现有的 Templater 模板一键创建文件。工作台是**你本人**的操作工具（等同你在 Obsidian 里操作），不实现 agent 治理层（Phase 3 再接入）。

## 1. P0 需求（用户原话映射）

| # | 需求 | 落点 |
|---|---|---|
| P0-1 | 接入 Obsidian，通过文件的标签获得文件情况 | Vault 扫描 + 标签索引 + 标签面板 |
| P0-2 | 根据标签情况进行整理，通过标签处理文件状态 | 标签统计/筛选视图 + frontmatter 状态字段编辑 |
| P0-3 | 在工作台处理文件、编写文件 | 文件树 + CodeMirror 编辑器 + 保存（编辑 = 用户本人级权限） |
| P0-4 | 使用 Obsidian Template 插件同一套模板创建文件 | 读 `资料库/模版/`（17 个模板）→ 变量模拟 → 创建 |

### 1.1 tags vs 状态 职责边界（P0-MUST-1）

| 概念 | 职责 | 示例 | 禁止 |
|---|---|---|---|
| `tags` | **分类与发现**（主题维度） | #AI #论文 #项目 #RAG | 不允许用工作流值作 tag（如 `未整理`） |
| `状态`（frontmatter） | **工作流生命周期**（处理维度） | 未整理 / 进行中 / 已完成 / 已归档 | 不允许被当作分类 tag 使用 |

工作台标签面板两个通道互相独立：按 tag 筛选（分类）、按 状态 分组（生命周期）。两者可在视图中交集，但**同一个值不得同时进入两个系统**。

## 2. 明确不做（Cut List）

- ❌ Markdown 只读化 / Permission Matrix / 签名 broker 对接（用户本人工具，Phase 3）
- ❌ FTS5 全文搜索、links 图谱表、AI Memory 建模、memory_nodes
- ❌ Graph View、Dataview 执行、.base 嵌入渲染（降级展示）
- ❌ 多 vault 管理（config 保留 vault.path 字段，默认单库）
- ❌ 删除/重命名文件（安全底线保留）
- ❌ Agent 自动修改、AgentView/IM/任务系统（后续 Phase）

## 3. 架构与技术栈

```
前端 React+Vite+Tailwind+CodeMirror 6+remark/rehype+Zustand
         │ REST (本地 127.0.0.1)
后端 FastAPI + SQLite(watchdog 增量) + python-frontmatter
         │
   /Users/xbpd/Documents/xbpd_obsidian（只经 config.yaml 配置）
```

后端模块：`api/`（files、tags、templates）、`scanner/`（vault_scanner、parser）、`database/`（sqlite）、`template/`（template_engine）、`security/`（path_guard、secret_detector）。

## 4. 数据模型（4 表）

```sql
files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, filename TEXT, title TEXT,
      folder TEXT, size INTEGER, created_at DATETIME, modified_at DATETIME,
      hash TEXT, indexed_at DATETIME);            -- hash = sha256(content)  ← P0-MUST-3
tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE);
file_tags(file_id INTEGER, tag_id INTEGER);
metadata(file_id INTEGER, key TEXT, value TEXT, value_type TEXT);
-- value_type: string | number | bool | list | yaml   ← Sol 补充（P0-MUST-1 相关修订）
-- 状态 字段写入 metadata(key='状态', value_type='list'|'string')
```

- 标签来源：frontmatter `tags`（分类）；`状态` 字段独立存 metadata，**不写入 tags 表**
- `hash`（sha256）用于 watchdog 增量判断是否需重新解析

## 5. Template 兼容层（Templater 子集 + 降级）

| 语法 | demo 行为 |
|---|---|
| `<% tp.date.now("YYYY-MM-DD") %>` | 替换为当前日期（格式化） |
| `<% tp.date.now(fmt, offset, reference) %>` | **支持 offset 参数解析**（普通表达式；如 `-1, "2026-09-02"` → 2026-09-01）← P0-MUST-4 |
| `<% tp.file.title %>` / `tp.file.path` | 替换为 render context 中的 title / target_path |
| `<% await tp.file.move("...") %>` | 不执行；解析目标目录 → `create_target_path`，创建时直接落位 |
| `<%* ... %>`（JS 块，如日记"复习区"） | 不执行；保留原文 + 注入标记 `<!-- workspace: unsupported Templater JS block -->`，UI 提示"请在 Obsidian 中运行模板" |
| `![[xxx.base#锚点]]` 等嵌入 | 渲染器显示占位：[Obsidian Base Embed → Open in Obsidian] |
| ` ```dataview ` 代码块 | 按代码块展示 + 提示"需 Obsidian Dataview 插件" |

**Render context（解析顺序固定）**：用户输入 title → date 变量 → target_path（由 tp.file.move 目标或用户选择目录决定）→ 其它 custom vars。变量解析只做**表达式级子集替换**，不执行任何 JS。

模板目录：`config.yaml` 的 `templates.dir`（默认 `资料库/模版`）。**模板文件本身只读**（改模板须经 Obsidian）。

## 6. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 当前 vault 路径/统计 |
| GET | `/api/files/tree` | 目录树（含标签、状态、大小、修改时间、hash） |
| GET | `/api/file/content?path=` | 读取文件内容（frontmatter 与 raw 分离） |
| POST | `/api/file/save` | `{path, content, expected_hash}`；比对失败 → **409 Conflict**（"文件已被外部修改，请重新加载"）← P0-MUST-2 |
| POST | `/api/file/create` | `{path, content}`；**目标已存在 → 409**，禁止覆盖 ← P0-MUST-5 |
| GET | `/api/tags` | 标签统计：`{tag, count, status_distribution}` |
| GET | `/api/files/by-tag?tag=` | 某标签下文件（按状态分组） |
| PATCH | `/api/file/status` | `{path, status}` 更新 frontmatter `状态` 字段（单文件操作，连同上条 409 语义） |
| GET | `/api/templates` | 模板列表（path/name/layer/supported） |
| GET | `/api/template/preview?path=` | 预览：变量替换后内容 + 目标路径建议 |
| POST | `/api/file/create-with-template` | `{template, title, vars}` → 变量模拟 → 创建 → 刷新索引 |
| ❌ | `DELETE` 任何文件路径 | **不存在该端点；后端代码无 delete_file 函数** |

## 7. 安全设计（P0 硬底线）

1. 路径防护：`Path.resolve()` 后必须 `startswith(vault_root)`；拒绝 symlink escape；所有文件操作经 `security/path_guard.py`。
2. 禁删除/禁重命名：无对应 API 与函数。
3. 服务绑定 `127.0.0.1`，README 声明"单用户本地工具，不对外暴露"。
4. 扫描排除：`.obsidian .claudian .codex .hermes copilot .trash 附件 credentials` + `*.epub *.pdf *.png *.excalidraw`；排除规则在 scanner 第一层执行。
5. Secret 防护：scanner 管线 `walker → exclude → secret-detector(api_key/token/private_key 模式) → parser`，命中即跳过该文件并记录到索引日志，**secret 永不进入 parser/SQLite/前端**。
6. 保存前自动备份：写入 `data/backups/`（同文件最近 1 份滚动），误改可恢复。

## 8. 目录结构

```
personal-workspace/
├── README.md
├── config.yaml              # vault.path / templates.dir / security
├── backend/                 # FastAPI（app/api|scanner|database|template|security）
├── frontend/                # Vite React（FileExplorer | Editor | TagPanel）
├── sample-vault/            # 复现用小 Vault（不含真实内容）
├── docs/                    # 方案与勘察材料
└── data/                    # SQLite + backups（gitignore）
```

前端三栏：文件树 | 编辑器（CodeMirror + 渲染预览切换）| 标签面板（标签统计、状态分布、按标签筛选、文件状态编辑）。

## 9. P0 验收标准（可测）

1. ✅ 连接真实 vault，首次全量扫描 2300+ md < 30s，落到 `data/vault.db`
2. ✅ 文件树展示完整目录，可打开 md（frontmatter/正文分离展示）
3. ✅ 标签面板：标签统计 + 数量 + 状态分布；按标签筛选；点击标签跳转
4. ✅ 编辑保存：修改正文或 `状态` → 保存到 vault 文件 → watchdog 刷新索引
5. ✅ 并发修改检测：打开文件 → 外部修改 → 保存返回 **409**（新验收，优先于 sample-vault 复现）
6. ✅ 模板创建：选模板 → 填标题 → 日期变量（含 offset）替换正确 → 按 `create_target_path` 落位 → 索引可见
7. ✅ JS 块模板（日记模版）创建：**不执行、保留原文 + 降级标记 + UI 提示**（不测"内容正确"）
8. ✅ 安全：API 无 DELETE；路径穿越/符号链接请求被拒；`.obsidian` 等被排除；secret 样本文件不被索引
9. ✅ sample-vault（小型，不模拟 2300 文件）一键复现（README 步骤）

## 10. 里程碑

- **M1** Vault Scanner + 索引（30s 扫描 + sha256 + watchdog 增量）← 本轮
- M2 API（文件树/内容/保存 409/创建 409/标签/模板）
- M3 前端三栏（编辑器 + 预览 + 标签面板）
- M4 模板创建链路（L1 变量 + offset + L2 路由 + JS 降级）
- M5 安全验收 + sample-vault + README
