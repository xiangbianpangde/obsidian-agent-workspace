# 个人工作台 IM 消息接入技术方案 v0.2.4 (Final Freeze · 冻结批准版)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Final Freeze Approved Version (Resolved P1-IM-5, P1-IM-6, P1-IM-7 per Sol Audit)

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

### 2.2 跨摄入路径稳定去重与 Digest 契约 (`IMIngestRecord`)
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

- **数据库终极唯一约束**：
  ```sql
  UNIQUE(source, account_id, dedupe_key)
  ```
- **服务端 Digest 信任根与 CanonicalPayload 规范**：
  数据库中存储与冲突校验使用的 `server_digest` 必须由**工作台服务端严格计算**：
  ```text
  server_digest = SHA256(CanonicalPayload(record.message))
  ```
  **CanonicalPayload 跨摄入路径稳定规则**：
  计算内容必须仅覆盖平台原始客观事实：
  `CanonicalPayload = JSON.stringify({ source, account_id, channel_id, source_message_id, sender_id, sender_name, is_self, text, message_type, mentions, attachments_clean, occurred_at_epoch_ms })`。
  **绝对剔除**工作台本地派生与环境依赖字段：`id`、`ingest_seq`、`observed_at`、`provenance`、`focus_tags`、`focus_reasons`、`local_ref`。由此确保同一消息无论经由 SSE 还是 `/timeline` 追溯摄入，计算出的 `server_digest` 绝对完全一致。
- **批次去重与事务执行算法 (Batch Ingestion Algorithm)**：
  ```text
  BEGIN TRANSACTION
  FOR EACH record IN batch:
      # 1. 服务端计算真实 digest
      server_digest = SHA256(CanonicalPayload(record.message))
      IF record.provided_digest IS NOT NULL AND record.provided_digest != server_digest:
          ROLLBACK TRANSACTION
          RAISE BadRequestError("Provided digest mismatch")
          
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
- **可靠性保障**：生产者持有本地持久 Spool，未获 200 OK 持续重试；工作台事务 COMMIT 成功后才响应 200 OK；工作台服务端严格计算 payload 规范化摘要并做冲突比对。

### 4.2 可靠 SSE 总线与 Resync Fence 机制 (`GET /api/im/events`)

- **SSE 消息规范**：
  普通消息下发时必须明确携带 `id`：
  ```text
  id: 1042
  event: message
  data: {"id": "msg_xxx", "ingest_seq": 1042, ...}
  ```
  浏览器原生 `EventSource` 会在接收后将内部的 `lastEventId` 更新为 `1042`。

- **双游标唯一判定规则 (Cursor Precedence)**：
  服务端同时接收 `Last-Event-ID` 请求头与 URL 参数 `?after_seq=`：
  ```text
  effective_after_seq = max(query.after_seq ?? 0, header.Last_Event_ID ?? 0)
  ```
  该规则确保当浏览器使用带有 `?after_seq=1000` 的 URL 发起连接并在接收到 1100 断线重连时，能够自动取较大的 `1100` 进行精确恢复，杜绝回退到旧参数触发误 resync。

- **EventSource 生命周期与 Fence 收敛协议**：
  1. 若 `effective_after_seq` 跌出服务端环形缓冲区下界，服务端下发：
     ```text
     event: resync_required
     data: {"snapshot_head_seq": 2000}
     ```
  2. **客户端生命周期控制**：前端在收到 `resync_required` 事件后，**必须立即调用 `eventSource.close()`**，彻底切断连接，防止原生浏览器机制自动重新发起请求；
  3. 前端发起 Timeline 全量加载，强制携带：
     ```text
     GET /api/im/timeline?snapshot_seq=2000&limit=50
     ```
     后端在 SQL 查询中应用栅栏过滤：`WHERE ingest_seq <= 2000`；
  4. 前端渲染完 Timeline 后，再显式实例化全新的 `EventSource`：
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
2. **[AT-2: 跨摄入路径规范化 Digest 一致性]** 微信同一消息分别经由 SSE 格式与 `/timeline` 格式摄入，验证服务端算出的 `server_digest` 与 `dedupe_key` 100% 相同，成功触发幂等跳过；
3. **[AT-3: 批次混合去重与水线推进]** 提交 `[已存在记录 A, 新记录 B, 重放记录 A]` 混合批次，验证 A 被幂等跳过、B 成功落库、Watermark 正确推进；
4. **[AT-4: 伪造 Provided Digest 阻断]** 提交修改了消息正文但伪造旧 `provided_digest` 的记录，验证服务端立即拒绝 (400) 且不写入；
5. **[AT-5: 身份冲突回滚与状态标记]** 提交相同 `dedupe_key` 但不同正文内容的记录，验证触发 `IdentityConflictError`，整批事务回滚且对应 Source 标记为 `degraded`；
6. **[AT-6: 双游标优先级精确恢复]** 请求同时携带 `?after_seq=1000` 与 `Last-Event-ID: 1100`，验证服务端从 `1100` 起始回放，不产生重复回放与误 resync；
7. **[AT-7: Resync 客户端主动 Close 与二次栅栏防残缺]** 收到 `resync_required` 后验证旧 EventSource 被立即 close()，拉取带 `snapshot_seq` 的 timeline 后通过全新 EventSource 连入，落后严重时再次收到 `resync_required`；
8. **[AT-8: 权限防御与预置宽文件修复]** 预先创建 `0666` 权限的 `im_hub.db`，启动服务后验证自动修复为 `0600`，且 WAL/SHM 均无 group/world 权限。
