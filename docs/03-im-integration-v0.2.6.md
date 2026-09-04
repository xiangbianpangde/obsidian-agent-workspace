# 个人工作台 IM 消息接入技术方案 v0.2.6 (Final Freeze Closure · 终审签字版)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Final Freeze Sign-off Version (Resolved Canonical Consistency & Envelope Boundaries per Sol Audit)

---

## 1. 核心架构定位与能力接口 (Scope & Capability Contracts)

### 1.1 阶段定位
- **P0 核心**：多源异构 IM 消息的只读接入、服务端统一去重规整、本地派生持久化、跨平台统一时间线与确定性聚焦呈现；
- **P1 预留**：Agent 深度语义理解、任务提醒与向 Obsidian 知识库自动生成待办。P0 仅输出带有溯源信息（Provenance）的纯事实消息，不持有反向发信权。

### 1.2 适配器共同基接口与能力契约 (Capability-based Contracts)

所有适配器均继承自共同基接口 `IMSourceAdapter`，QQ 显式声明能力且不伪造历史接口：

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
- **QQ (`ZhinQQAdapter`)**：实现 `IMIngestDriver`（`canReadHistory=false`，`coverage.kind='realtime_only'`，`rebuildability='none'`，不伪造 `readHistory()`）。

---

## 2. 状态、覆盖度、水印与跨源稳定去重契约

### 2.1 状态与覆盖度正式 DTO 规范
```typescript
export interface IMCoverageGap {
  from: string;
  through: string;
  reason: string;
}

export interface IMCoverage {
  kind: 'full' | 'bounded' | 'snapshot' | 'realtime_only' | 'unknown';
  from?: string;
  through?: string;
  gaps: IMCoverageGap[];
}

export interface IMFreshness {
  last_observed_at?: string;
  source_through_at?: string;
  lag_ms?: number;
  stale: boolean;
}

export interface IMWatermark {
  kind: 'source_cursor' | 'event_sequence' | 'snapshot_version' | 'timestamp' | 'none';
  value?: string;
  committed_at?: string;
}

export interface IMSourceStatus {
  source: 'wechat' | 'wecom' | 'qq';
  connectivity: 'live' | 'catching_up' | 'degraded' | 'offline' | 'error';
  coverage: IMCoverage;
  freshness: IMFreshness;
  watermark: IMWatermark;
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
  provided_digest?: string;       // 生产者自声明 Digest (可选校验)
  message: IMMessageItem;
}
```

- **数据库终极物理唯一约束**：
  ```sql
  UNIQUE(source, account_id, dedupe_key)
  ```

- **信封与载荷身份域强一致边界 (Envelope-Message Consistency Boundary)**：
  在进入任何哈希计算、去重比较与落库之前，服务端强制执行物理一致性校验：
  ```text
  IF record.source != record.message.source OR record.account_id != record.message.account_id:
      ROLLBACK TRANSACTION
      RAISE BadRequestError(400, "InvalidIngestEnvelope: source/account_id mismatch between envelope and message payload")
  ```

- **`synthetic_v1` 不变量硬契约 (Synthetic Key Physical Invariant)**：
  > **`synthetic_v1` MUST contain a stable source-side physical record locator (or a deterministic encoding of such locator). A `synthetic_v1` dedupe_key MUST NOT be derived solely from message content, sender, timestamp, CanonicalPayload digest, or any combination of content-only fields. The same physical source record observed through realtime and replay paths MUST produce exactly the same `synthetic_v1` dedupe_key.**  
  反例规则：两条正文文本、发送者、时间戳完全一致，但物理源定位符不同的真实消息，生成的 `dedupe_key` 必须严格不同，绝不允许被合并。

- **规范化载荷事实模型 (`CanonicalIMPayloadV1`)**：
  为彻底消除字段歧义，定义唯一规范的客观事实模型：
  ```typescript
  export interface CanonicalMentionFact {
    id: string | null;
    name: string | null;
    is_self: boolean | null;       // 未显式提供时一律为 null，严禁把未知降级伪造成 false
    is_all: boolean | null;        // 未显式提供时一律为 null
  }

  export interface CanonicalAttachmentFact {
    type: 'image' | 'voice' | 'video' | 'file';
    name: string | null;
    mime: string | null;
    size: number | null;
  }

  export interface CanonicalIMPayloadV1 {
    source: 'wechat' | 'wecom' | 'qq';
    account_id: string;
    channel_id: string;
    source_message_id: string | null;
    sender_id: string | null;
    sender_name: string;
    is_self: boolean | null;
    reply_to: string | null;       // 客观回复关系纳入客观事实模型
    text: string;
    message_type: 'text' | 'image' | 'voice' | 'video' | 'file' | 'link' | 'notice' | 'mixed' | 'unknown';
    mentions: CanonicalMentionFact[];
    attachments: CanonicalAttachmentFact[];
    occurred_at_epoch_ms: number;
  }
  ```
  **严格单义化规则**：
  1. 所有未显式提供的可选字段一律归一化为 `null`；
  2. 严格剔除 `local_ref`、`availability` 等所有工作台本地可变状态，仅保留客观附件元数据（`name, mime, size`）；
  3. 严格剔除 `id`、`ingest_seq`、`observed_at`、`provenance`、`focus_tags`、`focus_reasons`；
  4. 字段顺序严格按上述接口顺序序列化为 UTF-8 JSON 字节流；
  5. 服务端哈希：`server_digest = SHA256(CanonicalBytesV1(message))`。

- **批次去重与事务执行算法 (Batch Ingestion Algorithm)**：
  ```text
  BEGIN TRANSACTION
  FOR EACH record IN batch:
      # 0. 强校验信封与消息身份域一致性
      IF record.source != record.message.source OR record.account_id != record.message.account_id:
          ROLLBACK TRANSACTION
          RAISE BadRequestError(400, "InvalidIngestEnvelope")
          
      # 1. 服务端计算规范化 digest 并校验生产者自声明
      server_digest = SHA256(CanonicalBytesV1(record.message))
      IF record.provided_digest IS NOT NULL AND record.provided_digest != server_digest:
          ROLLBACK TRANSACTION
          RAISE BadRequestError(400, "Provided digest mismatch with server canonical digest")
          
      # 2. 查询已有记录
      SELECT payload_digest FROM messages 
      WHERE source = record.source AND account_id = record.account_id AND dedupe_key = record.dedupe_key
      
      IF 存在已有记录:
          IF existing_digest == server_digest:
              # 幂等跳过本条，继续处理批次中的其它记录
              CONTINUE
          ELSE:
              # 严重身份冲突：同一 dedupe_key 对应不同客观事实，坚决不静默吞掉！
              ROLLBACK TRANSACTION
              SET source.status = degraded
              RAISE IdentityConflictError
      ELSE:
          INSERT INTO messages (..., payload_digest = server_digest)
          
  # 批次全量新记录落库成功，原子更新水线并提交
  UPDATE watermarks SET ...
  COMMIT TRANSACTION
  RETURN batch_receipt
  ```

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
    is_self?: boolean | null;
    is_all?: boolean | null;
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
- **可靠性保障**：生产者持有本地持久 Spool，未获 200 OK 持续重试；工作台事务 COMMIT 成功后才响应 200 OK；工作台服务端严格计算 payload 规范化摘要并做冲突比对。

### 4.2 可靠 SSE 总线与 Resync Fence 机制 (`GET /api/im/events`)

- **SSE 普通消息规范**：必须输出 `id: <ingest_seq>`。浏览器在每次收到消息后自动将内部 `lastEventId` 更新为当前 `ingest_seq`。
- **开区间回放查询与双游标唯一判定**：
  ```text
  effective_after_seq = max(query.after_seq ?? 0, header.Last_Event_ID ?? 0)
  ```
  - **Future Cursor 防御**：若 `effective_after_seq > current_head_seq`，立即返回 `400 InvalidCursor`；
  - **开区间回放**：从服务端回放事件时，SQL 查询为：
    ```sql
    WHERE ingest_seq > :effective_after_seq ORDER BY ingest_seq ASC
    ```
    确保不重复回放 `effective_after_seq` 对应已确认事件。

- **Resync Snapshot 穷尽分页完成门禁契约 (Exhaustive Completion Invariant)**：
  > **Client MUST exhaustively page the timeline snapshot bounded by `ingest_seq <= snapshot_head_seq` before opening a new EventSource with `after_seq = snapshot_head_seq`. The server MUST expose deterministic pagination with `next_cursor` based on `ingest_seq`. The client MUST NOT reopen SSE until `next_cursor == null` reports that no additional records `<= snapshot_head_seq` remain.**

  **详细执行流程**：
  1. 若 `effective_after_seq` 跌出服务端环形缓冲区下界，服务端下发：
     ```text
     event: resync_required
     data: {"snapshot_head_seq": 2000}
     ```
  2. **客户端生命周期控制**：前端在收到 `resync_required` 事件后，**必须立即调用 `eventSource.close()`**，彻底切断连接，防止原生浏览器机制自动重新发起请求；
  3. 前端发起 Timeline 分页加载，携带参数：
     ```text
     GET /api/im/timeline?snapshot_seq=2000&limit=50&cursor=...
     ```
     响应结构：
     ```json
     {
       "items": [...],
       "next_cursor": "... | null",
       "snapshot_head_seq": 2000
     }
     ```
     服务端强制执行栅栏过滤：
     ```sql
     WHERE ingest_seq > :cursor AND ingest_seq <= :snapshot_head_seq ORDER BY ingest_seq ASC LIMIT :limit
     ```
     前端循环遍历拉取，直到 `next_cursor === null`，确认快照内全量历史已彻底加载；
  4. 确认穷尽加载完成后，前端显式实例化全新的 `EventSource`：
     ```javascript
     new EventSource('/api/im/events?after_seq=' + snapshot_head_seq)
     ```
  5. **服务端二次栅栏校验**：若此时 `after_seq` 再次跌出环形缓冲区下界，服务端**必须再次下发 `resync_required` 并关闭连接**，绝不允许从最早历史进行残缺回放（**Exact replay OR resync, never partial replay**）。

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
2. **[AT-2: 跨摄入路径规范化 Digest 一致性与 reply_to 敏感性]** 
   - 微信同一消息分别经由 SSE 格式与 `/timeline` 格式摄入，验证转换后的 `CanonicalIMPayloadV1` UTF-8 字节逐字节相等，算出的 `server_digest` 与 `dedupe_key` 100% 相同，成功触发幂等跳过；
   - 注入相同 `dedupe_key` 但不同 `reply_to` 的记录，验证计算出不同 `server_digest` 并触发 `IdentityConflictError`；
3. **[AT-3: 批次混合去重与水线推进]** 提交 `[已存在记录 A, 新记录 B, 重放记录 A]` 混合批次，验证 A 被幂等跳过、B 成功落库、Watermark 正确推进；
4. **[AT-4: 伪造 Provided Digest 阻断]** 提交修改了消息正文但伪造旧 `provided_digest` 的记录，验证服务端立即拒绝 (400) 且不写入；
5. **[AT-5: 身份域不一致阻断]** 提交 `record.source = "wechat"` 但 `message.source = "qq"` 的记录，验证服务端立即拒绝 (400 InvalidIngestEnvelope)；
6. **[AT-6: 物理定位符碰撞负面测试 (Negative Locator Collision)]** 注入相同正文、发送者、时间戳但物理定位符不同的两条消息，验证生成的 `synthetic_v1` `dedupe_key` 严格不同，两批数据均独立成功落库，不被错误合并；
7. **[AT-7: 开区间回放与双游标优先级]** 请求携带 `?after_seq=1000` 与 `Last-Event-ID: 1100`，验证服务端从开区间 `> 1100` 起始回放，不产生重复回放与误 resync；future cursor 传入报错 400；
8. **[AT-8: Resync 穷尽分页与生命周期对抗测试]** 待补消息总数 137 条、单页 `limit=50`，验证客户端收到 `resync_required` 后立即 `close()` 旧连接，连续分页 3 次直到 `next_cursor == null`，之后重新建立 SSE 连接，达成 0 消息遗漏；
9. **[AT-9: 权限防御与预置宽文件修复]** 预先创建 `0666` 权限的 `im_hub.db`，启动服务后验证自动修复为 `0600`，且 WAL/SHM 均无 group/world 权限。
