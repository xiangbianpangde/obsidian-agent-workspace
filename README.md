# Obsidian Agent Workspace (个人知识工作台)

> **Personal Knowledge Workspace = Vault Intelligence Layer + Governed Operation Layer**  
> 基于本地 Obsidian Vault 的增强型个人知识工作台，为人类开发者与未来 AI Agent 协作提供统一知识接口。

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
   - 支持 Edit、Preview、Split 双栏同步视图切换；
   - 维基链接 `[[Note]]` 高亮解析并支持在工作台中跨文件点击直达；
   - Obsidian Dataview 查询块、`.base` 嵌入块优雅降级展示；
4. **Templater 模板化创建体系**：
   - 原生支持用户本地的 16 个真实 Obsidian Templater 模板；
   - `<% tp.date.now() %>` 变量自动计算与字面量偏移量支持；
   - `<% tp.file.title %>` 与 `<% tp.file.path %>` 两阶段渲染，支持自定义变量 `vars`；
   - 自动从 `<% await tp.file.move(...) %>` 中解析目标目录建议，新笔记直接在目标路径落位并剔除悬挂移动代码；
   - 复杂 JavaScript 逻辑块（如艾宾浩斯复习算法）严格采用 **Fail-Closed 降级保护**，原样保留并在 Obsidian 中执行；
5. **银行级资产安全底线**：
   - **全局禁止删除**：代码层与路由中完全不存在任何 Delete 操作，杜绝知识资产误损；
   - **SHA256 乐观锁并发防覆盖**：保存时严格比对单快照 `expected_hash`，若检测到在 Obsidian 外部被修改，立即返回 `409 Conflict` 并阻断保存；
   - **原子文件写入与创建**：基于 `open(..., "x")`（O_EXCL）防止创建同名笔记覆盖；基于同目录临时文件 + `os.replace` 保证原子更新；
   - **敏感密钥隔离**：扫描器与读取 API 双重运行正则密钥拦截器，`.obsidian` 配置区与含 Secret 笔记绝不泄露到界面中。

---

## 🏗️ 系统整体架构

```text
                     浏览器客户端 (Web Browser)
                                │
               ┌────────────────┴────────────────┐
               │    Obsidian Agent Workspace     │
               │   (三栏现代化 SPA / CodeMirror)   │
               └────────────────┬────────────────┘
                                │ REST (127.0.0.1:8787)
            ┌───────────────────┴───────────────────┐
            │          FastAPI 后端服务             │
            ├───────────────────────────────────────┤
            │  /api/files  ·  /api/tags  ·  /api/...│
            ├───────────────────┬───────────────────┤
            │  Path Guard       │ Template Engine   │
            │  (Operation-Aware)│ (Templater 子集)  │
            ├───────────────────┴───────────────────┤
            │      Vault Scanner & Watcher          │
            │      (Secret Detector 首层拦截)       │
            └───────────┬───────────────────┬───────┘
                        │                   │
                        ▼                   ▼
            ┌──────────────────────┐  ┌─────────────┐
            │   SQLite 知识索引库  │  │ 本地用户    │
            │ (files/tags/metadata)│  │ Obsidian    │
            │   data/vault.db      │  │ Vault 知识库│
            └──────────────────────┘  └─────────────┘
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

本项目包含完备的自动化测试套件，全面覆盖 P0 验收标准：

```bash
# 运行全部测试套件
python -m unittest discover -s backend/tests -v
```

测试矩阵：
- `test_secret_detector.py`：密钥模式识别与普通环境变量误伤回归测试 (4/4 PASS)
- `test_template_engine.py`：两阶段渲染、日期字面量与偏移计算、JS 块降级、动态参数 Fail-Closed (9/9 PASS)
- `test_acceptance_p0.py`：端到端 P0 完整验收套件 (8/8 PASS)：
  * ✅ 真实 Vault 连接与秒级索引
  * ✅ 嵌套文件树与单快照读取
  * ✅ 标签热度排行与状态分布分组
  * ✅ 编辑保存与 409 乐观锁冲突拦截
  * ✅ 原子 O_EXCL 创建防覆盖
  * ✅ 模板化创建与自动落位解析
  * ✅ 复杂 JS 块优雅降级保护
  * ✅ 路径穿越、排除区读取、模板写保护与 Secret 读取拒绝

---

## 📚 架构设计与决策记录 (ADRs)

关于本系统的核心架构权衡与技术推导，请参阅：
- [技术方案 v0.2（开发合同）](docs/01-tech-design-v0.2.md)
- [ADR-001: 为什么是知识操作层而非替代 Obsidian](docs/architecture/ADR/ADR-001-why-not-replace-obsidian.md)
- [ADR-002: 为什么系统层全局禁止删除文件](docs/architecture/ADR/ADR-002-why-no-delete.md)
- [ADR-003: 模板兼容层子集与优雅降级设计](docs/architecture/ADR/ADR-003-template-degradation.md)
- [ADR-004: 权限模型与乐观锁防覆盖边界](docs/architecture/ADR/ADR-004-permission-model.md)
