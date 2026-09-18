"""Project directory model: safe paths, file inventory and classification.

Classification is explicit and evidence based; the evidence for every rule is
recorded in ``docs/research.md``.  Two rules matter most:

* Files that AutoShop treats as *project content* are always preserved byte for
  byte, even when this tool cannot interpret them.  In particular
  ``MAIN.dat`` (软元件内存/用户参数备份) and ``MAIN.mon`` (用户监视表) are project
  content, not caches, and are never excluded or quarantined.
* Only a small, explicitly enumerated set of stale compilation/upload artifacts
  is excluded from packages and quarantined inside a *new* copy.  Nothing is
  guessed from a comment or from an unknown file extension.

Nothing is ever deleted or rewritten in place: the source project is read only.
"""

from __future__ import annotations

import hashlib
import os
import stat
from typing import Any, Dict, List, Optional, Tuple

from . import hcp
from .errors import ToolError

KIND_POU_IL = "pou_il"
KIND_POU_LD = "pou_ld"
KIND_PROJECT_INDEX = "project_index"
KIND_CONFIG = "config"
KIND_STALE_ARTIFACT = "stale_artifact"
KIND_UNKNOWN = "opaque_unknown"
KIND_TOOL_ARTIFACT = "tool_artifact"
KIND_OTHER_DIR = "other_directory"

SUPPORT_EDITABLE = "editable_il"
SUPPORT_LD = "read_only_ld"
SUPPORT_PARSE_ONLY = "parse_only"
SUPPORT_OPAQUE = "opaque"
SUPPORT_STALE = "stale_excluded"

CONFIG_EXTENSIONS = (".cfg", ".ini", ".sdt", ".gdt", ".dev", ".prg")

# --- Explicit stale-artifact set (verified, not inferred) -------------------
# Compilation / upload output of the GUI.  None of these files is a project
# source; the GUI regenerates them.  Excluded from packages, quarantined in the
# patched copy, never delivered as "compiled output".
STALE_FILE_NAMES: Tuple[str, ...] = (
    "output.prg",
    "upload.prg",
    "output.itm",
    "output.sdt",
    "proginfo.dat",
    "steps.dat",
    "crosstable.crs",
    "elemuseinfo.esi",
    "main.odt",
    "mdi.cfg",
)
STALE_EXTENSIONS: Tuple[str, ...] = (".hcpp", ".tmp")
STALE_DIR_NAMES: Tuple[str, ...] = ("compile", "temp", "backup")

STALE_BASIS = {
    "output.prg": "编译/上传产物 Output.prg（不在工程索引中，GUI 重新生成）",
    "upload.prg": "上传产物 Upload.prg（GUI 重新生成）",
    "output.itm": "编译中间产物 Output.itm",
    "output.sdt": "编译输出 Output.sdt",
    "proginfo.dat": "编译/下载信息 ProgInfo.dat（不在工程索引中）",
    "steps.dat": "编译步骤数据 steps.dat（不在工程索引中）",
    "crosstable.crs": "交叉引用表，由 GUI 重新生成",
    "elemuseinfo.esi": "元件使用表，由 GUI 重新生成",
    "main.odt": "未登记的生成数据块 MAIN.odt，由 GUI 重新生成",
    "mdi.cfg": "失效的 MDI 配置缓存（人工核对确认）",
}

# Files whose bytes must survive patching and packaging.  Used for the explicit
# preservation report; every project-content file is preserved, not just these.
REQUIRED_PRESERVED_NAMES: Tuple[str, ...] = ("main.dat", "main.mon", "canlink.prg")
REQUIRED_PRESERVED_NOTE = (
    "MAIN.dat（软元件内存/用户参数备份）、MAIN.mon（用户监视表）、CANLink.prg（配置）"
    "属于工程内容，必须原字节保留，绝不作为缓存排除或隔离。"
)

TOOL_DIR_NAME = "_offline_patch"
PACKAGE_MANIFEST_NAME = "_offline_package_manifest.json"
TOOL_PREFIX = "_offline_"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_within(child: str, parent: str) -> bool:
    child_n = _norm(child)
    parent_n = _norm(parent)
    if child_n == parent_n:
        return True
    return child_n.startswith(parent_n.rstrip(os.sep) + os.sep)


def is_link_like(path: str) -> bool:
    """True for POSIX symlinks, Windows symlinks and mount points/junctions.

    Junctions are reparse points but are not reported by ``os.path.islink`` on
    older Python builds, so the file attributes are checked as well.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_no_link_components(path: str, project_dir: str, *, what: str) -> None:
    """Every component of ``path`` below ``project_dir`` must be a real entry."""
    base = os.path.abspath(project_dir)
    current = os.path.abspath(path)
    chain: List[str] = []
    while True:
        chain.append(current)
        if _norm(current) == _norm(base):
            break
        parent = os.path.dirname(current)
        if parent == current:  # pragma: no cover - defensive
            break
        current = parent
    for component in reversed(chain):
        if component == base:
            continue
        if is_link_like(component):
            raise ToolError(
                "symlink_rejected",
                "%s 的路径分量是符号链接/联接点（reparse point），拒绝访问：%s"
                % (what, os.path.relpath(component, base)),
                {"value": os.path.relpath(component, base), "kind": "link_component"},
            )


def resolve_entry(project_dir: str, name: str, *, what: str = "文件") -> str:
    """Resolve a caller supplied *basename* inside ``project_dir``.

    Rejects absolute paths, any directory component, ``..`` traversal, symlinks,
    junction/reparse points and anything that resolves outside the project.
    """
    if not isinstance(name, str) or not name.strip():
        raise ToolError("invalid_arguments", "%s 名称不能为空。" % what, {})
    raw = name.strip()
    if os.path.isabs(raw) or (len(raw) > 1 and raw[1] == ":") or raw.startswith(("\\\\", "//")):
        raise ToolError(
            "absolute_path_rejected",
            "%s 只接受工程内的相对 basename，拒绝绝对路径：%r" % (what, name),
            {"value": name},
        )
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise ToolError(
            "path_traversal_rejected",
            "%s 只接受工程内的相对 basename，拒绝路径分量：%r" % (what, name),
            {"value": name},
        )
    base = os.path.abspath(project_dir)
    if is_link_like(base):
        raise ToolError(
            "symlink_rejected",
            "工程目录本身是符号链接/联接点：%s" % base,
            {"project": base},
        )
    candidate = os.path.join(base, raw)
    if not is_within(candidate, base):
        raise ToolError(
            "path_outside_project",
            "%s 解析后越出工程目录：%r" % (what, name),
            {"value": name},
        )
    _assert_no_link_components(candidate, base, what=what)
    if not os.path.exists(candidate):
        raise ToolError(
            "file_not_found",
            "工程内不存在该 %s：%r" % (what, name),
            {"value": name, "project": base},
        )
    if os.path.isdir(candidate):
        raise ToolError("file_is_directory", "%s 是目录：%r" % (what, name), {"value": name})
    if is_link_like(candidate):
        raise ToolError(
            "symlink_rejected",
            "%s 是符号链接/联接点，拒绝跟随：%r" % (what, name),
            {"value": name},
        )
    if not is_within(os.path.realpath(candidate), os.path.realpath(base)):
        raise ToolError(
            "path_outside_project",
            "%s 的真实路径越出工程目录：%r" % (what, name),
            {"value": name},
        )
    return candidate


def check_destination(source_dir: str, dest_dir: str) -> str:
    """Validate a new output directory that must not exist and must be separate.

    Both the literal path and the *real* parent directory are checked, so a
    symlinked or junctioned parent cannot smuggle the output into the source
    project.
    """
    if not isinstance(dest_dir, str) or not dest_dir.strip():
        raise ToolError("invalid_arguments", "目标目录不能为空。", {})
    dest = os.path.abspath(dest_dir.strip())
    if is_link_like(dest):
        raise ToolError("symlink_rejected", "目标目录是符号链接/联接点：%s" % dest, {"dest": dest})
    if os.path.exists(dest):
        raise ToolError(
            "destination_exists",
            "目标目录已存在，拒绝覆盖：%s" % dest,
            {"dest": dest},
        )
    if _norm(dest) == _norm(source_dir):
        raise ToolError(
            "destination_inside_source",
            "目标目录不能与源工程目录相同（本工具从不原地修改）。",
            {"dest": dest},
        )
    if is_within(dest, source_dir):
        raise ToolError(
            "destination_inside_source",
            "目标目录不能位于源工程目录内部：%s" % dest,
            {"dest": dest, "source": os.path.abspath(source_dir)},
        )
    if is_within(source_dir, dest):
        raise ToolError(
            "source_inside_destination",
            "源工程目录不能位于目标目录内部：%s" % dest,
            {"dest": dest, "source": os.path.abspath(source_dir)},
        )

    parent = os.path.dirname(dest) or "."
    if not os.path.isdir(parent):
        try:
            os.makedirs(parent)
        except OSError as exc:
            raise ToolError(
                "destination_not_writable",
                "无法创建目标父目录：%s" % exc,
                {"dest": dest, "parent": parent},
            )
    _assert_no_link_components(parent, _nearest_existing_ancestor(parent), what="目标父目录")
    if not os.access(parent, os.W_OK):
        raise ToolError(
            "destination_not_writable",
            "目标父目录不可写：%s" % parent,
            {"dest": dest, "parent": parent},
        )
    real_parent = os.path.realpath(parent)
    real_dest = os.path.join(real_parent, os.path.basename(dest))
    real_source = os.path.realpath(source_dir)
    if is_within(real_dest, real_source) or is_within(real_source, real_dest):
        raise ToolError(
            "destination_inside_source",
            "目标目录的真实路径与源工程目录重叠（父目录可能是链接）：%s" % real_dest,
            {"dest": dest, "real_dest": real_dest, "real_source": real_source},
        )
    return dest


def _nearest_existing_ancestor(path: str) -> str:
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:  # pragma: no cover - defensive
            return current
        current = parent
    return current


def classify(rel_path: str, entry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify one file of the project by explicit rules."""
    name = os.path.basename(rel_path)
    lower = name.lower()
    file_type = entry.get("file_type") if entry else None

    if name.startswith(TOOL_PREFIX):
        return _record(
            KIND_TOOL_ARTIFACT,
            SUPPORT_OPAQUE,
            include=False,
            quarantine=False,
            basis="本工具产生的辅助文件（隔离目录/清单）",
            reason="tool_artifact",
            note="本工具生成，不属于工程内容，也不参与差异比较。",
        )

    if lower in STALE_FILE_NAMES:
        return _record(
            KIND_STALE_ARTIFACT,
            SUPPORT_STALE,
            include=False,
            quarantine=True,
            basis=STALE_BASIS.get(lower, "显式列出的失效缓存/陈旧编译产物"),
            reason="stale_cache_or_compiled_output",
            note="陈旧或由 GUI 重新生成的产物：打包时排除、补丁副本中隔离，绝不作为编译结果交付。",
        )

    for suffix in STALE_EXTENSIONS:
        if lower.endswith(suffix):
            return _record(
                KIND_STALE_ARTIFACT,
                SUPPORT_STALE,
                include=False,
                quarantine=True,
                basis="显式列出的临时/缓存文件类型 %s" % suffix,
                reason="stale_cache_or_compiled_output",
                note="临时或缓存文件：打包时排除、补丁副本中隔离。",
            )

    if lower.endswith(hcp.HCP_SUFFIX):
        return _record(
            KIND_PROJECT_INDEX,
            SUPPORT_PARSE_ONLY,
            include=True,
            quarantine=False,
            basis="AutoShop 工程索引 .hcp",
            reason=None,
            note="仅解析（机型/文件表/版本），不重写。",
        )

    if lower.endswith(".il"):
        return _record(
            KIND_POU_IL,
            SUPPORT_EDITABLE,
            include=True,
            quarantine=False,
            basis="IL 明文载荷 POU",
            reason=None,
            note="可读取并在新副本中打补丁（要求唯一匹配）。",
        )

    if lower.endswith(".ld"):
        return _record(
            KIND_POU_LD,
            SUPPORT_LD,
            include=True,
            quarantine=False,
            basis="梯形图 POU",
            reason=None,
            note="本版本不支持解析或修改 LD，只能原样复制保留。",
        )

    if lower in REQUIRED_PRESERVED_NAMES:
        return _record(
            KIND_CONFIG,
            SUPPORT_OPAQUE,
            include=True,
            quarantine=False,
            basis="工程内容（用户参数备份/监视表/配置），必须原字节保留",
            reason=None,
            note=REQUIRED_PRESERVED_NOTE,
        )

    if lower.endswith(CONFIG_EXTENSIONS):
        return _record(
            KIND_CONFIG,
            SUPPORT_OPAQUE,
            include=True,
            quarantine=False,
            basis="工程配置文件（按扩展名判定）",
            reason=None,
            note="按字节保留，不解析内容。",
        )

    note = "未识别的工程文件：原始字节保留，不做解释，也不清除。"
    if file_type is not None:
        note += " HCP FileType=%s 不足以判定内容，本工具不据此删除或隔离文件。" % file_type
    return _record(
        KIND_UNKNOWN,
        SUPPORT_OPAQUE,
        include=True,
        quarantine=False,
        basis="未识别文件，按字节保留（未知即保留）",
        reason=None,
        note=note,
    )


def _record(
    kind: str,
    support: str,
    *,
    include: bool,
    quarantine: bool,
    basis: str,
    reason: Optional[str],
    note: str,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "support": support,
        "include_in_package": include,
        "quarantine_on_patch": quarantine,
        "basis": basis,
        "excluded_reason": reason,
        "note": note,
    }


def classify_dir(rel_dir: str) -> Dict[str, Any]:
    """Classify a directory (only the explicit stale directories are excluded)."""
    name = os.path.basename(rel_dir.rstrip("/")).lower()
    if name in STALE_DIR_NAMES:
        return {
            "kind": KIND_STALE_ARTIFACT,
            "support": SUPPORT_STALE,
            "include_in_package": False,
            "quarantine_on_patch": True,
            "basis": "显式列出的失效目录 %s/（编译/临时/备份）" % name,
            "excluded_reason": "stale_cache_or_compiled_output",
            "note": "整个目录及其内容按陈旧缓存处理。",
        }
    return {
        "kind": KIND_OTHER_DIR,
        "support": SUPPORT_OPAQUE,
        "include_in_package": True,
        "quarantine_on_patch": False,
        "basis": "普通子目录",
        "excluded_reason": None,
        "note": "内容逐文件按字节保留。",
    }


def entry_index(project_meta: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for entry in project_meta.get("files", []):
        index[entry["file_name"].lower()] = entry
    return index


def iter_paths(project_dir: str) -> Tuple[List[str], List[str]]:
    """All files and directories below ``project_dir`` as relative paths.

    Symlinks, junctions or any other reparse point inside the project are
    rejected outright: they are never skipped silently, because skipping would
    hide project content and following would read outside the project.
    """
    files: List[str] = []
    dirs: List[str] = []
    base = os.path.abspath(project_dir)
    if is_link_like(base):
        raise ToolError(
            "symlink_rejected",
            "工程目录本身是符号链接/联接点：%s" % base,
            {"project": base},
        )
    for root, dir_names, file_names in os.walk(base, followlinks=False):
        for dir_name in sorted(dir_names):
            full = os.path.join(root, dir_name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if is_link_like(full):
                raise ToolError(
                    "symlink_rejected",
                    "工程内目录是符号链接/联接点（reparse point），拒绝处理：%s" % rel,
                    {"rel_path": rel, "kind": "directory"},
                )
            dirs.append(rel)
        dir_names[:] = sorted(dir_names)
        for file_name in sorted(file_names):
            full = os.path.join(root, file_name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            if is_link_like(full):
                raise ToolError(
                    "symlink_rejected",
                    "工程内文件是符号链接/联接点（reparse point），拒绝处理：%s" % rel,
                    {"rel_path": rel, "kind": "file"},
                )
            if not os.path.isfile(full):
                raise ToolError(
                    "unsupported_format",
                    "工程内存在既非普通文件也非目录的条目，拒绝处理：%s" % rel,
                    {"rel_path": rel},
                )
            files.append(rel)
    return sorted(files), sorted(dirs)


def iter_files(project_dir: str) -> List[str]:
    """All regular files below ``project_dir`` (see :func:`iter_paths`)."""
    return iter_paths(project_dir)[0]


def _first_stale_dir_component(rel_path: str) -> Optional[str]:
    parts = rel_path.split("/")[:-1]
    for part in parts:
        if part.lower() in STALE_DIR_NAMES:
            return part
    return None


def _logical_path(rel_path: str) -> Tuple[str, bool]:
    """Map a quarantine path back to the project-relative path it came from."""
    stale_prefix = TOOL_DIR_NAME + "/stale/"
    if rel_path.startswith(stale_prefix):
        return rel_path[len(stale_prefix) :], True
    return rel_path, False


def file_info(project_dir: str, rel_path: str, entries: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    full = os.path.join(project_dir, rel_path.replace("/", os.sep))
    logical, quarantined = _logical_path(rel_path)
    name = os.path.basename(logical)
    entry = entries.get(name.lower())
    info = classify(logical, entry)

    in_tool_dir = rel_path.startswith(TOOL_DIR_NAME + "/")
    if in_tool_dir and not quarantined:
        info = _record(
            KIND_TOOL_ARTIFACT,
            SUPPORT_OPAQUE,
            include=False,
            quarantine=False,
            basis="本工具产生的辅助文件（隔离区/清单）",
            reason="tool_artifact",
            note="本工具生成，不属于工程内容，也不参与差异比较。",
        )
    else:
        stale_dir = _first_stale_dir_component(logical)
        if quarantined and info["kind"] != KIND_STALE_ARTIFACT:
            info = _record(
                KIND_STALE_ARTIFACT,
                SUPPORT_STALE,
                include=False,
                quarantine=True,
                basis="已隔离的失效缓存（原相对路径 %s）" % logical,
                reason="quarantined_stale",
                note="由本工具的隔离步骤移动而来，未删除；仅存在于新副本中。",
            )
        elif stale_dir and info["kind"] != KIND_STALE_ARTIFACT:
            info = _record(
                KIND_STALE_ARTIFACT,
                SUPPORT_STALE,
                include=False,
                quarantine=True,
                basis="位于显式列出的失效目录 %s/" % stale_dir,
                reason="stale_cache_or_compiled_output",
                note="随失效目录整体排除与隔离。",
            )

    stat_result = os.stat(full)
    record: Dict[str, Any] = {
        "rel_path": rel_path,
        "logical_rel_path": logical,
        "name": name,
        "quarantined": quarantined,
        "size": stat_result.st_size,
        "mtime": int(stat_result.st_mtime),
        "sha256": sha256_file(full),
        "registered_in_hcp": entry is not None,
        "file_type": entry.get("file_type") if entry else None,
        "prog_type": entry.get("prog_type") if entry else None,
        "pou_id": entry.get("pou_id") if entry else None,
        "caption": entry.get("caption") if entry else None,
    }
    record.update(info)
    if record["kind"] == KIND_POU_IL:
        from . import il

        with open(full, "rb") as handle:
            verdict = il.detect(handle.read())
        record["il_supported"] = verdict["supported"]
        record["il_reason"] = verdict["reason"]
        if not verdict["supported"]:
            record["support"] = SUPPORT_OPAQUE
            record["note"] = "IL 头部/载荷未通过严格校验（%s），只按字节保留。" % verdict["reason"]
    return record


def inventory(project_dir: str, project_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    meta = project_meta if project_meta is not None else hcp.project_meta_from_dir(project_dir)
    entries = entry_index(meta)
    return [file_info(project_dir, rel, entries) for rel in iter_files(project_dir)]


def stale_paths(project_dir: str, project_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Everything that will be quarantined/excluded, listed explicitly."""
    meta = project_meta if project_meta is not None else hcp.project_meta_from_dir(project_dir)
    files, dirs = iter_paths(project_dir)
    entries = entry_index(meta)
    out: List[Dict[str, Any]] = []
    for rel in dirs:
        record = classify_dir(rel)
        if record["kind"] == KIND_STALE_ARTIFACT:
            out.append(
                {
                    "rel_path": rel + "/",
                    "entry_type": "directory",
                    "size": None,
                    "sha256": None,
                    "basis": record["basis"],
                }
            )
    for rel in files:
        info = file_info(project_dir, rel, entries)
        if info["kind"] == KIND_STALE_ARTIFACT:
            out.append(
                {
                    "rel_path": rel,
                    "entry_type": "file",
                    "size": info["size"],
                    "sha256": info["sha256"],
                    "basis": info["basis"],
                }
            )
    return out


def summary(files: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_kind: Dict[str, int] = {}
    for record in files:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    return {
        "total_files": len(files),
        "by_kind": dict(sorted(by_kind.items())),
        "editable_il": sorted(
            r["rel_path"] for r in files if r["kind"] == KIND_POU_IL and r.get("il_supported")
        ),
        "read_only_ld": sorted(r["rel_path"] for r in files if r["kind"] == KIND_POU_LD),
        "unregistered": sorted(r["rel_path"] for r in files if not r["registered_in_hcp"]),
        "stale_artifacts": sorted(r["rel_path"] for r in files if r["kind"] == KIND_STALE_ARTIFACT),
        "preserved_required": sorted(
            r["rel_path"] for r in files if r["name"].lower() in REQUIRED_PRESERVED_NAMES
        ),
    }
