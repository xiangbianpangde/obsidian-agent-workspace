# 个人工作台 IM 消息接入技术方案 v0.1 (第三个 P0 · 个人信息系统平台)

> 项目：个人工作台 (Personal AI Workspace) · 模块：统一消息中心 (Unified IM Hub)  
> 日期：2026-09-03 · 状态：待 Sol 架构评审

---

## 1. 目标与定位

根据用户 P0 需求：
> **搭建个人信息系统平台，将我的所有 im 的信息接入到当前平台，我能够在当前平台得到我的全部的个人消息，后续可以接入 agent 进行分析和提醒（p1）。**  
> 接入源：  
> - 微信：`wx-cli` (https://github.com/pandorafuture/wx-cli)  
> - 企业微信：`yichen-skills` (https://github.com/mcncarl/yichen-skills)  
> - QQ：`zhin` (https://github.com/zhinjs/zhin)  
> **当前范围严格限定为 P0**：只要求接入与统一呈现这些信息，不提前实现 Agent 自动提醒。

### 1.1 用户实际场景
用户为人工智能专业大学生，学校学院通知、科研沟通、同学协作分散在三款 IM 中：
- **微信**：同学私聊、学术好友、社团讨论；
- **企业微信**：学校官方通知、课程群、导师/辅导员通告、教务班级消息；
- **QQ**：课程大群、技术交流群、作业提交群。
当前痛点：信息割裂，多端频繁切换容易漏看关键学校任务。

---

## 2. 整体系统架构与数据流

```text
                  个人工作台 (Personal AI Workspace)
               三核现代化 Web 客户端 · http://127.0.0.1:8787/
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     ▼                           ▼                           ▼
【知识中心 (Obsidian)】   【AI 会话中心 (AgentsView)】  【统一消息中心 (IM Hub)】
  2400+ 笔记 / 标签 / 状态   1800+ 场历史 Agent 会话      微信 · 企微 · QQ 聚合信息流
     │                           │                           │
     ▼                           ▼                           ▼
/api/files, /api/tags...  /api/agentsview/*          /api/im/* (统一消息网关)
                                                             │
                         ┌───────────────────────────────────┼───────────────────────────────────┐
                         ▼                                   ▼                                   ▼
                   微信适配器 (WeChat)                企微适配器 (WeCom)                  QQ 适配器 (QQ)
                   (基于 wx-cli)                     (基于 yichen-wecom-vault)           (基于 Zhin.js)
                         │                                   │                                   │
                         ▼                                   ▼                                   ▼
                  wx-cli REST/CLI                   本地 WeCom 快照/CLI                 Zhin Webhook/API
               http://127.0.0.1:9100                 SQLite 只读快照                    http://127.0.0.1:8086
```

---

## 3. 统一消息规范 (Normalized IM Message DTO)

不同 IM 的底层数据格式各异，适配器层必须将其规整映射为统一的标准 DTO：

```typescript
interface IMChannelSummary {
  id: string;              // 全局唯一标识: e.g. "wechat:wxid_xxx", "wecom:16888@chatroom", "qq:group_12345"
  platform: 'wechat' | 'wecom' | 'qq';
  channel_type: 'direct' | 'group' | 'notice'; // 私聊 | 群聊 | 系统通知
  name: string;            // 联系人名称 / 群聊标题
  avatar_url?: string;     // 头像
  last_message: string;    // 最新消息摘要
  last_time: string;       // ISO datetime
  unread_count: number;    // 未读/待处理计数
}

interface IMMessageItem {
  id: string;              // 消息唯一 ID
  channel_id: string;      // 所属频道 ID
  platform: 'wechat' | 'wecom' | 'qq';
  sender_name: string;     // 发送人
  is_self: boolean;        // 是否为本人发送
  content: string;         // 消息文本正文
  msg_type: 'text' | 'image' | 'file' | 'voice' | 'notice';
  timestamp: string;       // 发生时间 ISO
  media_url?: string;      // 媒体/图片资源直链
}
```

---

## 4. 三大平台接入通道技术选型

### 4.1 微信通道 (WeChat Adapter)
- **底层依赖**：`wx-cli` (Rust 编写，支持 Mac 本地无损解密读取)；
- **接入模式**：
  - 优先通过 `wx-cli` 提供的本地 REST API (`http://127.0.0.1:9100/api/v1/sessions` 与 `/api/v1/timeline?since=...`) 进行高效读取；
  - 若服务未常驻，通过 CLI 命令 `wx-cli sessions --format json` 自动拉取；
  - 严格保持本地只读，不发送任何消息。

### 4.2 企业微信通道 (WeCom Adapter)
- **底层依赖**：`yichen-skills` 中的 `yichen-wecom-local-vault`；
- **接入模式**：
  - 自动定位 macOS 企业微信数据目录并读取解密快照数据库；
  - 优先拉取学校通知群与教务会话最新消息；
  - 严格只读查询。

### 4.3 QQ 通道 (QQ Adapter)
- **底层依赖**：`Zhin.js` (TypeScript 多端 IM 统一框架)；
- **接入模式**：
  - 工作台开放只读接收 Webhook 端点 `/api/im/webhook/zhin`，实时接收 QQ 官方 Bot 或 OneBot 投递的群消息与私聊；
  - 或通过 Zhin 本地 HTTP Host (`http://127.0.0.1:8086`) 轮询最近消息。

---

## 5. 后端 API 契约设计

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/im/status` | 无 | 探测三款 IM 通道的连通状态 (微信在线/离线、企微在线/离线、QQ 在线/离线) |
| GET | `/api/im/overview` | 无 | 消息全景看板：全平台消息总数、各平台未读/活跃度分布、今日新增消息数 |
| GET | `/api/im/channels` | `platform?` (wechat/wecom/qq), `type?` (direct/group/notice) | 统一会话/联系人列表 |
| GET | `/api/im/timeline` | `platform?`, `limit=50`, `since?` | **跨平台聚合时间线流**（按时间最新降序排列，一站式查看全平台消息） |
| GET | `/api/im/channel/{id}/messages` | `limit=50`, `from_id?` | 指定会话的历史消息有界分页拉取 |
| POST | `/api/im/webhook/zhin` | 消息 Payload | 接收 Zhin QQ 消息事件推送 |

---

## 6. 前端交互设计 (工作台三核界面)

在工作台顶栏切换器升级为三核选项卡：
- **【知识中心 (Obsidian)】** ↔ **【AI 会话中心 (AgentsView)】** ↔ **【统一消息中心 (IM Hub)】**

进入“统一消息中心”后：
1. **左栏 · 消息分类与通道筛选**：
   - 平台切换徽章：`全部 (All)` · `微信 🟢` · `企业微信 🔵` · `QQ 🔴`；
   - 类型过滤：`全部会话` · `重要通知 (学校/班级)` · `群聊` · `私聊`；
   - 会话列表卡片：显示联系人/群头像、平台小图标、最新消息摘要、时间徽章；
2. **中栏 · 会话对话流与消息回溯**：
   - 展现所选会话的历史消息流，气泡化区分发送者与本人，支持图片查看；
3. **右栏 · 重要通知聚合看板 (Focus Feed)**：
   - 专为大学生设计：智能高亮过滤来自企业微信教务通知、班级群 @全体成员、辅导员私聊等**高优先级消息卡片**，一目了然防止遗漏学校事务；
   - 为 P1 阶段预留“一键生成待办至 Obsidian”按钮。

---

## 7. 安全与隐私边界 (P0 硬底线)

1. **绝对只读，严禁发信**：
   - P0 阶段纯做信息感知与呈现，工作台后端绝对不集成任何自动发送消息的功能，避免封号与误发风险；
2. **全量隐私防缓存 (Cache-Control: no-store)**：
   - 所有 `/api/im/*` 接口统一注入 `Cache-Control: no-store, no-cache, must-revalidate`；
   - 消息数据不上传任何第三方云端，完全单机闭环；
3. **输出层 XSS 清洗**：
   - 所有来自微信、企微、QQ 的用户昵称、群名、消息文本必须经过 `escapeHtml()` 转义与 `DOMPurify` 清洗，严防恶意消息构造成的聊天 XSS。
