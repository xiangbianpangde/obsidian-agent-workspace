# 论文工作台 P0 · 修复复核送审（Remediation Re-review）

> 送审日期：2026-09-16
> 上轮裁决：**NO-GO**（Job `46149f03`），8 项必须修复
> 本轮范围：P0-B1 ~ P0-B8 全部修复
> 请求：判定是否可宣布 P0 通过

---

## 一、我如何回应上轮裁决

上轮你判定我「把写完了当成做对了」。我没有直接修补，而是**先独立复现每一条指控**，再修复，最后用变异测试验证修复有效。

### 1.1 复现结果（修复前）

| 你的指控 | 我的复现方式 | 结果 |
|---|---|---|
| 并发首次标注静默丢失 | 两个请求同时添加 | 确认：只剩 `ann_B`，`ann_A` 被删 |
| 损坏 sidecar 被覆盖 | 读写损坏文件 | 确认：读作空文档且带 hash，写回即销毁 |
| `os.write` 短写截断 | 模拟合法短写 | 确认：10 字节写成 3 字节 |
| 内部 symlink 绕过排除区 | `alias → .git` | 确认：`alias/config` 解析到 `.git/config` |
| `save()` 死锁 | 线程调用 `require_existing=False` | 确认：3 秒未返回 |
| source version 形同虚设 | 换版后重扫 | 确认：version 仍为 1 |
| 前后端路径/形状不一致 | 对比请求与路由 | 确认：前端请求 `/content`，后端要求 `/text` |
| `mark_sources_missing` 误标 | 传一个缺失文件 | 确认：全部来源被标记 |
| Bridge 第二次 open 超时 | 检查 `ready` 重置 | 确认：promise 永不 resolve |
| 死代码：重复 ID 检测 | 主键 GROUP BY | 确认：结构上永不返回 |

**十项全部成立。** 我此前的自评是系统性的偏差，不是个别疏忽。

### 1.2 我额外发现的问题

写验收场景时又暴露出 4 个此前未发现的缺陷：

1. **复制文件夹被当作移动** → 原位置的笔记、标注、阅读位置全部成为孤儿
2. **改名后的旧绑定永不回收** → 阅读器被提供磁盘上不存在的来源
3. **备份未 fsync**（你在评审判决第 220 行提到过，我当时未处理）
4. **索引器 `UnboundLocalError`** → 来源改名时直接崩溃

---

## 二、8 项必须修复的闭合情况

| 编号 | 要求 | 状态 | 关键证据 |
|---|---|---|---|
| **P0-B1** | 接通 Discovery→Adoption→Manifest 权威链 | 已闭合 | 删库后用全新空库，仅凭 Vault manifest 恢复出**相同 paper_id 与 source_id** |
| **P0-B2** | 消除权威文件的静默数据覆盖 | 已闭合 | 6 并发标注全部保留；损坏 sidecar 返回 409 且**原字节不变** |
| **P0-B3** | 修复 VaultWriteService 实际缺陷 | 已闭合 | 死锁、短写、symlink 三项均实测修复 |
| **P0-B4** | 建立真实 source hash/version 生命周期 | 已闭合 | 换版后 version 递增至 2；Range 从固定 FD 读取，**实测不混合两个版本** |
| **P0-B5** | 修正 Annotation 冻结合同 | 已闭合 | 引入 **anchor_schema_version 2**：v1 保持可读，v2 强制可解析锚点 |
| **P0-B6** | 补齐真实前端闭环 | 已闭合 | 浏览器实测：**切走再切回恢复页码 11** |
| **P0-B7** | 闭合零出站与私有响应边界 | 已闭合 | 外链图阻止；**500 带 no-store**；source 相对 asset 端点 |
| **P0-B8** | 生产等价测试重新验收 | 已闭合 | 19 个验收场景全部通过 |

---

## 三、请复核的具体决策

### Q1. 关于「不要悄悄改变冻结 v1 的语义」

你在裁决中要求：**已有数据时应提高 schema/anchor 版本并提供迁移，不要悄悄改变所谓"冻结 v1"的语义。**

我的做法：

- `anchor_schema_version` 接受 **1 或 2**
- **v1 保持可读** —— 收紧合同不追溯作废历史数据
- **v2 强制**：PDF 至少一个归一化坐标 + 文本引文；Markdown 需 block_fingerprint 或 text_position + 文本引文
- 写入端产 v2；客户端仍可提交 v1
- 版本规则在 `contracts.py` 中用代码表达（JSON Schema 子集无法表达"仅当版本≥2时必需"）

**请判定：这个迁移路径是否满足你的要求？v1→v2 的升级时机（"下次写入时"）是否需要更明确的机制？**

### Q2. 关于「复制 vs 移动」的判定依据

我的实现：**仅当原文件夹不再出现在本次扫描结果中**才认定为移动。

可能的争议点：
- 若用户"剪切"文件夹但目标位置恰好在扫描根之外 → 会被判为复制而 fail closed（安全但可能误报）
- 若扫描因权限错误漏掉原文件夹 → 同样误判为复制

**请判定：这个判据是否足够？是否需要额外的佐证（如 mtime、或要求显式 move 指令）？**

### Q3. 关于「启动断言不是并发模型的替代品」

你的原话我理解为三层责任，因此实现为：

| 层 | 机制 | 责任 |
|---|---|---|
| 1 | `assert_single_worker()` | 拒绝多 worker 启动 |
| 2 | `VaultWriteLock`（O_CREAT\|O_EXCL + PID 记录） | 阻止第二个进程占用同一 Vault |
| 3 | **进程内锁（per-path + per-paper RLock）** | **真正的串行化** |

并且：`_service()` 从「每次请求新建」改为**模块级单例**，否则同一 worker 内不同请求也拿不到同一把锁。

**请判定：这是否满足你对并发模型的要求？进程锁文件的陈旧处理（进程被 SIGKILL 后锁文件残留）是否需要额外机制？**

### Q4. 我仍未做的两件事

诚实列出，请你判定其严重性：

1. **多标签页并发**：Sol 提到「同一页面两个标签页并发标注」。后端已具备串行化（per-paper 锁 + 重试），但**前端未做跨标签协调**（无 BroadcastChannel / storage 事件）。两个标签页各自持有不同的 `expected_hash` 时，后者会得到 409 并提示重新加载 —— 这是否可接受？

2. **`source_positions` 的结构校验**：你在裁决中问过「后端把 source_positions 当自由 JSON 保存，没有按 PDF/Markdown Position 结构验证；PUT 也没有 expected state_version，多标签页会 last-write-wins」。我只修了前半（前端现在按结构写入），**后端仍未校验 Position 结构，也未加 `expected state_version`**。请判定这是否构成 P0 阻断。

---

## 四、变异测试（应你的要求设为验收门）

你要求「关键变异必须被杀死」。以下每项均验证过：**撤销修复 → 对应测试失败**。

| 撤销的修复 | 捕获 | 说明 |
|---|---|---|
| 非重入锁（`save()` 死锁） | 是 | |
| 单次 `os.write`（短写截断） | 是 | |
| `mark_sources_missing` 误标范围 | 是 | |
| NoteEditor 的 epoch 隔离 | 是 | |
| 索引器读取 manifest | 是 | 改为行为测试后才捕获 |
| v2 锚点校验 | 是 | |
| Range 的文件描述符固定 | 是 | |
| 移动 vs 复制检测 | 是 | |
| 过期绑定回收 | 是 | |
| 备份目录 fsync | 是 | 改为精确计数后才捕获 |

### 我在变异测试中发现的自身缺陷

两个断言最初**在有缺陷的代码上也会通过**：

1. **备份耐久性**：最初在源码里搜 `fsync` 字样 —— 移除调用后仍匹配（因为别处也有 fsync）。改为**精确断言 4 次 fsync 调用**后才捕获。
2. **索引器读 manifest**：最初只读源码判断。改为**真实跑一遍索引器并断言恢复出的 ID** 后才捕获。

这与上轮你指出的「测试与生产结构不一致」是同一类问题 —— 我又犯了两次。

---

## 五、证据

### 5.1 测试与场景

- **351 项测试通过**
- **19 个验收场景**，覆盖你要求的全部类别：
  - 身份：删库重建 / 移动 / 复制 / 改名 / 换版 / 缺失
  - 并发与崩溃：6 并发标注 / 损坏 sidecar / 崩溃后前滚 / Obsidian 并发编辑 / 快速切换
  - 单写者：多 worker 拒绝 / 第二实例拒绝 / 锁文件记录 PID / 释放幂等
  - 合同：前端 URL vs 后端路由 / no-store / 渲染器 fail closed / 备份耐久性

### 5.2 真实 Vault 状态

**零污染**：manifest 0 · sidecar 0 · lock 0

所有验证在 `~/.personal-ai-workspace/paper-verify/vault`（1.1GB / 78 篇真实结构，含 49 个 MinerU 容器）与临时目录完成。真实 Vault 从未被写入。

### 5.3 一个反复出现的根因

我的场景辅助类第一版把 `folder_relpath` 拼到 vault 根，写到了错误目录 —— **「两个根」（papers_root vs vault_root）这类 bug 已第三次出现**（Day 5 笔记路径、Adoption gate、本次测试辅助）。

我在辅助函数中强制区分并在注释中写明缘由。**请判定：是否需要在更基础的层面（如类型系统）阻止这类错误？**

---

## 六、输出要求

请给出：

1. **P0 判定**：通过 / 有条件通过 / 不通过
2. **Q1–Q4 逐条裁决**
3. **我遗漏的风险**：特别是仍未做的多标签页并发与 source_positions 校验，是否构成阻断
4. **是否可进入 P0.5 或受控真实使用**
5. **若有必须修复项**：请明确列出，不要用「建议」措辞

## 附：本轮提交

```
4a23a9e fix(paper): close the P0 review findings — data loss, path escape, adoption gate
e758e55 fix(paper): close P0-B6/P0-B7 — workspace state, epoch isolation, egress boundary
b400361 fix(paper): close P0-B4/B5/B7 — anchor schema v2, pinned reads, source-relative assets
c0bee55 test(paper): P0-B8 acceptance scenarios and single-writer coordination
```
