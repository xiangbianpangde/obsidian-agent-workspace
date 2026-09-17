1. P0 判定
不通过（NO-GO）
这不是“少数 P0.5 功能尚未完成”，而是 P0 的身份权威、写入完整性、来源版本、Annotation 锚点、核心前端路径仍有实质性断点。其中数项可以直接造成用户数据覆盖、引用失效或只在生产 Vault 结构下暴露的故障。
当前可以认可的是：数据模型骨架、SQLite 基础设施、PDF-only Paper、候选排序、ES module 拆分、本地 PDF.js 接入方向、软删除数据模型等确实已经实现，不是空壳。但“代码量完成”与“P0 不变量闭合”之间还有明显距离。
此外，送审包中没有 .git、测试源码、vendored PDF.js、应用启动与全局中间件、真实数据库或真实 Vault 样本。因此我无法独立确认：
归档是否精确对应提交 8f0d384
292 项测试的内容与执行结果
PDF.js 6.3.289 是否真的完整、同版本 vendor
282 篇真实论文索引结果
全局 no-store、单 worker、路由挂载是否在宿主应用中成立
这个证据缺口不是本次 NO-GO 的唯一依据；即使完全相信上述数字，源码和定向探针已经能证明 P0 未闭合。
必须修复项
编号	必须修复项	验收条件
P0-B1	接通 Discovery→Adoption→Manifest 权威链	首次状态变化、创建笔记、标注、标签或手工绑定之前，必须先成功写入 manifest；删库后能从 Vault 恢复相同 Paper/Source/Note ID
P0-B2	消除权威文件的静默数据覆盖	并发首次添加标注不能丢一条；损坏 sidecar 必须 fail closed；笔记创建不能产生双 note_id、孤儿文件或多 active note
P0-B3	修复 VaultWriteService 的实际缺陷	修复缺失文件死锁、短写截断、内部 symlink 绕过；不得再声称二次校验“封死”了跨进程窗口
P0-B4	建立真实的 source hash/version 生命周期	文件字节改变必须递增版本；重命名保持身份；缺失来源被正确标记；Range 请求不会混合两个文件版本
P0-B5	修正 Annotation 冻结合同	PDF 必须有真实归一化坐标；Markdown 必须有 fingerprint/position；后端从真实 source 派生 hash/version；orphan 检测实际运行
P0-B6	补齐真实前端闭环	Markdown 能加载；第二篇 PDF 能打开；快速切换 Paper 不串写笔记；WorkspaceState 真正捕获、flush、恢复
P0-B7	闭合零出站和私有响应边界	外链图片默认不加载、外链需确认、source-relative asset endpoint 落地、PDF popup 受控、包括错误响应在内均 no-store
P0-B8	用生产等价测试重新验收	覆盖删库重建、移动、复制、改名、换版、损坏文件、并发写、崩溃恢复、快速切换、多 worker 误配与真实嵌套 Vault
在这些项关闭前，不应宣布 P0 完成，也不应进入 P1 AI。当前对真实 Vault 的笔记和标注写入应暂停，至少限制在有完整备份的验证副本中。
2. Q1–Q5 逐条裁决
Q1. 是否达到 P0 验收标准？
没有。送审清单中至少以下条目被误判为“已完成”。
清单条目	裁决	实际情况
Paper 聚合 + 五实体模型	部分完成	类和表存在，但聚合根身份没有落入 manifest，生命周期没有闭合
递归扫描 + MinerU	部分完成	能识别 MinerU 容器，但没有把嵌套 full.md 绑定回父 Paper；rglob 也没有真正剪枝排除目录
manifest 读写与采纳	未完成	schema 和通用 writer 存在，但没有生产 manifest 读、写、采纳、重建调用
稳定 Paper/Source/Note ID	未完成	仅在同一 DB、同一路径内暂时稳定；删库、移动文件夹、来源改名都会重新生成或留下双记录
PDF.js page/zoom/search/selection	部分完成	首次打开可能工作；第二次 open() 会因 ready 未重置而超时；selection 没有坐标
Markdown 安全渲染与多来源切换	未完成	前端请求 /content，后端对 Markdown 明确返回 415，要求 /text
source-ID API + Range	部分完成	Range 语法大体存在，但 source version 不随内容变化，无法提供版本固定
笔记 autosave + 冲突处理	未完成	单 Paper 普通串行保存尚可，但跨 Paper 切换存在串写/草稿覆盖窗口；首次创建 ID 也错误
WorkspaceState	未完成	只有后端 CRUD 和 API wrapper，前端没有捕获、恢复、750ms/5s flush 或 visibility flush
首次打开触发 READING	部分完成	PDF 成功加载后会触发，但触发前没有 Adoption；Markdown-only 路径不触发
Annotation 记录、列表、跳转	未完成	锚点不满足 ADR；后端返回嵌套 anchor，前端却读取扁平字段，点击跳转基本失效
source 变化后标 orphan	未完成	只有字段和 UI 文案，没有 source hash/version 更新和 orphan 检测流程
AIContextV1	未完成	合成装配器存在，但真实 API 不返回 source SHA，note SHA 被传 null，实际对象不满足冻结 schema
Paper router 统一 no-store	部分完成/证据不足	正常 JSON 响应有 header；大量 HTTPException 错误路径没有局部 header，归档也没有全局 middleware
单 worker 约束	未完成	无启动断言；更严重的是 _service() 每次请求新建，进程内路径锁在同一 worker 内也不共享
相对可以判定已落地的有：PDF-only 合法性、基础候选/AMBIGUOUS 建模、原生 ES modules、localStorage 仅保存布局几何、SQLite WAL/foreign key/busy timeout 的基础设置。
Q2. 三项已知缺口是否可以作为 P0 遗留？
Q2-1：manifest 采纳流程
裁决：构成 P0 阻断缺陷。你的“P0 内可接受”判断不成立。
但需要精确区分两件事：
282 个纯 DISCOVERED Paper 没有 manifest，本身不一定违反 ADR。
ADR-006 明确禁止扫描时静默批量写入全部 manifest。
系统允许 Paper 在没有 manifest 的情况下产生依赖状态，才是 P0 缺陷。
当前首次打开会写 SQLite 状态，创建笔记会写 Vault，创建标注会写 sidecar，但这些操作之前没有 Adoption。此时 paper_id 仍只是数据库临时身份。一旦数据库重建：
状态无法与新 Paper ID 对齐；
sidecar 中的旧 paper_id 失去对应聚合根；
note/source/workspace 引用失去稳定语义；
文件夹移动会被识别为新 Paper。
因此正确修复不是盲目把 282 个全部写入 Vault，而是：
实现原子的 ensure_adopted(paper_id)；
在所有依赖状态写入前调用；
对当前已经存在依赖状态的 Paper 做一次迁移：
status != UNREAD
已有 note
已有 annotation sidecar
已有 tags/manual binding
已有 workspace state
未产生依赖状态的 Paper 继续保留为 DISCOVERED；
如需全部采纳，必须是用户显式执行的“Adopt all”操作，而不是扫描副作用。
Q2-2：PDF 高亮重绘
高亮 overlay 重绘可以延后到 P0.5；当前锚点与 orphan 实现不能延后。
目前不是“锚点已正确保存，只差画出来”：
annotations.js:39-41 固定写入空的 quad_points_normalized
annotations.js:55-56 把 Markdown block_fingerprint 和 text_position 写成 null
schema 又允许这些空值通过
API 接受客户端自报的 source hash/version，缺失时写入 64 个零
source hash/version 本身不更新
没有 orphan 扫描或重定位检测
所以可以延后的只有视觉 overlay。可持久化、可验证的锚点是 P0 本体。
Q2-3：单 worker 约束
需要显式约束，但启动断言本身不够。
当前 api.py:78-80 每次调用都会创建新的 VaultWriteService。因此即使只有一个 uvicorn worker，不同请求拿到的也是不同 _locks 字典，路径锁仍不能跨请求串行化。
最低要求是：
VaultWriteService 与 Paper mutation coordinator 必须是应用级 singleton；
Annotation 的读—改—写必须使用共享的 per-paper/per-sidecar operation lock；
启动时拒绝 workers > 1；
最好再使用进程级 lockfile，防止两个独立启动的单 worker 实例同时写同一 Vault。
所以结论是：启动期断言必需，但它不是修复并发模型的替代品。
Q3. “测试假保障”是否反映更深问题？
是。根因不是 tmp_path，而是 fixture 把语义不同的概念折叠成相同值。
tmp_path 完全可以用于可靠测试；问题是：
papers_root == vault_root
可能还有 source path == Vault-relative path
可能所有文件都在单层目录
可能 source_version 永不变化
可能 API fixture 与前端 fixture 使用了不同形状的假数据
可能客户端 contract test 手工补了生产 API 根本不返回的 SHA
建议按以下方向排查：
1. Fixture 不变量检查
在公共 fixture 构造后直接断言：
Python
Run
assert papers_root != vault_root
assert papers_root.is_relative_to(vault_root)
assert paper.folder_relpath != vault_relative_paper_path
任何本应是两个概念的字段，都要有至少一个测试强制它们不同。
2. 生产拓扑矩阵
至少覆盖：
Vault 根与 papers 根不同
方向分类/论文夹/MinerU 子目录多层嵌套
中文、emoji、全角标点、NFC/NFD 名称
内部 symlink、外部 symlink、symlink 指向排除区
.obsidian/subdir、附件目录的后代文件
只读目录、权限错误、磁盘短写模拟
23MB 级 PDF 与无文本层 PDF
3. 权威源与重建的变形测试
这类测试比简单 CRUD 更重要：
删除 SQLite 后重建，已采纳的 ID 必须不变
移动 Paper 文件夹，ID 必须不变
复制带 manifest 的文件夹，必须进入冲突而不是移动
PDF 原地换版，source ID 保持、version 增加、annotation orphan
来源改名，source ID 恢复而不是新增一条 active source
sidecar 损坏后，任何写操作都不得覆盖原文件
4. 并发与崩溃测试
必须加入：
两个请求同时向不存在的 sidecar 添加 Annotation
笔记保存同时被外部编辑器改写
文件写完、DB 尚未更新时进程退出
manifest 写完、note/sidecar 尚未写完时退出
快速 A→B→C 切换 Paper，同时有 autosave 在途
误开两个 worker 或两个应用实例
5. 客户端—服务端真实合同测试
不要分别用自制 fixture 验前后端。应把真实 API payload 喂给前端模块，至少验证：
/sources 的真实结果能生成合法 AIContext
Annotation list 的真实嵌套 anchor 能跳转
Markdown 客户端使用的 URL 后端确实返回 200
409/412/415/500 响应仍有 no-store
6. 把 mutation test 设成验收门
不仅要“测试绿”，还应要求关键变异必须被杀死：
删除 papers-root 前缀
固定 source_version=1
去掉 expected hash 校验
把 corrupt sidecar 当空文件
把 manifest parser 替换为随机 UUID
把 /text 改回 /content
你已经发现路径变异未被杀死。这不是偶发，而是说明当前测试主要验证了“代码按 fixture 工作”，尚未充分验证“架构不变量成立”。
Q4. 哪些决策会在 P1 变成不可逆技术债？
Q4-1：WorkspaceState
source_positions 使用 map 是正确决策。
但当前问题不是简单少了 pdf_scale 和 rotation，而是 WorkspaceState 前端生命周期整个没有接通：
没有读取恢复
没有监听页面、缩放、旋转、Markdown 滚动、note cursor
没有 750ms trailing debounce
没有 5 秒最大间隔
没有 Paper 切换 flush
没有 visibilitychange flush
没有 source-version 恢复降级逻辑
另外，后端把 source_positions 当自由 JSON 保存，没有按 PDF/Markdown Position 结构验证；PUT 也没有 expected state_version，多标签页会 last-write-wins。
必须在 P0 修复。 P1 开始后，AI 的“当前页、当前位置、当前来源”会直接依赖这个合同；此时再改会扩散到 AIContext、恢复逻辑和行为日志。
Q4-2：Annotation 的 kind 与 body_markdown
这项领域建模是正确的，不需要推翻。
“先选类型再写内容”只是 UI 流程，现有 create payload 可以同时携带：
JSON
{
  "kind": "QUESTION",
  "body_markdown": "这里为什么需要这个假设？"
}
一次创建一条 Annotation 也足够。
真正的问题是：
当前 UI 固定发送 kind: "HIGHLIGHT"；
没有类型选择与正文输入流程；
没有 Annotation 更新 API；
锚点合同本身不完整；
source hash/version 不可信。
如果 P0 声明 Annotation 只支持“创建高亮、软删除”，可以暂不增加编辑 API；若声明支持批注/感想/创新点/疑问，则当前实现尚未兑现。
更危险的不可逆债
比上面两项更危险的是：
冻结 schema 与 ADR 相互矛盾
当前 schema 允许空 PDF quads、空 Markdown fingerprint/position。
source version 是形式字段而非真实版本
一旦 P1 AI 将它写进引用、回答或 provenance，后续无法证明回答基于哪一版文件。
manifest 没有成为身份权威
P1 AI 若开始保存 Paper/Source 引用，删库后整批引用会失去对象。
这些必须在接 P1 前修正。已有数据时应提高 schema/anchor 版本并提供迁移，不要悄悄改变所谓“冻结 v1”的语义。
Q5. 下一步优先级
优先级应为：
A′ → C → B → D
其中 A′ 不是“静默给 282 篇全部写 manifest”，而是完整的 P0 完整性修复：
接通 Adoption 与 manifest 重建；
修复数据丢失、source version、Annotation anchor、前端关键路径；
对已有依赖状态 Paper 做身份迁移；
通过删库、移动、复制、换版、并发与崩溃验收。
随后进入 C：受控真实使用。应先在有备份的真实结构副本或小范围真实 Paper 上使用，观察 UI 摩擦和来源绑定，而不是直接把当前写入逻辑放到唯一 canonical Vault 上。
B：P0.5 高亮重绘 放在锚点合同稳定之后。否则是在空坐标、假版本之上构建 overlay，必然返工。
D：P1 AI 最后。 当前 AIContext 的真实生产装配连冻结 schema 都不能满足，过早接模型只会把不稳定 ID、空 SHA 和错误 locator 扩散成更昂贵的数据债。
3. 代码级审查
writer.py
乐观锁与提交前二次校验：没有封住窗口
writer.py:316-324 的流程是：
再读一次目标并校验 hash；
调用 _write_atomic()；
_write_atomic() 内执行 os.replace()。
第二次读与 os.replace() 是两个独立系统调用。外部编辑器可以在两者之间写入。
我插入了一个定向写入探针，结果是：
原始文件：v1
二次校验后外部改为：external-v2
工作台最终写入：workbench-v2
backup 中只有：v1
即外部版本被静默覆盖，而且备份中也没有它。
这不是“概率为零的小理论问题”，而是当前注释中“double check plus backup is the correct design”所声称的保证并不成立。便携的 os.replace 本身无法提供跨不合作进程的 compare-and-swap。必须：
把内部写者先统一到共享 coordinator；
将 temp fsync 后的最终校验和备份尽可能靠近发布点；
使用平台支持的原子交换/锁定机制，或与 Obsidian 插件协作；
无法完全封死时，必须承认剩余窗口，并设计 conflict copy/recovery，而不能宣称不变量已闭合。
另外三个具体缺陷
缺失文件路径会死锁
save() 在 writer.py:296 获得非重入 threading.Lock，随后在 299-300 调用 create()；create() 在 242 再次申请同一锁。
我调用 save(..., require_existing=False)，线程 500ms 后仍未退出。
os.write() 只调用一次，可能短写
writer.py:195-199 和 249-252 都假设一次 os.write 写完全部数据。合法短写会导致文件截断。探针模拟短写后，abcdefghij 最终只保存了 abc。必须循环写满或使用可靠的 file object 写法。
内部 symlink 可绕过排除区
resolve() 在 141 先解析 symlink，之后才从解析后的目标向上检查 symlink。若 alias -> .git，原始 parts 不含 .git，解析后链上也不再有 symlink。探针中 alias/config 被接受为 .git/config。
必须沿原始路径逐段 lstat，并对解析后的相对路径再次执行 excluded-segment 校验。
此外，backup 文件本身没有显式 fsync 及 backup 目录 fsync，崩溃耐久性没有达到注释所暗示的强度。
api.py
_paper_rel
正常的嵌套 papers-root 拼接已经修正，但配置异常时是 fail open：
_papers_root_ptr() 在 api.py:83-89 发现 papers root 不在 Vault 内时返回 Path(".")
_paper_rel() 随后把路径写到 Vault 根下的错误位置
我的探针把 papers root 指向 Vault 外部，结果 _paper_rel 仍返回 cat/P/notes.md，而不是拒绝操作。
这里必须直接抛错，不能把严重配置错误降级成“把 papers root 当 Vault root”。
笔记创建
api.py:367-401 存在两个不同问题：
普通 UI 首次输入会发送非空 content，于是默认 frontmatter 完全不会生成，note ID 不会锚定进 Vault；
若使用默认模板，370 生成一次 new_note_id() 写入 frontmatter，391 又生成第二个 ID 写 SQLite，两者必然不同。
并且文件先创建、数据库后更新，paper_write_intents 完全没有使用。进程在两者之间退出会留下：
Vault 中已有 notes.md
DB 中没有 note
重试得到 409
无自动 roll-forward
_write_sidecar
用户自认“并发后一条会 409”并不准确。首次创建时存在静默丢更新：
Python
Run
except AlreadyExistsError:
    current = _read_sidecar(paper)
    return service.save(rel, text, expected_hash=current.get("_hash"))
这里重新读取了新 hash，却仍保存旧的 text。两个请求同时首次添加 A、B 时：
A 创建含 A 的 sidecar；
B create 失败；
B 读取 A 的 hash；
B 用正确 hash 保存“只含 B”的旧 document；
A 被静默删除。
我的并发探针最终 sidecar 只剩 B，没有 409。
另一个严重问题是 api.py:469-478：
所有读取异常都被当成“sidecar 不存在”；
JSON 损坏时被当成空 annotations；
create/delete 随后会以损坏文件的 hash 为 expected hash，把原始损坏内容覆盖成新的空白文档。
这会销毁用户仍可能手工恢复的权威字节。损坏 sidecar 必须禁止任何 mutation，并保留原件。
Annotation 来源验证
api.py:570-580 直接相信客户端提供的：
source_id
source_sha256
source_version
anchor
没有确认 source：
是否存在；
是否属于当前 Paper；
是否 active；
当前真实 hash/version 是什么。
缺失 SHA 时还写入 "0" * 64。这些字段应由服务端从已绑定 source 派生，不能由客户端声明。
Annotation API/前端形状不一致
list_annotations() 返回 sidecar 原始记录，其中定位信息位于 annotation.anchor。但前端 annotations.js:170-185 和 main.js:294-309 读取：
item.anchor_type
item.page_index
item.heading_path_json
这些字段只存在于 SQLite 派生索引，不在返回的 sidecar record 中。因此列表 locator 和点击跳转并未真正闭环。
api_sources.py
Range 解析本身基本可用，版本固定不成立
api_sources.py:203-208 只比较 SQLite 中的 source.source_version。
但 paper_index.py:84-90 在同一路径重扫时无条件沿用旧的：
source_version
sha256
扫描器也没有计算 SHA。我的探针替换 PDF 内容后重跑索引，结果：
mtime 改变；
size 改变；
source_version 仍为 1；
sha256 仍为 null。
因此 ?version=1 会继续返回新文件，而不是 412。版本固定只是接口外观。
文件描述符没有被固定
api_sources.py:210 先 stat()，然后 StreamingResponse 在稍后才于 _iter_file_range():118-127 打开路径。文件可能在以下窗口被替换：
stat 后、generator 打开前；
多次 Range 请求之间；
FileResponse 实际打开文件前。
正确做法应是：
先打开文件描述符；
对该 FD fstat；
校验持久化 source hash/version；
从同一个 FD 流式读取；
在客户端提供的版本或 If-Range 不匹配时返回 412。
当前 ETag 虽含 mtime/size，但服务器没有执行 If-Range/If-Match 语义，前端也只传 source version。
Markdown 路径直接断裂
后端：/{source_id}/text 才返回 Markdown，/content 在 197-201 返回 415
前端：api.js:110-119 的 sourceText() 请求 /content
所以真实 Markdown source 切换会失败。
其他问题
完整 200 响应没有显式 Content-Encoding: identity
错误响应是否 no-store 未闭合
_get_source_or_404() 只验证解析后仍在 Vault 内，没有拒绝内部 symlink、排除目录或严格限制在该 Paper 文件夹内
note-pane.js
结论：单一 Paper 下普通串行保存大体安全，跨 Paper 与在途请求场景不安全
flush() 在 note-pane.js:137-140 遇到已有保存时，会立即返回 {reason: "in-flight"}，并不会等待该保存完成。
main.js:153-157 切换 Paper 时虽然 await this.note.flush()，但忽略返回结果。因此：
A 的 autosave 正在进行；
用户切换到 B；
flush() 立即返回；
B 被加载；
A 的响应稍后把 this.hash、noteId、dirty 状态写回同一个 NoteEditor；
pending flush 可能使用 B 的 selected.paper_id、B 的文本与 A 的 hash。
另外：
IO closure 在 main.js:77-86 每次读取可变的 this.selected
selectPaper() 没有 generation token 或 AbortController
快速 A→B→C 时，较旧的 load 可以晚到并覆盖新 Paper 内容
response sequence 检查在 note-pane.js:159-163 执行，但 hash 已经在 149-157 被修改；即使检测到旧响应，也已经污染状态
没有 visibilitychange flush
修复应把 paper_id、note identity 和 epoch 固定在每次操作中，并让切换真正等待所有在途保存结算；旧 epoch 的响应不得修改当前编辑器。
pdf-bridge.js
对 window.PDFViewerApplication 的依赖不是当前最大问题，Bridge 生命周期已经存在确定性故障
第一次 open() 后：
this.ready = true
this._wired = true
第二次 open() 在 77-110 创建新 promise 并更改 iframe URL，但没有重置这两个状态。新文档加载后 _markReady() 在 206-210 看到 ready === true 就直接返回，新 promise 永远不 resolve，最终 15 秒超时。
旧 event bus 也仍被 _wired 阻止重新绑定。
必须为每次 open：
建立独立 generation；
解绑旧 event bus；
重置 ready/_wired/pageCount;
只允许当前 generation resolve/reject；
防止旧 iframe load/probe 影响新 open。
其他具体问题
文件注释 pdf-bridge.js:141-143 声称慢 PDF 不会触发握手超时，但 97-101 明确存在 15 秒 timeout，文档与代码矛盾。
PDFJS_VERSION 只是显示在 UI；没有看到与实际 viewer build version 的运行时比较。
getSelection():281-309 直接读取 iframe selection/range，违反 ADR-009“父页面不得查询 iframe 内部 DOM”的冻结约束。
selection 只返回文本和页码，不能生成归一化 quads。
iframe sandbox 含 allow-popups，与“PDF 外链必须明确确认”冲突。
送审包未包含 viewer 文件及定制项，无法验证编辑器、附件动作、修改版下载是否已隐藏。
PDFViewerApplication 可以作为版本锁定后的适配层内部依赖，但必须有真实版本探针和 Bridge contract tests。当前还没有达到这一步。
storage.py、paper_index.py 与 scanner.py
“移动”与“复制”区分不完备
storage.py:290-324 只能看到“一条即将 upsert 的记录”，无法知道旧路径是否仍然存在，因此无法可靠判断：
旧路径消失、新路径出现：移动
旧路径和新路径同时存在：复制
find_duplicate_paper_ids() 在 385-396 对主键 paper_id 做 GROUP BY HAVING COUNT(*) > 1，但主键决定该查询永远不可能返回重复。
更严重的是 indexer 在 paper_index.py:74,103 始终传 allow_folder_move=True。将来接通 manifest 后，扫描到两个相同 ID 的复制文件夹时，很可能被解释为“最后一个路径获胜的移动”，而不是 fail closed。
正确实现需要：
一次扫描先建立完整 observation/staging set；
在修改 canonical rows 前，全局比较每个 manifest ID 的所有位置；
同一扫描中两个 live location → DUPLICATE_ID_CONFLICT
旧位置确实消失且只有一个新位置 → move
当前索引器不能处理真实移动、改名、换版
定向探针结果：
Paper 文件夹移动后：生成新 paper_id，旧 Paper 仍 active
source 改名后：新旧两个 source 都 active
PDF 字节替换后：version 仍为 1，SHA 仍为空
没有 manifest 读取，因此数据库重建必然重发身份
mark_sources_missing() 写错范围
storage.py:544-548 先把该 Paper 的所有 source 都标记 missing，然后才循环指定的 missing paths。传入只缺失 a.pdf，实际 a.pdf 与 b.pdf 都被标记。
多 active note
paper_notes 只有普通的 idx_notes_paper，没有“每个 Paper 至多一个 active note”的唯一约束。探针可以插入两个 active notes，get_note_for_paper() 无排序地返回其中一条。
“接口已实现但流程未实现”
以下组件存在，但没有生产闭环：
SQLite backup API：没有看到调度、保留策略与恢复演练
paper_write_intents：创建 note/manifest/sidecar 时没有使用，启动时也没有 recovery runner
state_version：只自动递增，没有 expected-version 冲突检查
append-only status event：有表，但状态机允许 API 任意目标转换，例如直接 UNREAD→COMPLETED
Scanner 的生产结构问题
scanner.py:235 使用 root.rglob("*")，后面的 current.name in exclude 只跳过当前目录，不会剪掉其后代：
.obsidian 被跳过
.obsidian/some-subdir 仍会被枚举并可能作为候选
MinerU 也类似：
容器本身在 252-254 被跳过
容器后代仍由 rglob 遍历
嵌套 full.md 没有绑定到父 Paper，scanner 只收集论文夹直接文件
所以“MinerU artifact 识别”成立，但“MinerU 来源正确进入 Paper 聚合”尚未成立。
4. 你没有列出的高风险问题
严重度	风险	后果
致命	损坏 sidecar 被当空文档后覆盖	用户全部历史 Annotation 可能被不可恢复地改写
致命	首次 sidecar 并发创建静默覆盖	两次成功响应可能最终只保留一条标注
致命	os.write 短写未处理	note、manifest、sidecar 可被截断
致命	note frontmatter ID 与 DB ID 不同或根本无 frontmatter	Note 无法从 Vault 重建，重命名后失去身份
高	Annotation schema 允许空坐标和空 fingerprint	已落盘历史数据未来无法可靠重定位
高	manifest schema 未约束重复 source ID、重复路径和多 primary	“通过 schema”不等于满足 ADR 语义
高	AIContext 合成测试与真实 API 脱节	P1 开始时才发现所有 source SHA 缺失
高	JS 的 value.length 被当成 byte budget	中文上下文实际 UTF-8 大小可远超 64KB 声称上限
高	全局 os.umask(0o077) 不恢复	创建 PaperStorage 后，进程内其他子系统新建文件权限会被意外改变
高	no-store 仅在成功响应局部设置	404/409/412/500 等包含私有路径或状态的信息可能被缓存
高	allow-popups + 无链接确认	PDF 外链可能绕过零出站交互边界
中高	source-relative asset endpoint 未实现	MinerU 图片可能错配同名文件、全库扫描或加载外部资源
中高	SQLite 权威状态没有实际周期 backup	WAL 数据库损坏或误删时，阅读状态与 WorkspaceState 无恢复路径
中高	custom JSON Schema validator 的 oneOf 实际是“至少一个分支通过”	将来分支重叠时不会执行标准 oneOf 的“恰好一个”语义
其中最典型的“写完了但没有做对”有三类：
paper_write_intents 表写完了，但没有任何核心写入使用它；
WorkspaceState API 写完了，但前端完全没有接入；
source version、orphan 字段和 AIContext schema 写完了，但生产数据没有提供其所需的真实 SHA/version。
5. 下一阶段最小边界
不要开始宽泛的 P0.5 或 P1。只做两个工作单元。
第一步：P0 完整性修复
范围严格限定为：
Manifest 读取、Adoption gate、删库重建与已有依赖状态迁移
source SHA/version、移动/复制/改名/缺失 reconciliation
sidecar operation lock、corrupt fail closed、真实 anchor/orphan
note ID、唯一 active note、write intent/recovery
VaultWriteService 死锁、短写、symlink 与跨进程冲突策略
Annotation schema 修订及已有数据迁移
该步的退出条件必须是：
删除数据库后，已采纳 Paper/Source/Note ID 不变
复制 manifest 文件夹必定冲突，移动必定保持身份
两个并发首次 Annotation 最终保留两条
损坏 sidecar 原字节不被覆盖
PDF 换版后 source version 增加，旧 Annotation 进入 orphan
任一关键写入阶段崩溃后能 roll forward，不产生不可解释孤儿
第二步：真实运行路径验收
范围严格限定为：
修复 Markdown /text 路径
WorkspaceState 捕获、flush、恢复
NoteEditor 的 Paper epoch、在途保存与快速切换
PdfBridge 多次 open、旧事件解绑、运行时版本检查
Annotation 返回形状与跳转
外链图片/链接/PDF popup/source-relative assets
所有状态码的 no-store
单 worker 启动约束和共享 mutation coordinator
使用真实结构副本完成以下端到端场景：
连续切换三篇 Paper 并同时编辑笔记
同一页面两个标签页并发标注
连续打开两篇不同 PDF
PDF 阅读位置在重启后恢复
Markdown 全文翻译与 MinerU full.md 正常切换
离线/禁止出站条件下无外部网络请求
412/409 后草稿和全部文件版本仍可恢复
这两步通过后，才进入受控实际使用；随后是 P0.5 高亮重绘，最后才是 P1 AI。
最终裁决：P0 不通过。架构方向仍可继续，但当前提交不能被标记为 P0 完成、不能开始 P1，也不应继续让现有笔记/标注写路径作用于唯一的真实 Vault。
