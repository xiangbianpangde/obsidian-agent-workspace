# 个人工作台 IM 消息接入技术方案 v0.2.2 (Final Closure · 冻结版)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Pending Final Freeze Sign-off (Revised from v0.2.1 per Sol Audit)

---

## 1. 核心架构定位与能力接口 (Scope & Capability Contracts)

### 1.1 阶段定位
- **P0 核心**：多源异构 IM 消息的只读接入、去重规整、本地派生持久化、跨平台统一时间线与确定性聚焦呈现；
- **P1 预留**：Agent 深度语义理解、任务提醒与向 Obsidian 知识库自动生成待办。P0 输出带有溯源信息（Provenance）的纯事实消息，不持有反向发信权。

### 1.2 适配器能力抽象协议 (Capability-based Contracts)

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

export interface IMSourceReader {
  readonly source: 'wechat' | 'wecom' | 'qq';
  readonly capabilities: IMCapabilities;
  getStatus(): Promise<IMSourceStatus>;
  readHistory(request: IMHistoryRequest): Promise<IMReadBatch>;
}

export interface IMIngestDriver {
  readonly source: 'wechat' | 'wecom' | 'qq';
  start(sink: IMIngestSink): Promise<void>;
  stop(): Promise<void>;
}

export interface IMIngestSink {
  commit(batch: IMIngestBatch): Promise<IMCommitReceipt>;
}
```

- **微信 (`WxCliAdapter`)**：实现 `IMSourceReader` (`canReadHistory=true`) + `IMIngestDriver` (SSE 实时流 + 重连 timeline 追溯)；
- **企业微信 (`WeComSnapshotAdapter`)**：实现 `IMSourceReader` (`canReadHistory=true`) + `IMIngestDriver` (快照版本探测)；
- **QQ (`ZhinQQAdapter`)**：**仅实现** `IMIngestDriver`（`canReadHistory=false`，绝不伪造 `readHistory()`，协调器绝不调用）。

---

## 2. 覆盖度、水印与跨源稳定去重契约 (Coverage & Deduplication Invariant)

### 2.1 摄入去重统一契约 (`IMIngestRecord`)
针对微信重连追溯重叠、企微快照重复扫描与 QQ 事件重放，必须提供跨摄入路径（Ingress Path）一致的确定性身份标识：

```typescript
export interface IMIngestRecord {
  source: 'wechat' | 'wecom' | 'qq';
  account_id: string;
  dedupe_key: string;             // 跨路径稳定去重键
  dedupe_basis: 'native_message_id' | 'source_event_id' | 'synthetic_v1';
  payload_digest: string;         // 规范化消息有效负载的 SHA-256 哈希
  message: IMMessageItem;
}
```

- **SQLite 唯一约束**：
  ```sql
  UNIQUE(source, account_id, dedupe_key)
  ```
- **核心去重与冲突判定语义**：
  1. `same dedupe_key + same payload_digest`：判定为完全幂等同一事件，返回已有 `CommitReceipt`，不插入重复消息，不报错；
  2. `same dedupe_key + different payload_digest`：判定为严重**身份冲突 (Identity Conflict)**，严禁静默忽略吞掉，立即抛出异常并中止事务，Watermark 坚决不推进，对应 Source 状态标为 `degraded`；
  3. **微信跨路径一致性**：无论来自 SSE 还是后续 `/timeline` 追溯对齐，相同源事件必须生成相同的 `dedupe_key`。

### 2.2 水印与事务原子性不变量 (P0 Core Invariant)
> **Watermark MUST NOT advance beyond data that has been successfully normalized and durably committed to `im_hub.db`.**

```text
接收数据批次 (Batch) 
   ↓
数据校验与归一化 (Normalize & Validate)
   ↓
BEGIN TRANSACTION (SQLite)
   ↓
写入消息表 (根据 UNIQUE(source, account_id, dedupe_key) 校验或幂等返回)
   ↓
更新对应源持久化 Watermark (UPDATE watermarks)
   ↓
COMMIT
```
**如果解析或校验失败，整批次事务回滚，Watermark 绝不越过失败点。**

### 2.3 诚实重建能力 (Rebuildability Semantics)
- **WeChat**：`rebuildability = 'full'`（微信本地数据库完整，可通过 `wx-cli` 完全重建）；
- **WeCom**：`rebuildability = 'snapshot_bounded'`（仅能从本地已保留的明文快照中重建）；
- **QQ/Zhin**：`rebuildability = 'none'`（若上游无本地持久 Spool，已删日志不可重放）。

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
  local_ref?: string;      // 不透明资源句柄，如 "asset://<uuid>"，严禁暴露系统绝对路径
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
  text: string;                    // 正文内容 (前端纯文本渲染，严防 XSS)
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
- **传输载荷**：
  ```typescript
  export interface ZhinIngressEvent {
    schema_version: 1;
    event_id: string;              // 生产者持久化事件 ID
    account_id: string;
    occurred_at: string;
    occurred_at_epoch_ms: number;
    payload_digest: string;        // 有效载荷 SHA-256
    payload: {
      message_type: string;
      sender_id: string;
      sender_name: string;
      group_id?: string;
      group_name?: string;
      text: string;
      mentions?: { id?: string; name?: string; is_self?: boolean; is_all?: boolean }[];
    };
  }
  ```
- **可靠性保障**：生产者持有本地持久 Spool，未获 200 OK 之前保持重试；工作台事务 COMMIT 成功后才响应 200 OK；若同一 `event_id` 携带不同 `payload_digest` 返回 409 Conflict 报错，阻断静默丢消息。

### 4.2 可靠 SSE 总线与 Resync Fence 机制 (`GET /api/im/events`)
为杜绝断网重连与拉取 Timeline 期间发生竞态丢事件：
1. **正常重放**：客户端携带 `Last-Event-ID: 1042`，服务端回放内存环形缓冲区中 `ingest_seq > 1042` 的事件；
2. **Resync Fence 栅栏收敛**：若客户端断线过久超出回放窗口：
   - 服务端下发降级事件：
     ```text
     event: resync_required
     data: {"snapshot_head_seq": 1000}
     ```
   - 客户端开始拉取 Timeline，强制带参数：
     ```text
     GET /api/im/timeline?snapshot_seq=1000&limit=50
     ```
     服务端执行强制 SQL 栅栏过滤：`WHERE ingest_seq <= 1000`；
   - 客户端完成 Timeline 加载并渲染完成后，以 `Last-Event-ID: 1000` 重新发起 SSE 连接；
   - 服务端回放 `ingest_seq > 1000` 的实时新事件，实现无缝衔接。

### 4.3 跨平台时间线 Keyset 游标分页
- **排序依据**：`ORDER BY occurred_at_epoch_ms DESC, ingest_seq DESC`；
- **游标结构**：Base64 编码的 `{ occurred_at_epoch_ms, ingest_seq, snapshot_seq? }`，避免字符串排序失真与漏项。

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

1. **[AT-1: 能力解耦]** `ZhinQQAdapter` 未实现 `readHistory`，协调器在拉取历史时正常跳过且不抛错；
2. **[AT-2: 微信 SSE 与 Timeline 去重]** 模拟微信 SSE 摄入消息 A，随后重连拉取包含 A 的 timeline，验证数据库最终只有 1 条记录，`dedupe_key` 保持一致；
3. **[AT-3: 身份冲突防静默丢弃]** 注入相同 `dedupe_key` 但不同 `payload_digest` 的记录，验证抛出 Conflict 异常且 Watermark 不推进；
4. **[AT-4: 水印原子性单事务]** 批次处理中注入校验失败异常，验证整批事务回滚，Watermark 绝不越过失败点；
5. **[AT-5: Resync Fence 对抗测试]** 在客户端执行带 `snapshot_seq` 的 timeline 分页拉取过程中持续摄入新消息，验证客户端通过 `Last-Event-ID: snapshot_head_seq` 重连后集合精确一致无遗漏；
6. **[AT-6: QQ 幂等与提交确认]** 重复推送相同 `event_id` 返回 200 且不新增数据；事务提交成功前不提前返回 200；
7. **[AT-7: 时间戳 Epoch 排序与游标分页]** 验证相同 ISO 格式但不同时区、同一毫秒但不同 `ingest_seq` 的稳定排序；
8. **[AT-8: 文件权限防御启动收紧]** 预先创建 `0666` 权限的 `im_hub.db`，启动服务后验证自动修复为 `0600`，且 WAL/SHM 均无 group/world 权限。
