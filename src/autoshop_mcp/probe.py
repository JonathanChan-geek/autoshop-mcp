"""Static native-compilation probe.

This module never loads or executes AutoShop binaries.  It only reads PE
headers/imports/exports statically, either from a supplied research evidence
JSON file or directly from an installation directory, plus SHA256 of the files
so the result can be tied to exact bytes.

The static probe alone cannot establish compilation availability. See
``capabilities.native_compile`` for the separate version-bound native backend.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any, Dict, List, Optional

from .errors import ToolError

DEFAULT_INSTALL_DIR_ENV = "AUTOSHOP_INSTALL_DIR"
DEFAULT_EVIDENCE_ENV = "AUTOSHOP_NATIVE_EVIDENCE"

# DLLs that the GUI uses for compiling/converting a project.
DEFAULT_TARGETS = (
    "AutoShop.exe",
    "Converter.dll",
    "STCompiler.dll",
    "InterfaceMgr.dll",
    "H530Converter.dll",
    "Parser.dll",
)

COMPILE_KEYWORDS = ("Compile", "Compiler", "Convert", "Verify", "Queryer")

# MFC/undocumented type names that a headless caller would have to reproduce.
ABI_BLOCKERS = {
    "CList<tagFileProp>": "$CList@UtagFileProp",
    "CWnd": "PAVCWnd@@",
    "CDataManageCenter": "CDataManageCenter",
}

MACHINE_ARCH = {
    0x014C: "x86",
    0x8664: "x86-64",
    0x01C0: "arm",
    0x01C4: "armv7",
    0xAA64: "arm64",
}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pe_header(path: str) -> Dict[str, Any]:
    """Read the DOS/PE headers with the standard library only."""
    with open(path, "rb") as handle:
        head = handle.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            return {"is_pe": False, "reason": "not_mz"}
        e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        handle.seek(e_lfanew)
        signature = handle.read(4)
        if signature != b"PE\x00\x00":
            return {"is_pe": False, "reason": "not_pe_signature"}
        coff = handle.read(20)
        if len(coff) < 20:
            return {"is_pe": False, "reason": "truncated_coff"}
        machine, sections, timestamp = struct.unpack_from("<HHI", coff, 0)
        characteristics = struct.unpack_from("<H", coff, 18)[0]
    return {
        "is_pe": True,
        "machine": machine,
        "architecture": MACHINE_ARCH.get(machine, "unknown(0x%04x)" % machine),
        "sections": sections,
        "timestamp": timestamp,
        "is_dll": bool(characteristics & 0x2000),
    }


def _pefile_summary(path: str) -> Optional[Dict[str, Any]]:
    try:
        import pefile  # type: ignore
    except ImportError:
        return None
    pe = pefile.PE(path, fast_load=False)
    exports: List[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if symbol.name:
                exports.append(symbol.name.decode("utf-8", "replace"))
    imports: Dict[str, int] = {}
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            imports[entry.dll.decode("utf-8", "replace")] = len(entry.imports)
    pe.close()
    return {"exports": exports, "imports": imports}


def _compiler_related(exports: List[str]) -> List[str]:
    out = []
    for name in exports:
        if any(keyword in name for keyword in COMPILE_KEYWORDS):
            out.append(name)
    return sorted(out)


def _abi_blockers(exports: List[str], blob: str) -> List[str]:
    found = []
    for label, needle in ABI_BLOCKERS.items():
        if needle in blob:
            found.append(label)
    return found


def _artifact_from_evidence(entry: Dict[str, Any], targets: List[str]) -> Optional[Dict[str, Any]]:
    name = entry.get("file") or ""
    if targets and name.lower() not in targets:
        return None
    exports = list(entry.get("exports") or [])
    imports = entry.get("imports") or {}
    machine = entry.get("machine")
    blob = "\n".join(exports) + "\n" + json.dumps(imports)
    return {
        "file": name,
        "source": "evidence_json",
        "machine": machine,
        "architecture": MACHINE_ARCH.get(machine, "unknown(0x%04x)" % machine) if machine else None,
        "sha256": entry.get("sha256"),
        "size": None,
        "export_count": len(exports),
        "compiler_related_exports": _compiler_related(exports),
        "abi_blockers": _abi_blockers(exports, blob),
        "imported_dlls": sorted(imports.keys()) if isinstance(imports, dict) else [],
    }


def _artifact_from_disk(path: str, name: str) -> Dict[str, Any]:
    header = read_pe_header(path)
    summary = _pefile_summary(path) if header.get("is_pe") else None
    artifact: Dict[str, Any] = {
        "file": name,
        "source": "install_dir_scan",
        "size": os.path.getsize(path),
        "sha256": sha256_file(path),
        "machine": header.get("machine"),
        "architecture": header.get("architecture"),
        "is_pe": header.get("is_pe", False),
        "pe_reason": header.get("reason"),
        "is_dll": header.get("is_dll"),
        "export_count": None,
        "compiler_related_exports": None,
        "abi_blockers": None,
        "imported_dlls": None,
        "pefile_used": summary is not None,
    }
    if summary is not None:
        artifact["export_count"] = len(summary["exports"])
        artifact["compiler_related_exports"] = _compiler_related(summary["exports"])
        artifact["abi_blockers"] = _abi_blockers(
            summary["exports"], "\n".join(summary["exports"]) + "\n" + json.dumps(summary["imports"])
        )
        artifact["imported_dlls"] = sorted(summary["imports"].keys())
    return artifact


def probe(
    install_dir: Optional[str] = None,
    evidence: Optional[str] = None,
    dlls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Static probe of compiler evidence; never executes anything."""
    install = install_dir or os.environ.get(DEFAULT_INSTALL_DIR_ENV)
    evidence_path = evidence or os.environ.get(DEFAULT_EVIDENCE_ENV)
    targets = [name.lower() for name in (dlls if dlls else list(DEFAULT_TARGETS))]

    result: Dict[str, Any] = {
        "native_compile_available": False,
        "native_compile_supported": False,
        "dll_execution_performed": False,
        "install_dir": install,
        "install_dir_exists": bool(install and os.path.isdir(install)),
        "evidence_file": evidence_path,
        "evidence_file_exists": bool(evidence_path and os.path.isfile(evidence_path)),
        "pefile_available": False,
        "artifacts": [],
        "blockers": [],
        "warnings": [],
        "next_step": None,
    }
    try:
        import pefile  # type: ignore  # noqa: F401

        result["pefile_available"] = True
    except ImportError:
        result["pefile_available"] = False

    if result["evidence_file_exists"]:
        try:
            with open(evidence_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            raise ToolError(
                "probe_unavailable",
                "PE 证据文件无法读取或不是 JSON：%s" % exc,
                {"evidence": evidence_path},
            )
        if isinstance(data, dict):
            data = data.get("files") or data.get("artifacts") or []
        if not isinstance(data, list):
            raise ToolError(
                "probe_unavailable",
                "PE 证据文件结构不是文件列表。",
                {"evidence": evidence_path},
            )
        for entry in data:
            if not isinstance(entry, dict):
                continue
            artifact = _artifact_from_evidence(entry, targets)
            if artifact:
                result["artifacts"].append(artifact)
        result["status"] = "evidence_json"
    elif result["install_dir_exists"]:
        result["status"] = "install_dir_scan"
        for name in sorted(os.listdir(install)):
            if targets and name.lower() not in targets:
                continue
            path = os.path.join(install, name)
            if os.path.isfile(path) and not os.path.islink(path):
                result["artifacts"].append(_artifact_from_disk(path, name))
        if not result["artifacts"]:
            result["warnings"].append("安装目录内没有找到目标 PE 文件。")
    else:
        result["status"] = "unavailable"
        result["reason"] = (
            "既没有可读的 PE 证据 JSON，也没有可读的 AutoShop 安装目录；"
            "本工具不会执行 DLL，因此无法给出编译能力结论。"
        )
        result["next_step"] = (
            "用 --evidence <native-evidence.json> 指定只读 PE 证据，"
            "或用 --install-dir <AutoShop 目录>（只做静态读取，不执行）"
        )
        return result

    blockers: List[str] = []
    for artifact in result["artifacts"]:
        for blocker in artifact.get("abi_blockers") or []:
            if blocker not in blockers:
                blockers.append(blocker)
    if blockers:
        result["blockers"] = blockers

    exports_hits = sum(len(a.get("compiler_related_exports") or []) for a in result["artifacts"])
    result["compiler_export_hits"] = exports_hits
    result["reason"] = (
        "静态证据显示编译相关导出存在（%d 个），但入口需要 MFC 对象"
        "（%s），静态探针不执行 DLL，不能独立证明可编译；请查看 capabilities.native_compile 的原生后端状态。"
        % (exports_hits, "、".join(blockers) if blockers else "未识别的 MFC 类型")
    )
    if exports_hits == 0:
        result["warnings"].append(
            "证据中没有匹配到编译相关导出名（可能需要 pefile 才能读到导出表）。"
        )
    result["next_step"] = "原生编译仍需 AutoShop GUI；本工具只做离线文本操作，不声称编译能力。"
    return result
