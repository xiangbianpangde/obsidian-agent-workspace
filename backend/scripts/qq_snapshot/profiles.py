"""Frozen QQ NT codec and schema profiles for local snapshot extraction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CodecProfile:
    profile_id: str
    wrapper_sha256: str
    salt_ptr_offset: int
    btree_offset: int
    read_ctx_offset: int
    write_ctx_offset: int
    cipher_key_offset: int = 0x8
    page_size: int = 4096
    kdf_iter: int = 4000
    hmac_algorithm: str = "HMAC_SHA1"
    kdf_algorithm: str = "PBKDF2_HMAC_SHA512"


# Verified against the loaded 6.9.98-51102 wrapper.node on Apple Silicon.
CODEC_PROFILES: Mapping[str, CodecProfile] = {
    "8c2ce709f5927a132f5028393028cb77e7140b751d864d4112b49a882983bbd1": CodecProfile(
        profile_id="ntqq-macos-arm64-6.9.98-51102-codec-v1",
        wrapper_sha256="8c2ce709f5927a132f5028393028cb77e7140b751d864d4112b49a882983bbd1",
        salt_ptr_offset=0x48,
        btree_offset=0x60,
        read_ctx_offset=0x68,
        write_ctx_offset=0x70,
    )
}

REQUIRED_EXPORTS = ("nt_msg.db", "group_info.db", "profile_info.db")

# This is deliberately a critical-subset profile rather than a loose table-name
# check. Added unrelated columns are tolerated; missing/type-changed critical
# fields fail closed.
CRITICAL_SCHEMA = {
    "nt_msg.db": {
        "group_msg_table": {
            "40001": ("INTEGER", 1),
            "40002": ("INTEGER", 0),
            "40003": ("INTEGER", 0),
            "40010": ("INTEGER", 0),
            "40011": ("INTEGER", 0),
            "40012": ("INTEGER", 0),
            "40013": ("INTEGER", 0),
            "40020": ("TEXT", 0),
            "40021": ("TEXT", 0),
            "40027": ("INTEGER", 0),
            "40030": ("INTEGER", 0),
            "40033": ("INTEGER", 0),
            "40050": ("INTEGER", 0),
            "40090": ("TEXT", 0),
            "40093": ("TEXT", 0),
            "40800": ("BLOB", 0),
        },
        "c2c_msg_table": {
            "40001": ("INTEGER", 1),
            "40002": ("INTEGER", 0),
            "40003": ("INTEGER", 0),
            "40010": ("INTEGER", 0),
            "40011": ("INTEGER", 0),
            "40012": ("INTEGER", 0),
            "40013": ("INTEGER", 0),
            "40020": ("TEXT", 0),
            "40021": ("TEXT", 0),
            "40027": ("INTEGER", 0),
            "40030": ("INTEGER", 0),
            "40033": ("INTEGER", 0),
            "40050": ("INTEGER", 0),
            "40090": ("TEXT", 0),
            "40093": ("TEXT", 0),
            "40800": ("BLOB", 0),
        },
    },
    "group_info.db": {
        "group_list": {"60001": ("INTEGER", 1), "60007": ("TEXT", 0)},
        "group_detail_info_ver1": {
            "60001": ("INTEGER", 1),
            "60007": ("TEXT", 0),
            "60026": ("TEXT", 0),
        },
    },
    "profile_info.db": {
        "buddy_list": {
            "1000": ("TEXT", 1),
            "1001": ("TEXT", 0),
            "1002": ("INTEGER", 0),
        },
        "profile_info_v6": {
            "1000": ("TEXT", 1),
            "1001": ("TEXT", 0),
            "1002": ("INTEGER", 0),
            "20002": ("TEXT", 0),
            "20009": ("TEXT", 0),
        },
    },
}

SCHEMA_PROFILE_ID = "ntqq-macos-6.9.98-critical-schema-v1"
NORMALIZATION_PROFILE_ID = "qq-im-normalization-v1"
LOCATOR_PROFILE_ID = "qq-locator-v1"


def critical_schema_fingerprint() -> str:
    canonical = json.dumps(CRITICAL_SCHEMA, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
