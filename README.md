# Obsidian Agent Workspace (个人知识工作台)

> **Personal Knowledge Workspace = Vault Intelligence Layer + Governed Operation Layer + Multi-IM Stream**  
> 基于本地 Obsidian Vault、AgentsView 与多端 IM 的增强型三核个人工作台，为人类开发者与未来 AI Agent 协作提供统一接口。

[![License: MIT](https://img.shields.io/badge/License-MIT-purple.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-emerald.svg)](https://fastapi.tiangolo.com/)
[![Code Style: Ruff/Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

---

## 🌟 核心特性 (P0 全部落地)

1. **Vault 秒级全量索引 & 增量监听**：
   - 采用高性能批量遍历与 SQLite WAL 架构，真实 Vault **2435 篇 Markdown 笔记 2.8 秒全量索引**完毕；
   - 内置 Watchdog 增量事件监听器（CREATE / MODIFY / MOVE / DELETE），Per-path 智能去抖与扫描互斥，毫秒级自同步；
2. **标签中心与工作流生命周期管理**：
   - 彻底划清 `tags`（主题分类属性，如 `#AI`、`#机器学习`）与 `状态`（工作流生命周期，如 `未整理`、`进行中`、`已完成`）的职责边界；
   - 自动解析 Frontmatter 元数据并展开标签热度与状态分布看板，支持按状态下钻筛选；
   - 顶栏下拉菜单支持一键无损更新笔记的 Frontmatter 状态；
3. **Markdown 在线编辑与实时渲染**：
   - 现代化深色质感三栏布局：**折叠文件树** | **代码编辑与实时渲染** | **标签知识中心**；
   - 轻量自包含单页架构（Tailwind CSS + Marked.js + Highlight.js + Lucide Icons），免除复杂前端构建链；
   - 支持 Edit、Preview、Split 双栏同步视图切换；
   - 维基链接 `[[Note]]` 高亮解析并支持在工作台中跨文件点击直达；
   - Obsidian Dataview 查询块、`.base` 嵌入块优雅降级展示；
4. **Templater 模板化创建体系**：
   - 原生支持用户本地的 16 个真实 Obsidian Templater 模板；
   - `<% tp.date.now() %>` 变量自动计算与字面量偏移量支持；
   - `<% tp.file.title %>` 与 `<% tp.file.path %>` 两阶段渲染，支持自定义变量 `vars`；
   - 自动从 `<% await tp.file.move(...) %>` 中解析目标目录建议，新笔记直接在目标路径落位并剔除悬挂移动代码；
   - 复杂 JavaScript 逻辑块（如艾宾浩斯复习算法）严格采用 **Fail-Closed 降级保护**，原样保留并在 Obsidian 中执行；
5. **工程化资产安全防御模型**：
   - **全局禁止删除**：代码层与路由中完全不存在任何 Delete 操作，杜绝知识资产误损；
   - **SHA256 乐观锁并发防覆盖**：保存时严格比对单快照 `expected_hash`，若检测到在 Obsidian 外部被修改，立即返回 `409 Conflict` 并阻断保存；
   - **原子文件写入与创建**：基于 `open(..., "x")`（O_EXCL）防止创建同名笔记覆盖；基于同目录临时文件 + `os.replace` 保证原子更新；
   - **敏感密钥隔离**：扫描器与读取 API 双重运行全量正则密钥拦截器，`.obsidian` 配置区与含 Secret 笔记绝不泄露到界面中；
7. **三核工作台与统一消息中心 IM Hub (第三个 P0 落地)**：
   - **三核无缝切换**：顶栏一键切换【知识中心 (Obsidian)】↔【AI 会话中心 (AgentsView)】↔【统一消息中心 (IM Hub)】；
   - **多源异构消息接入**：
     - **微信 (WeChat)**：基于 `wx-cli` 本地服务，支持历史追溯与 SSE 实时事件；
     - **企业微信 (WeCom)**：基于 `yichen-skills` 本地快照，支持时间戳明文快照解析；
     - **QQ**：基于 `Zhin.js` 入站事件框架，单向认证推送，工作台零发信凭证；
   - **本地私密 Derived IM Journal (`im_hub.db`)**：
     - 独立本地派生存储（`authority=derived`，`0700` 目录，`0600` 数据库与 WAL/SHM 文件，绝不污染 Obsidian Vault）；
     - 终极物理约束 `UNIQUE(source, account_id, dedupe_key)`；
     - `synthetic_v1` 物理定位符不变量与服务端规范化 SHA-256 Digest 信任根，杜绝重复与冲突覆盖；
   - **跨平台时间线流 (Timeline Feed) 与 Keysets 游标分页**：
     - 按 `occurred_at_epoch_ms DESC, ingest_seq DESC` 毫秒级统一时间线排序；
   - **确定性聚焦规则看板 (Focus Feed for Students)**：
     - 自动提炼高亮学校群、班级群通知、@全体成员 与辅导员私聊，明确展示高亮原因，防止大学生遗漏重要学业日程；
   - **原生浏览器可靠 SSE 与 Resync 穷尽分页门禁**：
     - 单调自增 `ingest_seq`，支持 `Last-Event-ID` 与 `?after_seq=` 开区间双游标恢复；
     - 遇到 `resync_required` 立即 `close()` 杜绝原生重连竞态，执行快照穷尽循环分页加载，实现 0 消息盲区；
   - **全量隐私与安全铁律**：
     - 绝对只读不发信，全端点注入 `Cache-Control: no-store`；正文使用纯文本数据绑定渲染，彻底杜绝聊天 XSS；
     - 包含可机械执行的 AT-1 ~ AT-9 自动化验收测试套件（全绿通过）。

---

## 🏗️ 系统整体架构

```text
                     浏览器客户端 (Web Browser)
                                │
         ┌──────────────────────┼──────────────────────┐
         ▼                      ▼                      ▼
  【知识中心 (Obsidian)】 【AI 会话中心 (AgentsView)】 【统一消息中心 (IM Hub)】
   2435+ 笔记 / 标签 / 状态  1821 场会话 / 20万+ 消息     微信 · 企微 · QQ 聚合流
         │                      │                      │
         │ REST (127.0.0.1:8787)│                      │ SSE & REST
         ▼                      ▼                      ▼
  /api/files, /api/tags  /api/agentsview/*      /api/im/*, /internal/im/*
         │                      │                      │
         ▼                      ▼                      ▼
  Obsidian 本地 Vault    AgentsView sessions.db  本地私密 IM Journal (0600)
                         (mode=ro 只读直连)      (dedupe / timeline / rules)
```

---

## 🚀 快速开始

### 方式一：连接真实 Vault（推荐）

1. **环境准备**：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r backend/requirements.txt
   ```

2. **配置 Vault 路径**：
   检查或修改 `config.yaml`：
   ```yaml
   vault:
     path: /Users/xbpd/Documents/xbpd_obsidian   # 指向你的实际 Vault 路径
   templates:
     dir: 资料库/模版
   ```

3. **全量扫描与建立索引**：
   ```bash
   python -m backend.scripts.scan
   # 输出示例: scan done: files=2435 tags=1480 secret_skipped=10 duration=2800ms
   ```

4. **启动工作台服务**：
   ```bash
   python -m backend.scripts.serve
   ```
   在浏览器中打开：**http://127.0.0.1:8787** 即可立即使用！

---

### 方式二：Sample-Vault 快速复现（用于开源审核与评估）

项目内置了脱敏的最小化知识库 `sample-vault/`，无需本地真实数据即可一键复现全部功能：

```bash
# 1. 复制样例配置
cp config.sample.yaml config.yaml

# 2. 索引样例知识库
python -m backend.scripts.scan

# 3. 运行服务
python -m backend.scripts.serve
```

---

## 🧪 自动化验收测试

本项目包含完备的 30 项自动化测试套件，全面覆盖 P0-1 与 P0-2 验收标准：

```bash
# 运行全部测试套件 (耗时 < 1s)
python -m unittest discover -s backend/tests -v
```

测试矩阵：
- `test_agentsview.py`：第二个 P0 核心测试 (5/5 PASS)：
  * ✅ 1. 自动探测并接入 `~/.agentsview/sessions.db` 与 CLI，返回版本与连通性
  * ✅ 2. 工作流全景看板（12 种 Agent 矩阵、项目排行榜、近 7 天活跃度）
  * ✅ 3. 会话列表按 Agent（如 Pi/Codex）及项目筛选与有界分页
  * ✅ 4. 单会话消息流有界分页拉取与 `Cache-Control: no-store` 隐私保护
  * ✅ 5. 会话工具调用（Tool Calls）记录回溯与 no-store 保护
- `test_secret_detector.py`：密钥模式识别、单反斜杠转义与 >256k 全量扫描防绕过回归测试 (5/5 PASS)
- `test_template_engine.py`：两阶段渲染、日期字面量与偏移计算、JS 块降级、动态参数 Fail-Closed (9/9 PASS)
- `test_acceptance_sample_vault.py`：基于独立 Sample-Vault 副本的端到端 P0 完整验收套件 (9/9 PASS)
- `test_acceptance_real_vault.py`：本地真实大库 (2400+ md) 规模与核心契约读取验证 (2/2 PASS，外部环境自动跳过)

---

## 📚 架构设计与决策记录 (ADRs)

关于本系统的核心架构权衡与技术推导，请参阅：
- [技术方案 v0.2（开发合同）](docs/01-tech-design-v0.2.md)
- [ADR-001: 为什么是知识操作层而非替代 Obsidian](docs/architecture/ADR/ADR-001-why-not-replace-obsidian.md)
- [ADR-002: 为什么系统层全局禁止删除文件](docs/architecture/ADR/ADR-002-why-no-delete.md)
- [ADR-003: 模板兼容层子集与优雅降级设计](docs/architecture/ADR/ADR-003-template-degradation.md)
- [ADR-004: 权限模型与乐观锁防覆盖边界](docs/architecture/ADR/ADR-004-permission-model.md)
