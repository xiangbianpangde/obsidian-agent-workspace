"""AgentsView 适配器与 API 自动化测试 (第二个 P0 / v0.2).
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

    def test_01_agentsview_status(self):
        """验证 AgentsView 连通性与基本元数据"""
        res = self.client.get("/api/agentsview/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("v0.40", data["version"])
        self.assertEqual(data["transport"], "sqlite-ro")
        self.assertGreater(data["session_count"], 1000, "应检测到 1000+ 场历史会话")
        self.assertGreater(data["message_count"], 10000, "应检测到 10000+ 条消息")

    def test_02_agentsview_overview(self):
        """验证全景工作流看板数据 (Agent 矩阵、项目排行、时间活跃度)"""
        res = self.client.get("/api/agentsview/overview")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreater(data["total_sessions"], 1000)
        self.assertGreaterEqual(data["recent_7d_count"], 0)
        
        # 验证 Top Agents 包含 Pi, Claude, Codex
        agents = [a["agent"] for a in data["agent_matrix"]]
        self.assertIn("pi", agents)
        self.assertIn("claude", agents)
        self.assertIn("codex", agents)

        # 验证项目排行存在
        self.assertGreater(len(data["project_ranking"]), 0)

    def test_03_list_sessions_filter_and_pagination(self):
        """验证会话列表的按 Agent 过滤与有界分页"""
        # 过滤 Pi
        res_pi = self.client.get("/api/agentsview/sessions?agent=pi&limit=5")
        self.assertEqual(res_pi.status_code, 200)
        data_pi = res_pi.json()
        self.assertGreater(data_pi["total"], 300)
        self.assertLessEqual(len(data_pi["sessions"]), 5)
        for s in data_pi["sessions"]:
            self.assertEqual(s["agent"], "pi")
            self.assertIn("title", s)
            self.assertIn("message_count", s)

        # 过滤 Codex
        res_codex = self.client.get("/api/agentsview/sessions?agent=codex&limit=5")
        self.assertEqual(res_codex.status_code, 200)
        data_codex = res_codex.json()
        self.assertGreater(data_codex["total"], 200)

    def test_04_session_messages_bounded_and_no_store(self):
        """验证单会话消息流有界分页与 Cache-Control: no-store 隐私保护"""
        # 获取一个会话 ID
        sess_res = self.client.get("/api/agentsview/sessions?limit=1")
        sample_id = sess_res.json()["sessions"][0]["id"]

        # 获取消息流
        msg_res = self.client.get(f"/api/agentsview/session/{sample_id}/messages?limit=10")
        self.assertEqual(msg_res.status_code, 200)
        # 验证隐私响应头
        self.assertIn("no-store", msg_res.headers.get("Cache-Control", ""))

        data = msg_res.json()
        self.assertEqual(data["session_id"], sample_id)
        self.assertIn("messages", data)
        self.assertLessEqual(len(data["messages"]), 10)

    def test_05_session_tool_calls(self):
        """验证会话工具调用接口与 no-store 响应头"""
        sess_res = self.client.get("/api/agentsview/sessions?limit=1")
        sample_id = sess_res.json()["sessions"][0]["id"]

        res = self.client.get(f"/api/agentsview/session/{sample_id}/tool-calls")
        self.assertEqual(res.status_code, 200)
        self.assertIn("no-store", res.headers.get("Cache-Control", ""))
        self.assertIn("tool_calls", res.json())


if __name__ == "__main__":
    unittest.main()
