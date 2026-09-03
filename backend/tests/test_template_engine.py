"""Template Engine 单元测试：真实 Templater 样例测试。
"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.template.engine import inspect_template, render_template


class TestTemplateEngine(unittest.TestCase):
    def test_normal_template(self):
        raw = """---
状态:
  - 未整理
  - 已整理
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
tags:
---
"""
        info = inspect_template(raw)
        self.assertFalse(info["has_js_block"])
        self.assertTrue(info["has_date"])
        self.assertEqual(info["supported_level"], "full")

        rendered, suggested_dir = render_template(
            raw, title="测试普通笔记", now_dt=datetime(2026, 9, 3)
        )
        self.assertIn("创建时间: '[[2026-09-03]]'", rendered)
        self.assertIsNone(suggested_dir)

    def test_collect_template_with_move(self):
        raw = """---
状态:
  - 已整理
  - 未整理
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
链接:
  - "[[采集笔记目录]]"
---
<% await tp.file.move ("/01. 采集 Grasp/所有采集/"+tp.file.title) %>

正文内容
"""
        info = inspect_template(raw)
        self.assertTrue(info["has_file_move"])
        self.assertEqual(info["suggested_dir"], "01. 采集 Grasp/所有采集")

        rendered, suggested_dir = render_template(
            raw, title="AI研究发现", now_dt=datetime(2026, 9, 3)
        )
        self.assertEqual(suggested_dir, "01. 采集 Grasp/所有采集")
        self.assertIn("创建时间: '[[2026-09-03]]'", rendered)
        # tp.file.move 行被安全剥离
        self.assertNotIn("tp.file.move", rendered)
        self.assertIn("正文内容", rendered)

    def test_date_offset(self):
        raw = '<% tp.date.now("YYYY-MM-DD", -1, "2026-09-03") %>'
        rendered, _ = render_template(raw, title="", now_dt=datetime(2026, 9, 3))
        self.assertEqual(rendered.strip(), "2026-09-02")

    def test_diary_js_block_degradation(self):
        raw = """---
今日机会:
---
## 复习区
<%*
const days = [1, 2, 4, 7, 15, 30];
tR += "复习内容";
%>
## 今天的笔记
"""
        info = inspect_template(raw)
        self.assertTrue(info["has_js_block"])
        self.assertEqual(info["supported_level"], "degraded")

        rendered, _ = render_template(raw, title="2026-09-03")
        # 验证降级标记与原代码保留
        self.assertIn("unsupported Templater JS block", rendered)
        self.assertIn("const days = [1, 2, 4, 7, 15, 30];", rendered)
        self.assertIn("## 今天的笔记", rendered)


if __name__ == "__main__":
    unittest.main()
