"""Deterministic, dependency-free decoder for the supported NTQQ message blob subset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class ProtoDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ProtoDecodeError("invalid varint")


def _parse_fields(data: bytes) -> list[ProtoField]:
    fields: list[ProtoField] = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if number <= 0:
            raise ProtoDecodeError("invalid field")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ProtoDecodeError("truncated fixed64")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ProtoDecodeError("truncated bytes")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ProtoDecodeError("truncated fixed32")
            value = int.from_bytes(data[offset:end], "little")
            offset = end
        else:
            raise ProtoDecodeError("unsupported wire type")
        fields.append(ProtoField(number=number, wire_type=wire_type, value=value))
    return fields


def _text(data: bytes) -> Optional[str]:
    if not data or b"\x00" in data:
        return None
    try:
        value = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not value:
        return ""
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in value)
    return value if printable / len(value) > 0.9 else None


def _walk(data: bytes, depth: int = 0) -> list[list[ProtoField]]:
    if not data or depth > 8:
        return []
    try:
        fields = _parse_fields(data)
    except ProtoDecodeError:
        return []
    result = [fields]
    for field in fields:
        if field.wire_type == 2 and isinstance(field.value, bytes) and _text(field.value) is None:
            result.extend(_walk(field.value, depth + 1))
    return result


def _first_int(fields: list[ProtoField], number: int) -> Optional[int]:
    for field in fields:
        if field.number == number and isinstance(field.value, int):
            return int(field.value)
    return None


def _first_string(fields: list[ProtoField], number: int) -> Optional[str]:
    for field in fields:
        if field.number == number and isinstance(field.value, bytes):
            value = _text(field.value)
            if value:
                return value
    return None


def _short(value: str, limit: int = 1000) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def decode_qq_message_blob(blob: Any) -> dict[str, Any]:
    if blob is None:
        return {"text": "", "attachments": [], "element_types": []}
    data = bytes(blob)
    parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    element_types: list[int] = []

    for fields in _walk(data):
        element_type = _first_int(fields, 45002)
        if element_type is None:
            continue
        element_types.append(element_type)
        name = _first_string(fields, 45402)
        size = _first_int(fields, 45405)
        text = _first_string(fields, 45101)
        transcript = _first_string(fields, 45923) or _first_string(fields, 45102)
        face = _first_string(fields, 47602)
        reply_text = _first_string(fields, 47413)
        reply_name = _first_string(fields, 47421)
        xml = _first_string(fields, 48602) or _first_string(fields, 47901)

        if element_type == 1 and text:
            parts.append(text)
        elif element_type == 2:
            parts.append("[图片]")
            attachments.append({"type": "image", "name": name, "size": size})
        elif element_type == 3:
            parts.append("[文件]" + (f" {name}" if name else ""))
            attachments.append({"type": "file", "name": name, "size": size})
        elif element_type == 4:
            parts.append("[语音]" + (f" {transcript}" if transcript else ""))
            attachments.append({"type": "voice", "name": name, "size": size})
        elif element_type == 5:
            parts.append("[视频]" + (f" {name}" if name else ""))
            attachments.append({"type": "video", "name": name, "size": size})
        elif element_type in (6, 11):
            parts.append(face or "[表情]")
        elif element_type == 7:
            quoted = ": ".join(value for value in (reply_name, reply_text) if value)
            parts.append("[引用]" + (f" {quoted}" if quoted else ""))
        elif element_type == 8:
            parts.append(text or _first_string(fields, 47713) or "[系统消息]")
        elif element_type in (10, 16):
            parts.append("[卡片]" + (f" {_short(xml, 500)}" if xml else ""))
        elif text:
            parts.append(text)

    if not parts:
        interesting = {45101, 45102, 45402, 45923, 47413, 47602, 47901, 48602}
        for fields in _walk(data):
            for field in fields:
                if field.number in interesting and isinstance(field.value, bytes):
                    value = _text(field.value)
                    if value:
                        parts.append(value)
    unique_parts = list(dict.fromkeys(_short(part) for part in parts if part))
    text_value = " ".join(unique_parts) if unique_parts else "[QQ 二进制消息]"
    unique_types = list(dict.fromkeys(element_types))
    return {"text": _short(text_value), "attachments": attachments, "element_types": unique_types}
