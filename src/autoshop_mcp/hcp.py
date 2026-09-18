"""Reader for the AutoShop project index file (``*.hcp``).

The ``.hcp`` file is obfuscated with a fixed byte-wise transform (no encryption
key, no authentication).  The supported layout uses:

    plain[i] = ((cipher[i] XOR (i & 0xFF if i % 2 == 0 else 0)) - key[(i + 1) % 11]) & 0xFF
    key      = b"SclSoftware"

The decoded buffer is UTF-16 encoded XML.

This module reads indexes. The LD-to-IL conversion wrapper updates the block
entry only in a new project copy; source indexes remain untouched.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

from .errors import ToolError

HCP_KEY = b"SclSoftware"
HCP_SUFFIX = ".hcp"
_XML_PROLOG = b"\xff\xfe<\x00?\x00x\x00m\x00l\x00"


def decode(data: bytes) -> bytes:
    """Undo the byte-wise obfuscation of a ``.hcp`` payload."""
    key = HCP_KEY
    out = bytearray(len(data))
    for i, value in enumerate(data):
        x = value ^ ((i & 0xFF) if i % 2 == 0 else 0)
        out[i] = (x - key[(i + 1) % len(key)]) & 0xFF
    return bytes(out)


def is_hcp_bytes(data: bytes) -> bool:
    """Cheap structural test: a real index decodes to the UTF-16 XML prolog."""
    if len(data) < len(_XML_PROLOG):
        return False
    return decode(data[: len(_XML_PROLOG)]) == _XML_PROLOG


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(element: Optional[ElementTree.Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse(data: bytes) -> Dict[str, Any]:
    """Parse a decoded project index into a plain dict.

    The result keeps the raw bytes absent on purpose: callers that need
    byte-preservation must keep the original buffer themselves.
    """
    decoded = decode(data)
    if not decoded.startswith(b"\xff\xfe"):
        raise ToolError(
            "unsupported_format",
            "文件不是可识别的 AutoShop 工程索引（.hcp 解码结果不是 UTF-16 XML）。",
            {"decoded_prefix_hex": decoded[:8].hex()},
        )
    # ElementTree refuses a str carrying an XML encoding declaration, so feed it
    # the bytes including the UTF-16 BOM.
    try:
        root = ElementTree.fromstring(decoded)
    except ElementTree.ParseError as exc:
        raise ToolError(
            "project_index_unreadable",
            "工程索引 XML 解析失败：%s" % exc,
            {},
        )
    if root.tag != "project":
        raise ToolError(
            "project_index_unreadable",
            "工程索引根节点不是 <project>，实际为 <%s>。" % root.tag,
            {},
        )

    history = root.find("historyversions")
    history_latest: Dict[str, Optional[str]] = {}
    if history is not None:
        first = history.find("v0")
        if first is None:
            children = list(history)
            first = children[0] if children else None
        if first is not None:
            for tag in ("projnameex", "asversion", "date", "operate", "lasttime"):
                history_latest[tag] = _text(first, tag)

    files: List[Dict[str, Any]] = []
    for entry in root.findall("file"):
        file_name = _text(entry, "FileName")
        if not file_name:
            continue
        if '/' in file_name or '\\' in file_name or ':' in file_name or file_name in ('.', '..'):
            raise ToolError('path_traversal_rejected', '工程索引引用不是本目录文件名。', {'file': file_name})
        files.append(
            {
                "id": _int_or_none(entry.get("id")),
                "file_name": file_name,
                "file_type": _int_or_none(_text(entry, "FileType")),
                "prog_type": _int_or_none(_text(entry, "ProgType")),
                "caption": _text(entry, "Caption"),
                "alias": _text(entry, "Alias"),
                "pou_id": _int_or_none(_text(entry, "POUID")),
                "encrypted": _int_or_none(_text(entry, "Encrypted")),
                "sys_st_func": _int_or_none(_text(entry, "SysSTFunc")),
                "create_time": _text(entry, "CreateTime"),
                "last_modify_time": _text(entry, "LastModifyTime"),
                "file_desc": _text(entry, "FileDesc"),
                "author": _text(entry, "Author"),
            }
        )

    return {
        "sha256": sha256_of(data),
        "size": len(data),
        "format_version": _text(root, "Version"),
        "hcp_file_version": _text(root, "HCPFileVer"),
        "project_name": history_latest.get("projnameex"),
        "machine_model": _text(root, "GCMModal"),
        "cpu_version": _text(root, "CPUVersion"),
        "hardware_file": _text(root, "HardwareFile"),
        "capability": _text(root, "Capbility"),
        "proj_version": _text(root, "ProjVersion"),
        "as_version": history_latest.get("asversion") or _text(root, "ProjVersion"),
        "proj_encoding": _text(root, "ProjEncodingFormat"),
        "project_desc": _text(root, "ProjectDesc"),
        "authority_type": _int_or_none(_text(root, "AuthorityType")),
        "has_password": bool((_text(root, "ProjPassword") or "").strip()),
        "var_alloc_version": _int_or_none(_text(root, "VarAllocVersion")),
        "tsc_file": _text(root, "TscFile"),
        "history_latest": history_latest,
        "file_count": len(files),
        "files": files,
    }


def find_index(project_dir: str) -> str:
    """Return the single ``.hcp`` file of ``project_dir`` or raise."""
    import os

    if not os.path.isdir(project_dir):
        if os.path.exists(project_dir):
            raise ToolError("project_is_file", "工程路径是文件而不是目录：%s" % project_dir, {})
        raise ToolError("project_not_found", "工程目录不存在：%s" % project_dir, {})

    candidates = []
    for name in sorted(os.listdir(project_dir)):
        path = os.path.join(project_dir, name)
        if name.lower().endswith(HCP_SUFFIX) and os.path.isfile(path) and not os.path.islink(path):
            candidates.append(path)
    if not candidates:
        raise ToolError(
            "no_project_index",
            "目录内没有 .hcp 工程索引文件：%s" % project_dir,
            {"project": os.path.abspath(project_dir)},
        )
    if len(candidates) > 1:
        raise ToolError(
            "multiple_project_index",
            "目录内有多个 .hcp 工程索引文件，无法确定目标工程。",
            {"candidates": [os.path.basename(p) for p in candidates]},
        )
    return candidates[0]


def project_meta_from_dir(project_dir: str) -> Dict[str, Any]:
    """Parse the index of a project directory and attach the path information."""
    import os
    from .project import iter_paths
    iter_paths(project_dir)

    index = find_index(project_dir)
    with open(index, "rb") as handle:
        data = handle.read()
    if not is_hcp_bytes(data):
        raise ToolError(
            "unsupported_format",
            "工程索引 .hcp 解码后不是 UTF-16 XML，格式不受支持。",
            {"file": os.path.basename(index), "size": len(data)},
        )
    meta = parse(data)
    meta["index_file"] = os.path.basename(index)
    meta["index_rel_path"] = os.path.basename(index)
    meta["project_dir"] = os.path.abspath(project_dir)
    meta["project_name"] = meta.get("project_name") or os.path.basename(os.path.abspath(project_dir))
    return meta


def xml_text(data: bytes) -> str:
    """Decoded index as text (used by tests and diffing)."""
    decoded = decode(data)
    match = re.match(rb"^\xff\xfe", decoded)
    text = decoded.decode("utf-16")
    if not match:  # pragma: no cover - defensive
        return text
    return text
