# Obsidian Agent Workspace 技术方案 v0.1（用户原文，待修订为 v0.2）

> 本文件是用户 2026-09-02 提交的方案原文，作为 v0.2 修订的输入基线。

## 1. 项目概述

### 1.1 项目名称
**Obsidian Agent Workspace（个人知识工作台）**

### 1.2 项目定位
基于用户本地 Obsidian Vault 的增强型工作台。不替代 Obsidian，而是在 Obsidian 之上提供：

- Vault 文件统一管理
- Markdown 在线阅读与编辑
- 标签与知识结构分析
- 模板化文件创建
- 为未来 Agent 接入提供统一知识接口

核心目标：将个人 Obsidian 知识库转化为可被人和 Agent 共同操作的 Personal Knowledge Workspace。

## 2. 项目背景

用户身份：人工智能专业大学生；使用 Agent 工具（pi、Codex 等）开发程序；使用 Obsidian 管理学习、研究、项目资料；已建立个人 Wiki、日记系统、模板系统和 Agent 工作流。

当前问题：知识孤岛 —— Obsidian（知识沉淀）、Agent（任务执行）、IM（外部信息）三者之间缺少统一工作入口。

## 3. 项目目标

### P0-1 Obsidian Vault 接入
连接本地 Obsidian Vault。支持：扫描 Markdown 文件、获取文件路径、获取文件标签、获取 Frontmatter 元数据、建立索引。

### P0-2 Markdown 渲染与编辑
Web 工作台完成 Markdown 文件查看/编辑/保存。支持：实时渲染、代码高亮、图片引用、Obsidian Wiki Link。

### P0-3 Vault 文件管理
展示 Obsidian 文件目录。支持：浏览目录、打开文件、创建文件、创建目录。禁止：删除文件（Obsidian Vault 是核心知识资产，Create/Read/Update 允许，Delete ×）。

## 4. 系统整体架构

```
                    User
                     |
          Obsidian Agent Workspace
                     |
        ----------------------------
        |                          |
   Frontend                  Backend
 React/Vite                 FastAPI
                                   |
                     ---------------------
                     |                   |
              Vault Scanner        Knowledge Index
                     |
              Local Obsidian Vault
```

## 5. 技术架构设计

前端：React + Vite + Tailwind CSS + CodeMirror 6 + remark/rehype + Zustand。
模块：FileExplorer（文件树）、MarkdownEditor、Preview（渲染）、TagPanel（标签分类）、Workspace（工作台主页）。

后端：Python + FastAPI + SQLite + watchdog + python-frontmatter。
结构：api（files.py / tags.py / workspace.py）、scanner（vault_scanner.py / parser.py）、database（sqlite.py）、watcher（filesystem.py）。

## 6. 数据模型设计

- files：id, path UNIQUE, filename, title, created_time, modified_time, size
- tags：id, name UNIQUE
- file_tags：file_id, tag_id
- metadata：file_id, key, value（保存 Frontmatter，如 status/priority/type）

## 7. 核心模块设计

- Vault Scanner：遍历 .md → 解析 frontmatter → 提取 tags → 写 SQLite → 生成索引
- 文件监听：watchdog 监听 CREATE/MODIFY/MOVE 触发刷新；DELETE 只记录
- Markdown Engine：标题/列表/表格/代码块/图片/Wiki Link

## 8. 用户界面设计
三栏：文件目录 | 编辑区 | 信息区（Tags、Metadata）。

## 9. 文件创建系统
兼容 Obsidian Template：目录 `Vault/Templates`（daily.md / research.md / project.md）。用户点击"新建研究文档"→ 选模板 → 替换变量 → 创建 Markdown → 刷新索引。

## 10. 安全设计
第一版：读取允许、编辑允许、创建允许、重命名可选、删除禁止。Local First：数据不上传云端。

## 11. API 设计
- GET /api/files → [{path, title, tags}]
- GET /api/file/{id}
- POST /api/file/save {path, content}
- POST /api/file/create
- GET /api/tags

## 12. 后续扩展规划
- Phase 2：知识图谱（Wiki Link 解析、文件关系、Graph View）
- Phase 3：Agent 接入（Personal Agent：读当前文件 → 理解上下文 → 辅助修改 → 生成内容）
- Phase 4：Agent Session 管理（pi、Codex、AgentView）→ Knowledge + Conversation + Task

## 13. MVP 验收标准
文件管理：连接本地 Vault、展示目录、打开 md、创建 md。内容管理：Markdown 渲染、编辑、保存。知识管理：自动读标签、标签分类展示、Frontmatter 解析。安全：禁止删除、本地存储。

## 14. 项目价值
未来扩展：Obsidian Workspace + Agent System + Message System + Task System = Personal AI Operating System。第一阶段重点不是新笔记工具，而是个人知识库与 AI Agent 之间的操作层。
