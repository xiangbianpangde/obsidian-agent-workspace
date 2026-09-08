# 个人工作台真实账号接入指南 (已全部完成)

> 项目：个人工作台 (Personal AI Workspace)  
> 日期：2026-09-08  
> 状态：**微信 + 企业微信 已实时接入真实账号；QQ 网关就绪等待推送**

---

## ✅ 当前接入状态

| IM 平台 | 真实账号 | 数据来源 | 接入状态 |
|---|---|---|---|
| **微信 (WeChat)** | `wxid_hxwpag2k3qi122`（七十八个钢笔尖，十三瓶墨水） | `wx-cli` 本地只读服务 `http://127.0.0.1:9100` | **🟢 已实时接入** |
| **企业微信 (WeCom)** | `1688857608826794`（袁浩岚 · 中南民族大学） | 本地明文快照 `~/Library/Application Support/wecom-local-vault/snapshots/` | **🔵 已接入（快照）** |
| **QQ** | 待绑定 | Zhin.js 单向推送 `POST /internal/im/ingest/zhin` | **⚪ 网关就绪** |

实测数据量：**6,400+ 条真实消息 · 167 个会话频道 · 26 条学校重要通告**。

---

## 一、微信接入（已完成）

### 已完成步骤

1. **安装 wx-cli v0.7.4**（官方 macOS arm64 预编译版本）：
   ```bash
   mkdir -p ~/.local/bin
   curl -L -o /tmp/wx-cli.tar.gz \
     "https://github.com/pandorafuture/wx-cli/releases/download/v0.7.4/wx-cli-v0.7.4-macos-arm64.tar.gz"
   tar -xzf /tmp/wx-cli.tar.gz -C ~/.local/bin/
   chmod +x ~/.local/bin/wx-cli
   ```

2. **关闭 SIP 并启用开发者调试**（用户已完成）：
   - 恢复模式终端执行 `csrutil disable` 后重启；
   - `sudo DevToolsSecurity -enable`；
   - `wx-cli doctor` 全绿通过。

3. **自动提取微信数据库密钥**（本仓库脚本，与微信版本解耦）：
   ```bash
   .venv/bin/python backend/scripts/extract_wechat_key.py
   ```
   该脚本通过 LLDB 在 `CCKeyDerivationPBKDF` 下断点，匹配 `message_0.db` 的 salt，
   校验 Page 1 后自动写入 `~/Library/Application Support/wx-cli/config/keys.toml`。

4. **启动只读服务**：
   ```bash
   ~/.local/bin/wx-cli server run --port 9100
   ```

### 验证

```bash
~/.local/bin/wx-cli sessions --limit 10     # 真实会话列表
curl -s "http://127.0.0.1:9100/api/v1/health"
```

---

## 二、企业微信接入（已完成）

### 已完成步骤

1. **安装 Frida**（企微内存密钥被动捕获）：
   ```bash
   .venv/bin/python -m pip install frida
   ```

2. **被动捕获企微数据库密钥**（需企业微信已登录运行）：
   ```bash
   python3 ~/Projects/vendor/yichen-skills/yichen-wecom-local-vault/scripts/capture_key_macos.py \
     capture --confirm-attach --duration 90
   ```
   密钥以 `0600` 权限写入 `~/Library/Application Support/wecom-local-vault/private/`。

3. **解密生成本地只读明文快照**：
   ```bash
   python3 ~/Projects/vendor/yichen-skills/yichen-wecom-local-vault/scripts/vault_cli.py decrypt \
     --data-dir "$HOME/Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data/1688857608826794/Data"
   ```
   19 个数据库全部解密成功。

### 验证

```bash
SNAP=$(ls -d ~/Library/Application\ Support/wecom-local-vault/snapshots/* | sort | tail -1)
python3 ~/Projects/vendor/yichen-skills/yichen-wecom-local-vault/scripts/vault_cli.py sessions --snapshot "$SNAP"
```

---

## 三、QQ 接入（网关就绪）

工作台已开放内部认证推入端点：

```http
POST http://127.0.0.1:8787/internal/im/ingest/zhin
X-IM-Secret: workspace_im_secret_token_default
Content-Type: application/json
```

### 接入步骤

1. 创建 Zhin 项目：
   ```bash
   mkdir -p ~/Projects/qq-bot && cd ~/Projects/qq-bot
   pnpm init && pnpm add zhin.js @zhin.js/adapter-icqq
   ```

2. 编写转发插件 `bot.ts`：
   ```typescript
   import { definePlugin } from 'zhin.js/plugin-runtime';

   export default definePlugin({
     name: 'workspace-forwarder',
     setup({ onMessage }) {
       onMessage(async (msg) => {
         await fetch('http://127.0.0.1:8787/internal/im/ingest/zhin', {
           method: 'POST',
           headers: {
             'Content-Type': 'application/json',
             'X-IM-Secret': 'workspace_im_secret_token_default',
           },
           body: JSON.stringify({
             event_id: `qq_${msg.id}`,
             account_id: 'my_qq',
             occurred_at: new Date(msg.time * 1000).toISOString(),
             occurred_at_epoch_ms: msg.time * 1000,
             payload: {
               message_type: 'text',
               sender_id: String(msg.sender.user_id),
               sender_name: msg.sender.nickname,
               group_id: msg.group_id ? String(msg.group_id) : undefined,
               group_name: msg.group_name,
               text: msg.raw_message,
               mentions: msg.at_all ? [{ is_all: true }] : [],
             },
           }),
         });
       });
     },
   });
   ```

3. 扫码登录并启动：
   ```bash
   npx zhin dev
   ```

---

## 四、一键启动

```bash
./scripts/start-im-adapters.sh
```

或手动启动工作台：

```bash
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8787
```

打开 <http://127.0.0.1:8787> → 点击顶部 **【统一消息中心 (IM Hub)】**。

---

## 五、环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WECHAT_ACCOUNT_ID` | `wxid_hxwpag2k3qi122` | 微信账号 ID |
| `WX_CLI_BASE_URL` | `http://127.0.0.1:9100` | wx-cli 服务地址 |
| `WECHAT_BACKFILL_DAYS` | `30` | 微信历史回溯天数 |
| `WECOM_ACCOUNT_ID` | `wecom_primary` | 企微账号 ID |
| `QQ_ACCOUNT_ID` | `qq_primary` | QQ 账号 ID |
| `IM_INGEST_SECRET` | `workspace_im_secret_token_default` | Zhin 推入共享密钥 |

---

## 六、安全边界（始终生效）

1. **源端只读**：工作台绝不向微信/企微/QQ 发送任何消息，无发信 API；
2. **零反向控制**：不持有 wx-cli 写入权限、不持有企微客户端、不持有 Zhin 发信凭证；
3. **私密存储**：IM Journal 位于 `~/.personal-ai-workspace/im/`，目录 `0700`、数据库与 WAL/SHM `0600`；
4. **明文快照隔离**：企微快照与密钥位于 `~/Library/Application Support/wecom-local-vault/`，`0600` 权限，绝不进入 Git；
5. **全端点 `Cache-Control: no-store`**：个人消息内容不落浏览器缓存；
6. **纯文本渲染**：聊天正文使用 `textContent` 绑定，杜绝聊天 XSS；
7. **日志脱敏**：消息正文与搜索关键词不写入应用日志。
