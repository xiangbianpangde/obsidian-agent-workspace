# 个人工作台真实账号接入向导 (WeChat · WeCom · QQ)

> 项目：个人工作台 (Personal AI Workspace)  
> 日期：2026-09-04  
> 状态：**工具链与适配层已完全就绪，等待用户本地授权/配置 Key**

---

## 现状总览 (本机环境探测结果)

| IM 平台 | 探测到的真实账号 / 数据集 | 核心数据文件路径 | 接入就绪度 |
|---|---|---|---|
| **微信 (WeChat)** | `wxid_hxwpag2k3qi122_53e3` | `~/Library/Group Containers/5A4RE8SF68.com.tencent.xinWeChat/` | **工具已就绪** (`wx-cli v0.7.4` 已装入 `~/.local/bin/wx-cli`)，需配置解密 Key |
| **企业微信 (WeCom)** | 账号 `1688857608826794` (数据集 `e968fc5b5a45`) | `~/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data/` | **数据完整存在** (`message.db` 2.5MB, `user.db` 36MB, `session.db`)，需配置解密 Key |
| **QQ** | 本机已安装 `/Applications/QQ.app` | 工作台端点 `POST http://127.0.0.1:8787/internal/im/ingest/zhin` | **网关已就绪**，启动 Zhin 实例即可推送 |

---

## 一、真实微信账号接入步骤 (`wx-cli`)

本机的 `~/.local/bin/wx-cli` 已成功安装并就绪。微信本地数据库是经过 SQLCipher / WeChat 加密的，要让工作台读取真实聊天记录：

### 方式 A：如果已有该微信账号的 64 位十六进制 Key（最推荐、无需关闭 SIP）
直接在 `~/Library/Application Support/wx-cli/config/keys.toml` 中配置（若目录不存在可新建）：
```toml
[accounts.wxid_hxwpag2k3qi122_53e3]
key = "your_64_character_hex_key_here"
```
配置完成后，启动后台服务：
```bash
~/.local/bin/wx-cli server run --port 9100
```
工作台刷新后，顶部即刻亮起绿灯：`🟢 微信: 在线`，真实微信好友与群聊将自动流入全景时间线！

### 方式 B：自动从当前运行的微信进程提取 Key
如果当前没有保存 key，根据 `wx-cli` 官方机制：
1. 确保微信处于登录状态；
2. 运行提取命令：
   ```bash
   ~/.local/bin/wx-cli key extract
   ```
   *(注：若 macOS 提示 SIP 限制，可参考 `wx-cli doctor` 的提示临时开启调试权限，或使用方式 A 直接填 key)*。

---

## 二、真实企业微信账号接入步骤 (`yichen-wecom-local-vault`)

已成功探测到用户本机的真实企业微信数据集：
- 数据目录：`/Users/xbpd/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data`
- 包含 16 个核心加密库（含教务通知 `message.db`、师生通讯录 `user.db`）。

### 操作步骤：
1. 企微数据采用 `wecom-wxsqlite3-aes128` 加密，获取 Key：
   可在终端使用辅助脚本捕获运行中企微的内存 Key，或直接填入已知 Key：
   ```bash
   python3 scripts/capture_key_macos.py capture --confirm-attach
   ```
2. 执行解密生成本地私密只读快照：
   ```bash
   python3 scripts/vault_cli.py decrypt --data-dir "/Users/xbpd/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data"
   ```
3. 工作台的企业微信适配器 `WeComSnapshotAdapter` 会自动挂载该快照数据库，辅导员私聊、课程群通知全量出现在右侧 **Focus 看板**中！

---

## 三、真实 QQ 账号接入步骤 (`Zhin.js`)

工作台内部已开放认证推入端点：`POST http://127.0.0.1:8787/internal/im/ingest/zhin`（携带请求头 `X-IM-Secret: workspace_im_secret_token_default`）。

### 操作步骤：
1. 在终端新建一个极简 Zhin QQ 接收端：
   ```bash
   mkdir -p ~/Projects/qq-bot && cd ~/Projects/qq-bot
   pnpm init
   pnpm add zhin.js @zhin.js/adapter-icqq
   ```
2. 编写极简推入插件 `bot.ts`：
   ```typescript
   import { definePlugin } from 'zhin.js/plugin-runtime';

   export default definePlugin({
     name: 'workspace-forwarder',
     setup({ onMessage }) {
       onMessage(async (msg) => {
         // 单向推送到个人工作台
         await fetch('http://127.0.0.1:8787/internal/im/ingest/zhin', {
           method: 'POST',
           headers: {
             'Content-Type': 'application/json',
             'X-IM-Secret': 'workspace_im_secret_token_default'
           },
           body: JSON.stringify({
             event_id: `qq_${msg.id}`,
             account_id: 'my_qq',
             occurred_at: new Date().toISOString(),
             payload: {
               message_type: msg.message_type,
               sender_id: String(msg.sender.user_id),
               sender_name: msg.sender.nickname,
               group_id: msg.group_id ? String(msg.group_id) : undefined,
               group_name: msg.group_name,
               text: msg.raw_message,
               mentions: msg.at_all ? [{ is_all: true }] : []
             }
           })
         });
       });
     }
   });
   ```
3. 启动扫码登录 QQ 账号：
   ```bash
   npx zhin dev
   ```
   登录成功后，用户 QQ 收到的大群通知或私聊将毫秒级流式推送到工作台！
