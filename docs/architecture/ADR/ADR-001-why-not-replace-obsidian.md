# ADR-001: 为什么是增强层而非替代 Obsidian (Why Not Replace Obsidian)

- **状态**: Accepted
- **日期**: 2026-09-02
- **决策者**: 用户 & Sol (GPT-5.6 Sol High)

## 背景与问题
在设计个人工作台时，有两种截然不同的架构路线：
1. 路线 A：克隆 Obsidian，自研完整的本地 Markdown 编辑、插件系统与图谱引擎；
2. 路线 B：基于用户现有的 Obsidian 本地 Vault，构建轻量、可供人类与未来 AI Agent 共同操作的 **Personal Knowledge Workspace**。

用户的真实 Vault 拥有 2400+ 篇 Markdown 笔记，具备成熟的 PARA 分类体系、Dataview 查询、Excalidraw 绘图以及 Templater 自动化流。若选择路线 A，本质是在重复造轮子，陷入对 Obsidian 庞大插件生态的泥潭中。

## 决策内容
我们明确放弃克隆 Obsidian 的路线，将项目定位为 **Vault Intelligence Layer + Governed Operation Layer**。
工作台专注于提供：
- 毫秒级的标签与元数据索引；
- 标签生命周期看板与工作流状态整理；
- 与 Obsidian 本地文件系统安全共存的双写保护与乐观锁；
- 供未来 Agent 调用的统一知识感知与文件操作接口。

## 收益与代价
- **收益**：架构轻量、高内聚，零破坏性兼容用户已有全部笔记与配置，开发周期短、稳定性极高。
- **代价**：复杂的动态插件（如 Dataview 运行时、Templater 任意 JS 执行）在工作台中采用优雅降级展示，需在 Obsidian 原生端完成动态计算。
