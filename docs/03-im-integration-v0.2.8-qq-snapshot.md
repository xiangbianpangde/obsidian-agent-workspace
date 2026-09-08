# IM Hub 协议受控变更规范 (v0.2.8): QQ 本地只读快照接入

- **版本**: v0.2.8 (基于已冻结规范 `v0.2.7` 的受控增量附录)
- **状态**: Implemented / Ready for Audit Freeze
- **关联 ADR**: `docs/architecture/ADR/ADR-005-qq-local-snapshot-reader.md`
- **审查记录**: Sol (GPT-5.6 Sol Pro Extended) 架构评审 CONDITIONAL GO

---

## 1. 变更说明与设计动机

原冻结文档 `03-im-integration-v0.2.7.md` 中将 QQ 定义为基于 Zhin.js Webhook 的单向推送驱动器。在实际落地中，因 Zhin 依赖库私有化阻断、双入口定位符不一致风险以及出站权限控制底线，经用户授权与 Sol 架构门评审，正式启动本次受控变更：

> **核心变更**：移除 Zhin Webhook 推送机制，全面采用与企业微信同级的**本地只读快照模式 (`QQSnapshotAdapter`)**。工作台运行时保持零出站能力、零调试特权，仅通过标准只读 SQLite 方式读取由独立提取器发布的快照。

原 `03-im-integration-v0.2.7.md` 保持作为历史冻结基线，本规范作为增量约束生效。

---

## 2. 契约更新矩阵

| 维度 | v0.2.7 (原设计) | v0.2.8 (本次受控变更) |
|---|---|---|
| **QQ 适配器类** | `ZhinQQAdapter(IMIngestDriver)` | `QQSnapshotAdapter(IMSourceReader, IMIngestDriver)` |
| **QQ 接口能力** | `canReadHistory: false`, `realtime: true` | `canReadHistory: true`, `realtime: false` |
| **数据覆盖类型** | `coverage.kind: "realtime_only"` | `coverage.kind: "snapshot"` |
| **水印类型** | `watermark.kind: "event_sequence"` | `watermark.kind: "snapshot_version"` |
| **数据可重建性** | `rebuildability: "none"` | `rebuildability: "snapshot_bounded"` |
| **未验证高级属性** | `mentions: true, replies: true, recall: true` | `mentions: false, replies: false, recall: false` (保守关闭) |
| **入站端点** | `POST /internal/im/ingest/zhin` (含共享密钥) | **彻底移除** (返回 404，无任何默认密钥) |
| **安全域划分** | 工作台监听本地端口接收推送 | 独立一次性提取器写入私有 Vault，工作台仅只读访问明文导出库 |

---

## 3. 快照目录与 Manifest 规范

### 3.1 目录结构
```text
~/Library/Application Support/qq-local-vault/
  accounts/<account_alias>/
    private/                         # 预留私有目录 (0700)
    staging/                         # 未发布工作区 (0700)
    quarantine/                      # 校验失败隔离区 (0700)
    snapshots/<snapshot_id>/         # 不可变快照发布目录 (0700)
      source/                        # 原始加密 DB/WAL/SHM 冻结副本 (0600)
      export/                        # 标准 SQLite 明文导出库 (0600)
        nt_msg.db
        group_info.db
        profile_info.db
      manifest.json                  # 内容绑定元数据 (0600)
    CURRENT                          # 普通文本文件，存 snapshot_id (0600)
    publish.lock                     # 文件锁 (0600)
```

### 3.2 Manifest Schema (`qq.snapshot/v1`)
每个快照必须包含且通过 SHA-256 自校验的 `manifest.json`：
- `schema`: `"qq.snapshot/v1"`
- `snapshot_id`: `"qqsnap-v1-<sha256[:24]>"` (由 manifest 其余规范化字段计算得到)
- `account_alias`: 绑定的账号别名
- `parent_snapshot_id`: 发布的上一版本 ID (用于乐观锁父级校验)
- `codec_profile_id`: 提取时使用的 live codec 布局配置 ID
- `schema_profile_id`: 匹配的关键 schema 规格 ID
- `critical_schema_fingerprint`: 关键数据表列定义的严格哈希
- `locator_profile_id`: `"qq-locator-v1"`
- `normalization_profile_id`: `"qq-im-normalization-v1"`
- `coverage`: 包含 `from_epoch`, `through_epoch`, `source_through_at`, 以及明确声明的 `gaps`
- `stats`: 各关键消息表的行数与唯一物理定位符数
- `files`: 导出的各数据库相对路径、大小及 SHA-256 校验和

---

## 4. 身份定位符不变量 (`synthetic_v1`)

QQ 消息的物理唯一键必须使用源端物理主键，严禁依赖内容或时间哈希：
```text
qq_locator:v1:{len(account_id)}:{account_id}:{len(table_role)}:{table_role}:i:{msg_id}
```
- `table_role`: `"group"` 或 `"c2c"`
- `msg_id`: 对应表中具有物理唯一性的 `40001` 整数主键
- 工作台内部暴露的 `IMMessageItem.id`:
  `qq_msg_` + `SHA256("qq\x00" + account_id + "\x00" + locator)[:32]`

---

## 5. 验收测试演进 (AT-1R 与 AT-10 ~ AT-18)

- **AT-1R**: 验证 QQSnapshotAdapter 同时满足 Reader 与 Driver 契约，能力基线严格保守，未验证能力全部为 False。
- **AT-10**: 快照捕获与 WAL 完整性，验证未 checkpoint 的 WAL 记录在解密导出后完整可见。
- **AT-11**: 暂停、恢复与故障隔离，验证提取失败时进程自动恢复（Resume Guard 兜底），CURRENT 不被篡改，失败产物进入 quarantine。
- **AT-12**: 严格文件权限 (0700/0600)、不可变性与拒绝软链接 / 硬链接穿越。
- **AT-13**: 零密钥/敏感路径泄漏与全量 IM 接口 `Cache-Control: no-store` 强制中间件保障。
- **AT-14**: 不兼容 codec/schema 漂移时 Fail-Closed，不损坏旧快照，不推进水位。
- **AT-15**: 跨快照去重幂等性与 Append-Only 知识资产无删除不变量。
- **AT-16**: 保守规范化映射，未知发送者使用确定性 fallback，`is_self` 保守为 `None`。
- **AT-17**: 工作台无出站能力，无 Zhin Webhook，无调试依赖。
- **AT-18**: 历史数据迁移门检验，确保无非法旧消息覆盖。
