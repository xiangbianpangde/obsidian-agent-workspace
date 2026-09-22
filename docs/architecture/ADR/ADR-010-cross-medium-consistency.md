# ADR-010: 跨介质一致性 —— 检测与收敛 (Cross-Medium Consistency)

- **状态**: Accepted
- **日期**: 2026-09-22
- **决策者**: 用户（裁决 A）
- **评审来源**: 独立评审 13 轮，会话 `01a0aeaf`；关键论证见下文「为什么不能更强」
- **不可逆属性**: **语义不可逆** —— 一旦 P1 AI 依赖此语义，改为严格锁将破坏外部编辑能力

## 背景与问题

论文工作台的状态跨两种介质：

- **Vault 文件**（`paper.workbench.json`、`notes.md`、`paper.annotations.json`）—— 权威源，用户可用 Obsidian 或其他工具直接编辑；
- **SQLite**（状态、阅读现场、派生索引）—— 权威源，仅工作台写入。

跨介质写入无法原子化：文件系统与数据库之间没有事务。独立评审在 13 轮中逐步逼出了这个问题的边界，最终给出一个**性质层面**的论证：

> 「读 → 检查 → 提交」无论把检查放得多近，检查与提交之间总存在窗口。事务内 callback 也只是把窗口推后，不是关闭它。外部编辑器不受任何我方锁约束。

评审用可复现的注入证明：在事务内校验 callback 的 `read()` 返回之后、`COMMIT` 之前替换 Manifest，提交仍会完成，从而 SQLite 与 Vault 不一致。

因此必须明确：**P0 的验收标准是什么？** 这直接决定 ADR-006/007 的既有前提是否要改。

## 决策内容

### 1. 裁决：检测 + 收敛（选项 A）

**不追求任一时刻的严格一致；追求「不一致必然被检出，且必然由权威源收敛」。**

具体语义：

| 阶段 | 行为 |
|---|---|
| 写入前 | 记录**决策所依据的** Manifest 摘要（pre-write digest）作为 CAS 前置条件 |
| 写入前 | 记录**本次打算发布的**摘要（published digest）写入 intent，与 intent 同时落库 |
| 写入时 | 若 Manifest 已偏离前置条件 → 拒绝写入（fail-closed），外部编辑保留 |
| 提交时 | 事务内 callback 再校验一次，已可见的偏离直接回滚事务 |
| **提交后** | **复读 Manifest；若在提交窗口内移动 → 返回 `post-commit-drift`，intent 保持 pending** |
| 后续 | 下一次 index 从 Manifest 回填 SQLite（ADR-007：Manifest 是身份/绑定/标题/tags 的权威） |

关键点：**提交后必须复读**。这是唯一能覆盖「callback 返回后到 COMMIT 之间」的机制——它不阻止不一致发生，但保证不一致**不会静默**。

### 2. 为什么不能更强（严格瞬时一致的代价）

要消除窗口，唯一办法是让**所有写者**服从同一跨进程文件锁。但写者包括：

- 工作台进程（可约束）；
- **用户用 Obsidian 直接编辑**（不可约束）；
- 未来任何同步工具（不可约束）。

若强行要求，只能是在检测到外部编辑时**拒绝或回滚用户的编辑**——这直接违背 ADR-006 的核心前提「Vault 是用户的知识资产，可被 Obsidian 直接编辑」。用一个内部一致性目标去锁住用户对自己笔记的编辑权，代价不可接受。

**因此：严格瞬时一致在「允许外部编辑」的前提下不可达。** 这不是实现缺陷，而是架构约束。

### 3. 权威方向不可颠倒

收敛方向**永远**是 Manifest → SQLite。理由见 ADR-007：

- Manifest 随文件夹移动，是重建后唯一能恢复身份的记录；
- SQLite 承载设备局部的高频状态，允许丢弃（可由 Manifest 重建）。

因此 `post-commit-drift` 的正确处置是「保持 pending 让 index 收敛」，**绝不是**「用 SQLite 覆盖 Manifest」。

### 4. 明确禁止的两种"修补"

评审过程中我实际犯过这两种，写在这里防止复发：

1. **禁止只移动检查位置**（把检查从提交前挪到提交内就宣称闭合）—— 窗口只是变窄；
2. **禁止用 SQLite 反向覆盖 Vault** 来"消除不一致" —— 方向颠倒，会丢掉用户编辑。

## 收益与代价

**收益**

- 用户始终可以用 Obsidian 编辑论文夹，不被工作台锁住（保留 ADR-006 前提）；
- 不一致**必然可见**（`post-commit-drift` + pending intent），不会静默积累；
- 收敛方向与 ADR-007 一致，删库重建仍能从 Vault 完整恢复；
- P0 验收标准明确，不需要为不可达的目标继续加锁。

**代价**

- 存在**短暂**不一致窗口（毫秒级，仅在并发外部编辑且恰好落在提交窗口时）；
- 极端情况下 UI 可能短暂显示 SQLite 的旧值，直至下一次 index 收敛；
- 需要运维理解「pending intent 非错误、是待收敛信号」。

## 相关契约与实现

- `backend/app/paper/manifest.py` — `update_manifest(expected_manifest_digest=...)` 前置条件；返回 published digest；
- `backend/app/paper/storage.py` — `commit_resolved_adoption(verify_manifest=...)` 事务内校验；`set_write_intent_payload_digest`；
- `backend/app/paper/recovery.py` — `post-commit-drift` 检测；`unverifiable` 拒绝无摘要 intent；
- 探针：`test_p0_w` / `test_p0_w2`（前置条件）、`test_p0_y` / `test_p0_y2`（事务内校验）、`test_p0_z`（提交窗口检测）、`test_p0_z2`（收敛保证）。

## 相关文档

- ADR-006（Paper 身份与 manifest）—— 本 ADR 保护其「Vault 可被外部编辑」前提
- ADR-007（权威分层）—— 本 ADR 沿用其收敛方向
