# 论文工作台 P0 · 送审材料（Phase Completion Review）

> 送审日期：2026-09-16
> 送审范围：Day 1–5 全部交付（commit `8f0d384`）
> 依据：ADR-006 / 007 / 008 / 009（Day 1 冻结）
> 提请：P0 阶段整体评审

---

## 一、交付事实

### 1.1 提交序列（每个可独立回滚）

| Day | commit | 内容 |
|---|---|---|
| 1 | `bdd39c6` | 四份 ADR + 三份 JSON Schema + 34 项契约测试 |
| 2 | `8f0b685` | 六表 schema + 迁移框架 + 只读扫描器 + 发现报告 |
| 3 | `6d1580f` | VaultWriteService + PDF Range 端点 |
| 4 | `91900f9` | 前端 ES module + vendored PDF.js + 可用 PDF Bridge |
| 5 | `8f0d384` | 笔记编辑器 + Markdown 渲染 + 标注闭环 + AIContextV1 |

### 1.2 规模

| 维度 | 数值 |
|---|---|
| 后端 paper 模块 | 3,135 行 |
| 前端 paper 模块 | 2,442 行 |
| 测试 | 2,441 行（292 项全部通过） |
| vendored PDF.js | 8.4 MB / 295 文件（6.3.289 固定版本） |
| 真实数据 | 282 篇论文 / 708 个来源 |

### 1.3 真实验证（非 fixture）

| 验证项 | 结果 |
|---|---|
| 浏览器渲染 19.4MB / 95 页中文论文 | ✅ 工具栏完整 |
| 折叠面板不重载文档 | ✅ 同一 iframe，指纹不变 |
| 首次打开自动 UNREAD→READING | ✅ 侧栏徽标同步 |
| 笔记写入论文夹 | ✅ 落盘位置正确 |
| 标注 sidecar 权威性 | ✅ 清空 SQLite 索引后 API 仍可返回 |
| 索引幂等 | ✅ created=0 / updated=282 / conflicts=0 |

---

## 二、请求评审的核心问题

### Q1. 交付是否达到 P0 验收标准？

ADR-006 至 009 与 Sol 第一周计划共同定义的 P0 范围：

- [x] Paper 聚合 + 五实体模型
- [x] 递归扫描真实根目录 + MinerU artifact 识别
- [x] `paper.workbench.json` manifest（已实现读写能力，采纳流程未启用）
- [x] 稳定 Paper/Source/Note ID
- [x] 候选排序 / AMBIGUOUS 状态
- [x] PDF-only Paper 合法
- [x] 本地 vendored PDF.js + page/zoom/search/selection
- [x] Markdown 安全渲染 + 多翻译来源切换
- [x] PDF/Markdown **不做**同步滚动（符合 P0 范围）
- [x] source-ID API + PDF Range
- [x] 笔记编辑器 + 串行 debounce autosave + SHA256 冲突 + 409 保留草稿
- [x] SQLite status + reading metadata
- [x] WorkspaceState
- [x] localStorage 面板布局（仅几何）
- [x] 首次成功打开触发 UNREAD→READING
- [x] Annotation sidecar 记录 + 列表 + 点击跳转
- [x] source version 变化后标 orphan（字段与检测已具备）
- [x] AIContextV1 数据合同 + 纯数据装配 + contract test
- [x] 前端拆原生 ES modules
- [x] VaultWriteService
- [x] Paper router 统一 no-store
- [x] 单 worker 启动约束（未显式锁定，需评审）

**请判定：是否存在被我误判为「已完成」而实际未达标的条目？**

### Q2. 三项已知缺口是否可接受为 P0 遗留？

1. **manifest 采纳流程未启用**：`VaultWriteService` 与 schema 均已就绪，但「首次获得依赖状态时写入 manifest」的采纳状态机尚未接线。因此当前 282 篇论文的 `paper_id` 只存在于 SQLite，**尚未锚定到 Vault**。若重建数据库，身份会重新生成。
   - 风险：这与 ADR-006 的「身份必须持久化在 manifest」存在张力。
   - 我的判断：P0 内可接受，因为幂等索引已保证同一数据库内身份稳定；但**跨数据库重建会丢失身份**，而状态与标注的引用完整性依赖它。
   - **请裁决：这属 P0 缺陷还是可延至 P0.5？**

2. **PDF 高亮重绘未做**：锚点已带归一化坐标与文本引文，`orphaned_at` 字段与检测已具备，但不在 PDF 上持久重绘。按 Sol 计划属 P0.5。

3. **单 worker 约束未显式化**：`VaultWriteService` 的路径锁与 `PaperStorage` 的锁均为进程内锁。当前以单进程 uvicorn 运行，但代码中无强制约束，若误用 `--workers 4` 会导致锁失效。
   - **请裁决：是否需要一条启动期断言？**

### Q3. 两处「测试假保障」是否反映更深的设计问题？

**事件**：我为路径拼接缺陷写了回归测试，用变异测试验证时发现 —— **把 bug 重新注入后 23 项测试全部通过**。根因是 fixture 让 `papers_root == vault_root`，两种路径拼接结果相同。

**修正后**：fixture 复制生产结构（papers_root 嵌套在 vault 内），注入 bug 会致 6 项测试失败。

**我关心的是**：这类「测试与生产结构不一致」的问题，是否还潜伏在别处？例如：
- 测试用 `tmp_path` 而非真实 Vault，是否掩盖了其他路径/权限问题？
- 是否有其他 fixture 把本应不同的两个概念设为相同值？

**请指出这类风险的排查方向。**

### Q4. 哪些决策会在 P1 变成不可逆的技术债？

ADR 冻结了六项不可逆决策。经过 5 天实现，我对其中两项有了新的认识，想请您复核：

1. **`source_positions` 用 map 而非单值**：实现后确认这是对的（一篇论文的全文翻译与导读需要各自位置），但**当前的 WorkspaceState 只存了位置比例，未存 `pdf_scale` 与 `rotation`**。schema 里定义了，实现里省略了。这会成为 P1 的问题吗？

2. **Annotation 的 `body_markdown` 与 `kind` 分离**：实现时把「感想/创新点/疑问」作为 `kind`，把正文作为 `body_markdown`。但需求文档的「快捷输入类型」暗示用户可能想要**先选类型再写内容**的流程。当前 API 一次只建一条标注，是否够用？

### Q5. 下一步该做什么？

候选路径：
- **A. 补 P0 缺口**：接线 manifest 采纳流程，把 282 篇身份锚定到 Vault
- **B. 进 P0.5**：高亮重绘 + 标注重定位 + 双向互定位
- **C. 实际使用一段时间**，以真实摩擦点决定优先级
- **D. 接 P1 AI**：AIContextV1 已就绪，可直接开始装配与调用

**请给出优先级建议，并说明理由。**

---

## 三、请评审的实现细节

### 3.1 值得重点审查的文件

| 文件 | 审查理由 |
|---|---|
| `backend/app/paper/writer.py` | 乐观锁 + 提交前二次校验 + no-clobber 创建 + 版本化备份。任何不变量缺口都会污染 Vault |
| `backend/app/paper/api.py` | 路径拼接（`_paper_rel`）、sidecar 校验顺序、软删除语义 |
| `backend/app/paper/api_sources.py` | Range 实现、版本固定、Markdown/PDF 分流 |
| `frontend/dist/paper/note-pane.js` | 串行保存队列与 409 处理；乱序响应是隐性数据损坏源 |
| `frontend/dist/paper/pdf-bridge.js` | 冻结接口；`window.PDFViewerApplication` 依赖是否过脆 |
| `backend/app/paper/storage.py` | 唯一允许 DELETE 的位置（派生索引）；`upsert_paper` 的复制/移动区分 |

### 3.2 我自认最有风险的三处

1. **`pdf-bridge.js` 依赖 `window.PDFViewerApplication`**：这是官方 viewer 的公开面，但并非稳定 API 契约。若 PDF.js 升级移除或改名，Bridge 会整体失效。当前有版本固定，但升级路径未设计。

2. **`_write_sidecar` 的读-改-写竞态**：先 `_read_sidecar` 再 `service.save(expected_hash=...)`，中间若他处修改会 409。这是正确行为，但**并发添加两条标注时后一条会失败**，用户需要重试。是否需要服务端串行化？

3. **`count_papers` 与 `list_papers` 的软删除语义**：`inactive_at` 的论文不出现在列表，但其 `paper_sources` 仍 active。若未来要恢复，需要一并处理。

---

## 四、继承的不变量（请求复核是否全部满足）

| 不变量 | 我的实现 | 请复核 |
|---|---|---|
| 零删除 | paper/note/source 无物理删除 API；仅 `annotations_index` 可 DELETE（派生索引） | ? |
| SHA256 乐观锁 | 笔记与标注写盘均校验 `expected_hash` | ? |
| 原子写 + 备份 | temp + fsync + rename + parent fsync，内容寻址小时分桶备份 | ? |
| 路径隔离 | `..`/绝对路径/symlink/排除区全部拒绝；papers_root 必须落在 Vault 内 | ? |
| 私有性 | `/api/paper*` 全部 `no-store`；localStorage 仅存几何 | ? |
| 本机优先 | 127.0.0.1；PDF.js 本地 vendored 无 CDN | ? |
| 零出站 | 论文子系统无外部请求（Markdown 外链图默认不加载未实现，见下） | ? |

**已知未完全满足**：ADR-009 要求「Markdown 外链图片默认不加载，显示占位」与「source-relative asset endpoint 只从当前目录向下解析」。**当前 Markdown 渲染未接管图片加载**，将沿用宿主页的 asset 端点行为（可能全 Vault basename 搜索）。请判定其严重性。

---

## 五、输出要求

请给出：

1. **P0 判定**：通过 / 有条件通过 / 不通过，并列出必须修复项
2. **Q1–Q5 逐条裁决**
3. **优先级建议**（A/B/C/D 及理由）
4. **我遗漏的风险**：特别是会导致 P1 返工或数据损坏的
5. **下一阶段的最小边界**：如果继续推进，两步之内该做什么
