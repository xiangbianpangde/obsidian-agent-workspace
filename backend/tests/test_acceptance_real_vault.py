"""本地真实大规模 Vault (2400+ md) 压力与真实度测试套件.
仅在本地存在真实 Vault (/Users/xbpd/Documents/xbpd_obsidian) 时运行；
外部开源 CI / 审核环境若无真实库则安全跳过，不阻塞通用构建。
"""
from __future__ import annotations

import hashlib
import os
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


class TestAcceptanceRealVault(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.cfg = load_config()
        except Exception:
            raise unittest.SkipTest("未能加载默认 config.yaml，跳过真实大库测试")

        # 检查是否为拥有 2000+ 文件的真实大库
        if not cls.cfg.vault_path.is_dir() or "Documents" not in str(cls.cfg.vault_path):
            raise unittest.SkipTest("当前未指向真实个人 Vault，跳过大库特定断言")

        init_state(cls.cfg)
        cls._client_ctx = TestClient(app)
        cls.client = cls._client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_client_ctx"):
            cls._client_ctx.__exit__(None, None, None)

    def test_real_vault_scale_and_index(self):
        """真实大库规模验证：2000+ 笔记与 1000+ 标签"""
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreater(data["files"], 2000)
        self.assertGreater(data["tags"], 1000)

    def test_real_vault_agents_contract(self):
        """真实大库核心契约文档单快照读取"""
        res = self.client.get("/api/file/content?path=AGENTS.md")
        self.assertEqual(res.status_code, 200)
        cdata = res.json()
        self.assertEqual(cdata["path"], "AGENTS.md")
        expected_sha = hashlib.sha256(cdata["raw"].encode("utf-8")).hexdigest()
        self.assertEqual(cdata["hash"], expected_sha)


if __name__ == "__main__":
    unittest.main()
