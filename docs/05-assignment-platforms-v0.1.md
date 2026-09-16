# 05 - 作业平台接入设计（学习通 + 数你最灵）v0.1

> 状态：**已实现（S1–S4 全部落地）**
> 分支：`feat/assignment-hub`　worktree：`/Users/xbpd/Projects/workbench-assignment`
> 目标：在个人工作台中**只读聚合**两个作业平台的作业/任务列表，统一展示与到期提醒；不自动答题、不代交作业。

### 实现状态（S1–S4）

| 步骤 | 状态 | 落点 |
|---|---|---|
| S1 guard/storage/models + 凭证 API | ✅ 完成 | `guard.py`（SSRF 拦截：localhost/私网/白名单外）、`storage.py`（Fernet 加密 + `0700`/`0600`、零删除）、`api/assignment.py` |
| S2 学习通适配器 | ✅ 完成 | `adapters/chaoxing.py`——课程列表 → 作业列表 → 详情页三跳，解析失败标 `status=unknown` 不 crash |
| S3 数你最灵适配器 | ✅ 完成 | `adapters/smartestu.py`——`cookies:` CDP 通道（2026-09 新协议唯一稳定路径）与 `<jwt>` 双模式 |
| S4 聚合 API + 前端工作区 + 到期提醒 | ✅ 完成 | 前端第五核【作业中心】；`sync_lock` 并发互斥；文档 §4.3 的 **1 次/5 分钟**频率上限已实现（`ASSIGNMENT_SYNC_MIN_INTERVAL`） |

> 与初版设计的差异：`refresh:<jwt>` 模式已被平台废弃（服务端不再接受 `POST /api/auth/refresh`），构造期即拒绝并提示改用 `cookies:`。

---

## 1. 调研结论

### 1.1 数你最灵（smartestu.cn）

SPA（React + Vite，静态资源在 `static.smartestu.cn`）。对其主 bundle（`index.oXDOTeVk.js`，约 5MB）做了静态分析，结论：

**认证模型：JWT Bearer Token（不是 cookie 会话）**

| 项 | 结论 | 证据 |
|---|---|---|
| 请求头 | `Authorization: Bearer <token>` | bundle 中多处 `Authorization:\`Bearer ${...}\`` |
| token 存储 | localStorage 键 `token` / `refreshToken` / `auth` | `localStorage.removeItem("token"/"refreshToken"/"auth")` |
| 刷新 | `POST /api/auth/refresh`（支持 `?retry=true`） | endpoint 表 |
| 登录 | `POST /api/auth/login`（学生号字段 `schoolUserId`；密码字段名未能从压缩代码确认，需一次真实抓包） | `{schoolUserId:E}` 等模式 |
| 登出 | `POST /api/auth/logout` | endpoint 表 |

**学生端作业 API（同源 `/api`，baseURL 为空即 `https://smartestu.cn`）**

```
POST /api/homework/student/portal-summary          # 门户汇总（作业概览）
POST /api/homework/student/course/query            # 课程列表
POST /api/homework/student/course-overview         # 单课程作业总览（?courseId=）
POST /api/homework/student/mark/queryHomeworks     # 作业列表
POST /api/homework/student/homework/submit         # 提交作业 —— 接入端禁用
POST /api/homework/student/homework/startOnlineExam # 开考 —— 接入端禁用
```

请求体/响应字段名未能从压缩 bundle 完全还原，**接入时先用浏览器 DevTools 抓一次真实流量**确认 JSON 形态。

### 1.2 学习通（超星 Chaoxing）

未带凭证访问 `i.chaoxing.com/base` 会 302 到 errorTips-404，**一切数据接口依赖 cookie 登录态**。参考开源实现（Samueli924/chaoxing，3.3k star）确认了可靠链路：

**cookie 关键项**：`_uid`（或 `UID`）、`fid`。`vc3` 等仅在账号密码登录时相关——本设计**不做账号密码登录**，只让用户从浏览器导入已有 cookie，避免触碰验证码/风控。

**只读拉取链路**（与签到脚本同源、久经验证）：

```
1. POST https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata
   data: {courseType:1, courseFolderId:0, query:"", superstarClass:0}
   headers: Referer: https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?...
   → 课程列表（HTML 片段，需解析 courseId / clazzId / cpi）
   （cookie 失效时响应体会 302/含 passport2.chaoxing.com 跳转 → 可判定登录态过期）

2. 对每门课程：进入课程页 mycourse/studentcourse?courseid=&clazzid=&cpi=&ut=s
   → 章节树（knowledge cards）中包含 jobid 形如 work-<id> 的作业任务及 enc/ktoken
3. GET https://mooc1.chaoxing.com/mooc-ans/api/work?api=1&workId=&jobid=&...&enc=&cpi=&ut=s&clazzId=&courseid=
   → 作业详情/题目页（HTML）。只读场景：仅取作业标题、截止时间、提交状态，
   不解析题目、不提交任何表单。
```

另有移动端活动接口 `mobilelearn.chaoxing.com/v2/apis/active/student/activelist`（JSON，签到活动为主），可作为后续"课堂活动"扩展，v1 不做。

**注意**：章节级作业链路需要遍历课程章节，请求量较大；开源项目还有一条"作业中心"HTML 列表路径（`_parse_work_record_list`，按课程解析已完成作业记录页）。v1 建议先做**课程列表 + 手动绑定 course/clazz**，作业列表按需拉取。

---

## 2. 设计方案

### 2.1 定位与铁律（继承项目既有安全模型）

1. **绝对只读**：两个平台适配器只实现 GET/查询类调用；`submit`/`startOnlineExam` 等写操作接口在代码层不封装、在出站白名单层禁止。
2. **凭证本地私密**：cookie/Bearer token 存 `data/assignment_platforms.db`（复用 IM Journal 的 `0700` 目录 + `0600` 文件加固，见 `secure_harden_path`），绝不进 Vault、绝不进 git、前端 API 不回显明文凭证（只回显掩码与状态）。
3. **出站请求合规**（对齐 Mimosa SSRF 约束）：仅允许 http/https；发请求前校验 host，拒绝 localhost、环回、私有与保留地址；目标 host 白名单硬编码为 `smartestu.cn`、`*.chaoxing.com`。
4. **零删除**：平台任务快照表只插入/更新，失效任务标记 `is_stale`，不物理删除。

### 2.2 后端结构（新增 `backend/app/assignment/`）

```
backend/app/assignment/
├── __init__.py
├── models.py        # PlatformTask dataclass + 归一化枚举
├── storage.py       # SQLite（复用 secure_harden_path / WAL 模式）
├── guard.py         # SSRF 出站守卫（host 白名单 + IP 黑名单解析）
├── adapters/
│   ├── base.py      # BaseAdapter: fetch_tasks() -> list[NormalizedTask]
│   ├── smartestu.py # Bearer token；自动 401 → 提示 refresh/重新提供 token
│   └── chaoxing.py  # cookie 导入；courselistdata → 课程 → 作业页解析
backend/app/api/assignment.py   # FastAPI router /api/assignment/*
```

**归一化数据模型**（映射到既有 `AcademicEvent.event_type="assignment"`，可直接喂给 schedule 提醒）：

```python
@dataclass
class NormalizedTask:
    platform: Literal["chaoxing", "smartestu"]
    external_id: str          # 平台内唯一 ID（work-xxx / homeworkId）
    course_name: str
    title: str
    due_at: Optional[datetime]  # 截止时间（能解析则填）
    status: Literal["unsubmitted", "submitted", "graded", "unknown"]
    score: Optional[float]
    detail_url: str           # 平台原链接，点击直达
    raw: dict                 # 原始 JSON/解析字段，便于排查
    fetched_at: datetime
```

**存储表**：

```sql
CREATE TABLE platform_credentials (
  platform TEXT PRIMARY KEY,          -- "chaoxing" | "smartestu"
  credential BLOB NOT NULL,           -- Fernet 加密后的 cookie/token
  updated_at INTEGER NOT NULL,
  last_validated_at INTEGER,
  is_valid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE platform_tasks (
  platform TEXT NOT NULL,
  external_id TEXT NOT NULL,
  course_name TEXT, title TEXT,
  due_at INTEGER, status TEXT, score REAL,
  detail_url TEXT, raw TEXT,
  first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL,
  is_stale INTEGER NOT NULL DEFAULT 0,
  UNIQUE(platform, external_id)       -- 同 IM Journal 的 dedupe 思想
);
```

### 2.3 API 设计

```
POST /api/assignment/credentials/{platform}   # 导入凭证（chaoxing=cookie串, smartestu=token），返回掩码+校验结果
DELETE→软禁用（改为 POST .../credentials/{platform}/disable，零删除）
GET  /api/assignment/credentials              # 各平台状态（是否配置/最近校验/是否有效）
POST /api/assignment/sync/{platform}          # 手动同步，落库并返回增量统计
GET  /api/assignment/tasks?platform=&status=&due_before=   # 聚合查询
POST /api/assignment/sync-all                 # 一键同步（供前端"刷新"与未来定时器）
```

- 所有响应注入 `Cache-Control: no-store`（与 IM Hub 同规）。
- 同步采用 `threading.Lock` 互斥（模式同 `api/schedule.py` 的 storage 锁），防止并发拉取触发平台风控。
- 学习通接口间加 0.5–1.5s 随机延迟 + 失败指数退避（参考开源项目風控经验）。

### 2.4 前端（`frontend/dist/index.html` 单页内扩展）

- 顶栏第四核【作业中心 (Assignment)】，新增 `workspace-assignment` 区块，风格与 schedule 工作区一致（深色、amber 强调色）。
- 布局：左侧平台筛选 + 状态筛选（未交/已交/已批改/已过期），右侧按截止时间排序的任务卡片列表；每卡带平台徽标、课程名、倒计时（<24h 红色高亮）、"打开平台"外链。
- 顶部按钮：`导入学习通 Cookie` / `导入数你最灵 Token` / `一键同步`；同步结果 toast 显示增量。
- 到期提醒复用 schedule 的 `schedule-reminder-banner` 机制：把 `due_at` 在 48h 内且 `status=unsubmitted` 的任务并入提醒条。

### 2.5 凭证获取指引（写入 docs/04-real-im-accounts-guide.md 同目录新文档）

- **学习通**：用户在已登录的浏览器里 F12 → Application → Cookies → `i.chaoxing.com`，复制整串 cookie（至少含 `_uid`、`fid`）粘贴到工作台导入框。有效期数周，失效后 API 返回 `credential_invalid`，前端提示重新导入。
- **数你最灵**：登录后 F12 → Network 任一 `/api/homework/...` 请求 → 复制 `Authorization` 头的 Bearer token。token 短期有效，工作台用 `POST /api/auth/refresh` 自动续期；refresh 也失效才提示重新登录获取。

---

## 3. 实施计划（建议 4 步）

| 步骤 | 内容 | 验收 |
|---|---|---|
| S1 | `guard.py` + `storage.py` + `models.py` + 凭证 API | 单测：SSRF 拦截（localhost/私网/白名单外）、凭证加密落库 0600、零删除 |
| S2 | `chaoxing.py` 适配器（course 列表→作业解析→归一化） | 用真实 cookie 手动跑通一门课；cookie 失效正确报 `credential_invalid` |
| S3 | `smartestu.py` 适配器（token + refresh + 查询链路） | DevTools 抓包确认请求体字段后实现；401→refresh→重试链路单测 |
| S4 | `/api/assignment/*` 聚合 API + 前端【作业中心】工作区 + 到期提醒接入 | 手动全链路验收；`no-store` 注入；并发同步互斥 |

## 4. 风险与开放问题

1. **学习通作业页是 HTML 而非 JSON**：解析依赖页面结构，平台改版会断——归一化层兜底（解析失败时任务标记 `status=unknown` 并记日志，不 crash）。首次实现建议先覆盖"课程列表"这条最稳的链路，作业详情解析迭代打磨。
2. **数你最灵请求体字段未定**：bundle 是压缩混淆的，字段名（尤其 login 的密码字段）需一次真实 DevTools 抓包。这不阻塞 S1/S2。
3. **风控**：学习通对高频请求有限制；同步频率默认上限 1 次/5 分钟，前端不提供自动轮询（或可配置关闭）。
4. **token 自动 refresh 的幂等**：`/api/auth/refresh` 多端同时刷新可能互踢，工作台刷新失败一次即放弃并标 `credential_invalid`，不进入刷新风暴。
5. 伦理边界：明确只读聚合与提醒，不做自动答题/代交，避免违反平台条款与学术诚信问题。
