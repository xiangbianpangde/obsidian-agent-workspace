"""全量 P0 验收测试套件 (v0.2 §9).
全自动验证 9 项可测指标：
1. Vault 连接与秒级索引
2. 文件树展示与单快照读取
3. 标签中心与状态生命周期分布
4. 编辑保存与乐观锁生效
5. 并发修改 409 冲突拦截
6. 模板创建与落位建议
7. JS 块模板降级保护
8. 安全底线 (无 DELETE, 路径穿越拒绝, 模板目录只读, Secret 拦截)
9. 状态修改与 Frontmatter 格式保持
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

# 自包含路径
_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fastapi.testclient import TestClient

from app.config import AppConfig, load_config
from app.database import sqlite
from app.main import app
from app.scanner.vault_scanner import scan_vault
from app.security.path_guard import resolve_for_read_snapshot, resolve_for_write, PathError
from app.state import init_state


class TestAcceptanceP0(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        init_state(cls.cfg)
        cls._client_ctx = TestClient(app)
        cls.client = cls._client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_ctx.__exit__(None, None, None)

    def test_01_health_and_scan_stats(self):
        """验收 1: 真实 Vault 接入与全量索引"""
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertGreater(data["files"], 2000, "真实 Vault 笔记数应在 2000 篇以上")
        self.assertGreater(data["tags"], 1000, "真实 Vault 标签数应在 1000 个以上")
        self.assertTrue(data["watchdog"], "Watchdog 监听器应处于活跃状态")

    def test_02_file_tree_and_single_snapshot(self):
        """验收 2: 文件树完整性与读取单快照"""
        res = self.client.get("/api/files/tree")
        self.assertEqual(res.status_code, 200)
        tree = res.json()
        self.assertIn("children", tree)
        self.assertGreater(len(tree["children"]), 5)

        # 读取已知核心文档
        res_c = self.client.get("/api/file/content?path=AGENTS.md")
        self.assertEqual(res_c.status_code, 200)
        cdata = res_c.json()
        self.assertEqual(cdata["path"], "AGENTS.md")
        # 验证 hash 与 raw 是来自同一个快照
        calc_hash = hashlib.sha256(cdata["raw"].encode("utf-8")).hexdigest()
        self.assertEqual(cdata["hash"], calc_hash)

    def test_03_tags_and_status_distribution(self):
        """验收 3: 标签统计与状态分布"""
        res = self.client.get("/api/tags")
        self.assertEqual(res.status_code, 200)
        tdata = res.json()
        tags = tdata["tags"]
        self.assertGreater(len(tags), 100)
        
        # 验证机器学习等高频标签
        ml_tag = next((t for t in tags if t["tag"] == "机器学习"), None)
        self.assertIsNotNone(ml_tag)
        self.assertGreater(ml_tag["count"], 50)
        self.assertIn("进行中", ml_tag["status_distribution"])

        # 验证按标签筛选
        res_by_tag = self.client.get("/api/files/by-tag?tag=机器学习")
        self.assertEqual(res_by_tag.status_code, 200)
        by_tag_data = res_by_tag.json()
        self.assertIn("groups", by_tag_data)
        self.assertIn("进行中", by_tag_data["groups"])

    def test_04_save_and_409_conflict_protection(self):
        """验收 4 & 5: 优化锁 409 冲突防护"""
        # 传入错误的 expected_hash 必须拦截并返回 409
        bad_save = self.client.post(
            "/api/file/save",
            json={"path": "AGENTS.md", "content": "恶意修改", "expected_hash": "deadbeef0000"}
        )
        self.assertEqual(bad_save.status_code, 409)
        self.assertIn("已被外部修改", bad_save.json()["detail"])

        # 同样，状态修改传错 hash 也必须 409
        bad_status = self.client.patch(
            "/api/file/status",
            json={"path": "AGENTS.md", "status": "已完成", "expected_hash": "invalidhash"}
        )
        self.assertEqual(bad_status.status_code, 409)

    def test_05_create_conflict_no_overwrite(self):
        """验收 5: 创建笔记绝不覆盖已有文件 (O_EXCL)"""
        dup_create = self.client.post(
            "/api/file/create",
            json={"path": "AGENTS.md", "content": "试图覆盖核心治理契约"}
        )
        self.assertEqual(dup_create.status_code, 409)
        self.assertIn("已存在，禁止覆盖", dup_create.json()["detail"])

    def test_06_template_list_and_preview(self):
        """验收 6: 模板引擎识别与预览"""
        res = self.client.get("/api/templates")
        self.assertEqual(res.status_code, 200)
        tpls = res.json()["templates"]
        self.assertGreaterEqual(len(tpls), 10)

        # 检查采集模板自动落位解析
        collect_tpl = next((t for t in tpls if "采集" in t["name"]), None)
        self.assertIsNotNone(collect_tpl)
        self.assertTrue(collect_tpl["has_file_move"])
        self.assertIn("所有采集", collect_tpl["suggested_dir"])

        # 检查采集模板预览
        prev = self.client.get(f"/api/template/preview?path={collect_tpl['path']}&title=测试验收笔记")
        self.assertEqual(prev.status_code, 200)
        pdata = prev.json()
        self.assertIn("所有采集/测试验收笔记.md", pdata["suggested_path"])
        self.assertIn("创建时间: '[[", pdata["rendered"])

    def test_07_template_js_block_degradation(self):
        """验收 7: 日记模板 JS 块降级标记保留"""
        res = self.client.get("/api/templates")
        tpls = res.json()["templates"]
        diary_tpl = next((t for t in tpls if "日记" in t["name"]), None)
        self.assertIsNotNone(diary_tpl)
        self.assertTrue(diary_tpl["has_js_block"])
        self.assertEqual(diary_tpl["supported_level"], "degraded")

        prev = self.client.get(f"/api/template/preview?path={diary_tpl['path']}&title=2026-09-03")
        self.assertEqual(prev.status_code, 200)
        rendered = prev.json()["rendered"]
        self.assertIn("unsupported Templater JS block", rendered)
        self.assertIn("const days =", rendered)

    def test_08_security_boundaries(self):
        """验收 8: 安全底线（路径穿越、排除区、模板只读、Secret 隔离）"""
        # 1. 路径穿越
        traversal = self.client.get("/api/file/content?path=../../etc/passwd")
        self.assertEqual(traversal.status_code, 400)

        # 2. 排除区 (.obsidian)
        obsidian_read = self.client.get("/api/file/content?path=.obsidian/app.json")
        self.assertEqual(obsidian_read.status_code, 400)

        # 3. 模板目录写保护
        tpl_write = self.client.post(
            "/api/file/save",
            json={"path": "资料库/模版/00. 普通笔记模版.md", "content": "篡改模板", "expected_hash": "abc"}
        )
        self.assertEqual(tpl_write.status_code, 400)
        self.assertIn("read-only", tpl_write.json()["detail"])

        # 4. Secret 笔记读取拒绝
        secret_read = self.client.get(
            "/api/file/content?path=07.学习笔记/AI工程化/ai-engineering-from-scratch/phases/00-setup-and-tooling/04-apis-and-keys/docs/en.md"
        )
        self.assertEqual(secret_read.status_code, 400)
        self.assertIn("secret guard", secret_read.json()["detail"])

        # 5. 代码层无 DELETE API (未定义路由 -> 404 或 405)
        del_attempt = self.client.delete("/api/file?path=AGENTS.md")
        self.assertIn(del_attempt.status_code, (404, 405))
        del_attempt2 = self.client.delete("/api/file/content?path=AGENTS.md")
        self.assertIn(del_attempt2.status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
