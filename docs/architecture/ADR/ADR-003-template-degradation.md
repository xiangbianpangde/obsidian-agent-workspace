# ADR-003: 模板兼容层子集与优雅降级设计 (Template Degradation)

- **状态**: Accepted
- **日期**: 2026-09-03
- **决策者**: 用户 & Sol (GPT-5.6 Sol High)

## 背景与问题
用户在 Obsidian 中广泛使用了 Templater 社区插件，模版内不仅包含常规日期变量，还包含了复杂的 JavaScript 逻辑块（如基于斐波那契/艾宾浩斯复习算法计算复习日期的 `<%* const days = [...] ... tR += ... %>`），以及移动文件的指令 `<% await tp.file.move(...) %>`。
若工作台尝试完整模拟 Templater，需要实现沙箱化的 Node.js / QuickJS 执行器及模拟全部 Obsidian 内部 API，范围急剧膨胀且极易引入安全漏洞与执行差异。

## 决策内容
采用 **Template Compatibility Layer (三层子集 + 严格 Fail-Closed 降级)** 策略：
1. **Layer 1（变量级支持）**：
   - 静态/字面量 `tp.date.now(fmt, offset, ref)` 正常求值替换；
   - `tp.file.title` 与 `tp.file.path` 按照严格两阶段渲染逻辑，分别替换为用户拟定标题与计算出的真实落位路径；
   - 支持自定义变量 `vars`（`<% tp.user.var %>` 与 `{{var}}`）；
2. **Layer 2（语义转换）**：
   - `tp.file.move("目标目录" + tp.file.title)` 不执行移动，而是将其解析为目标落位路径 `create_target_path`，在创建时直接落位于该目录，并在最终文件中自动剥离该行代码，避免悬挂残留；
3. **Layer 3（Fail-Closed 降级）**：
   - 含有 `<%* ... %>` 纯 JS 块的模板，一律标记为 `supported_level: "degraded"`；
   - 动态变量日期参数（如 `-day`, `baseDate`）与未识别的 inline 标签一律不作猜测执行，完整保留原表达式；
   - 创建笔记时保留原 JS 代码，并注入注释 `<!-- workspace: unsupported Templater JS block (will execute in Obsidian) -->`，由用户在 Obsidian 中无损继续运行。

## 收益与代价
- **收益**：以数十行精简 Python 代码完美打通了用户现有的全部 16 个真实模板，零安全沙箱风险。
- **代价**：复杂的纯 JS 动态脚本在 Web 端无法预计算，保留到 Obsidian 端执行。
