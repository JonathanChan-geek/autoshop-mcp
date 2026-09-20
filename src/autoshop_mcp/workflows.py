"""Transactional offline edits and composition of verified native operations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

from . import hcp, il, project as files
from .analysis import source_blocks
from .compiler import compile_copy, inventory, ld_to_il_copy, sha
from .errors import ToolError


def _plan(root, patches):
    if not isinstance(patches, list) or not 1 <= len(patches) <= 100:
        raise ToolError('invalid_arguments', 'patches 必须为 1–100 个文件补丁。')
    _, _, blocks, skipped = source_blocks(str(root))
    available = {b['file'] for b in blocks}
    plans = {}
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {'file', 'expected_sha256', 'edits'}:
            raise ToolError('invalid_arguments', '每个补丁需要且仅接受 file、expected_sha256、edits。')
        name = patch['file']
        if not isinstance(name, str) or name not in available:
            raise ToolError('unsupported_pou_type', '只能修改已登记、未保护且可解析的 IL。', {'file': name, 'skipped': skipped})
        if name in plans:
            raise ToolError('duplicate_patch_file', '一个文件只能列一次，请合并 edits。')
        path = Path(files.resolve_entry(str(root), name, what='IL'))
        doc = il.parse(path.read_bytes())
        if not isinstance(patch['expected_sha256'], str) or patch['expected_sha256'].lower() != doc.sha256:
            raise ToolError('hash_mismatch', '文件哈希与补丁基准不一致。', {'file': name, 'actual_sha256': doc.sha256})
        edits = patch['edits']
        if not isinstance(edits, list) or not 1 <= len(edits) <= 100:
            raise ToolError('invalid_arguments', 'edits 必须为 1–100 个替换。')
        ranges = []
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {'old_text', 'new_text'}:
                raise ToolError('invalid_arguments', 'edit 需要且仅接受 old_text、new_text。')
            old, new = edit['old_text'], edit['new_text']
            if not isinstance(old, str) or not old or not isinstance(new, str) or '\r' in old+new:
                raise ToolError('invalid_arguments', '补丁必须是 LF 文本，old_text 不能为空。')
            start = doc.text.find(old)
            if start < 0 or doc.text.find(old, start+1) >= 0:
                raise ToolError('patch_not_unique', '旧文本必须在原文件中唯一出现。', {'file': name})
            ranges.append((start, start+len(old), new))
        ranges.sort()
        if any(b[0] < a[1] for a,b in zip(ranges, ranges[1:])):
            raise ToolError('overlapping_edits', '同一文件中的补丁区间重叠。', {'file': name})
        text = doc.text
        for start, end, new in reversed(ranges):
            text = text[:start]+new+text[end:]
        rendered = doc.render(text)
        if rendered == doc.raw:
            raise ToolError('no_change', '补丁没有改变文件。', {'file': name})
        plans[name] = {'bytes': rendered, 'before_sha256': doc.sha256,
                       'after_sha256': hashlib.sha256(rendered).hexdigest(),
                       'diff': il.unified_text_diff(doc.text, text, 'a/'+name, 'b/'+name)}
    return plans


def il_batch_patch_copy(project, patches, dest):
    root = Path(project).resolve()
    before = inventory(root)
    plans = _plan(root, patches)  # Validate every edit before creating any deliverable.
    out = Path(files.check_destination(str(root), dest))
    meta = hcp.project_meta_from_dir(str(root))
    stale = files.stale_paths(str(root), meta)
    stale_files = {s['rel_path'] for s in stale if s['entry_type'] == 'file'}
    stale_dirs = [s['rel_path'].rstrip('/')+'/' for s in stale if s['entry_type'] == 'directory']
    batch = '_offline_patch/batches/'+uuid.uuid4().hex
    mapping = {}
    with tempfile.TemporaryDirectory(prefix='.as-patch-', dir=out.parent) as tmp:
        stage = Path(tmp)/'project'
        stage.mkdir()
        for name in before:
            relocate = name not in plans and (name in stale_files or any(name.startswith(d) for d in stale_dirs))
            target_name = batch+'/stale/'+name if relocate else name
            mapping[name] = target_name
            target = stage/target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in plans:
                target.write_bytes(plans[name]['bytes'])
            else:
                shutil.copyfile(root/name, target)
        if inventory(root) != before:
            raise ToolError('source_changed', '创建补丁期间原工程发生变化。')
        after = inventory(stage)
        for name, original in before.items():
            expected = plans[name]['after_sha256'] if name in plans else original
            if after.get(mapping[name]) != expected:
                raise ToolError('copy_integrity_failed', '副本文件核验失败。', {'file': name})
        report = dict(ok=True, tool='il_batch_patch_copy', dest_project=str(out),
                      source_unchanged=True, configuration_unchanged=True, native_compiled=False,
                      patches=[dict(file=n, **{k:v for k,v in p.items() if k != 'bytes'}) for n,p in plans.items()],
                      quarantined={n:t for n,t in mapping.items() if n != t})
        manifest = stage/batch/'manifest.json'
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
        stage.rename(out)
    return report


def _publish_compiled(current, final, out, before, original):
    if inventory(original) != before:
        raise ToolError('source_changed', '流水线执行期间原工程发生变化。')
    target = out/'project'
    shutil.copytree(current, target)
    if inventory(target) != inventory(current):
        raise ToolError('publication_integrity_failed', '交付文件哈希不一致。')
    archive = out/'compiled-project.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for path in sorted(target.rglob('*')):
            if path.is_file():
                z.write(path, path.relative_to(target).as_posix())
    with zipfile.ZipFile(archive) as z:
        if z.testzip():
            raise ToolError('archive_invalid', '交付包 CRC 错误。')
    return dict(project=str(target), archive=str(archive), archive_sha256=sha(archive),
                output_sha256=final['output_sha256'], native_compiled=True,
                configuration_unchanged=True, source_unchanged=True)


def _pipeline(project, dest, install_dir, convert_all, patches, tool='project_build_copy'):
    root = Path(project).resolve()
    before = inventory(root)
    meta = hcp.project_meta_from_dir(str(root))
    out = Path(files.check_destination(str(root), dest))
    report = dict(ok=False, tool=tool, stages=[], native_compiled=False,
                  report_path=str(out/'build-report.json'))
    with tempfile.TemporaryDirectory(prefix='.as-build-', dir=out.parent) as tmp:
        workspace = Path(tmp)
        publication = workspace/'publication'
        publication.mkdir()
        current = root
        try:
            if convert_all:
                for n, block in enumerate(meta['files']):
                    if block['file_type'] != 1 or block['prog_type'] not in (0,1,2,4,6):
                        continue
                    result = ld_to_il_copy(str(current), block['file_name'], str(workspace/f'convert-{n}'), install_dir)
                    report['stages'].append({'stage':'convert', 'file':block['file_name'], 'ok':result['ok'],
                                             'output_sha256':result.get('output_sha256'), 'error':result.get('error')})
                    if not result['ok']:
                        raise ToolError('conversion_failed', '程序块转换失败。', result)
                    current = Path(result['project'])
            if patches:
                result = il_batch_patch_copy(str(current), patches, str(workspace/'edited'))
                report['stages'].append({'stage':'patch', 'ok':True, 'patches':result['patches']})
                current = Path(result['dest_project'])
            final = compile_copy(str(current), str(workspace/'compiled'), install_dir)
            report['stages'].append({'stage':'compile', 'ok':final['ok'], 'diagnostics':final.get('diagnostics', []),
                                     'passes':final.get('passes', []), 'error':final.get('error')})
            if not final['ok']:
                raise ToolError('build_compile_failed', '原厂编译失败；没有交付工程。', final.get('error'))
            conversions = [s for s in report['stages'] if s['stage'] == 'convert']
            if conversions and not patches and final['output_sha256'] != conversions[-1]['output_sha256']:
                raise ToolError('conversion_not_equivalent', '最终编译与转换基准机器码不一致。')
            delivered = _publish_compiled(Path(final['project']), final, publication, before, root)
            # All temporary paths must be rebased before returning the final artifact locations.
            delivered.update(project=str(out/'project'), archive=str(out/'compiled-project.zip'))
            report.update(ok=True, **delivered)
            if tool == 'project_convert_all_copy':
                report['binary_equivalent'] = True
            (publication/'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
            publication.rename(out)
        except (ToolError, OSError) as exc:
            report['error'] = exc.to_dict()['error'] if isinstance(exc, ToolError) else {'code':'build_failed','message':str(exc)}
            # Only a diagnostic report is published on failure, never intermediate runnable projects.
            out.mkdir()
            (out/'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    return report


def project_build_copy(project, dest, patches=None, convert_all=False, install_dir=None):
    if type(convert_all) is not bool or (patches is not None and (not isinstance(patches, list) or not patches)):
        raise ToolError('invalid_arguments', 'convert_all 必须是布尔值，patches 若提供必须是非空列表。')
    return _pipeline(project, dest, install_dir, convert_all, patches)


def project_convert_all_copy(project, dest, install_dir=None):
    return _pipeline(project, dest, install_dir, True, None, tool='project_convert_all_copy')
