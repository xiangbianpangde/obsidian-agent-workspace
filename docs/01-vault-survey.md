# Vault 真实环境勘察（2026-09-02 本地扫描）

## Vault 路径
主 vault：`/Users/xbpd/Documents/xbpd_obsidian`
（另有 `/Users/xbpd/Projects/xbpd_obsidian` 为旧版/测试库，内容较乱，不作为默认）

## 规模与结构
- 约 **2300+ 个 .md 文件**
- 目录（一级）：
  - `01. 🟣 采集 Grasp`（214 md）：含 `所有采集/`、`Wiki 研究草稿/`（只读+策略暂停）、`任务/`（秘书inbox、秘书记忆、每日任务、秘书模板）
  - `02. 🟡 归类 Arrange`（406 md）：含 `所有归类/`
  - `03. 🟤 表达 Present`（103 md）：含 `个人 Wiki/`（**正式 Wiki，签名 broker 写**）、`obsidian笔记系统/`
  - `04. 🔵 日记周记`（162 md）：含 `01. 日记/`（**默认只读，人工签名 broker 追加**）
  - `05. 🟠 读书笔记`（6 md）
  - `06. 🟢 观影笔记`（14 md）
  - `07.学习笔记`（1361 md）
  - `资料库`（17 md）：`数据库/`（.base 文件）、`附件/`、`模版/`、`提示词/`
  - `AI Memory`：00 Memory Registry.md + Inbox/Curated/Processed/Conflicts/Archive/Skill Candidates/Tool Candidates
  - 根级：AGENTS.md、CLAUDE.md、Untitled.md、epub 文件、Excalidraw/、HTML import/、copilot/、tools/

## 治理契约（AGENTS.md 摘要）
详见 docs/vault治理契约/AGENTS.md 全文。关键点：

1. **权限分区表**（默认 agent 权限）：
   - `03. 🟤 表达 Present/个人 Wiki/`：**签名 broker only**（dry-run → 用户 macOS 签名 → commit）
   - `01. 🟣 采集 Grasp/Wiki 研究草稿/`：**Read/dry-run only**（draft apply 策略暂停）
   - `01. 🟣 采集 Grasp/任务/秘书inbox/`：受控写（专用脚本）
   - `01. 🟣 采集 Grasp/任务/秘书记忆/`：受控写（专用脚本 + 确认 token）
   - `01. 🟣 采集 Grasp/任务/每日任务/`：读 + 用户确认写
   - 其它可见目录：**只读，除非用户明确要求**
   - `.obsidian/ .claudian/ copilot/ .trash/ 附件/ 凭证`：**永不索引、永不写**
2. **禁止**：索引/暴露/记录 API keys、token、密码、证书、私密日记内容、Copilot 会话、插件配置。
3. **禁止**：直接写日记、原始笔记、项目日志、模板、Bases、`.obsidian`、`.claudian`、copilot、附件；不绕过受控 Writer。
4. 写面不混用：正式 Wiki 只能走 broker；inbox/记忆/每日任务各有专用脚本。
5. Entry points：Claude Code（CLAUDE.md）、Codex（.codex/AGENTS.md）、Hermes、Claudian。
6. 模板目录：`01. 🟣 采集 Grasp/任务/秘书模板/`（周日记忆周审.md、晚间复盘.md）。
7. 工具：`tools/` 下 30+ 脚本（wiki-broker v2/v3、secretary-* 系列、run-daily-scan-2300.sh、research-import-to-wiki-draft 等）。
8. Human-only：promote/merge/delete/rename/bulk-edit、改受控 Writer 配置、改 `.obsidian`/`.claude`/`.codex` 配置、改模板/Bases/日记/项目日志/附件 —— 均须先询问用户。

## 模板系统（资料库/模版/，Obsidian Templater 插件）
`templates.json`：`{"folder": "资料库/模版"}`

完整清单（17 个）：
- 00. 普通笔记模版.md
- 01. 采集笔记模版.md
- 02. 归类笔记模版.md
- 03. 表达笔记模版.md
- 04. 日记模版.md
- 05. 周记模版.md
- 06. 月记模版.md
- 07. 年记模版.md
- 08. 笔记-电影.md
- 09. 笔记-电视剧.md
- 10. 笔记-图书.md
- 11. 目录模版.md
- 12.学习笔记模板.md
- 14.学习规划模板.md
- 15.崩坏记录模板.md
- 16.实验记录模板.md
（另：`01. 🟣 采集 Grasp/任务/秘书模板/`：周日记忆周审.md、晚间复盘.md）

**Templater 语法样例**（详见 docs/02-模版样例合集.md）：
- `<% tp.date.now("YYYY-MM-DD") %>` —— 简单变量
- `<% tp.date.now("YYYY-MM-DD", -day, baseDate) %>` —— 带偏移量的日期计算
- `<% await tp.file.move("/01. 🟣 采集 Grasp/所有采集/"+tp.file.title) %>` —— 文件移动/路由
- `<%* const days=[...]; ... tR += ... %>` —— **完整 JS 执行块**（日记模版的"复习区"）
- `tR` 输出模式、`tp.file.title`、`tp.file.path`
- 嵌入语法：`![[../../数据库/06.链接笔记.base#表格]]`（.base 文件 + 标题锚点嵌入）、`![[../../数据库/12.任务数据库.base|11.任务数据库#未完成任务]]`
- Dataview 代码块：```` ```dataview TASK FROM "04. 日记周记/01. 日记" WHERE ... ``` ````
- Calendar 代码块：```` ```calendar-nav ``` ````

## AI Memory
`AI Memory/00 Memory Registry.md`：registry 型 frontmatter（projection: true, registry: true, update_interval_seconds: 900, last_successful_sync），表头 Sources（Claude 53 / Codex 65），Memories 表（Title | Source | Status(active/stale) | Sync state | source_updated_at | last_synced_at | memory_id）。另有 Inbox/Curated/Processed/Conflicts/Archive/Skill Candidates/Tool Candidates 目录。

## 其它
- `资料库/数据库/`：00.目录.base、01.表达笔记数据库.base、02.采集笔记数据库.base、03.归类笔记数据库.base、04.读书笔记数据库.base、05.观影笔记数据库.base、06.链接笔记.base、07.日记数据库.base、08.日记提取数据库.base、09.高数下数据库.base、12.任务数据库.base 等
- `.base` 文件是 Obsidian Bases 插件的数据源，含表格，被大量 `![[...base#锚点]]` 嵌入引用
- 运行环境：node v22.23.2、Python 3.12.7、git 2.55.0
