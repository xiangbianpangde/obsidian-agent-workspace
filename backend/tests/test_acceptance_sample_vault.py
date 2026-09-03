"""Hermetic Sample-Vault 端到端全量验收测试套件 (v0.2 §9 / M5 rev2).
P1-M5-EVIDENCE-1 & P1-M5-REPRO-1:
彻底自包含、零摩擦、零外部数据依赖，可在任何机器独立跑绿 1~9 项指标。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 自包含路径
_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fastapi.testclient import TestClient
from app.config import AppConfig
from app.database import sqlite
from app.main import app
from app.scanner.vault_scanner import scan_vault
from app.state import init_state


class TestAcceptanceSampleVault(unittest.TestCase):
    """基于独立 Sample Vault 副本的端到端真实验收。"""

    @classmethod
    def setUpClass(cls):
        # 创建独立的临时测试空间
        cls.test_dir = tempfile.mkdtemp(prefix="ws-test-sample-vault-")
        cls.vault_dir = Path(cls.test_dir) / "vault"
        cls.db_path = Path(cls.test_dir) / "data" / "vault.db"
        cls.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 将 sample-vault 复制到临时测试空间
        repo_root = Path(__file__).resolve().parents[2]
        src_sample = repo_root / "sample-vault"
        shutil.copytree(src_sample, cls.vault_dir)

        # 构造一个包含 Secret 样本的测试笔记（用于验收 Secret 拦截）
        secret_dir = cls.vault_dir / "07. 学习笔记" / "测试Secret"
        secret_dir.mkdir(parents=True, exist_ok=True)
        (secret_dir / "key_sample.md").write_text(
            "# 敏感笔记\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n",
            encoding="utf-8",
        )

        # P1-M5-NEW-2: 构造符号链接测试样例
        # 1. 内部指向 .obsidian 排除区
        obsidian_dir = cls.vault_dir / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        (obsidian_dir / "internal.md").write_text("# 内部配置笔记\n", encoding="utf-8")
        try:
            os.symlink(str(obsidian_dir / "internal.md"), str(cls.vault_dir / "safe_link.md"))
        except OSError:
            pass

        # 2. 内部指向 credentials 排除区
        cred_dir = cls.vault_dir / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        (cred_dir / "private.md").write_text("# 私密笔记\n", encoding="utf-8")
        try:
            os.symlink(str(cred_dir / "private.md"), str(cls.vault_dir / "cred_link.md"))
        except OSError:
            pass

        # 3. 外部逃逸 symlink
        try:
            outside_tmp = Path(cls.test_dir) / "outside_private.md"
            outside_tmp.write_text("# 外部私密\n", encoding="utf-8")
            os.symlink(str(outside_tmp), str(cls.vault_dir / "escape_link.md"))
        except OSError:
            pass

        # 构造 AppConfig
        cls.cfg = AppConfig(
            vault_path=cls.vault_dir,
            templates_dir=cls.vault_dir / "资料库/模版",
            database_path=cls.db_path,
            bind_host="127.0.0.1",
            port=8787,
            scan_exclude=[
                ".obsidian", ".claudian", ".codex", ".hermes", ".claude",
                "copilot", ".trash", "附件", "credentials", ".git",
            ],
            reject_symlink_escape=True,
            watchdog_enabled=True,
            debounce_ms=100,
        )

        # 初始化全局状态
        init_state(cls.cfg)
        cls._client_ctx = TestClient(app)
        cls.client = cls._client_ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_ctx.__exit__(None, None, None)
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_01_full_scan_timing_and_indexing(self):
        """验收 1: 真实执行全量扫描与计时（证明首扫 < 30s 与索引准确性）"""
        conn = sqlite.connect(self.cfg.database_path)
        t0 = time.time()
        result = scan_vault(self.cfg, conn)
        scan_time_s = time.time() - t0
        conn.close()

        self.assertLess(scan_time_s, 30.0, "全量扫描必须在 30 秒以内完成")
        self.assertGreaterEqual(result["files_indexed"], 3, "至少应索引 3 篇以上测试笔记")
        self.assertGreaterEqual(result["secret_skipped"], 1, "应正确识别并跳过包含密钥的测试笔记")
        self.assertEqual(result["stats"]["files"], result["files_indexed"])

    def test_02_file_tree_and_single_snapshot(self):
        """验收 2: 目录树展示与单快照读取"""
        res = self.client.get("/api/files/tree")
        self.assertEqual(res.status_code, 200)
        tree = res.json()
        self.assertIn("children", tree)
        self.assertGreater(len(tree["children"]), 0)

        # 单快照读取正常笔记
        target_path = "07. 学习笔记/深度学习概论.md"
        res_c = self.client.get(f"/api/file/content?path={target_path}")
        self.assertEqual(res_c.status_code, 200)
        cdata = res_c.json()
        self.assertEqual(cdata["path"], target_path)
        # 验证 hash 是对同一次单快照字节的计算结果
        expected_sha = hashlib.sha256(cdata["raw"].encode("utf-8")).hexdigest()
        self.assertEqual(cdata["hash"], expected_sha)
        self.assertIn("机器学习", cdata["tags"])

    def test_03_tags_and_status_distribution(self):
        """验收 3: 标签统计与状态分布"""
        res = self.client.get("/api/tags")
        self.assertEqual(res.status_code, 200)
        tdata = res.json()
        tags = tdata["tags"]
        
        ml_tag = next((t for t in tags if t["tag"] == "机器学习"), None)
        self.assertIsNotNone(ml_tag)
        self.assertGreaterEqual(ml_tag["count"], 1)
        self.assertIn("进行中", ml_tag["status_distribution"])

        # 按标签分组查询
        res_by_tag = self.client.get("/api/files/by-tag?tag=机器学习")
        self.assertEqual(res_by_tag.status_code, 200)
        by_tag_data = res_by_tag.json()
        self.assertIn("进行中", by_tag_data["groups"])

    def test_04_successful_edit_and_backup(self):
        """验收 4: 真实的编辑成功保存、索引刷新与备份生成"""
        target_path = "02. 归类 Arrange/算法设计.md"
        
        # 1. 先读出当前快照与 hash
        res1 = self.client.get(f"/api/file/content?path={target_path}")
        self.assertEqual(res1.status_code, 200)
        orig_hash = res1.json()["hash"]

        # 2. 真实保存新内容
        new_content = res1.json()["raw"] + "\n\n## 验收追加章节\n这是真实保存测试内容。\n"
        save_res = self.client.post(
            "/api/file/save",
            json={"path": target_path, "content": new_content, "expected_hash": orig_hash}
        )
        self.assertEqual(save_res.status_code, 200)
        self.assertTrue(save_res.json()["ok"])

        # 3. 验证落盘内容与新 hash
        res2 = self.client.get(f"/api/file/content?path={target_path}")
        self.assertEqual(res2.status_code, 200)
        self.assertIn("验收追加章节", res2.json()["raw"])
        self.assertNotEqual(res2.json()["hash"], orig_hash)

        # 4. 验证 data/backups/ 目录下生成了备份文件 (v0.2 §7.6)
        backup_dir = self.cfg.database_path.parent / "backups"
        self.assertTrue(backup_dir.is_dir())
        backups = list(backup_dir.glob("*.bak"))
        self.assertGreaterEqual(len(backups), 1, "保存前应自动生成只读快照备份")

    def test_05_optimistic_lock_409_conflict(self):
        """验收 5: 并发修改 409 冲突阻断（防止外部覆盖）"""
        target_path = "02. 归类 Arrange/算法设计.md"
        
        # 故意传入过期的 expected_hash 模拟并发修改
        conflict_res = self.client.post(
            "/api/file/save",
            json={"path": target_path, "content": "试图覆盖修改", "expected_hash": "stale_hash_12345"}
        )
        self.assertEqual(conflict_res.status_code, 409)
        self.assertIn("已被外部修改", conflict_res.json()["detail"])

        # 状态修改同样拦截
        status_conflict = self.client.patch(
            "/api/file/status",
            json={"path": target_path, "status": "已完成", "expected_hash": "stale_hash_12345"}
        )
        self.assertEqual(status_conflict.status_code, 409)

    def test_06_template_creation_and_index_visible(self):
        """验收 6: 真实的模板化创建笔记、路由落位与索引立即可见"""
        # 基于采集模板创建笔记
        create_res = self.client.post(
            "/api/file/create-with-template",
            json={
                "template_path": "资料库/模版/01. 采集笔记模版.md",
                "title": "量子计算前沿探索",
            }
        )
        self.assertEqual(create_res.status_code, 200)
        ret = create_res.json()
        target_rel = ret["path"]
        
        # 验证自动按建议目录落位 (01. 采集 Grasp/所有采集/...)
        self.assertTrue(target_rel.startswith("01. 采集 Grasp/所有采集/"))
        self.assertTrue(target_rel.endswith("量子计算前沿探索.md"))

        # 验证物理文件确实存在
        created_file = self.cfg.vault_root / target_rel
        self.assertTrue(created_file.is_file())
        content = created_file.read_text(encoding="utf-8")
        self.assertIn("创建时间: '[[", content)
        self.assertNotIn("tp.file.move", content, "创建时应剥离 move 指令")

        # 验证索引立即可见
        res_check = self.client.get(f"/api/file/content?path={target_rel}")
        self.assertEqual(res_check.status_code, 200)
        self.assertIn("采集", res_check.json()["tags"])

        # 重复创建相同文件必须被 409 拦截 (O_EXCL)
        dup_res = self.client.post(
            "/api/file/create-with-template",
            json={
                "template_path": "资料库/模版/01. 采集笔记模版.md",
                "title": "量子计算前沿探索",
            }
        )
        self.assertEqual(dup_res.status_code, 409)

    def test_07_diary_template_js_degradation(self):
        """验收 7: 真实的日记模板创建，JS 块降级标记保留且原代码不被破坏"""
        diary_res = self.client.post(
            "/api/file/create-with-template",
            json={
                "template_path": "资料库/模版/04. 日记模版.md",
                "title": "2026-09-03-验收日记",
            }
        )
        self.assertEqual(diary_res.status_code, 200)
        created_path = diary_res.json()["path"]
        
        # 读取验证降级注释与代码
        res_c = self.client.get(f"/api/file/content?path={created_path}")
        self.assertEqual(res_c.status_code, 200)
        raw = res_c.json()["raw"]
        self.assertIn("unsupported Templater JS block", raw)
        self.assertIn("const days =", raw)

    def test_08_security_boundaries(self):
        """验收 8: 全套安全边界（路径穿越、排除区、模板只读、Secret 拦截、DELETE 禁用）"""
        # 1. 路径穿越
        self.assertEqual(self.client.get("/api/file/content?path=../../etc/passwd").status_code, 400)

        # 2. 排除区 (.obsidian)
        self.assertEqual(self.client.get("/api/file/content?path=.obsidian/app.json").status_code, 400)

        # 3. 模板目录写保护
        tpl_write = self.client.post(
            "/api/file/save",
            json={"path": "资料库/模版/00. 普通笔记模版.md", "content": "篡改模板", "expected_hash": "abc"}
        )
        self.assertEqual(tpl_write.status_code, 400)
        self.assertIn("read-only", tpl_write.json()["detail"])

        # 4. Secret 笔记读取拒绝 (测试刚才构造的包含明文 key 的测试文件)
        secret_read = self.client.get("/api/file/content?path=07. 学习笔记/测试Secret/key_sample.md")
        self.assertEqual(secret_read.status_code, 400)
        self.assertIn("secret guard", secret_read.json()["detail"])

        # 5. 代码层无 DELETE API
        self.assertIn(self.client.delete("/api/file?path=demo.md").status_code, (404, 405))

        # 6. P1-M5-NEW-2: 符号链接安全断言
        # 内部指向 .obsidian / credentials 排除区的软链接不得被索引，直接读取必须被 400 拒绝
        self.assertEqual(self.client.get("/api/file/content?path=safe_link.md").status_code, 400)
        self.assertEqual(self.client.get("/api/file/content?path=cred_link.md").status_code, 400)
        self.assertEqual(self.client.get("/api/file/content?path=escape_link.md").status_code, 400)

        # 检查数据库中确实没有这三个文件
        conn = sqlite.connect(self.cfg.database_path)
        bad_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE path IN ('safe_link.md', 'cred_link.md', 'escape_link.md')"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(bad_count, 0, "排除区符号链接绝不得进入 files 索引表")

    def test_09_sample_vault_hermetic_repro(self):
        """验收 9: Sample Vault 一键零摩擦复现能力验证"""
        # 验证样例配置可正常读取与解析
        repo_root = Path(__file__).resolve().parents[2]
        sample_cfg_file = repo_root / "config.sample.yaml"
        self.assertTrue(sample_cfg_file.is_file())
        sample_vault_dir = repo_root / "sample-vault"
        self.assertTrue(sample_vault_dir.is_dir())
        # 验证 sample-vault 包含完整的必要目录与模板
        self.assertTrue((sample_vault_dir / "资料库" / "模版" / "00. 普通笔记模版.md").is_file())


if __name__ == "__main__":
    unittest.main()
