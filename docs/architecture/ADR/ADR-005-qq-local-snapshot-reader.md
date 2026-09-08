# ADR-005: QQ 本地只读快照接入与 Zhin Webhook 废弃 (QQ Local Snapshot Reader & Zhin Webhook Deprecation)

- **状态**: Accepted
- **日期**: 2026-09-08
- **决策者**: 用户 & Sol (GPT-5.6 Sol Pro Extended)

## 背景与问题
在构建统一消息中心 (IM Hub) 时，原设计 (v0.2.7) 将 QQ 规划为基于 Zhin.js / OneBot 的单向推送驱动器 (`ZhinQQAdapter`, `canReadHistory=false`, `coverage=realtime_only`)。但在实际集成过程中遇到以下硬阻断：
1. **依赖与可用性阻断**：`@zhin.js/adapter-icqq` 强依赖仅发布于 GitHub Packages 内部的 `@icqqjs/icqq`，开源公网环境无法安装；
2. **身份与去重不变量冲突**：Zhin push 事件传递的 `event_id` 无法证明能与 QQ 本地物理记录定位符保持一致，违背了“同一消息跨路径必须产生相同 dedupe_key 与 canonical digest”的全局冻结不变量；
3. **安全与权限外溢**：Webhook 接入需要维护默认共享密钥，且 bot 客户端存在潜在的出站发送能力，违背工作台“绝对无出站发送能力 (Zero Outbound Authority)”的安全底线。

与此同时，经用户明确授权，我们对 macOS 运行中的 QQ NT (v6.9.98) 进行了内存结构与本地数据库勘察，证实：
- QQ 本地核心库 (`nt_msg.db`, `group_info.db`, `profile_info.db`) 采用 SQLCipher 加密，首部带 1024 字节 `QQ_NT DB` 自定义头；
- 在不退出 QQ、不重启应用的前提下，可通过只读内存扫描匹配数据库 salt，安全提取实时 SQLCipher codec；
- 提取所得密钥参数可成功解密导出 101 个表的标准 SQLite 明文库，且连续快照验证表明物理消息主键 (`40001`) 跨快照 100% 稳定。

用户明确接受：“QQ 使用和微信一样的方式也行”（转向本地数据只读读取）。

## 决策内容

1. **全面转向本地只读快照架构 (`QQSnapshotAdapter`)**：
   - 废弃 `ZhinQQAdapter`，由 `QQSnapshotAdapter` 替代；
   - `QQSnapshotAdapter` 同时实现 `IMSourceReader` 与 `IMIngestDriver`：
     - `read_history()`：从已发布、已校验的不可变明文快照中按时间读取历史；
     - `_poll_loop()`：定期检测 `CURRENT` 快照指针是否前进，检测到新快照时全量扫描并依赖 Journal 的 `UNIQUE(source, account_id, dedupe_key)` 幂等入库；
   - 彻底删除 `/internal/im/ingest/zhin` 端点、`IM_INGEST_SECRET` 环境变量及相关 bot 安装文档，消除双入口冲突与密钥泄露风险。

2. **安全域强隔离：提取器与工作台生命周期解耦**：
   - **QQ 原始源域**：仅由独立提取工具在用户授权时读取，工作台运行时不得知道 QQ 容器路径；
   - **QQ 本地快照库 (`~/Library/Application Support/qq-local-vault`)**：由一次性提取器写入，工作台仅只读访问其中已发布的 `export/` 目录；
   - **工作台无特权运行**：工作台运行时严禁导入 `lldb`、严禁导入 SQLCipher 导出工具、严禁调用 `os.kill` 挂起进程、严禁发起 QQ 网络交互，且不持有任何出站凭证。

3. **数据一致性与原子快照发布**：
   - 提取工具在捕获时通过 `lsof` 枚举所有持有数据库写句柄的 QQ 进程并短暂暂停（`SIGSTOP`），配置独立子进程 `_ResumeGuard` 确保异常时无论如何恢复进程（`SIGCONT`）；
   - 在写进程静止期内，利用 macOS 同卷 `clonefile` COW 原子复制 DB、WAL 与 SHM 集合；
   - 离线去除自定义头并由 `sqlcipher` 导出为标准 SQLite 格式；
   - 校验明文 `PRAGMA integrity_check` 与核心 schema 指纹，全部通过后生成 `manifest.json`；
   - 在 `staging/` 校验通过后原子 `os.replace` 发布到 `snapshots/<snapshot_id>`，并通过带乐观锁检测的临时文件原子替换 `CURRENT` 指针。

4. **保守能力基线与身份不变量**：
   - `canReadHistory=True`, `realtime=False`, `media="placeholder"`；
   - `nativeUnread=False`, `reliableSelfIdentity=False`（无可靠本人标识前输出 `is_self=None`）；
   - `mentions=False`, `replies=False`, `recallEvents=False`（未通过结构化关系证明前不虚假声明支持）；
   - 物理定位符严格满足 `synthetic_v1` 不变量：`qq_locator:v1:{len(acc)}:{acc}:{len(role)}:{role}:i:{msg_id}`，绝对不使用正文或时间戳哈希。

5. **无删除与不可变性**：
   - 快照目录与 Journal 记录均严格遵循 Append-Only 原则；
   - 后续快照缺少旧消息时，绝不从工作台 Journal 中删除历史记录。

## 收益与代价
- **收益**：
  - 摆脱了无法维护的第三方 bot 依赖与 GitHub Packages 私有源阻断；
  - 统一了微信、企业微信、QQ 三大平台的本地只读、零出站安全范式；
  - 用户不需要退出登录 QQ，数秒内即可安全捕获并导出完整本地会话与群聊；
  - 保证了跨快照去重幂等性与绝对的数据保真度。
- **代价**：
  - QQ 数据更新由即时 webhook 降为快照轮询模式（`realtime=false`），需定期运行一次性快照提取工具获取新数据。
