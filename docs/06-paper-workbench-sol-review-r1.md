总体判断
总体结论：论文工作台的产品方向正确，但按当前需求文档原样进入实现是 NO-GO；完成下列架构修订后可 GO。
正确的核心是：Paper 必须是一级领域聚合，而不是三个文件查看器的拼接。
错误的部分是：当前需求仍隐含了“一个 Paper = 一个 PDF + 一个 translation.md + 一个 notes.md”的单值模型。真实 Vault 已经证明这一模型不成立：253 个论文夹中存在 620 个 PDF、414 个翻译文件、85 个 MinerU 嵌套目录，并且同一论文可能同时有导读、全文翻译、抽取 Markdown 和多个 PDF。Paper.translationPath、Paper.pdfPath 这种标量字段必须废弃。参见 docs/06-paper-workbench-recon.md L20–83。
建议先冻结六项高成本决策：
冻结项	裁决	属性
Paper 身份	随机稳定 UUID，持久化在论文夹内的版本化 manifest；绝不由路径、标题或 PDF hash 派生	不可逆决策
Paper 与文件关系	Paper 1:N PaperSource，来源有明确 role；导读、全文翻译、MinerU full.md 不能挤进一个字段	不可逆决策
Annotation 权威	Vault 中独立 JSON sidecar 为权威，SQLite 仅作索引；不把结构化标注塞进 notes.md	不可逆决策
权威分层	Vault 保存身份、绑定、标签、笔记、标注；SQLite 保存阅读状态、阅读现场和派生索引	不可逆决策
API 主键	前端只传 paper_id/source_id/note_id，不得把中文路径作为资源 API 主键	不可逆决策
PDF 组件边界	P0 使用本地化 PDF.js 官方 viewer，但必须包在稳定 Adapter/Bridge 后面	实现可逆，接口不可逆
另一个必须立刻纠正的事实是：01-tech-design-v0.2.md L113–115 仍写着 “Vite React”，但勘察报告 L100–107 明确说明真实前端是 3,865 行、200KB 的单 HTML、无构建系统。后续计划必须以真实实现为准，不能继续按过期文档估算。
Q1：Paper 实体发现与三方关联
明确结论
纯启发式只能用于生成候选，不能成为最终权威。必须加入显式、持久、版本化的绑定 manifest。
这是 不可逆决策。
建议默认文件名：
paper.workbench.json
不建议 P0 默认使用 .paper.yml：
点文件在不同同步、备份和文件浏览工具中的行为不一致。
YAML 的类型推断和 round-trip 容易制造格式噪声。
当前项目也没有可靠的保注释 YAML 写回基础。
不建议使用 00-paper.md：
会进入现有 Markdown 索引。
会和分类级 00-索引.md、翻译、笔记混在一起。
253 个文件会明显污染 Obsidian 文件树。
建议的 manifest：
JSON
{
  "schema_version": 1,
  "paper_id": "pw_3d9e…",
  "title_override": null,
  "sources": [
    {
      "source_id": "src_a12f…",
      "role": "ORIGINAL_PDF",
      "path": "Agent橙皮书.pdf",
      "primary": true,
      "active": true
    },
    {
      "source_id": "src_810c…",
      "role": "TRANSLATION_GUIDE",
      "path": "Agent橙皮书_翻译导读.md",
      "primary": false,
      "active": true
    },
    {
      "source_id": "src_c908…",
      "role": "TRANSLATION_FULL",
      "path": "Agent橙皮书_全文翻译.md",
      "primary": true,
      "active": true
    }
  ],
  "note": {
    "note_id": "note_f8cd…",
    "path": "notes.md"
  },
  "annotation_store": "paper.annotations.json",
  "tags": [],
  "created_at": "2026-09-16T00:00:00Z",
  "updated_at": "2026-09-16T00:00:00Z",
  "inactive_at": null
}
其中路径应当是相对于 manifest 所在论文夹的路径，禁止 ..。这样整个论文夹在分类间移动或重命名时，绑定关系不会失效。
Paper ID 裁决
目录重命名、论文标题修改、分类移动后，paper_id 必须保持不变。
正确方案：
paper_id = "pw_" + UUIDv4
source_id = "src_" + UUIDv4
note_id = "note_" + UUIDv4
ID 存在 manifest 中。
PDF SHA256 只用于来源版本识别、重命名恢复和重复候选提示，绝不能作为 Paper ID。
原因：
相同 PDF 可能被复制到不同方向，具有不同笔记和阅读状态。
PDF 可能被替换为出版社版、arXiv 新版，hash 会变化。
路径和标题都会重命名。
DOI/arXiv ID 并非每篇都有，也可能一篇论文存在多个版本。
是否应当一次性写入 253 个 manifest
不应在普通扫描过程中静默批量写 Vault。
采用“发现”和“采纳”两阶段：
扫描产生 DISCOVERED Paper candidate，只写 SQLite 派生索引。
在 Paper 第一次产生依赖状态前创建 manifest，包括：
首次成功打开并要从 UNREAD 转为 READING
添加 Paper tag
创建笔记
创建标注
手动确认来源绑定
manifest 创建成功后，该 Paper 进入 ADOPTED 状态。
这样未打开论文不会被无声写入 253 个文件；一旦产生状态、笔记或标注，身份又已经被稳定锚定。
启发式算法裁决
不要问“纯启发式命中率上限是多少”，因为当前统计无法推出合法数字。应优化自动绑定的精确率，而不是召回率。
严格自动绑定规则：
PDF
只考虑论文夹直接子文件作为默认主 PDF。
名称含 _layout.pdf 的文件永不自动设为主 PDF。
MinerU 嵌套目录中的 _origin.pdf 只登记为候选或副本。
直接子目录中恰有一个合法 PDF：自动绑定。
有多个直接 PDF：
只有一个 stem 与文件夹名标准化后精确相等，并且其他候选命中 _layout/_origin：自动绑定。
其他情况全部标为 AMBIGUOUS，要求用户选择。
Markdown 来源
保留所有来源，不强行选成一个 translationPath：
文件形态	Role
*_全文翻译.md	TRANSLATION_FULL
*_翻译导读.md	TRANSLATION_GUIDE
*_中文翻译.md	TRANSLATION_FULL 或待确认
MinerU full.md	EXTRACTED_MARKDOWN，不能默认当作中文翻译
其他自定义 Markdown	OTHER_MARKDOWN，候选排序但不自动决定
00-索引.md	排除，不是 PaperSource
notes.md	仅在 frontmatter/manifest 明确绑定后作为 PaperNote
默认显示优先级可以是：
TRANSLATION_FULL
> TRANSLATION_GUIDE
> EXTRACTED_MARKDOWN
> OTHER_MARKDOWN
但所有来源都必须保留并可切换。
PDF-only Paper
合法。
最低合法条件应是“至少有一个可读来源”，而不是三件套齐全：
PDF-only：合法。
PDF + 导读：合法。
PDF + 多个翻译：合法。
无笔记：合法。
只有自定义 Markdown、没有 PDF：P0 不自动采纳，但允许后续手动建立 Paper。
风险与缓解
错误绑定比未绑定严重得多：自动规则应宁可产生 AMBIGUOUS，也不能猜错。
在 04-Harness执行框架 试点之外，再抽取一组含 MinerU、自定义命名、多 PDF 的跨分类 canary。
试点验收要求不是“绑定率高”，而是自动绑定零错误。
检测同一 paper_id 被复制到两个文件夹的情况；必须报冲突，不得静默合并。用户确认后，对副本执行“fork identity”，生成新 ID。
Q2：Annotation 存储位置与格式
明确结论
在三个候选中：
(a) Vault JSON sidecar：权威存储
(c) SQLite：派生索引和查询缓存
(b) notes.md frontmatter/结构化区块：禁止作为 Annotation 数据库
这是 不可逆决策。
默认文件：
paper.annotations.json
理由：
标注、高亮、感想是用户产生的知识资产，不应只困在私有数据库中。
结构化 annotation 写入 notes.md 会导致：
每次标注都改写用户正在编辑的笔记。
与 Obsidian 并发冲突显著增加。
frontmatter 迅速膨胀。
Markdown 结构被机器区块绑死。
用户手工移动章节后难以保持结构完整。
JSON sidecar 可以：
随论文夹移动。
进入 Vault 备份。
保留稳定 ID 和 source locator。
被 P1 的 AIContext 直接装配。
SQLite 可以对 sidecar 建索引，但数据库删除后必须能够从 Vault 重建 Annotation 索引。
建议格式
JSON
{
  "schema_version": 1,
  "paper_id": "pw_…",
  "annotations": [
    {
      "annotation_id": "ann_…",
      "source_id": "src_…",
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
删除标注必须是：
JSON
"deleted_at": "..."
不得从数组中物理移除。
Anchor 必须现在定稿
这是 Annotation 中重构成本最高的部分。
PDF anchor
至少包含：
JSON
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
内部 page_index 固定使用 0-based。
UI 页码可以使用 page_label。
坐标必须相对于 PDF crop box 标准化，不能存 CSS 像素。
同时保存 Text Quote 作为重定位后备。
Markdown anchor
至少包含：
JSON
{
  "type": "MARKDOWN_TEXT",
  "heading_path": ["3 Method", "3.2 Training"],
  "block_fingerprint": "...",
  "text_position": {
    "start": 318,
    "end": 371
  },
  "text_quote": {
    "exact": "...",
    "prefix": "...",
    "suffix": "..."
  }
}
不要只保存 DOM selector。Markdown 重新渲染、KaTeX、表格和代码块都可能改变 DOM。
风险与缓解
JSON 不会原生显示为 Obsidian 笔记：P0 提供 Annotation 列表；P0.5 提供“提升到笔记”或显式导出。
“提升到笔记”是一次复制，不能形成双向同步。
source hash 变化时，Annotation 必须进入 ORPHANED/NEEDS_REANCHOR；禁止仍在旧坐标显示，造成假关联。
Annotation 写入必须携带 sidecar 当前 SHA256，409 时停止写入。
Q3：阅读状态与 Tag 的权威存储
明确结论
采用混合模型，但边界必须严格：
数据	权威位置
Paper 身份、显式来源绑定	Vault manifest
Paper-level semantic tags	Vault manifest
Note-level tags	notes.md frontmatter/正文
UNREAD/READING/COMPLETED	SQLite
firstOpenedAt/lastOpenedAt/completedAt	SQLite
folder/category 推导信息	SQLite 派生索引
翻译文件中的 arXiv、方向、推荐度	SQLite 带 provenance 的派生 metadata
WorkspaceState	SQLite
这是 不可逆决策。
为什么 status 不写 Vault
status 和阅读时间是高频、设备局部的操作状态。将其写进 notes.md 或 manifest 会造成：
每次打开论文都触发 Vault 写入。
第一次打开自动转 READING 时与 Obsidian 编辑产生无意义冲突。
状态修改会产生大量备份和文件系统事件。
未来多设备时出现双向合并问题。
因此 P0 应明确接受：阅读状态不在 Obsidian 中原生可见。
不要同时在 SQLite 和 frontmatter 保存 status，也不要做“尽力同步”。这会制造两个权威源。
Tag 的边界
paper_tags：论文主题分类，写 manifest。
note_tags：这份笔记自己的分类，写 notes.md。
方向目录如 04-Harness执行框架 是 category_path，不是自动写入的 tag。
从翻译 blockquote 推导出的方向、推荐度必须标记为 derived；用户确认前不得转为显式 tag。
阅读状态规则
UNREAD → READING：首个可读来源成功加载后触发，不能在用户仅点击列表时触发。
READING → COMPLETED：只能用户手动触发。
打开 COMPLETED Paper 不自动降级。
所有时间统一 UTC ISO-8601。
SQLite 最好附带 append-only paper_status_events，用于未来统计和回退审计。
风险与缓解
SQLite 在这里不再完全是“可随时重建的缓存”，因为它承载状态。必须：
使用 WAL。
PRAGMA foreign_keys=ON。
配置 busy_timeout。
使用 SQLite backup API 做周期快照，不能只复制 WAL 运行中的数据库文件。
数据库迁移使用 user_version 或独立 migration 表。
Q4：WorkspaceState 持久化
明确结论
存 SQLite，不写 Vault；P0 明确为单机、单设备语义。
该存储选择可迁移，但 API 字段应现在冻结。
不要只保存原始 pixel scrollTop
建议字段：
PDF
pdf_source_id
pdf_source_version
pdf_page_index
pdf_page_offset_ratio     # 0..1
pdf_scale
pdf_rotation
Markdown
markdown_source_id
markdown_source_version
markdown_heading_path
markdown_block_id
markdown_scroll_ratio
Note
note_id
note_cursor_start
note_cursor_end
note_content_sha256
通用
active_pane
last_opened_at
updated_at
state_version
由于一个 Paper 可能有导读、全文翻译、MinerU Markdown，最好把来源位置保存为：
source_positions[source_id] = Position
数据库内部可以拆成子表，而不是仅保存一套 Markdown 位置。
写入策略
滚动事件只更新前端内存。
trailing debounce：约 750ms。
最长落库间隔：约 5 秒。
切换 Paper 时强制 flush。
visibilitychange → hidden 时强制 flush。
不依赖 beforeunload 作为唯一保存机制。
不把阅读位置写 localStorage。
最后几秒状态在浏览器崩溃时丢失是可接受的；为了零丢失而每个 scroll event 写 SQLite 不值得。
源文件变化时
source version 相同：完整恢复。
source hash/version 已变化：只恢复到相同页或顶部 heading，不恢复细粒度 offset。
note SHA 不匹配：不恢复旧 cursor offset，避免跳入错误文本。
多设备
P0 明确：
workspace_scope = LOCAL_ONLY
不做合并、不做同步、不声称跨设备一致。
Q5：PDF 阅读器实现路线
明确结论
P0 选择 (a)：本地 vendored PDF.js 官方 generic viewer，通过同源 iframe + 版本化 Bridge 接入。
不选：
(b) 自建 canvas/text layer：两周内会吞掉大部分工期，而且文本层、虚拟滚动、rotation、selection、accessibility 都容易做成半成品。
(c) 浏览器内置 PDF：无法建立稳定 selection/annotation/context 接口，是明确的技术债。
PDF.js 的具体实现可换，但 Bridge 是 不可逆接口：
open(sourceId, sourceVersion)
goToPage(pageIndex)
setScale(scale)
getCurrentPage()
getSelection()
onDocumentLoaded(...)
onPageChanged(...)
onSelectionChanged(...)
dispose()
父页面不得查询 iframe 内部 DOM，也不得依赖 PDF.js 私有 class 名称。
必须本地 vendor
不能使用 PDF.js CDN：
viewer、worker 版本不一致会出现隐蔽错误。
worker 跨域和 CSP 配置复杂。
论文工作台的网络能力必须可关闭。
用户现有全局 CDN 已是遗留出站依赖，P0 不能再增加新的 CDN 依赖。
至少本地固定：
pdfjs/build/pdf.mjs
pdfjs/build/pdf.worker.mjs
pdfjs/web/viewer.html
pdfjs/web/viewer.mjs
cmaps/
standard_fonts/
wasm/
并锁定同一个 PDF.js 版本。
PDF 只读端点
不能复用 _ALLOWED_ASSET_EXTS。现有 path_guard.py L149–196 明确把 PDF 排除在通用静态资源之外，这个边界应保留。
新增专用接口：
GET /api/paper-sources/{source_id}/content?version={source_version}
HEAD /api/paper-sources/{source_id}/content?version={source_version}
不要使用：
/api/pdf?path=02.%20🟡%20...
端点必须支持：
200
单 byte-range 的 206
非法范围的 416
Accept-Ranges: bytes
Content-Range
正确的 Content-Length
Content-Type: application/pdf
RFC 5987 编码的 filename*=UTF-8''...
X-Content-Type-Options: nosniff
Cross-Origin-Resource-Policy: same-origin
私有内容合理的 Cache-Control
禁止响应压缩中间件对该端点做 gzip，否则 byte range 语义会被破坏
还要用 source_version 或 ETag 防止 PDF 在一组 Range 请求中途被外部替换，导致 PDF.js 拼接不同版本的字节。
PDF.js 内置 Annotation UI 的裁决
不得把 PDF.js 内置编辑或“保存带批注 PDF”当成工作台 Annotation 实现。
PDF 原文件 P0 永远只读。
不得改写 PDF 字节。
Annotation 全部写 sidecar。
应隐藏或禁用会产生修改后 PDF 的功能。
P0 范围裁决
P0 实现：
PDF 渲染
滚动
页码跳转
缩放
内置搜索
文本选择
从选择创建 Annotation
Annotation 列表和点击跳页
持久彩色高亮重新绘制、跨重排重定位移到 P0.5。
需求文档把“记录 Annotation”和“在 PDF 每次打开后精准重绘 overlay”混成了一个功能，后者不是两周 P0 应承担的内容。
Q6：面板系统与状态持久化
明确结论
手写 splitter 足够。
Pane size 与折叠状态放 localStorage。
但论文工作台代码禁止继续直接堆进 200KB 的 index.html。
splitter 选择是可逆的；前端模块边界现在必须建立。
建议在无构建系统前提下先使用浏览器原生 ES Module：
frontend/dist/paper/
├── main.js
├── api.js
├── store.js
├── layout.js
├── pdf-bridge.js
├── markdown-pane.js
├── note-pane.js
├── annotations.js
└── paper-workbench.css
index.html 只保留导航入口与：
HTML
<script type="module" src="/paper/main.js"></script>
这比立刻全站迁移 React/Vite 风险小，也不会继续扩大单文件。
布局实现
用 CSS Grid，而不是多层 flex 临时拼接：
Sidebar | splitter | PDF | splitter | Markdown | splitter | Note
必须具备：
左右栏独立折叠。
折叠前宽度单独保存。
中央 PDF/Markdown 比例可调。
pointerdown + setPointerCapture，避免鼠标离开 splitter 后丢事件。
splitter 使用 role="separator"，支持键盘方向键。
ResizeObserver 通知 PDF viewer 尺寸变化。
resize 回调节流，不能每个 pointer event 重排 PDF。
折叠时不要卸载 PDF iframe，否则每次展开都会重载文档和丢现场。
localStorage 边界
只保存：
JSON
{
  "schema_version": 1,
  "left_width": 260,
  "right_width": 340,
  "pdf_ratio": 0.52,
  "left_collapsed": false,
  "right_collapsed": false
}
键名例如：
personal-ai-workspace.paper-layout.v1
禁止保存：
note 内容
selected text
annotation body
Paper 标题列表
当前笔记草稿
这些内容具有私密性，Cache-Control: no-store 也管不到 localStorage。
Q7：写入安全边界
明确结论
所有论文写入必须继承冻结不变量，并统一经过一个新的公共组件：
VaultWriteService
不要从 route 层复制 _backup()、_atomic_write() 等私有函数。
这是 不可逆决策。
必须继承的完整清单
无物理删除
无删除 Paper、Note、Annotation sidecar、manifest 的 API。
“移除 Paper”只能写 inactive_at。
“删除 Annotation”只能写 deleted_at。
外部删除只记录 missing_since/tombstone。
客户端不决定路径
客户端传 paper_id/note_id/source_id。
服务端从 registry/manifest 得出实际路径。
路径仍要重新经过 guard。
Vault 内路径隔离
canonical resolve。
NFC 比较。
拒绝排除区。
拒绝 symlink escape。
manifest 相对路径禁止 ..。
文件实际类型与 role 匹配；PDF 至少校验 %PDF- magic。
SHA256 乐观锁
已存在文件保存必须携带 expected_hash。
在 canonical per-path lock 中读取当前 authoritative bytes。
hash 不同返回 409。
禁止 force=true 或“自动用最新 hash 重试”。
提交前二次校验
当前 in-memory lock 只能串行化工作台请求，不能锁住 Obsidian。
写好临时文件后，在最终 replace 前再次校验目标 hash。
如果期间被 Obsidian 修改，丢弃临时文件并返回 409。
便携 POSIX 文件系统不存在真正的跨进程 compare-and-swap，因此二次校验、备份和冲突 UI 都必须存在。
原子写
临时文件必须位于同目录。
写入后 flush/fsync。
保存已有文件时 os.replace。
replace 后 fsync(parent_directory)。
保留原权限；不要无意重置 mode。
临时文件加入 scanner ignore。
原子新建
现有 files.py::file_create L234–236 使用 open(..., "x")，能防覆盖，但崩溃时可能留下部分新文件。
Paper manifest、notes、annotation sidecar 应先完整写临时文件并 fsync，再通过 no-clobber 原子发布。
在同一文件系统上可用 hard-link publish：link(temp, target)，目标存在时失败；再清理 temp。
备份
对已有文件，备份必须来自与冲突检查相同的 preimage bytes。
备份使用内容 hash 或时间版本命名，不能每次无声覆盖同一份。
自动保存频繁时使用 hash 去重和时间分桶，避免每 750ms 生成一个备份。
备份存 ~/.personal-ai-workspace/papers/backups/，不污染 Vault。
成功响应返回新 hash
当前 file_save L210–223 只返回 ok/path，不足以支持连续 autosave。
所有写接口成功后必须返回：
JSON
{
  "new_hash": "...",
  "previous_hash": "...",
  "updated_at": "..."
}
私有端点统一 no-store
/api/papers/* 中含 status、tags、notes、annotations、workspace state 的响应统一加：
Cache-Control: no-store
不能依靠每个 handler 手工记忆，应用在 Paper router middleware/dependency 层。
单进程约束显式化
当前 per-path threading.Lock 只在单进程 uvicorn 有效。
P0 启动配置必须禁止 workers > 1，或以后引入 OS 级锁。
不得静默改成多 worker。
日志禁止记录正文
不记录 note content、selected text、annotation body、完整文件路径 query。
审计日志只记录 ID、operation、结果、hash 前缀和错误类型。
多文件写入的新增致命风险
“创建 notes.md + 更新 manifest”涉及两个 Vault 文件，文件系统没有跨文件事务。
正确策略是只前滚，不回滚删除：
SQLite 写入 paper_write_intent。
创建带 paper_id/note_id frontmatter 的 note。
更新 manifest 绑定 note。
标记 intent committed。
如果第 2 步后崩溃，重启时扫描 note frontmatter，自动补写 manifest。
绝不能为“回滚”而删除已经创建的 note。
同样适用于首次创建 paper.annotations.json。
Obsidian 并发编辑时的 UI
409 后：
停止自动保存。
保留当前编辑器中的本地草稿。
拉取远端新版本。
展示“远端已修改”。
P0 至少提供“复制本地草稿 / 重新加载”；禁止自动覆盖。
P0.5 再做三方 diff。
Q8：分期与验收边界
明确结论
扫描器必须从第一天支持完整、可配置、递归的真实结构；试点可以限定一个分类，但不能把一个分类硬编码进架构。
试点是可逆的 rollout 决策，不是数据模型决策。
扫描范围
建议配置：
YAML
papers:
  roots:
    - "02. 🟡 归类 Arrange/论文"
  max_depth: 6
max_depth 只是异常保护，不是“第三层就是论文夹”的业务规则。
Paper folder 判定基于内容特征：
存在直接 PDF；或
有明确 manifest；或
有可识别的翻译来源；
MinerU 嵌套目录命中特征时标为 artifact container，不创建第二个 Paper。
MinerU 识别可使用组合特征：
full.md
*_origin.pdf
*_layout.pdf
*_content_list.json
*_model.json
命中多个特征且父目录已有主 PDF 时，不把该嵌套目录识别为 Paper folder。
253 篇是否需要分页
253 个 Paper summary 不足以迫使服务器分页，但必须懒加载内容。
必须：
启动时先从 SQLite 立即返回上次 catalog。
后台做增量扫描。
扫描只读目录项、size、mtime，不读取全部 PDF。
不在冷启动时计算全部 620 个 PDF 的 SHA256。
仅对主候选、被打开文件、需要重命名恢复的文件异步计算完整 hash。
PDF、翻译、笔记只有选中 Paper 后才加载。
sidebar 按分类折叠；可做简单虚拟列表。
Watchdog 只重扫受影响论文夹，不重扫整个根目录。
试点方案
只用 04-Harness执行框架 31 篇不够，因为它未必覆盖全部异常形态。
正确试点：
31 篇分类试点。
额外加入跨分类 canary：
至少 3 个 MinerU 嵌套案例。
至少 2 个双翻译案例。
至少 2 个多 PDF 案例。
至少 3 个完全自定义 Markdown 名称案例。
试点期间允许写 manifest/notes/annotations。
对剩余 222 篇先只读扫描，生成绑定报告。
全量开放前人工检查所有 AMBIGUOUS 和低置信候选。
P0 必须通过的验收
文件夹重命名后 paper_id 不变。
Paper 从一个分类移动到另一个分类后状态和 Annotation 不丢。
同一 manifest 被复制到两个目录时 fail closed。
85 个 MinerU 嵌套目录不被错误识别为独立 Paper。
_layout.pdf 不被自动设为主 PDF。
PDF Range 返回正确的 206/416。
大 PDF 首次打开不下载完整文件后才显示。
外部修改 notes 后保存返回 409，本地草稿仍保留。
PDF 原文件从未被写入。
所有 Paper 私有端点带 no-store。
关闭网络后不发生 Paper 子系统外部请求。
AIContextV1 可以从同一 Paper 聚合出稳定、带 hash 的上下文快照。
勘察报告未覆盖、但工程上可能致命的风险
1. PDF.js worker 与 viewer 版本漂移
viewer 和 worker 必须是完全相同版本。版本不一致经常表现为部分 PDF 无法加载，而不是启动即报错。
措施：本地 vendor、版本锁定、启动自检。
2. PDF.js 可能产生“修改后 PDF”
官方 viewer 的 annotation editor、保存、下载功能可能生成新 PDF。若误接入现有保存按钮，会破坏 PDF 只读边界。
措施：禁用 PDF 写回和 save-modified-PDF；工作台 Annotation 永远 external sidecar。
3. Range 请求跨版本混合
Obsidian/Finder 在阅读过程中替换 PDF，前后 Range 请求可能读取不同版本。
措施：source version/ETag 固定一次 viewer session；版本变化返回 409/412，要求重新打开。
4. Unicode 路径与双重 URL 解码
中文、emoji、空格、全角冒号以及 macOS NFD/NFC 会使原始 path API 极其脆弱。
措施：资源 API 只接受 ASCII opaque ID；保留精确实际路径，另存 NFC 比较 key；禁止前端拼路径 URL。
5. PDF/Markdown 中的隐式出站请求
即使不接 arXiv API，仍可能通过以下方式出站：
Markdown 外链图片。
PDF 外部链接。
PDF 附件动作。
CDN PDF.js、KaTeX、marked 资源。
用户点击引用链接。
措施：
外部图片默认不加载，显示占位。
本地图片改写为同源受控 asset endpoint。
PDF 外链需要明确确认。
Paper 子系统不使用 CDN。
若“关闭网络”指浏览器完全零出站，则现有全局 CDN 也必须本地化；否则只能声称“论文数据不出站”。
6. Markdown 相对图片和 MinerU 资源
MinerU full.md 往往引用同目录或子目录图片。现有 resolve_for_asset_read() 可能通过全 Vault rglob 按 basename 查找，重复图片时歧义，且性能差。
措施：新增 source-relative asset endpoint，只从当前 Markdown 所在目录向下解析，不全库搜索。
7. Source 替换后的 Annotation 假定位
同一路径 PDF 被替换为新版本后，旧 page 5 的坐标可能仍能显示，但对应文本已经不同。
措施：Annotation 带 source_sha256/source_version；变化即 orphan，不静默套用。
8. 复制论文夹导致 ID 冲突
用户复制整个文件夹时会连 manifest 一起复制，产生两个相同 paper_id。
措施：全局唯一约束；扫描时两个位置同时出现即进入 DUPLICATE_ID_CONFLICT，不自动选胜者。
9. 多文件操作不存在原子事务
创建 note、manifest、annotation sidecar、更新索引是多个资源。
措施：SQLite write-intent + 可幂等恢复 + 只前滚。
10. Autosave 请求乱序
连续 autosave A、B，如果 B 先返回而 A 后返回，旧响应可能覆盖当前 hash 或状态。
措施：单 Paper 串行保存队列、客户端单调 save_seq、成功返回 new_hash，忽略过期响应。
11. 200KB 单文件继续膨胀
PDF Bridge、Annotation、四栏 layout、Paper store 全部塞进 index.html 后，任何修改都会影响所有核心视图，回归测试成本快速失控。
措施：P0 即拆原生 ESM；不要求同时迁移框架。
12. SQLite 被当成普通文件复制备份
WAL 模式运行时直接复制 .db 可能得到不一致备份。
措施：使用 SQLite backup API，并在 migration 前强制快照。
13. PDF 页面全部高 DPI 渲染导致内存爆炸
长论文若所有页面按 Retina 分辨率保留 canvas，浏览器内存会迅速增长。
措施：使用 PDF.js viewer 自带虚拟化；限制高分屏 render scale；离屏页面释放 canvas。
14. 扫描 PDF 没有文本层
部分扫描版 PDF 无法选中文本。不能把“无 selection”错误解释为 PDF 损坏。
措施：P0 显示“本 PDF 无可用文本层”；OCR、MinerU fallback 放 P1。
15. 本机单进程服务仍有跨站调用风险
127.0.0.1 并不自动意味着任意网页都不能触发本机接口。
措施：
不开放 wildcard CORS。
校验 Origin/Host。
写接口使用页面启动时注入的 session nonce。
JSON 写端点拒绝 form/simple request。
不把 token 写入日志或 URL。
P0 最小可交付边界
P0：两周必须完成
领域与扫描
Paper 聚合及五实体模型。
PaperSource 一对多。
递归扫描真实根目录。
MinerU artifact 识别。
paper.workbench.json manifest。
稳定 Paper/Source/Note ID。
候选排序、AMBIGUOUS 状态和最小手动绑定 UI。
PDF-only Paper 合法。
阅读
本地 vendored PDF.js generic viewer。
PDF page/scroll/zoom/search/selection。
Markdown 翻译安全渲染。
多翻译来源切换。
PDF 和 Markdown 不做同步滚动。
source-ID API 与 PDF Range。
笔记
缺失时显式创建 notes.md。
Markdown 编辑。
串行 debounce autosave。
SHA256 冲突。
备份。
409 后保留本地草稿。
快捷类型只向编辑器缓冲区插入 Markdown snippet，不在服务端“智能改章节”。
Metadata 与现场
SQLite status 和 reading metadata。
Vault Paper tags。
WorkspaceState。
localStorage 面板布局。
首次成功打开触发 UNREAD → READING。
Annotation 最小闭环
PDF/Markdown 选择文本。
创建 Annotation sidecar 记录。
Annotation 列表。
点击 Annotation 跳到对应页/heading。
source version 变化后标 orphan。
P1 接口预留
AIContextV1 数据合同。
纯数据装配服务和 contract test。
不调用任何模型、不出站。
工程边界
Paper 前端拆成原生 ES modules。
VaultWriteService。
Paper router 统一 no-store。
单 worker 启动约束。
试点 + 全量只读扫描。
P0.5：应延后
持久高亮 overlay 的重新绘制。
Annotation 自动重定位和人工 re-anchor UI。
PDF ↔ Markdown 点击互定位。
Annotation 批量导出为 Markdown。
三方 merge/diff。
全量 253 篇批量 manifest 采纳流程。
阅读状态历史统计。
搜索和高级筛选。
多标签批量编辑。
对大型 Markdown 的 section virtualization。
P1：明确延后
解释选中段落、总结当前页。
AI 检查用户理解。
自动提取创新点和实验依据。
Paper Agent / Library Agent。
arXiv、Crossref 等网络 metadata。
双向同步滚动。
OCR。
语义搜索和跨论文关联。
数据模型定稿建议
1. Paper
paper_id: str
schema_version: int
display_title: str
title_origin: MANIFEST | FOLDER | SOURCE
folder_relpath: str
category_relpath: str
manifest_relpath: str?
binding_state:
  DISCOVERED
  ADOPTED
  PDF_ONLY
  RESOLVED
  AMBIGUOUS
  DEGRADED
  DUPLICATE_ID_CONFLICT
  INACTIVE
primary_pdf_source_id: str?
primary_translation_source_id: str?
note_id: str?
paper_tags: list[str]
external_ids: { arxiv?: str, doi?: str }
status: UNREAD | READING | COMPLETED
first_opened_at: datetime?
last_opened_at: datetime?
completed_at: datetime?
status_changed_at: datetime?
created_at: datetime
updated_at: datetime
inactive_at: datetime?
权威位置：
paper_id/title_override/source bindings/paper_tags/external_ids：Vault manifest。
当前 folder/category、binding state：SQLite 派生索引。
status/reading metadata：SQLite 权威。
API 返回的是二者装配后的 aggregate。
2. PaperSource
source_id: str
paper_id: str
role:
  ORIGINAL_PDF
  SUPPLEMENTAL_PDF
  TRANSLATION_FULL
  TRANSLATION_GUIDE
  EXTRACTED_MARKDOWN
  OTHER_MARKDOWN
media_kind: PDF | MARKDOWN
rel_path: str
rel_path_key_nfc: str
is_primary: bool
binding_origin: MANIFEST | STRICT_RULE | MANUAL
binding_confidence: float?
size_bytes: int
mtime_ns: int
sha256: str?
source_version: int
mime_type: str
language: str?
page_count: int?
active: bool
missing_since: datetime?
created_at: datetime
updated_at: datetime
权威位置：
文件 bytes：Vault。
source_id/role/path/is_primary：manifest。
size、mtime、hash、page_count、language 等：SQLite 派生。
同一 role family 只允许一个 primary，但允许多个来源。
3. PaperNote
note_id: str
paper_id: str
rel_path: str
content_sha256: str
note_tags: list[str]
schema_version: int
created_at: datetime
updated_at: datetime
missing_since: datetime?
inactive_at: datetime?
默认 notes.md frontmatter：
YAML
---
paper_id: pw_...
paper_note_id: note_...
paper_role: notes
tags: []
---
不要写入 status。
权威位置：
内容、note tags：Vault Markdown。
路径、hash、索引字段：SQLite 镜像。
manifest 持有 note binding。
用户可以更名，系统通过 paper_note_id 重新识别。
4. Annotation
annotation_id: str
paper_id: str
source_id: str
kind:
  HIGHLIGHT
  COMMENT
  THOUGHT
  INNOVATION
  QUESTION
  CONCLUSION
body_markdown: str
selected_text: str?
anchor_schema_version: int
anchor: json
source_sha256: str
source_version: int
created_at: datetime
updated_at: datetime
deleted_at: datetime?
orphaned_at: datetime?
revision: int
权威位置：
paper.annotations.json。
SQLite 只保存可重建索引。
API 用 operation 写入，不允许前端随意上传未经校验的整份 JSON。
5. WorkspaceState
paper_id: str
active_pdf_source_id: str?
active_markdown_source_id: str?
active_pane: PDF | MARKDOWN | NOTE
source_positions: map<source_id, Position>
note_id: str?
note_cursor_start: int?
note_cursor_end: int?
note_content_sha256: str?
last_opened_at: datetime
updated_at: datetime
state_version: int
Position 按 source kind 区分：
PDF:
  page_index
  page_offset_ratio
  scale
  rotation
  source_version
MARKDOWN:
  heading_path
  block_id
  scroll_ratio
  source_version
权威位置：SQLite。
Panel width 不属于 WorkspaceState，它是浏览器 UI preference，留在 localStorage。
Paper 与 PDF / 翻译 / 笔记的确定性关联算法
按以下优先级执行，禁止跨级覆盖：
第 1 级：manifest
存在合法 paper.workbench.json：
校验 schema。
校验 paper/source/note ID 格式。
校验所有相对路径无 ..。
通过 path guard 验证。
manifest 绑定具有最高优先级。
引用文件缺失时标 DEGRADED，不能静默找一个“看起来相似”的文件替代。
第 2 级：稳定 ID 恢复
无 manifest 或路径变化时：
匹配 note frontmatter 中的 paper_id/note_id。
匹配 SQLite 已知 source_id + sha256。
同一 Paper 只有一个唯一匹配时恢复。
hash 出现多个候选时进入冲突，不自动合并。
第 3 级：严格自动规则
只自动选择唯一且无歧义的直接 PDF。
_layout 永不作为主 PDF。
MinerU 嵌套 PDF 永不优先于直接 PDF。
翻译文件按明确名称分类，允许多项。
full.md 分类为 EXTRACTED_MARKDOWN。
00-索引.md 永远排除。
notes.md 只有显式 ID/manifest 才作为 Note。
第 4 级：候选排序
对自定义名称计算相似度和后缀分数，但只用于 UI 排序，不直接落权威绑定。
第 5 级：人工确认
用户选定后：
写入 manifest。
为新 source 生成稳定 source_id。
保留原候选证据。
后续扫描完全服从 manifest。
P1 AIContext 预留接口
这是 不可逆接口。P0 应定义并测试，但不调用 AI。
JSON
{
  "schema_version": "1",
  "assembled_at": "2026-09-16T00:00:00Z",
  "paper": {
    "paper_id": "pw_...",
    "title": "...",
    "tags": [],
    "status": "READING"
  },
  "focus": {
    "pane": "PDF",
    "source_id": "src_...",
    "source_version": 3,
    "source_sha256": "...",
    "locator": {
      "page_index": 7
    },
    "selection": {
      "exact": "...",
      "prefix": "...",
      "suffix": "..."
    }
  },
  "source_refs": [],
  "note": {
    "note_id": "note_...",
    "sha256": "...",
    "content": "..."
  },
  "annotations": [],
  "resource_versions": []
}
关键规则：
所有内容带 source/note hash。
focus locator 与 Annotation 使用同一套 schema。
允许按字节或 token budget 装配。
默认只包含当前页、当前 section、笔记和相关 Annotation，不无界加载整篇论文。
P0 的 context assembler 是纯本地数据函数，无模型、无网络。
给实现者的第一周行动清单
第 1 天：冻结合同，不写 UI
新增 ADR：
Paper identity 与 manifest。
Authority split。
Annotation storage/anchor。
PDF viewer 与零出站边界。
定稿：
paper.workbench.schema.json
paper.annotations.schema.json
AIContextV1
修订 01-tech-design-v0.2.md 中不真实的 Vite React 描述。
第 2 天：数据库与扫描器
建立 papers、paper_sources、paper_notes、annotations_index、workspace_states、paper_write_intents。
加 SQLite migration、WAL、foreign keys、backup。
建 fixture 覆盖：
标准双语。
导读 + 全文。
MinerU。
多 PDF。
自定义 Markdown。
中文、emoji、全角冒号、NFD/NFC。
duplicate manifest ID。
输出 discovery/binding report，不写真实 Vault。
第 3 天：统一写服务与资源端点
抽取 VaultWriteService。
实现：
expected hash。
二次 pre-commit hash。
immutable/versioned backup。
atomic replace。
no-clobber create。
parent fsync。
new hash response。
实现 source-ID PDF endpoint。
自动测试 200/206/416/HEAD。
Paper router 统一 no-store。
第 4 天：前端模块边界与 PDF Spike
建 frontend/dist/paper/ ES module 目录。
Vendor 固定版本 PDF.js。
建 iframe Bridge。
验证：
大 PDF Range。
page changed event。
selection event。
iframe collapse 后不重载。
worker 离线可用。
实现 CSS Grid splitter 和 localStorage schema。
第 5 天：Note、状态、现场与试点
实现 note create/read/save/409。
成功响应更新客户端 hash。
建串行 autosave queue。
实现 status transition 和 WorkspaceState。
实现最小 Annotation sidecar CRUD。
实现 AIContextV1 assembler contract test。
对 31 篇试点 + 跨分类 canary 运行扫描。
第一周停止门：
未通过 Paper ID rename test、409 test、Range test、duplicate-ID test 和零错误自动绑定审查，不进入第二周 UI 完善。
最终架构应当是：
Paper Aggregate
├── Manifest            Vault，身份/绑定/Paper tags
├── PaperSource[]       Vault 文件 + SQLite 索引
├── PaperNote           Vault Markdown
├── Annotation[]        Vault JSON sidecar + SQLite 索引
├── Reading Metadata    SQLite
├── WorkspaceState      SQLite
└── AIContext Adapter   P0 定义，P1 使用
这套划分继承了零删除、SHA256 乐观锁、原子写、备份、路径隔离和本机私有性，同时不会把真实 Vault 强行改造成不存在的固定三件套。
