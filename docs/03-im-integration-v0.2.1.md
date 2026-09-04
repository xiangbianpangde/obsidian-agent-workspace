# 个人工作台 IM 消息接入技术方案 v0.2.1 (Final Closure · 待最终冻结)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Pending Final Freeze (Revised from v0.2 per Sol Audit)

---

## 1. 核心架构定位与能力边界 (Scope & Capabilities)

### 1.1 阶段定位
- **P0 核心**：多源异构 IM 消息的只读接入、归一化、本地派生持久化、跨平台统一时间线与确定性聚焦呈现；
- **P1 预留**：Agent 深度语义理解、任务提醒与向 Obsidian 知识库自动生成待办。P0 仅输出带有溯源信息（Provenance）的纯事实消息，不持有反向发信与复杂自动化操作权。

### 1.2 适配器能力抽象协议 (Capability-based Contracts)

系统坚决不采用 `if (platform == "wechat")` 的硬编码分流，而是基于纯正的能力接口定义：

```typescript
export interface IMCapabilities {
  history: boolean;               // 是否支持拉取全部历史
  realtime: boolean;              // 是否支持低延迟实时流
  media: 'none' | 'placeholder' | 'local'; // 媒体支持级别 (P0 不支持 remote 自动加载)
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
  readHistory?(request: IMHistoryRequest): Promise<IMReadBatch>;
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

- **微信 (`WxCliAdapter`)**：实现 `IMSourceReader` (提供 history) + `IMIngestDriver` (SSE 实时流 + 重连 timeline 追溯)；
- **企业微信 (`WeComSnapshotAdapter`)**：实现 `IMSourceReader` (提供快照 history) + `IMIngestDriver` (快照版本变更探测)；
- **QQ (`ZhinQQAdapter`)**：**仅实现** `IMIngestDriver`（接收单向推入，不伪造 `readHistory()`，协调器也绝不调用）。

---

## 2. 诚实覆盖度、保鲜度与水印契约 (Coverage, Freshness & Watermark)

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

### 2.1 核心不变量 (P0 Core Invariant)
> **Watermark MUST NOT advance beyond data that has been successfully normalized and durably committed to `im_hub.db`.**

在数据摄入时，必须满足原子性事务：
```text
接收数据批次 (Batch) 
   ↓
数据校验与归一化 (Normalize & Validate)
   ↓
BEGIN TRANSACTION (SQLite)
   ↓
写入 / 去重 消息表 (INSERT OR IGNORE messages)
   ↓
更新各源对应持久化 Watermark (UPDATE watermarks)
   ↓
COMMIT
```
**如果某一批次中部分解析失败，严禁推进该源 Watermark，并将对应 Source 状态标为 `degraded` 并记录 `gap`。**

### 2.2 诚实重建能力 (Rebuildability Semantics)
打破全局无条件 `rebuildable=true` 的误区，诚实暴露分源重建语义：
- **WeChat**：`rebuildability = 'full'`（微信本地数据库完整，可通过 `wx-cli` 完全重建）；
- **WeCom**：`rebuildability = 'snapshot_bounded'`（仅能从本地已保留的明文快照中重建）；
- **QQ/Zhin**：`rebuildability = 'none'`（P0 push-only 模式，若无上游 spool，已删日志不可重放）。

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
  ingest_seq: number;              // 本地递增有序序号 (用于可靠游标与 SSE 重放)
  platform: 'wechat' | 'wecom' | 'qq';
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
  observed_at: string;             // 工作台观察时间戳 ISO
  provenance: {
    mode: 'sse' | 'webhook' | 'snapshot' | 'poll';
    snapshot_id?: string;
    cursor?: string;
  };
  focus_tags: ('school' | 'mention_all' | 'mention_self' | 'direct_important')[];
  focus_reasons: string[];         // 确定性高亮解释
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
  local_unseen_count: number;          // 工作台未阅数
  is_focus: boolean;                   // 用户配置或规则判定的重要关注频道
}
```

---

## 4. 可靠性与通信契约 (Reliability Contracts)

### 4.1 QQ 单向推送摄入：Durable Commit → 200 OK
- **端点**：`POST /internal/im/ingest/zhin` (仅限 `127.0.0.1`，强校验 `X-IM-Secret`)；
- **负载模型**：
```typescript
export interface ZhinIngressEvent {
  schema_version: 1;
  event_id: string;       // 生产者稳定去重 ID
  account_id: string;
  occurred_at: string;
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
- **数据库唯一约束**：`UNIQUE(platform, account_id, event_id)`；
- **响应语义**：只有在写入 SQLite 且事务 COMMIT 成功后，才向 Zhin 返回 HTTP 200。异常超时时 Zhin 重发，工作台自动幂等命中，不丢消息。

### 4.2 可靠 SSE 总线与重连收敛 (`GET /api/im/events`)
- **事件结构**：
```text
id: 1042
event: message.committed
data: {"id": "msg_xxx", "channel_id": "...", "text": "...", ...}
```
- **重连协议**：前端携带 `Last-Event-ID: 1042`，服务端回放 `ingest_seq > 1042` 的近期事件；
- **失效降级**：若断线时间过长超出内存回放缓存，下发 `event: resync_required`，前端收到后主动拉取 `GET /api/im/timeline` 与 `/api/im/status` 重新对齐。

### 4.3 跨平台时间线稳定 Keyset 游标分页
- **接口**：`GET /api/im/timeline?cursor=<opaque_base64>&limit=50&platform=&focus_only=`
- **排序依据**：`ORDER BY occurred_at DESC, ingest_seq DESC`；
- **游标解码**：内部包含 `(last_occurred_at, last_ingest_seq)`，彻底解决时间戳并列与跨平台 ID 无序问题。

---

## 5. 确定性聚焦规则引擎 (Deterministic Focus Rules)

多规则累加支持：
1. **学校频道重点**：若频道为 `is_focus` 或名称含“通知/班/教务/学院”：
   - 若含 `@全体成员`，追加 tag `mention_all`，原因：`学校/班级群「{name}」发布了 @全体成员`；
   - 若为通知类型或含“通知/提醒”，追加 tag `school`，原因：`学校/班级群「{name}」发布了重要通知`；
2. **@我 提醒**：若含 `@本人`，追加 tag `mention_self`，原因：`在「{name}」中被提及`；
3. **重要私聊**：若为私聊且处于关注列表，追加 tag `direct_important`，原因：`重要联系人「{name}」发送了私聊`。

---

## 6. 安全与系统权限硬底线 (Security Invariants)

1. **源端只读与权限隔离**：绝对不向微信、企微、QQ 反向发信；工作台无发信凭证；
2. **SQLite 存储与附属文件安全**：
   - 存储路径：`~/.personal-ai-workspace/im/im_hub.db`；
   - 进程执行 `os.umask(0o077)`，确保 `im_hub.db`、`im_hub.db-wal`、`im_hub.db-shm` 均为 `0600` 权限，所属目录 `0700`，绝不进入 Vault 或 Git；
3. **本地 HTTP 防御**：严格绑定 `127.0.0.1`，校验 `Origin`，变更接口要求 CSRF/自定义头防穿透；
4. **前端安全渲染**：聊天正文采用 Vue `{{ message.text }}` 纯文本数据绑定，严禁使用 `v-html`；
5. **不泄露真实文件路径**：资源引用统一输出为 `asset://<uuid>` 抽象句柄；
6. **日志零敏感泄漏**：消息正文与关键词搜索严禁打入应用日志。

---

## 7. 验收测试清单 (Acceptance Tests)

1. **[AT-1: 能力解耦]** `ZhinQQAdapter` 未实现 `readHistory`，协调器在调用历史接口时正常跳过且不报错；
2. **[AT-2: 水印单事务]** 注入解析异常，验证 Watermark 不越过失败条目，且数据库不产生不一致状态；
3. **[AT-3: 诚实 Coverage]** 状态端点返回各端真实连通性与重建能力（微信 full，企微 snapshot_bounded，QQ none）；
4. **[AT-4: QQ 幂等与提交确认]** 重复推送同一 `event_id` 不产生重复记录，事务成功前不提前返回 200；
5. **[AT-5: SSE 断线重放与降级]** 测试 `Last-Event-ID` 回放与溢出下发 `resync_required`；
6. **[AT-6: 跨平台 Keyset 分页]** 验证相同时间戳下的稳定游标分页无遗漏；
7. **[AT-7: Focus 多规则累加]** 验证一条同时包含学校群 + @全体成员 + @我的消息能够同时生成多条 tag 与原因；
8. **[AT-8: 权限负面测试]** 验证 SQLite 及 WAL 文件均不存在 group/world 可读权限。
