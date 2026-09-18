"""Reader/writer for the plain (unencrypted) IL payload of an AutoShop POU.

Supported unencrypted IL container layout:

    [0, 217)      header, byte preserved verbatim
    [217]         marker 0xFF
    [218, 220)    payload length, uint16 little endian
    [220, 220+n)  GBK encoded IL text, LF line endings only
    [220+n, end)  footer, byte preserved verbatim (8 zero bytes in the sample)

Detection is strict: the magic, the ``CLVTItem`` header marker, the 0xFF payload
marker, the length field and a strict GBK round-trip must all agree.  A file that
merely contains 0xFF somewhere is rejected.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Dict, List

from .errors import ToolError

MAGIC = b"AutoShop"
HEADER_MARKER = b"CLVTItem"
HEADER_MARKER_OFFSET = 143
HEADER_SIZE = 217
PAYLOAD_MARKER = 0xFF
LENGTH_OFFSET = 218
TEXT_OFFSET = 220
MAX_PAYLOAD = 0xFFFF
PAYLOAD_ENCODING = "gbk"
IL_SUFFIX = ".il"


def detect(data: bytes) -> Dict[str, Any]:
    """Classify the container without raising.

    Returns ``{"supported": bool, "reason": str, "declared_length": int|None}``.
    """
    if len(data) < TEXT_OFFSET:
        return {"supported": False, "reason": "short_file", "declared_length": None}
    if not data.startswith(MAGIC):
        return {"supported": False, "reason": "bad_magic", "declared_length": None}
    if data[HEADER_MARKER_OFFSET : HEADER_MARKER_OFFSET + len(HEADER_MARKER)] != HEADER_MARKER:
        return {"supported": False, "reason": "missing_header_marker", "declared_length": None}
    if data[HEADER_SIZE] != PAYLOAD_MARKER:
        return {"supported": False, "reason": "missing_payload_marker", "declared_length": None}
    declared = struct.unpack_from("<H", data, LENGTH_OFFSET)[0]
    if declared == 0:
        return {"supported": False, "reason": "empty_payload", "declared_length": declared}
    if TEXT_OFFSET + declared > len(data):
        return {"supported": False, "reason": "length_exceeds_file", "declared_length": declared}
    payload = data[TEXT_OFFSET : TEXT_OFFSET + declared]
    if b"\x00" in payload:
        return {"supported": False, "reason": "nul_in_payload", "declared_length": declared}
    if b"\r" in payload:
        return {"supported": False, "reason": "non_lf_line_endings", "declared_length": declared}
    try:
        text = payload.decode(PAYLOAD_ENCODING)
    except UnicodeDecodeError:
        return {"supported": False, "reason": "payload_not_gbk", "declared_length": declared}
    if text.encode(PAYLOAD_ENCODING) != payload:
        return {"supported": False, "reason": "payload_roundtrip_mismatch", "declared_length": declared}
    return {"supported": True, "reason": "ok", "declared_length": declared}


class IlDocument(object):
    """A parsed plain IL container that remembers every original byte."""

    def __init__(self, raw: bytes, text: str):
        self.raw = raw
        self.text = text
        self.header = raw[:HEADER_SIZE]
        self.footer = raw[TEXT_OFFSET + self.declared_length :]

    @property
    def declared_length(self) -> int:
        return struct.unpack_from("<H", self.raw, LENGTH_OFFSET)[0]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def payload_encoding(self) -> str:
        return PAYLOAD_ENCODING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bytes": len(self.raw),
            "header_bytes": HEADER_SIZE,
            "footer_bytes": len(self.footer),
            "declared_payload_length": self.declared_length,
            "payload_encoding": PAYLOAD_ENCODING,
            "line_endings": "lf",
            "sha256": self.sha256,
            "lines": self.text.count("\n") + (0 if self.text.endswith("\n") else 1),
        }

    def render(self, new_text: str) -> bytes:
        """Rebuild the container around ``new_text``, preserving header/footer."""
        return render(new_text, self.header, self.footer)


def parse(data: bytes, *, origin: str = "") -> IlDocument:
    verdict = detect(data)
    if not verdict["supported"]:
        reason = verdict["reason"]
        if reason in ("bad_magic", "missing_header_marker", "missing_payload_marker", "short_file"):
            raise ToolError(
                "unknown_il_header",
                "IL 头部结构不是已验证的布局（%s），拒绝处理以保留原始字节。" % reason,
                {"origin": origin, "reason": reason, "size": len(data)},
            )
        raise ToolError(
            "unsupported_il_payload",
            "IL 载荷不受支持（%s）。" % reason,
            {"origin": origin, "reason": reason},
        )
    declared = verdict["declared_length"]
    payload = data[TEXT_OFFSET : TEXT_OFFSET + declared]
    return IlDocument(data, payload.decode(PAYLOAD_ENCODING))


def render(new_text: str, header: bytes, footer: bytes) -> bytes:
    """Assemble a container from a header, GBK text and footer."""
    if not new_text or "\x00" in new_text:
        raise ToolError("unsupported_il_payload", "IL 文本不能为空或含 NUL。", {})
    if "\r" in new_text:
        raise ToolError(
            "invalid_line_endings",
            "IL 文本必须使用 LF 换行（文件中不得出现回车符）。",
            {},
        )
    try:
        payload = new_text.encode(PAYLOAD_ENCODING)
    except UnicodeEncodeError as exc:
        raise ToolError(
            "unsupported_il_payload",
            "新文本无法用 %s 编码：%s" % (PAYLOAD_ENCODING, exc),
            {},
        )
    if len(payload) > MAX_PAYLOAD:
        raise ToolError(
            "payload_too_large",
            "新的 IL 载荷 %d 字节超过 uint16 长度上限 %d。" % (len(payload), MAX_PAYLOAD),
            {"payload_bytes": len(payload), "limit": MAX_PAYLOAD},
        )
    if len(header) != HEADER_SIZE:
        raise ToolError(
            "unknown_il_header",
            "头部长度不是 %d 字节。" % HEADER_SIZE,
            {"header_bytes": len(header)},
        )
    out = bytearray()
    out += header
    out.append(PAYLOAD_MARKER)
    out += struct.pack("<H", len(payload))
    out += payload
    out += footer
    return bytes(out)


def inspect_bytes(data: bytes) -> Dict[str, Any]:
    """Non-raising summary used by ``project_inspect``."""
    verdict = detect(data)
    if verdict["supported"]:
        doc = parse(data)
        info = doc.to_dict()
        info["supported"] = True
        info["reason"] = "ok"
        return info
    return {
        "supported": False,
        "reason": verdict["reason"],
        "declared_payload_length": verdict["declared_length"],
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def unified_text_diff(old_text: str, new_text: str, old_label: str, new_label: str) -> List[str]:
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    return list(
        difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label, n=3)
    )
