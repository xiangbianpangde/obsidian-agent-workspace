# 个人工作台 IM 消息接入技术方案 v0.2.3 (Final Freeze Closure · 正式冻结版)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Final Freeze Sign-off Version (Resolved P1-IM-5, P1-IM-6, P1-IM-7 per Sol Audit)

---

## 1. 核心架构定位与能力接口 (Scope & Capability Contracts)

### 1.1 阶段定位
- **P0 核心**：多源异构 IM 消息的只读接入、服务端去重规整、本地派生持久化、跨平台统一时间线与确定性聚焦呈现；
- **P1 预留**：Agent 深度语义理解、任务提醒与向 Obsidian 知识库自动生成待办。P0 仅输出带有溯源信息（Provenance）的纯事实消息，不持有反向发信权。

### 1.2 适配器共同基接口与能力契约 (Capability-based Contracts)

为了避免类型断层，所有适配器均继承自共同基接口 `IMSourceAdapter`，QQ 不再缺失能力与状态声明：

```typescript
export interface IMCapabilities {
  canReadHistory: boolean;        // 是否支持拉取历史接口 readHistory()
  realtime: boolean;              // 是否支持低延迟实时流
  media: 'none' | 'placeholder' | 'local'; // 媒体支持级别 (P0 严禁 remote 自动加载)
  nativeUnread: boolean;          // 上游是否具备原生未读标记
  reliableSelfIdentity: boolean;  // 上游是否具备可靠的“本人”判断 (企微等填 false)
  mentions: boolean;              // 是否支持 @ 结构化解析
  replies: boolean;               // 是否支持消息引用与回复
  recallEvents: boolean;          // 是否支持消息撤回事件感知
}

export interface IMSourceAdapter {
  readonly source: 'wechat' | 'wecom' | 'qq';
  readonly capabilities: IMCapabilities;
  getStatus(): Promise<IMSourceStatus>;
}

export interface IMSourceReader extends IMSourceAdapter {
  readHistory(request: IMHistoryRequest): Promise<IMReadBatch>;
}

export interface IMIngestDriver extends IMSourceAdapter {
  start(sink: IMIngestSink): Promise<void>;
  stop(): Promise<void>;
}

export interface IMIngestSink {
  commit(batch: IMIngestBatch): Promise<IMCommitReceipt>;
}
```

- **微信 (`WxCliAdapter`)**：实现 `IMSourceReader` (`canReadHistory=true`) + `IMIngestDriver` (SSE 实时流 + 重连 timeline 追溯)；
- **企业微信 (`WeComSnapshotAdapter`)**：实现 `IMSourceReader` (`canReadHistory=true`) + `IMIngestDriver` (快照版本探测)；
- **QQ (`ZhinQQAdapter`)**：实现 `IMIngestDriver`（`canReadHistory=false`，`coverage.kind='realtime_only'`，`rebuildability='none'`，绝不伪造 `readHistory()`，协调器也绝不调用）。

---

## 2. 状态、覆盖度、水印与跨源稳定去重契约

### 2.1 状态与诚实覆盖度 DTO (`IMSourceStatus`)
```typescript
export interface IMCoverageGap {
  from: string;
  through: string;
  reason: string;
}

export interface IMSourceStatus {
  source: 'wechat' | 'wecom' | 'qq';
  connectivity: 'live' | 'catching_up' | 'degraded' | 'offline' | 'error';
  coverage: {
    kind: 'full' | 'bounded' | 'snapshot' | 'realtime_only' | 'unknown';
    from?: string;
    through?: string;
    gaps: IMCoverageGap[];
  };
  freshness: {
    last_observed_at?: string;
    source_through_at?: string;
    lag_ms?: number;
    stale: boolean;
  };
  watermark: {
    kind: 'source_cursor' | 'event_sequence' | 'snapshot_version' | 'timestamp' | 'none';
    value?: string;
    committed_at?: string;
  };
  rebuildability: 'full' | 'snapshot_bounded' | 'none';
}
```

### 2.2 跨摄入路径稳定去重契约 (`IMIngestRecord`)
```typescript
export interface IMIngestRecord {
  source: 'wechat' | 'wecom' | 'qq';
  account_id: string;
  dedupe_key: string;             // 跨摄入路径稳定键
  dedupe_basis: 'native_message_id' | 'source_event_id' | 'synthetic_v1';
  payload_digest: string;         // 服务端强制计算规范化哈希
  message: IMMessageItem;
}
```

- **服务端 Digest 信任根**：
  数据库中存储与冲突校验使用的 `payload_digest` 必须由**工作台后端计算**：
  `server_digest = SHA256(CanonicalPayload(record.message))`。
  若外部生产者自行附带 `provided_digest`，后端强制比对；若两者不一致，直接拒绝（400 Bad Request），杜绝生产者错误复用 Digest 造成的静默覆盖。
- **`synthetic_v1` 不变量规则**：
  `synthetic_v1` 必须包含跨摄入路径一致的**源端物理记录定位符**（例如微信本地数据库 `msg_svr_id` 或稳定的序列标识）。**严禁**使用纯内容指纹（如 `hash(sender + timestamp + text)`）作为去重键，防止不同时刻发送的两条相同文本消息被误判合并。
- **批次去重与事务执行算法 (Batch Ingestion Semantics)**：
  ```text
  FOR EACH record IN batch:
      查库: SELECT payload_digest FROM messages WHERE source=:s AND account_id=:a AND dedupe_key=:k
      IF 存在匹配记录:
          IF record.payload_digest == existing_digest:
              # 同一消息幂等重放，跳过本条，继续处理批次中的其余记录
              CONTINUE
          ELSE:
              # 严重身份冲突：同一键对应不同内容，严禁静默忽略！
              ROLLBACK TRANSACTION
              source 状态标为 degraded
              RAISE IdentityConflictError
      ELSE:
          INSERT INTO messages (...)
  
  # 批次内所有新记录插入完成后，原子推进水线
  UPDATE watermarks SET ...
  COMMIT TRANSACTION
  RETURN receipt
  ```
  在 `[已存在记录 A, 新记录 B]` 的混合批次中，A 安全跳过，B 正常插入，Watermark 正确推进。

### 2.3 水印原子性不变量 (P0 Core Invariant)
> **Watermark MUST NOT advance beyond data that has been successfully normalized and durably committed to `im_hub.db`.**

---

## 3. 规整化数据契约 (Normalized DTOs)

### 3.1 统一消息契约 (`IMMessageItem`)
```typescript
export interface IMAttachment {
  type: 'image' | 'voice' | 'video' | 'file';
  name?: string;
  mime?: string;
  size?: number;
  availability: 'local' | 'placeholder' | 'unavailable'; // 绝不包含 remote 自动加载
  local_ref?: string;      // 不透明句柄，如 "asset://<uuid>"，严禁暴露系统绝对路径
}

export interface IMMessageItem {
  id: string;                      // 工作台全局稳定 opaque ID
  ingest_seq: number;              // 本地自增全局序号 (用于可靠游标与 SSE 重放)
  source: 'wechat' | 'wecom' | 'qq';
  account_id: string;              // 多账号支持
  channel_id: string;              // 频道/会话全局 ID
  source_message_id?: string;      // 上游原生消息 ID
  source_id_quality: 'native' | 'synthetic';
  sender: {
    id?: string;
    name: string;
    role?: string;
  };
  is_self: boolean | null;         // 企微等不可靠时为 null，不捏造事实
  text: string;                    // 正文内容 (前端通过 {{ message.text }} 纯文本渲染)
  message_type: 'text' | 'image' | 'voice' | 'video' | 'file' | 'link' | 'notice' | 'mixed' | 'unknown';
  mentions: {
    id?: string;
    name?: string;
    is_self?: boolean;
    is_all?: boolean;
  }[];
  reply_to?: string;
  attachments: IMAttachment[];
  occurred_at: string;             // 平台原始时间戳 ISO
  occurred_at_epoch_ms: number;    // 标准 Unix 毫秒时间戳 (作为时间线主排序列)
  observed_at: string;             // 工作台观察时间戳 ISO
  provenance: {
    mode: 'sse' | 'webhook' | 'snapshot' | 'poll';
    snapshot_id?: string;
    cursor?: string;
  };
  focus_tags: ('school' | 'mention_all' | 'mention_self' | 'direct_important')[];
  focus_reasons: string[];         // 确定性高亮解释列表 (支持多规则叠加)
}
```

### 3.2 统一会话摘要契约 (`IMChannelSummary`)
```typescript
export interface IMChannelSummary {
  id: string;                      // 唯一 ID: "wechat:xxx", "wecom:yyy", "qq:zzz"
  platform: 'wechat' | 'wecom' | 'qq';
  account_id: string;
  channel_type: 'direct' | 'group' | 'notice';
  name: string;
  avatar: {
    availability: 'local' | 'placeholder' | 'unavailable';
    local_ref?: string;            // 不透明句柄 asset://...，禁止直载远程 URL
  };
  last_message: string;
  last_time: string;
  native_unread_count?: number | null; // 上游原生未读
  local_unseen_count: number;          // 工作台本地未读数
  is_focus: boolean;                   // 用户配置或规则判定的重要关注频道
}
```

---

## 4. 可靠性与通信契约 (Reliability Contracts)

### 4.1 QQ 单向推送与持久 Spool
- **端点**：`POST /internal/im/ingest/zhin` (仅限 `127.0.0.1`，强校验 `X-IM-Secret`)；
- **可靠性保障**：生产者持有本地持久 Spool，未获 200 OK 持续重试；工作台事务 COMMIT 成功后才响应 200 OK；工作台服务端计算 payload 规范化摘要并做冲突比对。

### 4.2 可靠 SSE 总线与 Resync Fence 机制 (`GET /api/im/events`)
由于浏览器原生 `EventSource` 不支持自定义 Header 设置 `Last-Event-ID`，服务端同时支持显式查询参数恢复：
- **恢复端点**：`GET /api/im/events?after_seq=1000`（同时兼容 `Last-Event-ID` 请求头）；
- **重连与 Fence 执行协议**：
  1. 正常连接断开时，原生 `EventSource` 自动携带内部 `Last-Event-ID` 重连；
  2. 若断线时间过长跌出服务端环形缓冲区，服务端下发：
     ```text
     event: resync_required
     data: {"snapshot_head_seq": 1000}
     ```
     随后立即关闭连接；
  3. 前端接收到 `resync_required` 后，向后端请求 Timeline 全量重载，强制携带参数：
     ```text
     GET /api/im/timeline?snapshot_seq=1000&limit=50
     ```
     后端在 SQL 查询中强制应用栅栏：`WHERE ingest_seq <= 1000`；
  4. 前端渲染完 Timeline 后，创建新的 EventSource 显式绑定：
     ```javascript
     new EventSource('/api/im/events?after_seq=' + snapshot_head_seq)
     ```
  5. **服务端二次栅栏校验**：若此时 `after_seq` 再次跌出环形缓冲区下界，服务端**必须再次下发 `resync_required` 并断开**，绝不允许从最早历史进行残缺回放（**Exact replay OR resync, never partial replay**）。

### 4.3 跨平台时间线 Keyset 游标分页
- **排序依据**：`ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC`；
- **游标结构**：Base64 编码的 `{ occurred_at_epoch_ms, ingest_seq, snapshot_seq? }`。

---

## 5. 安全启动校验与权限硬底线 (Security Invariants)

1. **源端只读与权限隔离**：严禁向微信、企微、QQ 反向发信，无发信 API；
2. **SQLite 存储与附属文件安全（主动校验收紧）**：
   - 存储路径：`~/.personal-ai-workspace/im/im_hub.db`；
   - **启动主动收紧**：工作台启动时主动校验并执行 `chmod 0700` 私有目录，对已存在的 `im_hub.db`、`im_hub.db-wal`、`im_hub.db-shm` 校验并执行 `chmod 0600`，若权限宽松且无法收紧则 Fail-Closed 拒绝启动；
3. **本地 HTTP 防御**：严格绑定 `127.0.0.1`，校验 `Origin`；
4. **前端安全渲染**：聊天正文采用 Vue `{{ message.text }}` 纯文本数据绑定，严禁使用 `v-html`；
5. **不泄露真实文件路径**：资源统一输出为 `asset://<uuid>` 抽象句柄；
6. **日志零敏感泄漏**：消息正文与关键词搜索严禁打入应用日志。

---

## 6. 验收测试清单 (Acceptance Tests)

1. **[AT-1: 能力基接口解耦]** `ZhinQQAdapter` 实现 `IMSourceAdapter` 与 `IMIngestDriver`，`canReadHistory=false`，协调器在拉取历史时安全跳过；
2. **[AT-2: 微信 SSE 与 Timeline 重放去重]** 模拟微信 SSE 摄入消息 A，随后重连拉取包含 A 的 timeline，验证数据库最终仅有 1 条记录，`dedupe_key` 保持一致；
3. **[AT-3: 批次去重混合提交]** 提交 `[已存在记录 A, 新记录 B]`，验证 A 跳过、B 插入、Watermark 正常推进；
4. **[AT-4: 身份冲突防静默丢弃]** 注入相同 `dedupe_key` 但不同 `payload_digest` 的记录，验证抛出 Conflict 异常、事务回滚且 Watermark 不推进；
5. **[AT-5: Resync Fence 对抗测试]** 客户端在带着 `snapshot_seq` 拉取 Timeline 过程中持续向后端注入新消息，通过 `after_seq` 重连后集合精确一致无遗漏；
6. **[AT-6: QQ 幂等与提交确认]** 重复推送相同 `event_id` 返回 200 且不新增数据；事务提交成功前不提前返回 200；
7. **[AT-7: 时间戳 Epoch 排序与游标分页]** 验证相同 ISO 格式但不同时区、同一毫秒但不同 `ingest_seq` 的稳定排序；
8. **[AT-8: 文件权限防御启动收紧]** 预先创建 `0666` 权限的 `im_hub.db`，启动服务后验证自动修复为 `0600`，且 WAL/SHM 均无 group/world 权限。
