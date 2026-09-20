"""Version-bound H3U native compiler. Vendor code runs only in a child and a copy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from xml.etree import ElementTree

from . import hcp, il
from .errors import ToolError
from .project import iter_paths

NATIVE = Path(__file__).with_name("native")
PROFILE = json.loads((NATIVE / "profile.json").read_text("utf-8"))
DERIVED = {"output.prg", "upload.prg", "output.itm", "output.sdt", "proginfo.dat",
           "steps.dat", "crosstable.crs", "elemuseinfo.esi", "main.odt", "mdi.cfg"}
SKIP_DIRS = {"compile", "temp", "backup", "_offline_patch"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path) -> dict[str, str]:
    iter_paths(str(root))  # reject links/junctions before walking or copying
    return {p.relative_to(root).as_posix(): sha(p) for p in root.rglob("*") if p.is_file()}


def derived(name: str) -> bool:
    p = Path(name)
    return (p.name.lower() in DERIVED or p.suffix.lower() in {".tmp", ".hcpp"}
            or any(s.lower() in SKIP_DIRS for s in p.parts[:-1]))


def runtime_status(install_dir: str | None = None) -> dict:
    configured = install_dir or os.environ.get("AUTOSHOP_INSTALL_DIR")
    root = Path(configured).resolve() if configured else None
    failures = []
    if root is None:
        failures.append("AUTOSHOP_INSTALL_DIR is not configured")
    else:
        for name, expected in PROFILE["files"].items():
            p = root / name
            if not p.is_file() or sha(p) != expected:
                failures.append(name)
    explicit_mfc = os.environ.get("AUTOSHOP_MFC_PATH")
    candidates = [Path(explicit_mfc)] if explicit_mfc and not failures else []
    # Without a valid vendor installation, scanning WinSxS cannot make this
    # backend available and can stall capabilities on a cold Windows runner.
    if not explicit_mfc and os.name == "nt" and not failures:
        if root:
            candidates.extend([root / "mfc90.dll", root / "Microsoft.VC90.MFC/mfc90.dll"])
        sxs = Path(os.environ.get("WINDIR", "C:/Windows")) / "WinSxS"
        candidates.extend(sxs.glob("x86_microsoft.vc90.mfc_*/mfc90.dll"))
        candidates.extend(sxs.glob("Fusion/x86_microsoft.vc90.mfc_*/*/*/mfc90.dll"))
    matching_mfc = next((p for p in candidates if p.is_file() and sha(p) == PROFILE["mfc_sha256"]), None)
    if not matching_mfc:
        failures.append("mfc90.dll")
    if not (NATIVE / "host.exe").is_file():
        failures.append("host.exe (run native/build.cmd)")
    if os.name != "nt":
        failures.append("native backend requires Windows")
    return {"available": not failures,
            "profile": PROFILE["profile"], "install_dir": str(root) if root else None,
            "mfc_candidate": str(matching_mfc) if matching_mfc else None,
            "mismatches": failures, "backend": "vendor-dll-x86-child", "gui_required": False,
            "supported_model": "H3U", "native_compiled": False}


def compile_copy(project: str, dest: str, install_dir: str | None = None) -> dict:
    started = time.perf_counter()
    src, out = Path(project).resolve(), Path(dest).resolve()
    if out == src or src in out.parents or out in src.parents:
        raise ToolError("invalid_destination", "构建目录必须与原工程分开。")
    if out.exists():
        raise ToolError("destination_exists", "构建目录已存在，换一个新目录。")
    runtime = runtime_status(install_dir)
    if not runtime["available"]:
        raise ToolError("native_runtime_rejected", "原厂 DLL/运行库版本不匹配，或缺少 x86 host.exe。", runtime)
    meta = hcp.project_meta_from_dir(str(src))
    xml = hcp.decode((src / meta["index_file"]).read_bytes())
    if b"<!DOCTYPE" in xml.replace(b"\x00", b""):
        raise ToolError("unsupported_project", "不接受包含外部声明的工程索引。")
    if meta["machine_model"] != "H3U" or meta["hardware_file"].lower() != "h3u.dll":
        raise ToolError("unsupported_project", "当前原生后端仅验证了 H3U。")
    encrypted_all = ElementTree.fromstring(xml).findtext("AllEncrypted", "0").strip()
    if encrypted_all not in ("", "0") or meta["has_password"] or any(f["encrypted"] not in (0, None) for f in meta["files"]):
        raise ToolError("encrypted_project_rejected", "当前后端不支持受保护的工程或程序块。")
    blocks = [f for f in meta["files"] if f["prog_type"] in (0, 1, 2, 4, 6)]
    if not blocks or any(f["file_type"] not in (0, 1) for f in blocks):
        raise ToolError("unsupported_project", "当前编译后端只验证了 IL/LD 程序块。")
    expected = [Path(f["file_name"]).stem + ".compile" for f in blocks]
    if len(set(s.lower() for s in expected)) != len(expected):
        raise ToolError("unsupported_project", "存在同名程序块，不能确认编译覆盖率。")
    original = inventory(src)
    if original.get(meta["index_file"]) != meta["sha256"]:
        raise ToolError("source_changed", "读取索引期间工程发生变化。")
    for block in blocks:
        if block["file_name"] not in original:
            raise ToolError("missing_source", "工程缺少程序块。", {"file": block["file_name"]})
    out.mkdir(parents=True)
    logs = out / "logs"
    logs.mkdir()
    report = {"ok": False, "native_compiled": False, "tool": "native_compile_copy",
              "profile": runtime["profile"], "source": str(src), "destination": str(out),
              "source_hashes": original, "blocks": [f["file_name"] for f in blocks],
              "passes": [], "diagnostics": [], "strategy": "fresh-cache-coverage-and-stable-link"}
    # Separate ASCII path avoids the ANSI vendor host's path/code-page limitations.
    with tempfile.TemporaryDirectory(prefix="as-native-") as tmp:
        work = Path(tmp) / "project"
        work.mkdir()
        try:
            for name in original:
                # Cross-reference data is an input to LoadAll, but never a compiled block cache.
                if derived(name) and name.lower() != "crosstable.crs":
                    continue
                target = work / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / name, target)
            decoded = Path(tmp) / "index.xml"
            decoded.write_bytes(xml)
            previous = None
            complete = False
            for number in range(1, len(blocks) + 3):
                output = work / "Output.prg"
                output.unlink(missing_ok=True)  # each pass must generate a new output
                cmd = [str(NATIVE / "host.exe"), str(NATIVE / "autoshop-native.manifest"),
                       str(work), runtime["install_dir"], str(decoded), meta["index_file"]]
                proc = subprocess.run(cmd, cwd=work, capture_output=True,
                                      timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                text = proc.stdout.decode("gbk", errors="replace").replace("\r\n", "\n")
                (logs / f"pass-{number:02d}.log").write_text(text, "utf-8")
                diagnostics = []
                for line in text.splitlines():
                    m = re.match(r"diagnostic .* level=(\d+) page=(\d+) text=(.*)", line)
                    if m and int(m[1]) in (1, 3):
                        diagnostics.append({"severity": "error" if m[1] == "1" else "warning", "text": m[3]})
                report["diagnostics"].extend(d for d in diagnostics if d not in report["diagnostics"])
                missing = [s for s in expected if not (work / "Compile" / s).is_file()]
                digest = sha(output) if output.is_file() and output.stat().st_size else None
                report["passes"].append({"pass": number, "returncode": proc.returncode,
                                         "missing_blocks": missing, "output_sha256": digest})
                pass_files = inventory(work)
                changed_inputs = [n for n, v in original.items() if not derived(n) and pass_files.get(n) != v]
                if proc.returncode or any(d["severity"] == "error" for d in diagnostics):
                    raise ToolError("native_compile_failed", "原厂编译器报错。", {"pass": number, "changed_inputs": changed_inputs})
                if changed_inputs:
                    raise ToolError("configuration_changed", "编译改动了源码或配置，拒绝交付。", {"files": changed_inputs})
                if "compile_finished w=2 l=0" not in text or not digest:
                    raise ToolError("native_result_missing", "未取得原厂完成信号和新生成的程序。")
                if not missing and digest == previous:
                    complete = True
                    break
                previous = digest
            if not complete:
                raise ToolError("native_incomplete", "程序块未编齐或最终产物不稳定，拒绝交付。")
            after = inventory(work)
            changed = [n for n, v in original.items() if not derived(n) and after.get(n) != v]
            if changed:
                raise ToolError("configuration_changed", "编译改动了源码或配置，拒绝交付。", {"files": changed})
            if inventory(src) != original:
                raise ToolError("source_changed", "构建期间原工程发生变化，拒绝交付。")
            deliver = out / "project"
            deliver.mkdir()
            # Preserve ALL non-derived input content. Publish only measured fresh native outputs.
            for name in original:
                if not derived(name):
                    target = deliver / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / name, target)
            generated = {}
            for name in sorted(DERIVED):
                found = next((p for p in work.iterdir() if p.is_file() and p.name.lower() == name), None)
                if found and (name == "output.prg" or sha(found) != original.get(found.name)):
                    shutil.copy2(found, deliver / found.name)
                    generated[found.name] = sha(found)
            # Source/config copy must remain exact; cache is intentionally absent in delivery.
            final = inventory(deliver)
            if any(final.get(n) != v for n, v in original.items() if not derived(n)):
                raise ToolError("publication_integrity_failed", "交付副本内容核对失败。")
            archive = out / "compiled-project.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
                for path in sorted(deliver.rglob("*")):
                    if path.is_file():
                        z.write(path, path.relative_to(deliver).as_posix())
            with zipfile.ZipFile(archive) as z:
                if z.testzip() is not None:
                    raise ToolError("archive_invalid", "压缩包 CRC 校验失败。")
            report.update(ok=True, native_compiled=True, source_unchanged=True,
                          configuration_unchanged=True, fresh_block_coverage=len(expected),
                          output_sha256=digest, generated=generated, project=str(deliver),
                          archive=str(archive), archive_sha256=sha(archive))
        except (ToolError, subprocess.TimeoutExpired, OSError) as exc:
            report["error"] = (exc.to_dict()["error"] if isinstance(exc, ToolError)
                               else {"code": "native_execution_failed", "message": str(exc)})
            # Failed builds never publish a runnable project/zip, even after publication failure.
            archive = out / "compiled-project.zip"
            archive.unlink(missing_ok=True)
            deliver = out / "project"
            if deliver.exists():
                shutil.rmtree(deliver)
        finally:
            report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            report["report_path"] = str(out / "compile-report.json")
            (out / "compile-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report


def ld_to_il_copy(project: str, file: str, dest: str, install_dir: str | None = None) -> dict:
    """Vendor LD-to-IL conversion, gated by identical compiled bytes before/after."""
    return _convert_copy(project, file, dest, install_dir, reverse=False)


def il_to_ld_copy(project: str, file: str, dest: str, install_dir: str | None = None) -> dict:
    """Native IL-to-LD plus textual roundtrip and fresh machine-code equality."""
    return _convert_copy(project, file, dest, install_dir, reverse=True)


def _convert_copy(project, file, dest, install_dir, reverse):
    src, out = Path(project).resolve(), Path(dest).resolve()
    if out == src or src in out.parents or out in src.parents or out.exists():
        raise ToolError("invalid_destination", "转换目标必须是原工程以外的全新目录。")
    runtime = runtime_status(install_dir)
    if not runtime["available"]:
        raise ToolError("native_runtime_rejected", "原厂运行库版本不匹配。", runtime)
    meta = hcp.project_meta_from_dir(str(src))
    item = next((f for f in meta["files"] if f["file_name"] == file), None)
    source_type, target_type = (0, 1) if reverse else (1, 0)
    source_ext, target_ext = ('.il', '.LD') if reverse else ('.ld', '.IL')
    if not item or item["file_type"] != source_type or Path(file).suffix.lower() != source_ext:
        raise ToolError("unsupported_pou_type", "只能转换工程索引中已登记的对应语言程序块。")
    new_name = str(Path(file).with_suffix(target_ext))
    if (src / new_name).exists():
        raise ToolError("destination_exists", "同名目标块已存在，拒绝覆盖。")
    before = inventory(src)
    out.mkdir(parents=True)
    report = {"ok": False, "tool": "il_to_ld_copy" if reverse else "ld_to_il_copy", "source": str(src), "file": file,
              "new_file": new_name, "native_compiled": False}
    with tempfile.TemporaryDirectory(prefix="as-convert-") as tmp:
        root = Path(tmp)
        try:
            baseline = compile_copy(str(src), str(root / "baseline"), install_dir)
            (out / "baseline-report.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), "utf-8")
            if not baseline["ok"]:
                raise ToolError("baseline_compile_failed", "原工程编译未通过，停止转换。", baseline.get("error"))
            work = root / "work"
            shutil.copytree(src, work)
            decoded = root / "index.xml"
            index = work / meta["index_file"]
            xml = hcp.decode(index.read_bytes())
            decoded.write_bytes(xml)
            cmd = [str(NATIVE / "host.exe"), str(NATIVE / "autoshop-native.manifest"), str(work),
                   runtime["install_dir"], str(decoded), meta["index_file"], str(item["id"]), str(target_type)]
            proc = subprocess.run(cmd, cwd=work, capture_output=True,
                                  timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            log = proc.stdout.decode("gbk", errors="replace").replace("\r\n", "\n")
            (out / "conversion.log").write_text(log, "utf-8")
            if proc.returncode or "convert_finished w=1 " not in log or not (work / new_name).is_file():
                raise ToolError("native_conversion_failed", "原厂语言转换未成功。")
            if not reverse:
                il.parse((work / new_name).read_bytes(), origin=new_name)
            current = inventory(work)
            changed = [n for n, v in before.items() if not derived(n) and current.get(n) != v]
            unexpected = [n for n in current if n not in before and n != new_name and not derived(n)]
            if changed or unexpected:
                raise ToolError("conversion_changed_inputs", "原厂转换改动了其他文件，拒绝交付。", {"files": changed})
            # Preserve the decoded XML's layout; only change two fields in exactly one file element.
            text = xml.decode("utf-16")
            pattern = r'(<file\s+id="' + str(item["id"]) + r'">)(.*?)(</file>)'
            def replace(match):
                body = match[2]
                if body.count(f"<FileType>{source_type}</FileType>") != 1 or body.count("<FileName>" + file + "</FileName>") != 1:
                    raise ToolError("unsupported_index_layout", "索引字段格式不匹配。")
                return match[1] + body.replace(f"<FileType>{source_type}</FileType>", f"<FileType>{target_type}</FileType>").replace(
                    "<FileName>" + file + "</FileName>", "<FileName>" + new_name + "</FileName>") + match[3]
            updated, count = re.subn(pattern, replace, text, flags=re.S)
            if count != 1:
                raise ToolError("unsupported_index_layout", "程序块索引匹配数量不为 1。")
            plain = updated.encode("utf-16")
            key = hcp.HCP_KEY
            index.write_bytes(bytes(((v + key[(i + 1) % 11]) & 255) ^ (i & 255 if i % 2 == 0 else 0)
                                    for i, v in enumerate(plain)))
            (work / file).unlink()
            if reverse:
                decoded.write_bytes(plain)
                # Ignore blank lines and mnemonic/operand separator formatting only.
                # Keep operand strings, comments and every instruction in original order.
                def canonical(text):
                    lines = []
                    for line in text.splitlines():
                        if not line.strip():
                            continue
                        if line.lstrip().startswith('//'):
                            lines.append(line)
                        else:
                            lines.append(' '.join(line.split(maxsplit=1)))
                    return '\n'.join(lines)
                original_text = canonical(il.parse((src/file).read_bytes()).text)
                def roundtrip(label):
                    check = root/label
                    shutil.copytree(work, check)
                    back_cmd = [str(NATIVE/'host.exe'), str(NATIVE/'autoshop-native.manifest'),
                                str(check), runtime['install_dir'], str(decoded), meta['index_file'], str(item['id']), '0']
                    back = subprocess.run(back_cmd, cwd=check, capture_output=True, timeout=30,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
                    back_log = back.stdout.decode('gbk', errors='replace').replace('\r\n','\n')
                    (out/(label+'.log')).write_text(back_log, 'utf-8')
                    if (back.returncode or 'convert_finished w=1 ' not in back_log or not (check/file).is_file()
                            or canonical(il.parse((check/file).read_bytes()).text) != original_text):
                        actual = il.parse((check/file).read_bytes()).text if (check/file).is_file() else ''
                        raise ToolError('conversion_roundtrip_failed', '梯形图转回 IL 的文本不一致。',
                                        {'stage':label, 'diff':il.unified_text_diff(original_text, canonical(actual), 'original', 'roundtrip')})
                roundtrip('roundtrip-before')
                # The vendor compiler normalizes a newly generated LD once. Only that
                # block may change, followed by another IL roundtrip and strict compile.
                pre_normalize = inventory(work)
                normalize_cmd = [str(NATIVE/'host.exe'), str(NATIVE/'autoshop-native.manifest'),
                                 str(work), runtime['install_dir'], str(decoded), meta['index_file']]
                normalized = subprocess.run(normalize_cmd, cwd=work, capture_output=True, timeout=30,
                                             creationflags=subprocess.CREATE_NO_WINDOW)
                normalize_log = normalized.stdout.decode('gbk', errors='replace').replace('\r\n','\n')
                (out/'normalization.log').write_text(normalize_log, 'utf-8')
                if normalized.returncode or 'compile_finished w=2 l=0' not in normalize_log:
                    raise ToolError('native_compile_failed', '新梯形图原厂初始化编译失败。')
                normalized_files = inventory(work)
                old_inputs = {n:v for n,v in pre_normalize.items() if not derived(n) and n != new_name}
                new_inputs = {n:v for n,v in normalized_files.items() if not derived(n) and n != new_name}
                if old_inputs != new_inputs:
                    raise ToolError('configuration_changed', '梯形图初始化改动了其他源码或配置。')
                roundtrip('roundtrip-after')
                report['il_roundtrip_equal'] = True
                report['ld_normalized'] = pre_normalize[new_name] != normalized_files[new_name]
            compiled = compile_copy(str(work), str(out / "compiled"), install_dir)
            if not compiled["ok"] or compiled.get("output_sha256") != baseline["output_sha256"]:
                archive = out / "compiled" / "compiled-project.zip"
                archive.unlink(missing_ok=True)
                delivered = out / "compiled" / "project"
                if delivered.exists():
                    shutil.rmtree(delivered)
                raise ToolError("conversion_not_equivalent", "转换前后编译产物不一致，拒绝交付。")
            if inventory(src) != before:
                raise ToolError("source_changed", "转换期间原工程发生变化。")
            report.update(ok=True, native_compiled=True, binary_equivalent=True, source_unchanged=True,
                          configuration_unchanged=True, output_sha256=compiled["output_sha256"],
                          project=compiled["project"], archive=compiled["archive"],
                          changed_files=[meta["index_file"], file, new_name])
        except (ToolError, subprocess.TimeoutExpired, OSError) as exc:
            report["error"] = (exc.to_dict()["error"] if isinstance(exc, ToolError)
                               else {"code": "native_execution_failed", "message": str(exc)})
            for published in [out / "compiled" / "compiled-project.zip", out / "compiled" / "project"]:
                if published.is_dir():
                    shutil.rmtree(published)
                elif published.exists():
                    published.unlink()
        finally:
            report["report_path"] = str(out / "conversion-report.json")
            (out / "conversion-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return report
