# 模版样例合集（Obsidian Templater 实际语法）

## 00. 普通笔记模版.md
````markdown
---
状态:
  - 未整理
  - 已整理
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
tags:
---
````

## 01. 采集笔记模版.md
````markdown
---
状态:
  - 已整理
  - 未整理
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
  - "[[📍 采集笔记目录]]"
---
<% await tp.file.move ("/01. 🟣 采集 Grasp/所有采集/"+tp.file.title) %>

````

## 02. 归类笔记模版.md
````markdown
---
状态:
  - 进行中
  - 已完成
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
  - "[[📍 归类笔记目录]]"
---
<% await tp.file.move ("/02. 🟡 归类 Arrange/所有归类/"+tp.file.title) %>

---
### 相关笔记
![[../数据库/06.链接笔记.base#表格]]
````

## 03. 表达笔记模版.md
````markdown
---
状态:
  - 进行中
  - 已完成
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
  - "[[📍 表达笔记目录]]"
---
<% await tp.file.move ("/03. 🟤 表达 Present/所有表达/"+tp.file.title) %>

---
### 相关笔记
![[../数据库/06.链接笔记.base#表格]]
````

## 04. 日记模版.md（含完整 JS 块）
````markdown
---
今日机会:
今日反思:
今日感恩:
今日照片:
---
```calendar-nav
```



## 今日任务清单 
![[../../数据库/12.任务数据库.base|11.任务数据库#未完成任务]]

# Day Planner 
* 

# Day Do
* 

# 日记复盘

## 复习区
<%*
const days = [1, 2, 4, 7, 15, 30];
const baseDate = tp.file.title; // 以当前文件的标题作为日期基础
let reviewList = [];

if (/\d{4}-\d{2}-\d{2}/.test(baseDate)) { // 检查标题是否是有效的日期格式
  days.forEach(day => {
    const reviewDate = tp.date.now("YYYY-MM-DD", -day, baseDate);
    const linkedNote = `[[${reviewDate}]]`; // 自动链接到相应日期的笔记
    reviewList.push(`- [ ] 复习 ${linkedNote} 的内容`);
  });

  tR += reviewList.join("\n");
} else {
  tR += "当前文件标题不是有效的日期格式，无法生成复习列表。";
}
%>

## 今天的笔记
![[../../资料库/数据库/06.链接笔记.base#今天创建的笔记]]]

## 历史上的今天
![[../../资料库/数据库/08.日记提取数据库.base#历史上的今天]]

## 📊 今日未完成任务（实时预览） 
```dataview 
TASK 
FROM "04. 日记周记/01. 日记"
WHERE !completed AND contains(text, "⏳") AND file.name = this.file.name 
SORT text ASC
```
````

## 秘书模板（01. 🟣 采集 Grasp/任务/秘书模板/）
- 周日记忆周审.md
- 晚间复盘.md

## 配置文件
`.obsidian/templates.json`:
```json
{
  "folder": "资料库/模版"
}
```

## 观察结论
1. 简单变量：`<% tp.date.now("YYYY-MM-DD") %>` 无空格变体 `tp.date.now("YYYY-MM-DD")%>`。
2. 带偏移：`tp.date.now("YYYY-MM-DD", -day, baseDate)` —— 需要解析参数列表。
3. 路由：`await tp.file.move (path + tp.file.title)` —— 决定新文件落点。
4. 完整 JS：`<%* ... tR += ... %>` —— 日记模版"复习区"，**无法静态求值**（依赖 tp API 与运行时）。
5. 嵌入：`![[相对路径.base#锚点]]`、`![[base|别名#锚点]]` —— 渲染器需支持或优雅降级。
6. Dataview / calendar-nav 代码块 —— 渲染器需以代码块呈现或提示"Obsidian 插件渲染"。
