"""P1-M2-5 regression tests: env 风格 secret pattern（单反斜杠修复验证）。
运行: .venv/bin/python -m unittest backend.tests.test_secret_detector
"""
from __future__ import annotations

import unittest

from app.security.secret_detector import looks_like_secret


class TestSecretDetector(unittest.TestCase):
    def test_env_assignment_hits(self):
        for s in [
            "OPENAI_API_KEY=abcdefghijk",
            "export GITHUB_TOKEN=abcdefghijklmnopqrstuvwxyz123456",
            "MY_SECRET_KEY = xyz123456789",
        ]:
            with self.subTest(s=s):
                hit, note = looks_like_secret(s)
                self.assertTrue(hit, f"should hit: {s} (via {note})")

    def test_normal_setting_not_hit(self):
        for s in [
            "NORMAL_SETTING=plainlongvalue",
            "TITLE=好好学习天天向上",
            "NORMAL_SETTING = abcdefghij",
        ]:
            with self.subTest(s=s):
                hit, _ = looks_like_secret(s)
                self.assertFalse(hit, f"should NOT hit: {s}")

    def test_short_value_not_hit(self):
        hit, _ = looks_like_secret("API_KEY=short")
        self.assertFalse(hit)

    def test_assignment_with_quotes_still_hits(self):
        hit, _ = looks_like_secret('api_key = "verysecret1234567890"')
        self.assertTrue(hit)


if __name__ == "__main__":
    unittest.main()
