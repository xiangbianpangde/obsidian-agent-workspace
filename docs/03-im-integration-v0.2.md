# 个人工作台 IM 消息接入技术方案 v0.2 (第三个 P0 · 统一消息中心 IM Hub)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-04 · 状态：Sol 评审通过 (Revised from v0.1)

---

## 1. 目标与范围界定 (P0 Scope)

### 1.1 核心目标
建立个人工作台的第三大核心支柱——**【统一消息中心 (IM Hub)】**，将微信（`wx-cli`）、企业微信（`yichen-skills / yichen-wecom-local-vault`）、QQ（`zhin`）三大异构消息源规范汇聚于本地，提供统一的跨平台全景时间线、分类会话窗与学校重要信息聚合看板。

### 1.2 阶段边界 (P0 vs P1)
- **P0 严格限定**：
  1. 异构源只读数据接入、事件规整与本地持久化派生流；
  2. 统一跨平台时间线流 (Timeline Feed) 与会话历史查看；
  3. 基于确定性规则 (Deterministic Focus Rules) 的重要通知/学校信息高亮呈现；
  4. 平台连接状态与 Coverage 诚实度展示。
- **P1 明确留待后续**：
  1. Agent 深度语义理解与消息分类；
  2. 智能待办提取与主动任务提醒；
  3. 自动同步任务到 Obsidian 日记/知识库。

---

## 2. 异构数据源角色与能力模型 (Source Capabilities)

三个上游数据源具有截然不同的数据语义与运行机制，严禁采用单一粗暴的“高频轮询数据库”处理：

| 数据源 | 核心定位与角色 | 接入机制 | 覆盖度 (Coverage) | 实时性与更新策略 |
|---|---|---|---|---|
| **微信 (WeChat)** | 完整型历史+实时源 | `wx-cli` (Rust 本地服务 `http://127.0.0.1:9100`) | 全历史 + 实时流 | **混合摄入**：SSE 实时事件流 + 重连跨会话 timeline 追溯对齐 |
| **企业微信 (WeCom)** | 快照只读源 (Snapshot) | `yichen-wecom-local-vault` (Mac 企微 5.x 本地私密快照) | 本地快照历史 | **快照检测**：低频探测本地解密快照变更 / 手动刷新，绝不高频解密 |
| **QQ** | 入站事件流框架 (Event Ingress) | `Zhin.js` (极简 Observer 插件) | 接入后实时事件 (ICQQ 个人/群或官方 Bot) | **单向推入**：Zhin 本地推送到工作台内部 Ingress 端点，工作台无反向发信权 |

### 2.1 适配器能力契约 (`IMCapabilities`)
```typescript
export interface IMCapabilities {
  history: boolean;               // 是否支持拉取全部历史
  realtime: boolean;              // 是否支持低延迟实时流
  media: 'none' | 'placeholder' | 'local' | 'remote'; // 媒体支持级别
  nativeUnread: boolean;          // 上游是否具备可靠未读标记
  reliableSelfIdentity: boolean;  // 上游是否具备可靠的“本人”标识 (企微可能为 false)
  mentions: boolean;              // 是否支持 @ 结构化解析
  replies: boolean;               // 是否支持消息引用与回复
  recallEvents: boolean;          // 是否支持消息撤回事件感知
}
```

---

## 3. 总体架构：本地 Derived IM Journal (`im_hub.db`)

为了避免“浏览器关掉即丢 QQ 消息”和“三端异构数据无法统一跨平台游标分页”的问题，工作台建立专有的本地派生消息日志：

```text
                  个人工作台 (Personal AI Workspace)
               三核现代化 Web 客户端 · http://127.0.0.1:8787/
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
【知识中心 (Obsidian)】   【AI 会话中心 (AgentsView)】  【统一消息中心 (IM Hub)】
  2400+ 笔记 / 标签 / 状态   1800+ 场历史 Agent 会话      微信 · 企微 · QQ 聚合信息流
                                                             │
                                                     GET /api/im/events (统一工作台 SSE)
                                                     GET /api/im/timeline /channels
                                                             │
                                                             ▼
                                                ┌──────────────────────────┐
                                                │   本地 Derived IM Journal │
                                                │   SQLite (~/.im_hub.db)  │
                                                │   (authority = derived)  │
                                                └─────────────▲────────────┘
                                                              │
                                            ┌─────────────────┴─────────────────┐
                                            │      IngestionCoordinator         │
                                            │ (去重、排序、水印推进、Focus 规则) │
                                            └─────────────────▲─────────────────┘
                                                              │
                         ┌────────────────────────────────────┼────────────────────────────────────┐
                         ▼                                    ▼                                    ▼
                   微信适配器 (WeChat)                 企微适配器 (WeCom)                   QQ 适配器 (QQ)
                   (WxCliAdapter)                      (WeComSnapshotAdapter)              (ZhinQQAdapter)
                         │                                    │                                    │
                         ▼                                    ▼                                    ▼
                   wx-cli SSE 流                       本地解密快照                         Zhin Ingress
               + /timeline 断线重连对齐                 时间戳明文检测                       单向推送 + 密钥鉴权
```

### 3.1 IM Journal 定位原则
- **`authority = derived`，`rebuildable = true`**：真源始终在微信、企微与 QQ 上游中，工作台 IM Journal 属于本地派生只读加速层；
- **存储路径与权限隔离**：存放在私有目录（`~/.personal-ai-workspace/im/im_hub.db`），目录模式 `0700`、数据库文件 `0600`，**绝对不写入 Obsidian Vault，不进入 Git 仓库**。

---

## 4. 统一数据契约 (Normalized Contracts)

### 4.1 统一消息 DTO (`IMMessageItem`)
```typescript
export interface IMAttachment {
  type: 'image' | 'voice' | 'video' | 'file';
  name?: string;
  mime?: string;
  size?: number;
  availability: 'local' | 'remote' | 'placeholder' | 'unavailable';
  local_ref?: string;      // 本地缓存/快照路径，走安全受控的 asset proxy
  remote_ref?: string;     // 仅存元数据，P0 绝不前端自动 fetch
}

export interface IMMessageItem {
  id: string;                      // 工作台全局稳定 opaque ID
  platform: 'wechat' | 'wecom' | 'qq';
  account_id: string;              // 账号标识 (支持多账号)
  channel_id: string;              // 会话频道 ID
  source_message_id?: string;      // 上游源消息 ID
  source_id_quality: 'native' | 'synthetic'; // 原生 ID 还是合成 ID
  sender: {
    id?: string;
    name: string;
    role?: string;                 // e.g. 群主/管理员/老师
  };
  is_self: boolean | null;         // 企微等内部 ID 不可靠时显式填 null，严禁瞎猜
  text: string;                    // 正文内容
  message_type: 'text' | 'image' | 'voice' | 'video' | 'file' | 'link' | 'notice' | 'mixed' | 'unknown';
  mentions: {
    id?: string;
    name?: string;
    is_self?: boolean;
    is_all?: boolean;
  }[];
  reply_to?: string;
  attachments: IMAttachment[];
  occurred_at: string;             // 平台消息真实发生时间 (ISO)
  observed_at: string;             // 工作台观察/摄入时间 (ISO)
  provenance: {
    mode: 'sse' | 'webhook' | 'snapshot' | 'poll';
    snapshot_id?: string;
    cursor?: string;
  };
  focus_tag?: 'school' | 'mention_all' | 'mention_self' | 'direct_important' | null;
  focus_reason?: string | null;    // 确定性高亮原因，如：“学校频道 + @全体成员”
}
```

### 4.2 统一会话摘要 DTO (`IMChannelSummary`)
```typescript
export interface IMChannelSummary {
  id: string;                      // 唯一 ID: "wechat:xxx", "wecom:yyy", "qq:zzz"
  platform: 'wechat' | 'wecom' | 'qq';
  account_id: string;
  channel_type: 'direct' | 'group' | 'notice';
  name: string;
  avatar_url?: string;
  last_message: string;
  last_time: string;
  native_unread_count?: number | null; // 上游真实未读
  local_unseen_count: number;          // 工作台本地未浏览计数
  is_focus: boolean;                   // 是否属于重要关注频道 (如学校群/班群)
}
```

---

## 5. 确定性聚焦规则引擎 (Deterministic Focus Rules for Students)

在 P0 阶段坚决不引入复杂的黑盒 Agent 逻辑，采用确定性、透明、用户可配置的高亮规则：

```python
def evaluate_focus_rule(msg: IMMessageItem, channel: IMChannelSummary) -> tuple[Optional[str], Optional[str]]:
    # 规则 1: 标记为学校频道的通知或 @全体成员
    if channel.is_focus or "通知" in channel.name or "班" in channel.name:
        for m in msg.mentions:
            if m.get("is_all"):
                return ("mention_all", f"学校/班级群「{channel.name}」发布了 @全体成员")
        if msg.message_type == "notice" or "通知" in msg.text[:30]:
            return ("school", f"学校/班级群「{channel.name}」重要通知")
            
    # 规则 2: @我
    for m in msg.mentions:
        if m.get("is_self"):
            return ("mention_self", f"在「{channel.name}」中被提到 (@你)")
            
    # 规则 3: 重要人员私聊
    if channel.channel_type == "direct" and channel.is_focus:
        return ("direct_important", f"重要联系人「{channel.name}」发送了私聊")
        
    return (None, None)
```

前端 Focus Feed 将这些消息以醒目的卡片聚合在右侧专用面板，并明确标注**为什么被高亮**，完美切合大学生及时捕捉课程与教务消息的需求。

---

## 6. 后端 API 与内部 Ingress 设计

### 6.1 前端查询端点 (带 `Cache-Control: no-store`)
| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/im/status` | 无 | 获取三大通道的连通状态与 Coverage 诚实度报告 |
| GET | `/api/im/overview` | 无 | 获取消息总览统计（总数、今日新增、各平台分布） |
| GET | `/api/im/channels` | `platform?`, `type?`, `focus_only?` | 查询会话频道列表 |
| GET | `/api/im/timeline` | `platform?`, `limit=50`, `before_id?`, `focus_only?` | **跨平台统一时间线流**（游标分页） |
| GET | `/api/im/channel/{id}/messages` | `limit=50`, `before_id?` | 指定会话历史消息流 |
| POST | `/api/im/channel/{id}/seen` | 无 | 将指定频道标记为本地已阅 |
| GET | `/api/im/events` | 无 | **工作台统一 SSE 事件总线**（新消息实时推送到浏览器） |

### 6.2 内部单向推入端点 (Internal Ingress)
| 方法 | 路径 | 鉴权要求 | 说明 |
|---|---|---|---|
| POST | `/internal/im/ingest/zhin` | 本地回环 (127.0.0.1) + `X-IM-Secret` 头鉴权 + 5MB 体积限制 | 接收来自 Zhin 极简 Observer 插件的 QQ 入站事件 |

---

## 7. 严格安全与隐私硬边界 (Security & Privacy Boundaries)

1. **源数据只读 (Source Read-Only)**：
   - 工作台绝不具备向微信、企微、QQ 发送任何消息的权限或能力；
2. **零反向控制 (Zero Outbound Authority)**：
   - 工作台不持有 Zhin Host Token、ICQQ 发信接口或企微发信权限；Zhin 与工作台之间为纯单向投递；
3. **单向 Ingress 严格鉴权**：
   - `/internal/im/ingest/zhin` 仅监听 `127.0.0.1`，验证随环境生成的共享密钥 `X-IM-Secret`，阻断外部网络伪造；
4. **不越权提取底层密钥**：
   - 绝不自动执行关闭系统 SIP、修改 `taskport` 或注入进程的操作；微信与企微密钥必须由用户根据官方文档独立安全配置；
5. **明文快照不进项目与 Git**：
   - 企微解密快照与 IM Journal 严格限制在 `0700` 专用私有工作空间中，`.gitignore` 全面封死；
6. **全链路脱敏与无痕日志**：
   - 消息正文与搜索关键词绝不打入系统应用日志；
7. **远程媒体资源防御**：
   - P0 绝不在前端自动发起远程媒体资源下载，防止恶意追踪与 IP 泄漏；
8. **全站 XSS 消毒与缓存隔离**：
   - 前端所有消息文本与发送人信息必须经过 `escapeHtml()` 与 `DOMPurify` 清洗，接口统一注入 `Cache-Control: no-store`。

---

## 8. 实施路径与测试策略

1. **Phase 1: 存储与数据底座**：
   - 实现 `IMJournal` (SQLite) 表结构、索引与读写管理（含单向去重与游标分页）；
2. **Phase 2: 适配器与摄入协调器**：
   - `WxCliAdapter`（HTTP / SSE 摄入）
   - `WeComSnapshotAdapter`（快照解析器）
   - `ZhinQQAdapter`（内部 Ingress 事件规整）
   - `IngestionCoordinator`（去重、时间戳归一、Focus 规则计算、推进 Watermark）；
3. **Phase 3: 业务 API 与统一 SSE**：
   - 实现 `/api/im/status`、`/overview`、`/channels`、`/timeline`、`/events` 以及 `/internal/im/ingest/zhin`；
4. **Phase 4: 前端三核工作台集成**：
   - 在前端顶部增设【统一消息中心 (IM Hub)】面板；
   - 实现多维度平台筛选、跨平台聚合时间线、会话消息详情与学校重要信息 Focus 视图；
5. **Phase 5: 自动化端到端测试与真实验证**：
   - 编写完整的自动化测试套件（覆盖去重、各平台适配、Focus 规则、安全边界、SSE 派发），确保全绿通过。
