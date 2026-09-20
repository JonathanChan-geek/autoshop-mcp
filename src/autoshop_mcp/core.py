"""The offline tool core shared by the JSON CLI and the MCP server.

Every tool returns a JSON-serialisable dict.  Failures raise :class:`ToolError`
which :func:`run_tool` turns into ``{"ok": false, "error": {...}}``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
import zipfile
import zlib
from typing import Any, Callable, Dict, List, Optional

from . import TOOL_VERSION, hcp, il, probe
from .compiler import compile_copy, runtime_status, ld_to_il_copy
from . import project as project_files
from .errors import ToolError

NATIVE_COMPILE_NOTE = (
    "本工具只做离线文本/打包操作，没有执行 AutoShop 编译器；"
    "产物未经过原生编译，也不是可下载的编译结果。"
)

PACKAGE_FORMAT = "autoshop-offline-package/1"
PATCH_FORMAT = "autoshop-offline-patch/1"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _ok(tool: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "tool": tool, "tool_version": TOOL_VERSION, "at": _now()}
    out.update(payload)
    return out


def _require_project(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("invalid_arguments", "缺少 project 参数（工程目录）。", {})
    return os.path.abspath(path.strip())


def _norm_sha(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError("invalid_arguments", "缺少 %s（SHA256 十六进制字符串）。" % field, {})
    text = value.strip().lower()
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ToolError(
            "invalid_arguments",
            "%s 不是 64 位十六进制 SHA256：%r" % (field, value),
            {"field": field},
        )
    return text


# --------------------------------------------------------------------------
# capabilities
# --------------------------------------------------------------------------
FORMAT_MATRIX = [
    {
        "format": ".hcp 工程索引",
        "read": True,
        "write": False,
        "support": "parse_only",
        "note": "解码为 UTF-16 XML 后只读解析（机型/文件表/版本），字节原样保留。",
    },
    {
        "format": "纯明文 .IL POU",
        "read": True,
        "write": "new_copy_only",
        "support": "editable_il",
        "note": "头部 217 字节 + 0xFF + uint16 长度 + GBK 文本，严格校验后可在新副本中打补丁。",
    },
    {
        "format": ".LD 梯形图 POU",
        "read": False,
        "write": False,
        "support": "read_only_ld",
        "note": "可用 ld_to_il_copy 经原厂转换并验证机器码等价，再修改 IL。",
    },
    {
        "format": "工程内容中本工具无法解释的文件（MAIN.dat 软元件内存、MAIN.mon 用户监视表、*.cfg/*.ini/*.sdt/*.gdt/*.dev、CANLink.prg 等）",
        "read": False,
        "write": False,
        "support": "opaque",
        "note": "未知即保留：不解释内容，始终按原始字节保留与传递，绝不当作缓存排除。",
    },
    {
        "format": "陈旧编译/上传产物与失效缓存（显式名称清单）",
        "read": False,
        "write": False,
        "support": "stale_excluded",
        "note": "Output.prg、Upload.prg、Output.itm、Output.sdt、ProgInfo.dat、steps.dat、"
        "CrossTable.crs、ElemUseInfo.esi、MAIN.odt、MDI.cfg、*.hcpp、*.tmp 及 Compile/Temp/BackUP 目录："
        "打包时排除、补丁副本中隔离，绝不当作编译结果交付。",
    },
]

# Explicit stale set, reported by capabilities so callers can see the exact rules.
STALE_RULES = {
    "file_names": list(project_files.STALE_FILE_NAMES),
    "extensions": list(project_files.STALE_EXTENSIONS),
    "directories": list(project_files.STALE_DIR_NAMES),
    "not_included": list(project_files.REQUIRED_PRESERVED_NAMES),
    "note": "不按注释或未知扩展名推断；未列出的文件一律原始保留。",
}

LIMITATIONS = [
    "native_compile_copy 支持已验证版本的 H3U IL/LD 原厂无界面编译；其他工具不代表已编译。",
    "原厂 DLL 仅在独立 x86 子进程和工程副本中执行；不注入、不做 GUI 自动化、不连接 PLC。",
    "il_patch_copy 仅修改明文 IL；LD 先经原厂转换，当前不支持加密程序和 ST。",
    "补丁只做唯一匹配的纯文本替换，不做语法检查，不判断逻辑是否正确。",
    "失效缓存只做隔离与排除并完整记录，是否可安全删除由 AutoShop 决定；未列出的未知文件一律保留。",
]


def _mcp_sdk_info() -> Dict[str, Any]:
    try:
        import mcp  # type: ignore
    except ImportError:
        return {"available": False, "version": None}
    version = getattr(mcp, "__version__", None)
    if version is None:
        try:
            import importlib.metadata as md

            version = md.version("mcp")
        except Exception:  # pragma: no cover - metadata is optional
            version = None
    return {"available": True, "version": version}


def capabilities() -> Dict[str, Any]:
    """Report tool list, supported formats and the honest compile status."""
    probe_status = {
        "native_compile_available": False,
        "evidence_env": probe.DEFAULT_EVIDENCE_ENV,
        "install_dir_env": probe.DEFAULT_INSTALL_DIR_ENV,
        "pefile_available": False,
    }
    try:
        import pefile  # type: ignore  # noqa: F401

        probe_status["pefile_available"] = True
    except ImportError:
        pass
    return _ok(
        "capabilities",
        {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "tools": [
                {"name": spec["name"], "summary": spec["summary"], "params": spec["params"]}
                for spec in TOOL_SPECS
            ],
            "formats": FORMAT_MATRIX,
            "stale_rules": STALE_RULES,
            "limitations": LIMITATIONS,
            "native_compile": runtime_status(),
            "static_probe": probe_status,
            "native_compiled": False,
            "mcp_sdk": _mcp_sdk_info(),
        },
    )


# --------------------------------------------------------------------------
# project_inspect
# --------------------------------------------------------------------------
def project_inspect(project: str) -> Dict[str, Any]:
    """Inventory one project directory with per-file SHA256 and support level."""
    root = _require_project(project)
    meta = hcp.project_meta_from_dir(root)
    files = project_files.inventory(root, meta)
    return _ok(
        "project_inspect",
        {
            "project": {
                "dir": root,
                "dir_name": os.path.basename(root.rstrip(os.sep)),
                "index_file": meta["index_file"],
                "index_sha256": meta["sha256"],
                "index_size": meta["size"],
                "name": meta["project_name"],
                "machine_model": meta["machine_model"],
                "cpu_version": meta["cpu_version"],
                "hardware_file": meta["hardware_file"],
                "proj_version": meta["proj_version"],
                "as_version": meta["as_version"],
                "encoding": meta["proj_encoding"],
                "hcp_file_version": meta["hcp_file_version"],
                "has_password": meta["has_password"],
                "registered_file_count": meta["file_count"],
            },
            "summary": project_files.summary(files),
            "files": files,
            "stale_artifacts": project_files.stale_paths(root, meta),
            "preserved_required_note": project_files.REQUIRED_PRESERVED_NOTE,
            "native_compile": {"performed": False, "note": NATIVE_COMPILE_NOTE},
            "native_compiled": False,
        },
    )


# --------------------------------------------------------------------------
# il_read
# --------------------------------------------------------------------------
def il_read(project: str, file: str) -> Dict[str, Any]:
    """Return the decoded IL text of one plain IL POU, LF line endings."""
    root = _require_project(project)
    target = project_files.resolve_entry(root, file, what="POU 文件")
    if not target.lower().endswith(il.IL_SUFFIX):
        raise ToolError(
            "unsupported_pou_type",
            "只支持纯明文 .IL 文件；该文件为 %s。" % os.path.splitext(file)[1],
            {"file": file, "hint": "LD 不支持解析或修改。"},
        )
    with open(target, "rb") as handle:
        data = handle.read()
    doc = il.parse(data, origin=file)
    return _ok(
        "il_read",
        {
            "project_dir": root,
            "file": file,
            "sha256": doc.sha256,
            "text": doc.text,
            "container": doc.to_dict(),
        },
    )


# --------------------------------------------------------------------------
# il_patch_copy
# --------------------------------------------------------------------------
def _copy_project(source: str, dest: str) -> List[str]:
    """Copy every regular file verbatim.

    Symlinks and reparse points were already rejected by
    :func:`project_files.iter_paths`, so nothing can escape the project here.
    """
    copied: List[str] = []
    for rel in project_files.iter_files(source):
        src_path = os.path.join(source, rel.replace("/", os.sep))
        dst_path = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst_path) or dest, exist_ok=True)
        shutil.copyfile(src_path, dst_path)
        copied.append(rel)
    return copied


def _preservation_report(
    source_hashes: Dict[str, str],
    dest: str,
    *,
    patched_rel: str,
) -> Dict[str, Any]:
    """Prove that stashed/quarantined/copied files kept their exact bytes."""
    prefix = project_files.TOOL_DIR_NAME + "/stale/"
    root_prefix = project_files.TOOL_DIR_NAME + "/"
    checked: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    outside: List[Dict[str, Any]] = []
    for rel in project_files.iter_files(dest):
        if rel.startswith(root_prefix) and not rel.startswith(prefix):
            continue
        key = rel[len(prefix) :] if rel.startswith(prefix) else rel
        expected = source_hashes.get(key)
        if expected is None:
            outside.append({"rel_path": rel, "reason": "not_in_source"})
            continue
        if key == patched_rel:
            continue
        actual = project_files.sha256_file(os.path.join(dest, rel.replace("/", os.sep)))
        if actual == expected:
            checked.append(key)
        else:
            mismatches.append({"rel_path": rel, "expected_sha256": expected, "actual_sha256": actual})

    required = []
    for name in project_files.REQUIRED_PRESERVED_NAMES:
        for rel, digest in sorted(source_hashes.items()):
            if os.path.basename(rel).lower() != name:
                continue
            required.append(
                {
                    "rel_path": rel,
                    "sha256": digest,
                    "preserved_in_copy": rel in set(checked),
                }
            )
    return {
        "unmodified_files_verified": len(checked),
        "unmodified_files": sorted(checked),
        "mismatches": mismatches,
        "unexpected_files": outside,
        "required_preserved": required,
        "required_preserved_note": project_files.REQUIRED_PRESERVED_NOTE,
    }


def il_patch_copy(
    project: str,
    file: str,
    expected_sha256: str,
    old_text: str,
    new_text: str,
    dest: str,
    quarantine: bool = True,
) -> Dict[str, Any]:
    """Replace one unique text occurrence in a new project copy.

    The source project is only ever read.  Every other file is copied byte for
    byte, and the explicitly listed stale artifacts are *moved* (never deleted)
    into ``_offline_patch/stale/`` inside the new copy only.
    """
    if quarantine is not True:
        raise ToolError("invalid_arguments", "修改副本必须隔离陈旧编译产物。", {})
    root = _require_project(project)
    expected = _norm_sha(expected_sha256, field="expected_sha256")
    if not isinstance(old_text, str) or old_text == "":
        raise ToolError("invalid_arguments", "old_text 不能为空。", {})
    if not isinstance(new_text, str):
        raise ToolError("invalid_arguments", "new_text 必须是字符串。", {})
    if "\r" in old_text or "\r" in new_text:
        raise ToolError(
            "invalid_line_endings",
            "old_text/new_text 必须使用 LF 换行，不接受回车符。",
            {},
        )

    meta = hcp.project_meta_from_dir(root)
    target = project_files.resolve_entry(root, file, what="POU 文件")
    if not target.lower().endswith(il.IL_SUFFIX):
        raise ToolError(
            "unsupported_pou_type",
            "只支持纯明文 .IL 文件；%s 本版本不支持修改（LD 不支持），拒绝处理。" % file,
            {"file": file},
        )
    rel_path = os.path.relpath(target, root).replace(os.sep, "/")

    with open(target, "rb") as handle:
        original = handle.read()
    actual = _sha256(original)
    if actual != expected:
        raise ToolError(
            "hash_mismatch",
            "文件 SHA256 与 expected_sha256 不一致，拒绝修改（并发编辑或参数错误）。",
            {"file": file, "expected_sha256": expected, "actual_sha256": actual},
        )
    doc = il.parse(original, origin=file)

    occurrences = doc.text.count(old_text)
    if occurrences == 0:
        raise ToolError(
            "old_text_not_found",
            "old_text 在 IL 文本中未出现，无法替换。",
            {"file": file, "old_text_preview": old_text[:120]},
        )
    if occurrences > 1:
        raise ToolError(
            "old_text_ambiguous",
            "old_text 出现 %d 次，要求唯一匹配，拒绝修改。" % occurrences,
            {"file": file, "occurrences": occurrences},
        )

    index = doc.text.index(old_text)
    patched_text = doc.text[:index] + new_text + doc.text[index + len(old_text) :]
    patched = doc.render(patched_text)

    source_records = {r["rel_path"]: r for r in project_files.inventory(root, meta)}
    stale_plan = project_files.stale_paths(root, meta)

    dest_dir = project_files.check_destination(root, dest)
    os.makedirs(dest_dir)
    copied = _copy_project(root, dest_dir)

    dest_target = os.path.join(dest_dir, rel_path.replace("/", os.sep))
    with open(dest_target, "wb") as handle:
        handle.write(patched)

    quarantined: List[Dict[str, Any]] = []
    stale_root = os.path.join(dest_dir, project_files.TOOL_DIR_NAME, "stale")
    if quarantine:
        moved_prefixes: List[str] = []
        # Directories first, shallow to deep, so their contents travel with them.
        plan_dirs = [e for e in stale_plan if e["entry_type"] == "directory"]
        plan_files = [e for e in stale_plan if e["entry_type"] == "file"]
        for entry in sorted(plan_dirs, key=lambda e: (e["rel_path"].count("/"), e["rel_path"])):
            rel_dir = entry["rel_path"].rstrip("/")
            if rel_dir == rel_path or rel_path.startswith(rel_dir + "/"):
                continue  # never move the file we just patched
            src_dir = os.path.join(dest_dir, rel_dir.replace("/", os.sep))
            if not os.path.isdir(src_dir):
                continue
            dst_dir = os.path.join(stale_root, rel_dir.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
            shutil.move(src_dir, dst_dir)
            moved_prefixes.append(rel_dir + "/")
            quarantined.append(
                {
                    "rel_path": rel_dir + "/",
                    "entry_type": "directory",
                    "sha256": None,
                    "basis": entry["basis"],
                    "moved_to": os.path.relpath(dst_dir, dest_dir).replace(os.sep, "/"),
                }
            )
        for entry in sorted(plan_files, key=lambda e: e["rel_path"]):
            rel = entry["rel_path"]
            if rel == rel_path or any(rel.startswith(p) for p in moved_prefixes):
                continue
            src_path = os.path.join(dest_dir, rel.replace("/", os.sep))
            if not os.path.isfile(src_path):
                continue
            dst_path = os.path.join(stale_root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            shutil.move(src_path, dst_path)
            quarantined.append(
                {
                    "rel_path": rel,
                    "entry_type": "file",
                    "sha256": source_records[rel]["sha256"] if rel in source_records else None,
                    "basis": entry["basis"],
                    "moved_to": os.path.relpath(dst_path, dest_dir).replace(os.sep, "/"),
                }
            )

    preservation = _preservation_report(
        {rel: r["sha256"] for rel, r in source_records.items()},
        dest_dir,
        patched_rel=rel_path,
    )

    new_sha = _sha256(patched)
    manifest = {
        "format": PATCH_FORMAT,
        "tool": "autoshop-mcp",
        "tool_version": TOOL_VERSION,
        "created_at": _now(),
        "source_project_name": meta["project_name"],
        "source_index_sha256": meta["sha256"],
        "patched_file": rel_path,
        "expected_sha256": expected,
        "old_sha256": actual,
        "new_sha256": new_sha,
        "old_size": len(original),
        "new_size": len(patched),
        "occurrences_replaced": 1,
        "quarantined": quarantined,
        "copied_file_count": len(copied),
        "preservation": {
            "unmodified_files_verified": preservation["unmodified_files_verified"],
            "mismatches": preservation["mismatches"],
            "required_preserved": preservation["required_preserved"],
        },
        "native_compiled": False,
        "native_compile": {
            "performed": False,
            "certified": False,
            "note": NATIVE_COMPILE_NOTE,
        },
    }
    manifest_dir = os.path.join(dest_dir, project_files.TOOL_DIR_NAME)
    os.makedirs(manifest_dir, exist_ok=True)
    manifest_path = os.path.join(manifest_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)

    diff_lines = il.unified_text_diff(doc.text, patched_text, "a/" + rel_path, "b/" + rel_path)
    return _ok(
        "il_patch_copy",
        {
            "source_project": root,
            "source_untouched": True,
            "dest_project": dest_dir,
            "dest_file": dest_target,
            "file": rel_path,
            "expected_sha256": expected,
            "old_sha256": actual,
            "new_sha256": new_sha,
            "old_size": len(original),
            "new_size": len(patched),
            "replaced": {"old_text": old_text, "new_text": new_text, "count": 1},
            "unified_diff": diff_lines,
            "copied_file_count": len(copied),
            "quarantined_stale": quarantined,
            "preservation": preservation,
            "manifest": os.path.relpath(manifest_path, dest_dir).replace(os.sep, "/"),
            "native_compiled": False,
            "native_compile": {"performed": False, "certified": False, "note": NATIVE_COMPILE_NOTE},
            "warnings": [
                "隔离目录 %s 只移动文件、不删除；确认无误后可自行删除或还原。" % project_files.TOOL_DIR_NAME
            ]
            + (["未启用隔离，失效缓存按原样保留在副本中。"] if not quarantine else []),
        },
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# project_diff
# --------------------------------------------------------------------------
MAX_DIFF_LINES = 2000


def project_diff(before: str, after: str) -> Dict[str, Any]:
    """Compare two project directories: added / removed / changed plus IL diff."""
    left = _require_project(before)
    right = _require_project(after)
    meta_left = hcp.project_meta_from_dir(left)
    meta_right = hcp.project_meta_from_dir(right)
    inv_left = {r["rel_path"]: r for r in project_files.inventory(left, meta_left)}
    inv_right = {r["rel_path"]: r for r in project_files.inventory(right, meta_right)}

    tool_left = sorted(p for p, r in inv_left.items() if r["kind"] == project_files.KIND_TOOL_ARTIFACT)
    tool_right = sorted(p for p, r in inv_right.items() if r["kind"] == project_files.KIND_TOOL_ARTIFACT)
    stale_left = sorted(p for p, r in inv_left.items() if r["kind"] == project_files.KIND_STALE_ARTIFACT)
    stale_right = sorted(p for p, r in inv_right.items() if r["kind"] == project_files.KIND_STALE_ARTIFACT)
    ignored = set(tool_left) | set(tool_right) | set(stale_left) | set(stale_right)

    added, removed, changed = [], [], []
    for rel in sorted(set(inv_right) - set(inv_left)):
        if rel in ignored:
            continue
        added.append(
            {
                "rel_path": rel,
                "size": inv_right[rel]["size"],
                "sha256": inv_right[rel]["sha256"],
                "kind": inv_right[rel]["kind"],
            }
        )
    for rel in sorted(set(inv_left) - set(inv_right)):
        if rel in ignored:
            continue
        removed.append(
            {
                "rel_path": rel,
                "size": inv_left[rel]["size"],
                "sha256": inv_left[rel]["sha256"],
                "kind": inv_left[rel]["kind"],
            }
        )
    unchanged = 0
    for rel in sorted(set(inv_left) & set(inv_right)):
        if rel in ignored:
            continue
        a, b = inv_left[rel], inv_right[rel]
        if a["sha256"] == b["sha256"]:
            unchanged += 1
            continue
        changed.append(
            {
                "rel_path": rel,
                "kind": b["kind"],
                "size_before": a["size"],
                "size_after": b["size"],
                "sha256_before": a["sha256"],
                "sha256_after": b["sha256"],
                "basis": b["basis"],
            }
        )

    il_diffs: Dict[str, Any] = {}
    for entry in changed:
        rel = entry["rel_path"]
        if entry["kind"] != project_files.KIND_POU_IL and inv_right[rel]["kind"] != project_files.KIND_POU_IL:
            continue
        path_before = os.path.join(left, rel.replace("/", os.sep))
        path_after = os.path.join(right, rel.replace("/", os.sep))
        if not (os.path.isfile(path_before) and os.path.isfile(path_after)):
            continue
        try:
            with open(path_before, "rb") as handle:
                doc_before = il.parse(handle.read(), origin=rel)
            with open(path_after, "rb") as handle:
                doc_after = il.parse(handle.read(), origin=rel)
        except ToolError as exc:
            il_diffs[rel] = {"comparable": False, "reason": exc.code, "message": exc.message}
            continue
        lines = il.unified_text_diff(doc_before.text, doc_after.text, "a/" + rel, "b/" + rel)
        truncated = len(lines) > MAX_DIFF_LINES
        il_diffs[rel] = {
            "comparable": True,
            "unified_diff": lines[:MAX_DIFF_LINES],
            "truncated": truncated,
            "text_changed": doc_before.text != doc_after.text,
        }

    config_changes = [
        {"rel_path": e["rel_path"], "size_before": e["size_before"], "size_after": e["size_after"]}
        for e in changed
        if e["kind"] in (project_files.KIND_CONFIG, project_files.KIND_UNKNOWN)
    ]
    meta_fields = (
        "machine_model",
        "cpu_version",
        "proj_version",
        "as_version",
        "project_name",
        "hcp_file_version",
        "proj_encoding",
        "registered_file_count",
    )
    meta_changes = {
        field: {"before": meta_left.get(field), "after": meta_right.get(field)}
        for field in meta_fields
        if meta_left.get(field) != meta_right.get(field)
    }
    files_index_changed = [
        e["file_name"]
        for e in meta_left["files"]
        if e["file_name"] not in {x["file_name"] for x in meta_right["files"]}
    ] + [
        e["file_name"]
        for e in meta_right["files"]
        if e["file_name"] not in {x["file_name"] for x in meta_left["files"]}
    ]

    return _ok(
        "project_diff",
        {
            "before": left,
            "after": right,
            "identical": not (added or removed or changed),
            "added": added,
            "removed": removed,
            "changed": changed,
            "counts": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
                "unchanged": unchanged,
            },
            "il_text_diffs": il_diffs,
            "config_changes": config_changes,
            "project_meta_changes": meta_changes,
            "index_file_list_changes": sorted(set(files_index_changed)),
            "stale_artifact_handling": {
                "before": stale_left,
                "after": stale_right,
                "note": "失效缓存/陈旧编译产物：不参与 added/removed/changed 比较；"
                "补丁副本中只被移动到 %s/stale/ 下，未删除，也不属于工程内容。"
                % project_files.TOOL_DIR_NAME,
            },
            "tool_artifacts": {"before": tool_left, "after": tool_right},
            "native_compile": {"performed": False, "note": NATIVE_COMPILE_NOTE},
            "native_compiled": False,
        },
    )


# --------------------------------------------------------------------------
# package_project
# --------------------------------------------------------------------------
def _check_zip_target(project_dir: str, out_zip: str) -> str:
    if not isinstance(out_zip, str) or not out_zip.strip():
        raise ToolError("invalid_arguments", "缺少 out_zip（输出 ZIP 路径）。", {})
    dest = os.path.abspath(out_zip.strip())
    if not dest.lower().endswith(".zip"):
        dest = dest + ".zip"
    if project_files.is_link_like(dest):
        raise ToolError(
            "symlink_rejected", "输出 ZIP 是符号链接/联接点：%s" % dest, {"out_zip": dest}
        )
    if os.path.exists(dest):
        raise ToolError("destination_exists", "输出 ZIP 已存在，拒绝覆盖：%s" % dest, {"out_zip": dest})
    if project_files.is_within(dest, project_dir):
        raise ToolError(
            "destination_inside_source",
            "输出 ZIP 不能写在工程目录内部（会改变源工程内容）：%s" % dest,
            {"out_zip": dest},
        )
    parent = os.path.dirname(dest) or "."
    if not os.path.isdir(parent):
        os.makedirs(parent)
    real_parent = os.path.realpath(parent)
    real_source = os.path.realpath(project_dir)
    if project_files.is_within(
        os.path.join(real_parent, os.path.basename(dest)), real_source
    ) or project_files.is_within(real_source, os.path.join(real_parent, os.path.basename(dest))):
        raise ToolError(
            "destination_inside_source",
            "输出 ZIP 的真实路径与源工程重叠（父目录可能是链接）：%s" % dest,
            {"out_zip": dest},
        )
    if not os.access(parent, os.W_OK):
        raise ToolError(
            "destination_not_writable",
            "输出目录不可写：%s" % parent,
            {"out_zip": dest, "parent": parent},
        )
    return dest


def package_project(project: str, out_zip: str, include_stale: bool = False) -> Dict[str, Any]:
    """Package sources and configuration, excluding stale/compiled artifacts."""
    if include_stale is not False:
        raise ToolError("invalid_arguments", "源码包不能包含陈旧编译产物。", {})
    root = _require_project(project)
    meta = hcp.project_meta_from_dir(root)
    dest = _check_zip_target(root, out_zip)
    records = project_files.inventory(root, meta)

    included: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for record in records:
        if record["kind"] == project_files.KIND_TOOL_ARTIFACT:
            excluded.append(
                {"rel_path": record["rel_path"], "reason": "tool_artifact", "basis": record["basis"]}
            )
            continue
        if record["include_in_package"] or include_stale:
            with open(os.path.join(root, record["rel_path"].replace("/", os.sep)), "rb") as handle:
                payload = handle.read()
            included.append(
                {
                    "rel_path": record["rel_path"],
                    "size": len(payload),
                    "sha256": record["sha256"],
                    "crc32": "%08x" % (zlib.crc32(payload) & 0xFFFFFFFF),
                    "kind": record["kind"],
                    "support": record["support"],
                    "basis": record["basis"],
                }
            )
        else:
            excluded.append(
                {
                    "rel_path": record["rel_path"],
                    "reason": record.get("excluded_reason") or "excluded",
                    "basis": record["basis"],
                    "sha256": record["sha256"],
                }
            )

    included.sort(key=lambda item: item["rel_path"])
    excluded.sort(key=lambda item: item["rel_path"])
    stale_excluded = sorted(e["rel_path"] for e in excluded if e["reason"].startswith("stale"))
    required_preserved = sorted(
        item["rel_path"] for item in included if os.path.basename(item["rel_path"]).lower() in project_files.REQUIRED_PRESERVED_NAMES
    )
    manifest = {
        "format": PACKAGE_FORMAT,
        "tool": "autoshop-mcp",
        "tool_version": TOOL_VERSION,
        "created_at": _now(),
        "source_project_name": meta["project_name"],
        "source_index_sha256": meta["sha256"],
        "machine_model": meta["machine_model"],
        "cpu_version": meta["cpu_version"],
        "proj_version": meta["proj_version"],
        "as_version": meta["as_version"],
        "encoding": meta["proj_encoding"],
        "include_stale": bool(include_stale),
        "entries": included,
        "excluded": excluded,
        "stale_excluded": stale_excluded,
        "required_preserved": required_preserved,
        "required_preserved_note": project_files.REQUIRED_PRESERVED_NOTE,
        "native_compiled": False,
        "native_compile": {
            "performed": False,
            "certified": False,
            "note": NATIVE_COMPILE_NOTE,
            "must_compile_in": "native_compile_copy or AutoShop",
        },
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in included:
            info = zipfile.ZipInfo(item["rel_path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(os.path.join(root, item["rel_path"].replace("/", os.sep)), "rb") as handle:
                bundle.writestr(info, handle.read())
        manifest_info = zipfile.ZipInfo(
            project_files.PACKAGE_MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0)
        )
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o644 << 16
        bundle.writestr(manifest_info, manifest_bytes)

    zip_sha = project_files.sha256_file(dest)
    zip_size = os.path.getsize(dest)
    with zipfile.ZipFile(dest, "r") as bundle:
        crc_check = bundle.testzip()
    return _ok(
        "package_project",
        {
            "project": root,
            "zip": dest,
            "zip_sha256": zip_sha,
            "zip_bytes": zip_size,
            "entry_count": len(included) + 1,
            "entries": included,
            "excluded": excluded,
            "manifest_name": project_files.PACKAGE_MANIFEST_NAME,
            "manifest_sha256": _sha256(manifest_bytes),
            "stale_excluded": stale_excluded,
            "required_preserved": required_preserved,
            "required_preserved_note": project_files.REQUIRED_PRESERVED_NOTE,
            "zip_crc_check": "ok" if crc_check is None else "failed",
            "zip_crc_failed_entry": crc_check,
            "native_compiled": False,
            "native_compile": manifest["native_compile"],
            "warnings": [
                "包内没有编译产物，native_compiled=false；请用 native_compile_copy 原生编译，或在 AutoShop 中编译。"
            ]
            + (
                ["已按 include_stale=true 包含失效缓存/陈旧产物，它们是旧数据、未重新生成。"]
                if include_stale
                else []
            ),
        },
    )


# --------------------------------------------------------------------------
# native_compile_probe
# --------------------------------------------------------------------------
def native_compile_probe(
    install_dir: Optional[str] = None,
    evidence: Optional[str] = None,
    dlls: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Static probe only; never executes a DLL and never fakes a compile."""
    if dlls is not None and not isinstance(dlls, list):
        raise ToolError("invalid_arguments", "dlls 必须是字符串数组。", {})
    result = probe.probe(install_dir=install_dir, evidence=evidence, dlls=dlls)
    return _ok("native_compile_probe", result)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "capabilities",
        "summary": "工具清单、支持格式矩阵、限制与原生运行库状态。",
        "handler": capabilities,
        "params": {},
    },
    {
        "name": "project_inspect",
        "summary": "列出工程机型、POU/文件类型、每个文件的 SHA256、支持级别与派生表。",
        "handler": project_inspect,
        "params": {"project": {"type": "str", "required": True, "help": "工程目录（含唯一 .hcp）"}},
    },
    {
        "name": "il_read",
        "summary": "读取纯明文 .IL POU 的 IL 源码文本（LF 换行，GBK 解码）。",
        "handler": il_read,
        "params": {
            "project": {"type": "str", "required": True, "help": "工程目录"},
            "file": {"type": "str", "required": True, "help": "工程内 POU basename，如 MAIN.IL"},
        },
    },
    {
        "name": "il_patch_copy",
        "summary": "在不改动源工程的前提下，生成打了唯一匹配文本补丁的新工程副本，并把失效缓存/陈旧产物移入隔离区。",
        "handler": il_patch_copy,
        "params": {
            "project": {"type": "str", "required": True, "help": "源工程目录（只读）"},
            "file": {"type": "str", "required": True, "help": "要修改的 .IL basename"},
            "expected_sha256": {"type": "str", "required": True, "help": "源文件当前 SHA256（防并发改动）"},
            "old_text": {"type": "str", "required": True, "help": "必须唯一匹配的原文（LF）"},
            "new_text": {"type": "str", "required": True, "help": "替换后的文本（LF）"},
            "dest": {"type": "str", "required": True, "help": "新工程目录（必须不存在）"},
            "quarantine": {"type": "bool", "required": False, "default": True, "help": "是否隔离派生表"},
        },
    },
    {
        "name": "project_diff",
        "summary": "对比两个工程目录：新增/删除/改动文件、IL 文本 diff、配置与索引变化。",
        "handler": project_diff,
        "params": {
            "before": {"type": "str", "required": True, "help": "旧工程目录"},
            "after": {"type": "str", "required": True, "help": "新工程目录"},
        },
    },
    {
        "name": "package_project",
        "summary": "打包源码与配置（排除派生表/陈旧编译结果），输出 ZIP 与 SHA256/CRC32 清单。",
        "handler": package_project,
        "params": {
            "project": {"type": "str", "required": True, "help": "工程目录"},
            "out_zip": {"type": "str", "required": True, "help": "输出 ZIP 路径（必须不存在）"},
            "include_stale": {"type": "bool", "required": False, "default": False, "help": "是否包含失效缓存/陈旧编译产物（默认排除）"},
        },
    },
    {
        "name": "native_compile_probe",
        "summary": "静态读取 PE 证据/安装目录，报告架构、SHA256 与编译相关导出；不执行 DLL、不假编译。",
        "handler": native_compile_probe,
        "params": {
            "install_dir": {"type": "str", "required": False, "default": None, "help": "AutoShop 安装目录（只读静态扫描）"},
            "evidence": {"type": "str", "required": False, "default": None, "help": "PE 研究证据 JSON 路径"},
            "dlls": {"type": "list", "required": False, "default": None, "help": "只关注这些文件名"},
        },
    },
]

TOOL_SPECS.append({
    "name": "native_compile_copy", "summary": "在全新副本中调用原厂 x86 DLL 编译 H3U IL/LD；校验完整覆盖、稳定产物和配置不变，成功后输出工程 ZIP。无需 GUI，不连接 PLC。",
    "handler": compile_copy,
    "params": {
        "project": {"type": "str", "required": True, "help": "原工程目录，只读"},
        "dest": {"type": "str", "required": True, "help": "构建结果目录，必须不存在"},
        "install_dir": {"type": "str", "required": False, "help": "AutoShop 安装目录，DLL 必须匹配已验证指纹"},
    },
})

TOOL_SPECS.append({
    "name": "ld_to_il_copy", "summary": "调用原厂接口将指定 LD 转成可编辑 IL，仅转换前后编译产物逐字节一致时交付新副本。无需 GUI。",
    "handler": ld_to_il_copy,
    "params": {
        "project": {"type": "str", "required": True, "help": "源工程目录，只读"},
        "file": {"type": "str", "required": True, "help": "索引中登记的 LD 文件名"},
        "dest": {"type": "str", "required": True, "help": "全新转换结果目录"},
        "install_dir": {"type": "str", "required": False, "help": "版本指纹匹配的安装目录"},
    },
})

from .extensions import TOOLS as EXTENSION_TOOLS
TOOL_SPECS.extend(EXTENSION_TOOLS)
TOOLS_BY_NAME: Dict[str, Dict[str, Any]] = {spec["name"]: spec for spec in TOOL_SPECS}


def run_tool(name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one tool and always return JSON-serialisable output."""
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return ToolError(
            "invalid_arguments",
            "未知工具：%r" % name,
            {"available": sorted(TOOLS_BY_NAME)},
        ).to_dict()
    kwargs = dict(params or {})
    known = set(spec["params"])
    unexpected = sorted(set(kwargs) - known)
    if unexpected:
        return ToolError(
            "invalid_arguments",
            "工具 %s 收到未知参数：%s" % (name, ", ".join(unexpected)),
            {"available": sorted(known)},
        ).to_dict()
    for param, desc in spec["params"].items():
        if desc.get("required") and (param not in kwargs or kwargs[param] in (None, "")):
            return ToolError(
                "invalid_arguments",
                "工具 %s 缺少必填参数 %s。" % (name, param),
                {"missing": param},
            ).to_dict()
    handler: Callable[..., Dict[str, Any]] = spec["handler"]
    try:
        return handler(**kwargs)
    except ToolError as exc:
        return exc.to_dict()
    except Exception as exc:  # pragma: no cover - defensive
        return ToolError(
            "internal_error",
            "内部错误：%s: %s" % (type(exc).__name__, exc),
            {},
        ).to_dict()
