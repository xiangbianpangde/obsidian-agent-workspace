# Obsidian Agent Workspace

标签驱动的 Obsidian 文件工作台（P0 Demo）。方案：`docs/01-tech-design-v0.2.md`。

## 状态

- M1 Vault Scanner + 索引 —— 进行中（扫描脚本：`backend/scripts/scan.py`）
- M2 API / M3 前端 / M4 模板创建 / M5 安全验收 —— 待启动

## 快速开始（M1）

```sh
# 后端依赖（已创建 .venv）
.venv/bin/pip install -r backend/requirements.txt

# 全量扫描真实 vault（config.yaml 指向 /Users/xbpd/Documents/xbpd_obsidian）
.venv/bin/python -m backend.scripts.scan            # 一次性扫描
.venv/bin/python -m backend.scripts.scan --watch    # 扫描后增量监听
```

索引落在 `data/vault.db`（SQLite，gitignore）。安全底线：无删除 API、secret 文件不入索引、扫描排除 `.obsidian/.claudian/附件` 等。

## 注意

- 单用户本地工具（绑定 127.0.0.1），不对外暴露。
- 模板文件只读；Templater 完整 JS 块由工作台降级标记，请在 Obsidian 中运行。
