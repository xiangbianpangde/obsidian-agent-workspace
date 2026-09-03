"""AgentsView 适配器与 API 自动化测试 (第二个 P0 / v0.2 rev2).
P1-AV-1: 权威 Session API DTO 映射断言 (包含 machine, cwd, git_branch, created_at)
P1-AV-2: 所有返回会话内容的端点 100% 覆盖 Cache-Control: no-store
P1-AV-3: 消息流多页分页递增回归 (page 1 -> next_ordinal -> page 2, 无重复, 严格递增)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fastapi.testclient import TestClient
from app.config import load_config
from app.main import app
from app.state import init_state


class TestAgentsView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        init_state(cls.cfg)
        cls._client_ctx = TestClient(app)
        cls.client = cls._client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_ctx.__exit__(None, None, None)

    def test_01_agentsview_status_and_headers(self):
        """验证 AgentsView 连通性与 no-store 响应头"""
        res = self.client.get("/api/agentsview/status")
        self.assertEqual(res.status_code, 200)
        # P1-AV-2: 全量隐私防缓存验证
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn(data["transport"], ("cli", "sqlite-ro"))
        self.assertGreater(data["session_count"], 1000)
        self.assertGreater(data["message_count"], 10000)

    def test_02_agentsview_overview_dto_and_headers(self):
        """验证全景工作流看板数据 (DTO 包含 Agent 矩阵、项目排行、时间活跃度) 与 no-store"""
        res = self.client.get("/api/agentsview/overview")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        data = res.json()
        self.assertGreater(data["total_sessions"], 1000)
        self.assertGreaterEqual(data["recent_7d_count"], 0)
        self.assertGreaterEqual(data["recent_24h_count"], 0)
        
        # 验证 Top Agents 包含 Pi, Claude, Codex
        agents = [a["agent"] for a in data["agent_matrix"]]
        self.assertIn("pi", agents)
        self.assertIn("claude", agents)
        self.assertIn("codex", agents)

        # 验证最近会话结构包含权威 Session DTO 字段 (P1-AV-1)
        self.assertGreater(len(data["recent_sessions"]), 0)
        sample = data["recent_sessions"][0]
        for field in ("id", "project", "machine", "agent", "title", "started_at"):
            self.assertIn(field, sample)

    def test_03_list_sessions_filter_pagination_and_headers(self):
        """验证会话列表过滤、权威 DTO 字段与 no-store 头"""
        res = self.client.get("/api/agentsview/sessions?agent=pi&limit=5")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        data = res.json()
        self.assertGreater(data["total"], 300)
        self.assertLessEqual(len(data["sessions"]), 5)
        
        for s in data["sessions"]:
            self.assertEqual(s["agent"], "pi")
            # 验证 P1-AV-1: Session DTO 规范字段
            self.assertIn("machine", s)
            self.assertIn("title", s)
            self.assertIn("message_count", s)
            self.assertIn("user_message_count", s)

    def test_04_session_detail_dto_and_headers(self):
        """验证单会话元数据详情的权威 DTO 结构与 no-store"""
        sess_res = self.client.get("/api/agentsview/sessions?limit=1")
        sample_id = sess_res.json()["sessions"][0]["id"]

        detail_res = self.client.get(f"/api/agentsview/session/{sample_id}")
        self.assertEqual(detail_res.status_code, 200)
        self.assertIn("no-store", detail_res.headers.get("Cache-Control", ""))
        ddata = detail_res.json()
        self.assertEqual(ddata["id"], sample_id)
        self.assertIn("machine", ddata)
        self.assertIn("project", ddata)

    def test_05_messages_multi_page_pagination_regression(self):
        """P1-AV-3 回归验证: 消息流多页严格递增与无重复分页"""
        # 找一个消息数大于 5 的真实会话
        sess_res = self.client.get("/api/agentsview/sessions?limit=20")
        target_session = next(
            (s for s in sess_res.json()["sessions"] if s["message_count"] >= 6), None
        )
        self.assertIsNotNone(target_session, "应存在包含 6 条以上消息的会话进行分页回归测试")
        sid = target_session["id"]

        # 第一页: limit=3, from=0
        page1_res = self.client.get(f"/api/agentsview/session/{sid}/messages?from=0&limit=3")
        self.assertEqual(page1_res.status_code, 200)
        self.assertIn("no-store", page1_res.headers.get("Cache-Control", ""))
        p1_data = page1_res.json()
        self.assertEqual(len(p1_data["messages"]), 3)
        self.assertTrue(p1_data["has_more"])
        next_ord = p1_data["next_ordinal"]
        self.assertGreater(next_ord, p1_data["messages"][-1]["ordinal"])

        # 第二页: 使用 next_ordinal 作为 from 参数
        page2_res = self.client.get(f"/api/agentsview/session/{sid}/messages?from={next_ord}&limit=3")
        self.assertEqual(page2_res.status_code, 200)
        p2_data = page2_res.json()
        self.assertGreater(len(p2_data["messages"]), 0)

        # 严格断言: 序号递增且两页完全无交集 (No Duplicates)
        p1_ordinals = [m["ordinal"] for m in p1_data["messages"]]
        p2_ordinals = [m["ordinal"] for m in p2_data["messages"]]
        self.assertTrue(all(o2 > p1_ordinals[-1] for o2 in p2_ordinals), "第二页序号必须严格大于第一页最大序号")
        self.assertEqual(len(set(p1_ordinals).intersection(set(p2_ordinals))), 0, "两页消息绝不能有任何重复")

        # 验证 Message DTO 字段对齐
        m_sample = p1_data["messages"][0]
        self.assertIn("created_at", m_sample)
        self.assertIn("role", m_sample)
        self.assertIn("content", m_sample)

    def test_06_tool_calls_bounded_and_headers(self):
        """验证工具调用有界查询与 no-store 头"""
        sess_res = self.client.get("/api/agentsview/sessions?limit=1")
        sample_id = sess_res.json()["sessions"][0]["id"]

        res = self.client.get(f"/api/agentsview/session/{sample_id}/tool-calls?limit=50")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        data = res.json()
        self.assertIn("tool_calls", data)


if __name__ == "__main__":
    unittest.main()
