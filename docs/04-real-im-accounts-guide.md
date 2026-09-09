# 个人工作台真实账号接入指南 (已全部完成)

> 项目：个人工作台 (Personal AI Workspace)  
> 日期：2026-09-08  
> 状态：**微信 + 企业微信 + QQ 已全部接入真实本地账号**

---

## ✅ 当前接入状态

| IM 平台 | 真实账号 | 数据来源 | 接入状态 |
|---|---|---|---|
| **微信 (WeChat)** | `wxid_hxwpag2k3qi122`（七十八个钢笔尖，十三瓶墨水） | `wx-cli` 本地只读服务 `http://127.0.0.1:9100` | **🟢 已实时接入** |
| **企业微信 (WeCom)** | `1688857608826794`（袁浩岚 · 中南民族大学） | 本地明文快照 `~/Library/Application Support/wecom-local-vault/snapshots/` | **🟢 已接入（快照）** |
| **QQ** | `qq_primary`（本机已登录账号） | 本地只读快照 `~/Library/Application Support/qq-local-vault/` | **🟢 已接入（快照）** |

实测数据量：**15,500+ 条真实消息 · 200+ 个会话频道**（含微信 2,580+ 条、企微 3,880+ 条、QQ 9,115 条）。

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

## 三、QQ 接入（本地只读快照模式）

基于 ADR-005 及 v0.2.8 受控变更，QQ 彻底废弃 Zhin Webhook 推送，采用与企业微信同级的**本地只读明文快照模式**。

### 核心特性
- **无需退出 QQ**：实时通过只读内存扫描匹配数据库 salt，提取 live SQLCipher codec；
- **零特权运行**：日常工作台不加载 LLDB、不拥有解密密钥、不接触 QQ 容器；
- **安全原子发布**：提取器短暂暂停 QQ 写进程（毫秒级）并 COW 克隆 DB/WAL/SHM，校验 `PRAGMA integrity_check` 与核心 schema 后原子发布为只读明文快照。

### 快照提取步骤

在 QQ 正常登录运行的前提下，执行单次本地快照提取：

```bash
.venv/bin/python -m backend.scripts.qq_snapshot --confirm-capture --account-alias qq_primary
```

输出示例：
```text
QQ_SNAPSHOT_OK qqsnap-v1-dee898e87a48d9bc28a350a8 messages=9115
```

快照发布于：
```text
~/Library/Application Support/qq-local-vault/accounts/qq_primary/
  snapshots/<snapshot_id>/
    export/
      nt_msg.db          # 标准 SQLite 格式（8.5MB+，7700+ 群消息，1380+ 私聊）
      group_info.db      # 群名称及成员详情
      profile_info.db    # 好友与联系人信息
    manifest.json        # 内容哈希自校验元数据
  CURRENT                # 指向当前有效快照 ID
```

工作台中的 `QQSnapshotAdapter` 会自动只读加载该快照并导入 IM Journal。后续如需更新 QQ 消息，只需再次运行一次上述提取命令。

> **自动同步（推荐）**：`im_sync_daemon` 守护进程会监听 QQ / 企业微信本地数据库文件变更，自动在后台执行快照提取（QQ 使用 `private/keys.json` 密钥缓存，日常提取约 5 秒，无需再次内存扫描），并自动清理旧快照（默认保留最近 24 份，可用 `IM_SNAPSHOT_KEEP` 环境变量调整，设为 `0` 禁用清理）。收到新消息后一般 **≤20 秒** 内自动出现在工作台。

---

## 四、一键启动

```bash
./scripts/start-im-adapters.sh
```

该脚本会依次：① 启动微信 `wx-cli` 只读服务；② 校验企微 / QQ 快照可用性；③ 启动 `im_sync_daemon` 自动同步守护进程（日志：`/tmp/im_sync_daemon.log`）；④ 启动工作台。

或手动启动工作台：

```bash
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8787
nohup .venv/bin/python backend/scripts/im_sync_daemon.py >/tmp/im_sync_daemon.log 2>&1 &
```

前端 IM Hub 进入页面或点击刷新按钮时会自动调用 `POST /api/im/sync` 触发一次即时同步（通过 `/tmp/im_sync_trigger` 通知守护进程）。
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
| `QQ_SNAPSHOT_ROOT` | `~/Library/Application Support/qq-local-vault/accounts/qq_primary` | QQ 快照根目录（可选） |

---

## 六、安全边界（始终生效）

1. **源端只读**：工作台绝不向微信/企微/QQ 发送任何消息，无发信 API；
2. **零反向控制**：不持有 wx-cli 写入权限、不持有企微客户端、不持有 QQ bot 发信凭证；
3. **私密存储**：IM Journal 位于 `~/.personal-ai-workspace/im/`，目录 `0700`、数据库与 WAL/SHM `0600`；
4. **明文快照隔离**：企微与 QQ 快照位于用户私有 Vault 目录，`0700/0600` 权限，绝不进入 Git；
5. **全端点 `Cache-Control: no-store`**：个人消息内容不落浏览器缓存，由全局 HTTP 中间件强制守护；
6. **纯文本渲染**：聊天正文使用 `textContent` 绑定，杜绝聊天 XSS；
7. **日志脱敏**：消息正文、密钥与搜索关键词严禁写入任何应用日志或临时文件。
