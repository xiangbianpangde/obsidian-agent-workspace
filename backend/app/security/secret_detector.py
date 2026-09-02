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
]


def looks_like_secret(content: str, max_chars: int = 256_000) -> tuple[bool, str]:
    """Return (is_secret, matched_pattern_description). Content is scanned, not stored."""
    sample = content[:max_chars]
    for i, pat in enumerate(_PATTERNS):
        m = pat.search(sample)
        if m:
            return True, f"pattern#{i}"
    return False, ""
