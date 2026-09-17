# ADR-009: PDF 阅读器与零出站边界 (PDF Viewer & Zero-Egress Boundary)

- **状态**: Accepted
- **日期**: 2026-09-16
- **决策者**: 用户 & Sol (GPT-5.6 Sol Pro Extended)
- **评审来源**: Oracle Job `2ecfbf15-5e89-49ba-a839-77e52acd0544`（Q5 / Q7 裁决）
- **不可逆属性**: 实现可逆，**Bridge 接口不可逆**

## 背景与问题

工作台前端当前是**单文件** `frontend/dist/index.html`（3,865 行 / 200KB），**无构建系统**，通过 CDN 引入 Tailwind / lucide / marked / highlight.js / KaTeX / DOMPurify。

论文工作台需要 PDF 阅读能力（渲染、翻页、缩放、搜索、文本选择），并需为 P1 AI 提供「当前页 + 当前选择」上下文。

三条候选路线：

| 路线 | 描述 |
|---|---|
| (a) PDF.js 官方 viewer | iframe 接入，功能最全 |
| (b) 自建 canvas 渲染 + 文本层 | 完全可控，可与翻译栏双向联动 |
| (c) 浏览器内置 PDF 插件 | 最省事，但无法建立稳定 selection/annotation 接口 |

同时必须处理一个既有安全边界冲突：`backend/app/security/path_guard.py` 的 `_ALLOWED_ASSET_EXTS` **显式禁止 PDF**（原设计意图是阻止内联主动可执行内容）。

## 决策内容

### 1. 路线裁决

**P0 选择 (a)：本地 vendored PDF.js 官方 generic viewer，通过同源 iframe + 版本化 Bridge 接入。**

**拒绝 (b) 的理由**：两周内会吞掉大部分工期。文本层、虚拟滚动、rotation、selection、accessibility 每一项都容易做成半成品 —— 而半成品的 PDF 阅读器比不做更糟。

**拒绝 (c) 的理由**：无法建立稳定的 selection / annotation / context 接口，是明确的技术债 —— 直接阻断 P1 AI 的「解释这一段」能力。

### 2. Bridge 接口冻结（不可逆）

PDF.js 的内部实现可换，但 **Bridge 是冻结接口**：

```typescript
interface PdfBridge {
  open(sourceId: string, sourceVersion: number): Promise<void>
  goToPage(pageIndex: number): void          // 0-based
  setScale(scale: number): void
  getCurrentPage(): number
  getSelection(): SelectionSnapshot | null
  onDocumentLoaded(cb: (info: DocumentInfo) => void): Unsubscribe
  onPageChanged(cb: (pageIndex: number) => void): Unsubscribe
  onSelectionChanged(cb: (sel: SelectionSnapshot) => void): Unsubscribe
  dispose(): void
}
```

**硬性约束**
- 父页面**不得查询 iframe 内部 DOM**；
- **不得依赖 PDF.js 私有 class 名称**。

### 3. 必须本地 vendor，禁止 CDN

不能使用 PDF.js CDN：

- viewer 与 worker 版本不一致会出现**隐蔽错误**；
- worker 跨域与 CSP 配置复杂；
- 论文工作台的网络能力**必须可关闭**；
- 用户现有全局 CDN 已是遗留出站依赖，**P0 不能再增加新的 CDN 依赖**。

至少本地固定以下文件，且**锁定同一个 PDF.js 版本**：

```
pdfjs/build/pdf.mjs
pdfjs/build/pdf.worker.mjs
pdfjs/web/viewer.html
pdfjs/web/viewer.mjs
cmaps/
standard_fonts/
wasm/
```

### 4. PDF 只读端点（不复用通用资源白名单）

**保留** `path_guard.py` 对 PDF 的排除 —— 该边界用于阻止通用静态资源路径返回任意二进制。PDF 使用**专用只读端点**：

```
GET  /api/paper-sources/{source_id}/content?version={source_version}
HEAD /api/paper-sources/{source_id}/content?version={source_version}
```

**禁止**使用路径式 API（如 `/api/pdf?path=02.%20🟡%20...`）—— 中文、emoji、全角冒号与 macOS NFD/NFC 会让路径 API 极度脆弱。

端点必须支持：

| 要求 | 说明 |
|---|---|
| `200` | 完整请求 |
| `206` + `Content-Range` | **单** byte-range 请求 |
| `416` | 非法范围 |
| `Accept-Ranges: bytes` | 声明 Range 支持 |
| 正确的 `Content-Length` | 不可因压缩而失真 |
| `Content-Type: application/pdf` | |
| `filename*=UTF-8''...` | RFC 5987 编码的文件名 |
| `X-Content-Type-Options: nosniff` | |
| `Cross-Origin-Resource-Policy: same-origin` | |
| 私有内容合理的 `Cache-Control` | |

**禁止响应压缩中间件对该端点做 gzip** —— 否则 byte range 语义被破坏。

必须用 `source_version` 或 `ETag` 防止 PDF 在一组 Range 请求**中途**被外部替换，导致 PDF.js 拼接不同版本的字节。版本变化返回 **409/412**，要求重新打开。

### 5. PDF 原文件永久只读

**不得**把 PDF.js 内置编辑或「保存带批注 PDF」当成工作台 Annotation 实现。

- PDF 原文件 **P0 永远只读**；
- **不得改写 PDF 字节**；
- Annotation 全部写 sidecar（见 ADR-008）；
- 必须**隐藏或禁用**会产生「修改后 PDF」的功能（注释编辑器、下载修改版）。

### 6. 零出站边界

即使不接 arXiv API，仍有多种隐式出站途径：

| 途径 | 缓解措施 |
|---|---|
| Markdown 外链图片 | **默认不加载**，显示占位 |
| Markdown 本地图片 | 改写为同源受控 asset endpoint |
| PDF 外部链接 | 需**明确确认**才可打开 |
| PDF 附件动作 | 禁用 |
| CDN 资源（PDF.js / KaTeX / marked） | Paper 子系统**不使用 CDN** |
| 用户点击引用链接 | 明确确认 |

**边界声明**：若「关闭网络」指浏览器**完全**零出站，则现有全局 CDN（Tailwind / KaTeX / marked / DOMPurify）也必须本地化；否则只能声称「**论文数据不出站**」。P0 采用后者，并在文档中明确标注该边界。

**Markdown 图片解析约束**：MinerU 的 `full.md` 常引用同目录或子目录图片。现有 `resolve_for_asset_read()` 会通过全 Vault `rglob` 按 basename 查找，既有歧义风险又有性能问题。**必须新增 source-relative asset endpoint，只从当前 Markdown 所在目录向下解析，不做全库搜索。**

### 7. PDF 渲染内存约束

长论文若所有页面按 Retina 分辨率保留 canvas，浏览器内存会迅速增长。

**必须**：使用 PDF.js viewer 自带虚拟化；限制高分屏 render scale；离屏页面释放 canvas。

### 8. 扫描版 PDF（无文本层）

部分扫描版 PDF 无法选中文本。**不能把「无 selection」错误解释为 PDF 损坏。**

P0 显示「本 PDF 无可用文本层」；OCR 与 MinerU fallback 放 P1。

## 收益与代价

**收益**
- 两周内获得生产级 PDF 阅读能力，不与工期对赌；
- Bridge 接口冻结后，P0.5 可替换为自建渲染而不影响调用方；
- 论文子系统**自身**零 CDN 依赖（PDF.js 与全部 ES 模块均本地 vendored）+ 同源 iframe + 专用端点，确保论文数据不出站且原文件永不被改写。
  （宿主页仍从 3 个 CDN 加载 Tailwind / KaTeX / marked / DOMPurify —— 论文工作台不引入新的外部依赖，但也不消除既有的。若需浏览器完全零出站，须把这些也本地化，属独立工作。）
- Range 支持使 23MB 级 PDF 可快速跳页。

**代价**
- iframe 内样式不可完全控制，视觉一体化程度有限；
- 跨 iframe 通信需要维护 Bridge 封装；
- 需自行 vendor 与锁版本，升级 PDF.js 需手动同步 viewer/worker；
- 持久高亮 overlay 的重绘无法依赖 PDF.js 内置实现，延后至 P0.5。

## 相关契约

- `backend/app/paper/schemas/paper.workbench.schema.json`
- ADR-008（Annotation 存储与 anchor）、ADR-002（零删除）
