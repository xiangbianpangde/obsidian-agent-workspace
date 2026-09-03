"""First-layer secret detector: secrets never reach the parser (v0.2 §7.5)."""
from __future__ import annotations

import re

# 只在"疑似密钥赋值/明文密钥"形态上命中，降低误伤（AGENTS.md 等治理文本本身不含这些形态）
_PATTERNS: list[re.Pattern] = [
    # 常见密钥赋值：key = "value" / key: "value"（值 >= 10 chars）
    re.compile(
        r"""(?i)(api[_-]?key|apikey|access[_-]?token|secret[_-]?key|secret[_-]?token|
            password|passwd|private[_-]?key|auth[_-]?token|bearer)\s*[:=]\s*
            ["'][^"']{10,}["']""",
        re.VERBOSE,
    ),
    # PEM 私钥块
    re.compile(r"BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"),
    # 常见明文密钥前缀
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # env 风格：无引号赋值 OPENAI_API_KEY=sk-... / KEY=value (Sol M1-D；单反斜杠)
    re.compile(r"(?m)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:_(?:KEY|TOKEN|SECRET|PASSWORD))\s*=\s*\S{8,}"),
]


def looks_like_secret(content: str) -> tuple[bool, str]:
    """Return (is_secret, matched_pattern_description). Content is scanned in full, not stored (P1-M5-NEW-1)."""
    for i, pat in enumerate(_PATTERNS):
        m = pat.search(content)
        if m:
            return True, f"pattern#{i}"
    return False, ""
