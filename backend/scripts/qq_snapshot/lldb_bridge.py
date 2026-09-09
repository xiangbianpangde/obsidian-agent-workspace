"""LLDB-only bridge that returns live SQLCipher material through an inherited pipe.

This module never writes a key to disk and never prints key material. It is
loaded only by the one-shot snapshot extractor, not by the workspace runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

import lldb

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from profiles import CODEC_PROFILES, REQUIRED_EXPORTS  # noqa: E402

CHUNK = 8 * 1024 * 1024
MAX_SCAN = 6 * 1024 * 1024 * 1024


def _emit(payload: dict) -> None:
    try:
        fd = int(os.environ["QQ_SNAPSHOT_SECRET_FD"])
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        os.write(fd, data)
        os.close(fd)
    except Exception:
        pass


def _read(process, address: int, size: int) -> bytes:
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() and data is not None else b""


def _u32(process, address: int) -> int:
    data = _read(process, address, 4)
    return struct.unpack("<I", data)[0] if len(data) == 4 else 0


def _u64(process, address: int) -> int:
    data = _read(process, address, 8)
    return struct.unpack("<Q", data)[0] if len(data) == 8 else 0


def _writable_regions(process):
    address = 0
    scanned = 0
    while scanned < MAX_SCAN:
        region = lldb.SBMemoryRegionInfo()
        error = process.GetMemoryRegionInfo(address, region)
        if not error.Success():
            break
        start, end = region.GetRegionBase(), region.GetRegionEnd()
        if end <= start:
            break
        if region.IsReadable() and region.IsWritable() and not region.IsExecutable():
            yield start, end
            scanned += end - start
        address = end


def _has_loaded_wrapper(target) -> bool:
    for index in range(target.GetNumModules()):
        module = target.GetModuleAtIndex(index)
        if (module.GetFileSpec().GetFilename() or "") == "wrapper.node":
            return True
    return False


def _source_salts(fd_map: dict[str, int]) -> dict[bytes, str]:
    """用 os.pread 从父进程预打开的库文件 fd 读取 SQLCipher salt。

    全程只接受整数 fd，不做任何路径解析/open 调用——不存在路径 sink。
    """
    result: dict[bytes, str] = {}
    for name in REQUIRED_EXPORTS:
        fd = fd_map[name]
        header = os.pread(fd, 64, 0)
        if not header.startswith(b"SQLite header 3\x00") or b"QQ_NT DB" not in header:
            raise ValueError("QQ_SNAPSHOT_SOURCE_HEADER")
        salt = os.pread(fd, 16, 1024)
        if len(salt) != 16 or salt in result:
            raise ValueError("QQ_SNAPSHOT_SOURCE_SALT")
        result[salt] = name
    return result


def _find_salts(process, regions, wanted: dict[bytes, str]) -> dict[str, list[int]]:
    found = {name: [] for name in wanted.values()}
    for start, end in regions:
        cursor = start
        while cursor < end:
            data = _read(process, cursor, min(CHUNK, end - cursor))
            if not data:
                break
            for salt, name in wanted.items():
                offset = 0
                while True:
                    hit = data.find(salt, offset)
                    if hit < 0:
                        break
                    found[name].append(cursor + hit)
                    offset = hit + 1
            cursor += len(data)
    return found


def _find_pointer_refs(process, regions, salt_addresses: dict[str, list[int]]) -> dict[str, list[tuple[int, int]]]:
    needles: dict[bytes, tuple[str, int]] = {}
    for name, addresses in salt_addresses.items():
        for address in addresses:
            needles[struct.pack("<Q", address)] = (name, address)
    refs = {name: [] for name in salt_addresses}
    for start, end in regions:
        cursor = start
        while cursor < end:
            data = _read(process, cursor, min(CHUNK, end - cursor))
            if not data:
                break
            for needle, identity in needles.items():
                offset = 0
                while True:
                    hit = data.find(needle, offset)
                    if hit < 0:
                        break
                    refs[identity[0]].append((cursor + hit, identity[1]))
                    offset = hit + 1
            cursor += len(data)
    return refs


def _candidate(process, ref_address: int, salt_address: int, profile) -> dict | None:
    codec = ref_address - profile.salt_ptr_offset
    if _u32(process, codec + 0x4) != profile.kdf_iter:
        return None
    if _u32(process, codec + 0x1C) != profile.page_size:
        return None
    salt_size = _u32(process, codec + 0xC)
    key_size = _u32(process, codec + 0x10)
    if salt_size != 16 or key_size != 32:
        return None
    if _u64(process, codec + profile.salt_ptr_offset) != salt_address:
        return None
    read_ctx = _u64(process, codec + profile.read_ctx_offset)
    write_ctx = _u64(process, codec + profile.write_ctx_offset)
    if not read_ctx or not write_ctx:
        return None
    read_key_ptr = _u64(process, read_ctx + profile.cipher_key_offset)
    write_key_ptr = _u64(process, write_ctx + profile.cipher_key_offset)
    if not read_key_ptr or not write_key_ptr:
        return None
    read_key = _read(process, read_key_ptr, key_size)
    write_key = _read(process, write_key_ptr, key_size)
    salt = _read(process, salt_address, salt_size)
    if len(read_key) != key_size or read_key != write_key or not any(read_key):
        return None
    if len(salt) != salt_size or not any(salt):
        return None
    return {"key_hex": read_key.hex(), "salt_hex": salt.hex()}


def qq_secure_capture(debugger, command, result, internal_dict) -> None:
    stage = "init"
    try:
        try:
            fd_map_raw = json.loads(os.environ["QQ_SNAPSHOT_EXPORT_FDS"])
            fd_map = {str(k): int(v) for k, v in fd_map_raw.items()}
        except (KeyError, ValueError, TypeError, AttributeError):
            _emit({"error_code": "QQ_SNAPSHOT_SOURCE_MISSING"})
            return
        if set(fd_map) != set(REQUIRED_EXPORTS) or any(fd < 0 for fd in fd_map.values()):
            _emit({"error_code": "QQ_SNAPSHOT_SOURCE_MISSING"})
            return
        stage = "target"
        target = debugger.GetSelectedTarget()
        process = target.GetProcess()
        if not _has_loaded_wrapper(target):
            _emit({"error_code": "QQ_SNAPSHOT_WRAPPER_MISSING"})
            return
        wrapper_digest = os.environ.get("QQ_SNAPSHOT_WRAPPER_SHA256", "")
        profile = CODEC_PROFILES.get(wrapper_digest)
        if profile is None:
            _emit({"error_code": "QQ_SNAPSHOT_CODEC_UNSUPPORTED"})
            return

        stage = "source_salts"
        wanted = _source_salts(fd_map)
        stage = "regions"
        regions = list(_writable_regions(process))
        stage = "find_salts"
        locations = _find_salts(process, regions, wanted)
        if any(not locations[name] for name in REQUIRED_EXPORTS):
            _emit({"error_code": "QQ_SNAPSHOT_CODEC_NOT_LIVE"})
            return
        stage = "find_refs"
        refs = _find_pointer_refs(process, regions, locations)

        stage = "candidates"
        keys = {}
        for name in REQUIRED_EXPORTS:
            unique = {}
            for ref_address, salt_address in refs[name]:
                item = _candidate(process, ref_address, salt_address, profile)
                if item is not None:
                    unique[(item["key_hex"], item["salt_hex"])] = item
            if len(unique) != 1:
                _emit({"error_code": "QQ_SNAPSHOT_CODEC_AMBIGUOUS"})
                return
            keys[name] = next(iter(unique.values()))

        _emit(
            {
                "profile_id": profile.profile_id,
                "wrapper_sha256": wrapper_digest,
                "keys": keys,
            }
        )
    except Exception:
        _emit({"error_code": "QQ_SNAPSHOT_CAPTURE_FAILED", "stage": stage})


def __lldb_init_module(debugger, internal_dict) -> None:
    debugger.HandleCommand("command script add -f lldb_bridge.qq_secure_capture qq_secure_capture")
