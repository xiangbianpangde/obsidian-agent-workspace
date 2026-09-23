---
type: handoff
handoff_id: HO-20260923-001
status: canonical
sender: "pi-agent (01a0adc8)"
intended_receiver: "next-agent / 用户本人"
created: 2026-09-23
updated: 2026-09-23
state_pointer: "docs/00-proposal-v01-original.md"
workspace_path: "/Users/xbpd/Projects/个人工作台"
---

# 当前全局交接：个人工作台（Personal AI Workspace）

## 1. 项目背景与当前阶段

- **项目名称**: 个人工作台（Personal AI Workspace / Obsidian Agent Workspace）
- **工作区绝对路径**: `/Users/xbpd/Projects/个人工作台`
- **当前阶段**: **P0 全部结项**（论文工作台经 13 轮独立评审 PASS；真实 Vault 写路径已启用）。**P0.5 已立项待实现**；P1 未启动。
- **运行环境**: macOS | Python 3.12.7（`.venv`）| node v22.23.2 | uvicorn @ `http://127.0.0.1:8787`
- **仓库**: https://github.com/xiangbianpangde/obsidian-agent-workspace （main，已同步）

### 五个工作台核心（全部可用）

| 核心 | 状态 | 数据规模 |
|---|---|---|
| 知识中心（Obsidian） | 运行中 | 扫描 3617 md 文件（2026-09-23 实测）/ 2.8s |
| AI 会话中心（AgentsView） | 运行中 | 1800+ 会话 |
| 统一消息中心（IM Hub） | 运行中 | 19141 条（微信 3168 / 企微 3903 / QQ 12070） |
| 课程与校历（Schedule） | 运行中 | 12 课程 / 16 时段，第 4 周·教学周 |
| 作业中心（Assignment） | 运行中 | 动态同步 |
| **论文工作台（Papers）** | **P0 结项** | 383 篇 / 20 采纳 / 32 待确认 |

## 2. 已完成工作

### 2.1 知识层与三核心（P0-1 ~ P0-2）
1. Obsidian 知识层：Vault 扫描、Watchdog 同步、CRUD-无删除、SHA256 乐观锁、三栏 UI（KaTeX/assets/Dataview 降级）、Templater 兼容。
2. AgentsView 集成：双核心切换、CLI 主 + SQLite-RO 备、有界分页、工具调用回放、`Cache-Control: no-store`。
3. IM Hub：本地派生日志（WAL + UNIQUE）、适配器、协调器、SSE、三核心前端。真实微信（LLDB 取密钥）、企微（Frida + 快照）、QQ（本地快照）全部接入。

### 2.2 论文工作台 P0（2026-09-16 ~ 2026-09-22）
- 4 份 ADR（006~009）、3 份冻结 JSON Schema、34 项契约测试；
- 六张 SQLite 表 + 幂等迁移 + MinerU 产物识别；
- `VaultWriteService`（可重入锁 + 短写修复 + symlink 拒绝）；
- Vendored PDF.js 6.3.289、ES module 前端、四栏 CSS-Grid、笔记/标注/阅读位置/跨标签同步；
- **13 轮独立评审**（会话 `01a0aeaf`）：**7 处真实缺陷**修复 + 变异验证，3 条误报被撤回，1 个性质问题由用户裁定。

### 2.3 关键性质决策：ADR-010 跨介质一致性
评审逼出的**性质层面**结论：文件与 SQLite 无法由进程内代码做成原子。用户裁定 **选项 A（检测 + 收敛）**：
- 允许提交窗口内短暂不一致；
- 必须被检出（`post-commit-drift` + intent 保持 pending，**绝不报 clean completed**）；
- 必须由下一次 index 从 Manifest（ADR-007 权威）收敛。

拒绝选项 B（严格瞬时一致）的理由：需锁住**所有**写者，包括用户用 Obsidian 直接编辑 —— 与 ADR-006 前提冲突。

### 2.4 IM Hub 静默故障修复（2026-09-23）
P0 收尾后发现 QQ 摄入**已停摆 13 天**，修复三个真实缺陷：
1. 一条不可对齐记录冻结**全部**摄入 → 改为隔离（库中原值保留、其余照常入库、冲突入表可查）；
2. 故障完全不可见（只记常量字符串 13 天）→ 改为记录异常类型 + 内容 + 栈追踪；
3. `QQ_MESSAGE_TYPES[5]` 映射错误（`image` → `notice`，经 3000 条真实数据交叉验证）。

## 3. 待完成工作与当前唯一下一步

- **唯一下一步**: 用户裁决 IM 冲突数据的处置方式（见 §5 待决问题 P-01）。
- **次一步（用户已授权方向）**: 实现 P0.5 `NO_CONTENT_AVAILABLE` 状态（提案 + 三问裁定已就绪，见 `docs/11-p05-no-content-available-proposal.md`）。
- **未开始**: P1（AI 接入：AIContextV1 合同已定义并测试，从未调用模型）。

## 4. 核心设计边界与不变量 (Invariants)

1. **零删除铁律**（ADR-002）：所有 DELETE 均为软删除（`is_deleted` / `deleted_at` / `is_revoked`），知识资产永不物理删除。
2. **零出站边界**（ADR-009）：PDF.js 本地 vendored；远端图片渲染时不加载（占位按钮 + 用户显式动作）。
3. **权威分层**（ADR-007）：Vault manifest 管身份/绑定/标题/tags；SQLite 管状态/阅读现场。**禁止双写**。
4. **不做 Obsidian 替代品**（ADR-001）：工作台是增强层，Vault 可被 Obsidian 直接编辑。
5. **IM 无出站发送能力**：工作台对任何 IM 不持有发送能力。
6. **状态分离**：`tested` ≠ `human-accepted`。

## 5. 关键文件与状态对照表

| 文件路径 | 状态 | 依据与说明 |
|---|---|---|
| `HANDOFF.md` | canonical | 全库唯一当前交接（本文件） |
| `docs/用户与需求画像/00_索引.md` | active | **用户与需求画像索引**（本次新建） |
| `docs/00-proposal-v01-original.md` | immutable | 用户 2026-09-02 方案原文（需求基线） |
| `docs/01-tech-design-v0.2.md` | approved | 技术方案 v0.2，已过 Sol 审核 |
| `docs/03-decisions-and-questions.md` | approved | 用户已拍板决策 5 条 + 环境冲突事实 |
| `docs/01-vault-survey.md` | recorded | Vault 真实环境勘察 |
| `docs/10-cross-medium-consistency-acceptance.md` | approved | 跨介质一致性验收标准 |
| `docs/11-p05-no-content-available-proposal.md` | proposed | P0.5 提案（三问已裁定） |
| `docs/architecture/ADR/` | approved | 10 份架构决策记录（ADR-001 ~ ADR-010，完整列表见 `docs/用户与需求画像/00_索引.md` §4） |
| `config.yaml` | active | vault 路径 / papers_root / 安全开关 |
| `backend/app/paper/` | tested+accepted | 论文工作台 P0 |
| `backend/app/im/` | tested | IM Hub（含 2026-09-23 修复） |
| `backend/app/schedule/` | tested+accepted | 课程与校历 |
| `.venv/bin/pytest` | 427 passed | 全量测试 |

## 6. 待决问题（需用户裁决）

| ID | 问题 | 选项 |
|---|---|---|
| P-01 | IM 库中约 823 条记录的 `message_type` 仍为旧的错误值 `image`（修正后应为 `notice`），无法自动对齐 | (a) 保持隔离（当前）(b) 迁移这批记录并重算摘要 (c) 回退映射修正 |
| P-02 | 17 篇 `00-元数据.md` 占位目录的 `NO_CONTENT_AVAILABLE` 状态是否纳入 P0.5 实现 | 已裁定纳入，待实现 |
| P-03 | 15 篇多候选论文需人工在 UI 排序主次 | 待用户操作 |

## 7. 后续接手者操作指南

1. **先读**: 本文件 → `docs/用户与需求画像/00_索引.md` → 相关 ADR。
2. **启动服务**: `nohup .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8787 &`
3. **跑测试**: `.venv/bin/pytest -q`（当前 427 passed）
4. **改动前**: 确认是否触及零删除/零出站/权威分层三条不变量。
5. **涉及设计决策**: 先咨询 Sol（`/sol`）；Sol 不可用时用独立评审会话。
6. **提交**: `git push origin main`（pre-push 钩子会跑全量测试）。

## 8. 知识复利与文档状态

- **架构决策**: `docs/architecture/ADR/`（ADR-001~010，10 份）
- **治理规范**: `docs/governance/AGENTS.md`
- **验收标准**: `docs/10-cross-medium-consistency-acceptance.md`
- **提案**: `docs/11-p05-no-content-available-proposal.md`
- **用户与需求**: `docs/用户与需求画像/`（本次新建，见其索引）
