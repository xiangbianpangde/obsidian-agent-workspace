"""Template Engine 单元测试 (v0.2 §5 / M4 rev2).
测试两阶段渲染、tp.file.path 闭合、custom vars 与 fail-closed 降级。
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.template.engine import (
    compute_target_path,
    inspect_template,
    render_template,
)


class TestTemplateEngine(unittest.TestCase):
    def test_normal_template(self):
        raw = """---
状态:
  - 未整理
创建时间: '[[<% tp.date.now("YYYY-MM-DD")%>]]'
---
"""
        info = inspect_template(raw)
        self.assertFalse(info["has_js_block"])
        self.assertEqual(info["supported_level"], "full")

        rendered, suggested_dir = render_template(
            raw, title="测试普通笔记", target_path="07.学习笔记/测试普通笔记.md", now_dt=datetime(2026, 9, 3)
        )
        self.assertIn("创建时间: '[[2026-09-03]]'", rendered)
        self.assertIsNone(suggested_dir)

    def test_file_path_two_phase_render(self):
        # P1-M4-1: 验证 tp.file.path 正确填充为最终 target_path
        raw = "文件路径是: <% tp.file.path %>，标题是: <% tp.file.title %>"
        target_path = compute_target_path("算法导论", "07.学习笔记")
        self.assertEqual(target_path, "07.学习笔记/算法导论.md")

        rendered, _ = render_template(
            raw, title="算法导论", target_path=target_path
        )
        self.assertIn("文件路径是: 07.学习笔记/算法导论.md", rendered)
        self.assertIn("标题是: 算法导论", rendered)

    def test_custom_vars(self):
        # 合同收口: 支持 vars 参数
        raw = "作者: <% tp.user.author %>，学科: {{subject}}"
        rendered, _ = render_template(
            raw, title="笔记", vars={"author": "张三", "subject": "人工智能"}
        )
        self.assertEqual(rendered, "作者: 张三，学科: 人工智能")

    def test_unsupported_tag_fail_closed(self):
        # P1-M4-3: 未知标签必须标记为 degraded，且不能静默破坏原文
        raw = "标题: <% tp.file.title %>，未知: <% tp.file.unknown_prop %>"
        info = inspect_template(raw)
        self.assertTrue(info["has_unsupported_tags"])
        self.assertEqual(info["supported_level"], "degraded")

        rendered, _ = render_template(raw, title="测试")
        self.assertIn("<% tp.file.unknown_prop %>", rendered)

    def test_dynamic_date_args_fail_closed(self):
        # P1-M4-3: 动态日期参数不被猜测为今天，而是保持原样并判定为 degraded
        raw = '<% tp.date.now("YYYY-MM-DD", -day, baseDate) %>'
        info = inspect_template(raw)
        self.assertTrue(info["has_unsupported_tags"])
        self.assertEqual(info["supported_level"], "degraded")

        rendered, _ = render_template(raw, title="")
        self.assertIn('<% tp.date.now("YYYY-MM-DD", -day, baseDate) %>', rendered)

    def test_date_literal_offset(self):
        raw = '<% tp.date.now("YYYY-MM-DD", -2, "2026-09-03") %>'
        rendered, _ = render_template(raw, title="", now_dt=datetime(2026, 9, 3))
        self.assertEqual(rendered.strip(), "2026-09-01")

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
        self.assertIn("unsupported Templater JS block", rendered)
        self.assertIn("const days = [1, 2, 4, 7, 15, 30];", rendered)

    def test_dynamic_format_fail_closed(self):
        # Sol P1-M4-3: 动态 format (如 fmt) 必须判定为 degraded 且原样保留
        raw = "<% tp.date.now(fmt) %>"
        info = inspect_template(raw)
        self.assertTrue(info["has_unsupported_tags"])
        self.assertEqual(info["supported_level"], "degraded")

        rendered, _ = render_template(raw, title="")
        self.assertEqual(rendered.strip(), raw)

    def test_template_secret_detection(self):
        # P1-M4-2: 模板读取必须受 secret detector 保护
        from app.security.secret_detector import looks_like_secret
        secret_tpl = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n# 模板正文"
        hit, note = looks_like_secret(secret_tpl)
        self.assertTrue(hit)


if __name__ == "__main__":
    unittest.main()
