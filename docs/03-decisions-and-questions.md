# 用户决策与待 Sol 评审的问题（2026-09-02）

## 背景
项目：个人工作台（Personal AI OS 的 Obsidian Knowledge Layer demo）
用户身份：AI 专业大学生；日常用 pi、Codex 等 agent 开发；Obsidian 管理学习/研究/项目；已建成 Wiki+日记+模板+Agent 体系（含 AI 秘书、签名 broker、AI Memory）。
后续：不交原审核人员；**通过 GitHub 完成审核**；今后重大设计先咨询 Sol，再结合 Sol 建议下结论。

## 已定决策（用户已拍板）
1. **先文档后代码**：分两个交付——Deliverable 1 = 技术方案 v0.2（作为开发合同：架构/数据模型/API/安全边界/Template 兼容策略/验收标准）；Deliverable 2 = Demo 实现 v0.1（React + FastAPI + SQLite Index + Vault Adapter）。
2. **vault 路径不写死**：`config.yaml`（vault.path / security.allow_delete=false / index.database=./data/vault.db），未来可多 vault（主库/实验库/项目库）。
3. **Template Compatibility Layer 三层**：
   - Layer 1（P0）：纯变量，仅支持 `tp.date.now`（简单格式）、`tp.file.title`、`tp.file.path`；`tp.system.prompt` 后置；**JS 执行 ×**。
   - Layer 2：`tp.file.move` 语义不执行，转为 `create_target_path`（按模板声明的目标目录创建文件）。
   - Layer 3（未来，现在不做）：Obsidian Plugin 桥接层（Web Workspace → Template Adapter → Obsidian Plugin → Templater Runtime）。
4. **数据模型需扩展**：v0.1 的 files/tags/file_tags/metadata 之外，新增 Vault Intelligence Layer（frontmatter/tags/links/templates/AGENTS.md 统一解析）、AI Memory 特殊建模（如 memory_nodes：id/path/type/priority/agent_scope）。
5. **新增验收项**：读取 2300+ md；首次扫描 <30s；增量更新；标签统计；PARA 分类；模板读取与变量替换；安全：代码层无 delete_file()，API 无 DELETE /api/file。

## 环境的重大事实（v0.1 与现实的冲突点，需 Sol 给出裁决）
- **v0.1 说"编辑允许、删除禁止"，但 AGENTS.md 治理契约大部分目录默认只读**：正式 Wiki 需签名 broker；研究草稿只读（apply 暂停）；日记默认只读、追加需人工签名；模板/Bases/附件/.obsidian 属 Human-only 禁改区。工作台作为"AI 可操作的访问层"，直接"全局编辑"违反既有治理——v0.2 必须给出**写能力矩阵**。
- 模板是 Templater DSL（含完整 JS 块 `tR`、日期偏移参数、`tp.file.move` 路由、`.base`/`![[...]]` 嵌入、Dataview 代码块）——v0.1"替换变量"太粗糙。
- AI Memory 已有 registry 结构（Claude 53 / Codex 65 条，active/stale/current 状态机）——工作台如何建模/只读化？

## 请 Sol 评审的问题
1. **写能力矩阵**：demo 中工作台应支持哪些写面？是否 = AGENTS.md 权限分区表的镜像（只读区只读、受控区提示走专用脚本/签名 broker、用户明确授权区可直接写）？"编辑允许"应如何改写为正确表述？
2. **数据模型**：files/tags/file_tags/metadata + memory_nodes 是否足够？是否应加 links 表（wikilink，Phase 2 图谱基础）、文件钩子表（dataview/base 依存）、索引版本表？2300+ 文件规模下 SQLite schema 建议（FTS5? 索引列?）。
3. **Template 兼容层**：三层方案差距？具体问题：日记模版完整 JS 块无法静态求值 → demo 策略（标记不支持 / 部分模拟 / 提示在 Obsidian 中运行）？`tp.date.now` 第二参数偏移量解析规则？文件名如何从"新建对话框输入标题"映射为 tp.file.title？`![[...base#锚点]]` 与 Dataview 在渲染器中的降级策略？
4. **AI Memory 建模**：demo 阶段只读 registry 同步（解析 00 Memory Registry.md 表格）还是建 memory_nodes 表？P0/P1 边界？
5. **增量索引**：watchdog + 全量重扫 vs 增量解析的取舍；首次 <30s 的实现路径；扫描排除清单（.obsidian/.claudian/.codex/.hermes/copilot/.trash/附件/凭证/二进制 epub/Excalidraw?）；"永不索引 secret"如何在扫描层强制执行？
6. **API 与安全**：v0.1 端点集是否足够？需要哪些新端点（模板列表/预览/create-with-template、标签统计、文件树、搜索）？路径穿越防护、符号链接防护、本地服务绑定（127.0.0.1）？无认证是否可接受（纯本机）？
7. **GitHub 审核**：项目将以 GitHub 仓库形式被审核。仓库结构/文档怎么组织最能支持审核（README + 方案文档 + ADR + 验收矩阵 + Milestone 计划 + 复现说明）？有没有已知坑（大文件、.obsidian 环境依赖、模板语法截图等）？
8. **scope 切割**：Demo v0.1 与 P1 的清晰切割建议（哪些进 v0.1，哪些明确不进）？务必给出"为什么"。

## 约束
- 本地优先：数据不上云；索引仅存本机 SQLite。
- 技术栈已定：React/Vite/Tailwind/CodeMirror 6/remark+rehype/Zustand；FastAPI/SQLite/watchdog/python-frontmatter。
- 运行环境：node v22.23.2、Python 3.12.7、macOS。
- 未来接入：pi/Codex/AgentView、IM（企业微信/微信/QQ）、任务系统。
