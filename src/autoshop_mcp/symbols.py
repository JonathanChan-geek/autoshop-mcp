"""Fingerprint-bound native global symbol table I/O, always in disposable copies."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from xml.etree import ElementTree

from . import compiler, hcp
from .errors import ToolError
from .project import check_destination


def _context(src, install_dir):
    runtime = compiler.runtime_status(install_dir)
    if not runtime['available']:
        raise ToolError('native_runtime_rejected', '原厂运行库版本不匹配。', runtime)
    meta = hcp.project_meta_from_dir(str(src))
    xml = hcp.decode((src/meta['index_file']).read_bytes())
    if b'<!DOCTYPE' in xml.replace(b'\0', b''):
        raise ToolError('unsupported_project', '不接受外部声明。')
    if meta['machine_model'] != 'H3U' or meta['hardware_file'].lower() != 'h3u.dll':
        raise ToolError('unsupported_project', '当前符号接口仅验证 H3U。')
    if (meta['has_password'] or ElementTree.fromstring(xml).findtext('AllEncrypted', '0').strip() not in ('', '0')
            or any(f['encrypted'] not in (0, None) for f in meta['files'])):
        raise ToolError('encrypted_project_rejected', '不支持受保护工程。')
    if not (src/'VarList.gdt').is_file():
        raise ToolError('missing_source', '缺少 VarList.gdt。')
    return runtime, meta, xml


def _parse(log):
    rows = []
    try:
        for line in log.splitlines():
            if not line.startswith('symbol\t'):
                continue
            _, index, name, address, comment, check = line.split('\t')
            decode = lambda s: '' if s == '-' else bytes.fromhex(s).decode('gbk')
            rows.append(dict(index=int(index), name=decode(name), address=decode(address),
                             comment=decode(comment), check_result=int(check)))
        counts = re.findall(r'^symbols_count=(\d+)$', log, re.M)
        if (counts != [str(len(rows))] or [r['index'] for r in rows] != list(range(len(rows)))
                or 'symbols_finished=1' not in log):
            raise ValueError('incomplete symbol result')
    except (ValueError, UnicodeError) as exc:
        raise ToolError('native_result_invalid', '符号表读取结果不完整。') from exc
    return rows


def _run(work, root, runtime, meta, xml, request=None):
    index = root/'index.xml'
    index.write_bytes(xml)
    cmd = [str(compiler.NATIVE/'host.exe'), str(compiler.NATIVE/'autoshop-native.manifest'),
           str(work), runtime['install_dir'], str(index), meta['index_file'],
           'symbols-write' if request else 'symbols-read']
    if request:
        cmd.append(str(request))
    proc = subprocess.run(cmd, cwd=work, capture_output=True, timeout=30,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    log = proc.stdout.decode('gbk', errors='replace').replace('\r\n', '\n')
    if proc.returncode:
        raise ToolError('native_symbols_failed', '原厂符号表接口失败。', {'returncode':proc.returncode, 'log':log})
    return _parse(log)


def symbols_read(project, install_dir=None):
    src = Path(project).resolve()
    before = compiler.inventory(src)
    runtime, meta, xml = _context(src, install_dir)
    with tempfile.TemporaryDirectory(prefix='as-symbols-') as tmp:
        root = Path(tmp)
        work = root/'project'
        shutil.copytree(src, work)
        if compiler.inventory(work) != before:
            raise ToolError('source_changed', '读取期间工程发生变化。')
        rows = _run(work, root, runtime, meta, xml)
        if compiler.inventory(work) != before or compiler.inventory(src) != before:
            raise ToolError('source_changed', '符号读取改变了文件，拒绝结果。')
    return dict(ok=True, tool='symbols_read', file='VarList.gdt', sha256=before['VarList.gdt'],
                rows=rows, source_unchanged=True, profile=runtime['profile'])


def _plan(rows, changes):
    if not isinstance(changes, list) or not 1 <= len(changes) <= 100:
        raise ToolError('invalid_arguments', 'changes 需要 1–100 条修改。')
    expected = [dict(r) for r in rows]
    touched = set()
    lines = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'index', 'name', 'address', 'comment'}:
            raise ToolError('invalid_arguments', '每项需要 index、name、address、comment。')
        index = change['index']
        if type(index) is not int or index < -1 or index >= len(rows) or (index != -1 and index in touched):
            raise ToolError('invalid_arguments', 'index 必须是现有行号或 -1（新增），不能重复修改一行。')
        touched.add(index)
        fields = []
        for name, limit in [('name',255), ('address',63), ('comment',4095)]:
            value = change[name]
            if not isinstance(value, str) or '\0' in value:
                raise ToolError('invalid_arguments', '符号字段必须是无 NUL 的字符串。')
            try:
                raw = value.encode('gbk')
            except UnicodeError as exc:
                raise ToolError('unsupported_encoding', '原厂符号表只支持 GBK 字符。') from exc
            if len(raw) > limit:
                raise ToolError('invalid_arguments', '符号字段超过当前接口长度上限。', {'field':name})
            fields.append(raw.hex() or '-')
        if not re.fullmatch(r'[^\W\d]\w*', change['name'], re.UNICODE):
            raise ToolError('invalid_arguments', '名称只接受以字母、中文或下划线开头的标识符。')
        if not re.fullmatch(r'(?:X[0-7]+|Y[0-7]+|(?:M|D|R|S|T|C|SD|SM)\d+)', change['address']):
            raise ToolError('invalid_arguments', '当前只接受大写直接软元件地址。')
        row = dict(change, index=len(expected) if index == -1 else index, check_result=0)
        if index == -1:
            expected.append(row)
        else:
            expected[index] = row
        lines.append(str(index)+'\t'+'\t'.join(fields)+'\n')
    # Allow untouched legacy aliases, but never introduce another ambiguous name/address.
    for change in changes:
        for field in ('name', 'address'):
            key = change[field].casefold()
            if sum(r[field].casefold() == key for r in expected) > 1:
                raise ToolError('duplicate_symbol', '修改后存在重复名称或地址。', {'field':field})
    return expected, ''.join(lines)


def _values(rows):
    return [(r['index'],r['name'],r['address'],r['comment']) for r in rows]


def symbols_patch_copy(project, dest, expected_sha256, changes, install_dir=None):
    src = Path(project).resolve()
    out = Path(check_destination(str(src), dest))
    before = compiler.inventory(src)
    if not isinstance(expected_sha256, str) or before.get('VarList.gdt') != expected_sha256.lower():
        raise ToolError('hash_mismatch', 'VarList.gdt 已变化，先重新读取。')
    runtime, meta, xml = _context(src, install_dir)
    with tempfile.TemporaryDirectory(prefix='as-symbol-write-') as tmp:
        root = Path(tmp)
        work = root/'work'
        shutil.copytree(src, work)
        if compiler.inventory(work) != before:
            raise ToolError('source_changed', '复制期间原工程发生变化。')
        rows = _run(work, root, runtime, meta, xml)
        expected, protocol = _plan(rows, changes)
        request = root/'changes.txt'
        request.write_text(protocol, 'ascii')
        _run(work, root, runtime, meta, xml, request)
        after = compiler.inventory(work)
        if ({n:v for n,v in after.items() if n != 'VarList.gdt'} !=
                {n:v for n,v in before.items() if n != 'VarList.gdt'}):
            raise ToolError('configuration_changed', '符号写入改动了其他文件。')
        reloaded = _run(work, root, runtime, meta, xml)
        if _values(reloaded) != _values(expected) or compiler.inventory(work) != after:
            raise ToolError('symbols_roundtrip_failed', '独立进程回读不一致。')
        baseline = compiler.compile_copy(str(src), str(root/'baseline'), install_dir)
        compiled = compiler.compile_copy(str(work), str(root/'compiled'), install_dir)
        if not baseline['ok'] or not compiled['ok']:
            raise ToolError('native_compile_failed', '符号修改前后编译未全部通过。', {'baseline':baseline,'modified':compiled})
        if baseline['output_sha256'] != compiled['output_sha256']:
            raise ToolError('symbols_changed_logic', '符号修改改变了机器码；当前接口仅允许保持逻辑的修改。')
        if compiler.inventory(src) != before:
            raise ToolError('source_changed', '验证期间原工程发生变化。')
        # Stage beside destination so rename publishes the complete result on one volume.
        with tempfile.TemporaryDirectory(prefix='.as-symbol-publish-', dir=out.parent) as staging:
            stage = Path(staging)/'result'
            shutil.copytree(root/'compiled', stage)
            compile_report = dict(compiled, destination=str(out), project=str(out/'project'),
                                  archive=str(out/'compiled-project.zip'), report_path=str(out/'compile-report.json'))
            (stage/'compile-report.json').write_text(json.dumps(compile_report, ensure_ascii=False, indent=2), 'utf-8')
            (stage/'baseline-report.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2), 'utf-8')
            report = dict(ok=True, tool='symbols_patch_copy', source_unchanged=True,
                          other_configuration_unchanged=True, binary_equivalent=True, native_compiled=True,
                          before_sha256=before['VarList.gdt'], after_sha256=after['VarList.gdt'],
                          changed_files=['VarList.gdt'], rows=reloaded, output_sha256=compiled['output_sha256'],
                          project=str(out/'project'), archive=str(out/'compiled-project.zip'),
                          archive_sha256=compiled['archive_sha256'], report_path=str(out/'symbols-report.json'))
            (stage/'symbols-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
            stage.rename(out)
    return report
