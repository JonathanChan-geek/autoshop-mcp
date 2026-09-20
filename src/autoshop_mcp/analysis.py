"""Evidence-oriented IL analysis. This is not a PLC scan-cycle simulator."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import tempfile

from . import hcp, il, project as files
from .compiler import inventory
from .errors import ToolError

# Exact operand references only. Indexed/packed operands are reported unresolved.
DEVICE = re.compile(r"^(SD|SM|D|M|X|Y|S|T|C|R|Z|V)([0-9]+)$", re.I)
CONTACTS = {'LD', 'LDI', 'LDP', 'LDF', 'AND', 'ANI', 'ANDP', 'ANDF', 'OR', 'ORI', 'ORP', 'ORF'}
WRITES_FIRST = {'OUT', 'SET', 'RST'}
WRITES_LAST = {'MOV', 'DMOV', 'MOVP', 'DMOVP', 'ADD', 'DADD', 'ADDP', 'DADDP',
               'SUB', 'DSUB', 'SUBP', 'DSUBP', 'MUL', 'DMUL', 'DIV', 'DDIV'}
READ_WRITE = {'INC', 'DINC', 'INCP', 'DINCP', 'DEC', 'DDEC', 'DECP', 'DDECP'}
NO_OPERANDS = {'ANB', 'ORB', 'MPS', 'MRD', 'MPP', 'INV', 'END', 'FEND', 'SRET', 'IRET', 'NOP', 'MEP', 'MEF'}
COMPARE = re.compile(r'^(?:LD|AND|OR)(?:D)?(?:=|<>|>=|<=|>|<)$')


def canonical(token):
    match = DEVICE.fullmatch(token)
    if not match:
        return None
    family, number = match[1].upper(), match[2]
    if family in ('X', 'Y') and re.search('[89]', number):
        return None
    return family + str(int(number))  # preserve octal spelling, normalize leading zeros


def _rows(text):
    network = 0
    for line, source in enumerate(text.splitlines(), 1):
        # A // inside a quoted constant is data, not a comment delimiter.
        pieces = re.findall(r'"(?:[^"\\]|\\.)*"|//.*|[^\s"]+', source)
        code = []
        for token in pieces:
            if token.startswith('//'):
                break
            code.append('"STRING"' if token.startswith('"') else token)
        if source.strip().startswith('//'):
            network += 1
        if not code or code[0].startswith(';'):
            continue
        parts = code
        if parts and parts[0].isdigit():
            parts.pop(0)
        if parts:
            yield {'line': line, 'network': network, 'instruction': parts[0].upper(),
                   'operands': parts[1:], 'source': source}


def source_blocks(project):
    root = Path(project).resolve()
    meta = hcp.project_meta_from_dir(str(root))
    decoded = hcp.decode((root / meta['index_file']).read_bytes())
    from xml.etree import ElementTree
    protected = meta['has_password'] or ElementTree.fromstring(decoded).findtext('AllEncrypted', '0').strip() not in ('', '0')
    blocks, skipped = [], []
    for entry in meta['files']:
        if entry['prog_type'] not in (0, 1, 2, 4, 6):
            continue
        name = entry['file_name']
        reason = None
        if protected or entry['encrypted'] not in (None, 0):
            reason = 'protected'
        elif not (root / name).is_file():
            reason = 'missing_source'
        elif entry['file_type'] != 0 or Path(name).suffix.lower() != '.il':
            reason = 'requires_ld_conversion' if entry['file_type'] == 1 else 'unsupported_type'
        if reason:
            skipped.append({'file': name, 'reason': reason})
            continue
        try:
            doc = il.parse((root / name).read_bytes(), origin=name)
        except ToolError as exc:
            skipped.append({'file': name, 'reason': exc.to_dict()['error']['code']})
            continue
        blocks.append({'file': name, 'sha256': doc.sha256, 'text': doc.text,
                       'rows': list(_rows(doc.text))})
    return root, meta, blocks, skipped


def _coverage(blocks, skipped):
    return {'complete_source_coverage': not skipped and bool(blocks),
            'analyzed_blocks': [b['file'] for b in blocks], 'skipped_blocks': skipped,
            'scope': 'registered unencrypted IL blocks only; convert LD before full analysis'}


def project_search(project, query, case_sensitive=False, limit=200, context=2):
    if not isinstance(query, str) or not query:
        raise ToolError('invalid_arguments', 'query 必须为非空字面文本。')
    if type(case_sensitive) is not bool:
        raise ToolError('invalid_arguments', 'case_sensitive 必须是布尔值。')
    if type(limit) is not int or not 1 <= limit <= 2000 or type(context) is not int or not 0 <= context <= 10:
        raise ToolError('invalid_arguments', 'limit 范围 1–2000，context 范围 0–10。')
    _, _, blocks, skipped = source_blocks(project)
    needle = query if case_sensitive else query.casefold()
    matches, total = [], 0
    for block in blocks:
        lines = block['text'].splitlines()
        for n, text in enumerate(lines):
            if needle not in (text if case_sensitive else text.casefold()):
                continue
            total += 1
            if len(matches) < limit:
                matches.append({'file': block['file'], 'line': n+1, 'text': text,
                                'context_start': max(0, n-context)+1,
                                'context': lines[max(0,n-context):n+context+1]})
    return dict(ok=True, tool='project_search', matches=matches, total=total,
                truncated=total > limit, **_coverage(blocks, skipped))


def references(blocks):
    refs, unresolved, unknown = [], [], Counter()
    for block in blocks:
        for row in block['rows']:
            op, operands = row['instruction'], row['operands']
            known = op in CONTACTS | WRITES_FIRST | WRITES_LAST | READ_WRITE | NO_OPERANDS or bool(COMPARE.fullmatch(op))
            if not known:
                unknown[op] += 1
            for pos, token in enumerate(operands):
                dev = canonical(token)
                if not dev:
                    if re.search(r'(?:SD|SM|[DMXYSTRCZV])\d', token, re.I):
                        unresolved.append({'file': block['file'], 'line': row['line'], 'operand': token,
                                           'reason': 'compound, indexed, bit, packed or unsupported address'})
                    continue
                access = 'unknown'
                if op in CONTACTS or COMPARE.fullmatch(op):
                    access = 'read'
                elif op in WRITES_FIRST:
                    access = 'write' if pos == 0 else 'read'
                elif op in WRITES_LAST:
                    access = 'write' if pos == len(operands)-1 else 'read'
                elif op in READ_WRITE:
                    access = 'read_write'
                refs.append(dict(file=block['file'], line=row['line'], network=row['network'],
                                 instruction=op, device=dev, access=access, operand_index=pos,
                                 source=row['source']))
    return refs, unresolved, dict(sorted(unknown.items()))


def project_xref(project, device=None):
    if device is not None and (not isinstance(device, str) or canonical(device) is None):
        raise ToolError('invalid_arguments', 'device 必须是直接软元件地址，例如 X10、Y2、D500。')
    _, _, blocks, skipped = source_blocks(project)
    refs, unresolved, unknown = references(blocks)
    if device:
        refs = [r for r in refs if r['device'] == canonical(device)]
    return dict(ok=True, tool='project_xref', device=canonical(device) if device else None,
                references=refs, unresolved_operands=unresolved, unknown_instructions=unknown,
                indirect_and_implicit_coverage=False,
                note='只列显式地址；双字相邻寄存器、范围指令及间接地址未展开。unknown 不能当成只读或未使用。',
                **_coverage(blocks, skipped))


def project_audit(project):
    root, meta, blocks, skipped = source_blocks(project)
    refs, unresolved, unknown = references(blocks)
    findings, writers = [], defaultdict(list)
    for ref in refs:
        if ref['access'] in ('write', 'read_write'):
            writers[ref['device']].append(ref)
        if ref['access'] in ('write', 'read_write') and ref['device'].startswith('X'):
            findings.append({'code': 'input_write', 'severity': 'review', 'locations': [ref]})
    for dev, items in sorted(writers.items()):
        coils = [r for r in items if r['instruction'] == 'OUT']
        if len(coils) > 1:
            findings.append({'code': 'multiple_out', 'severity': 'review', 'device': dev, 'locations': coils,
                             'message': '同一软元件多处 OUT；核对调用顺序和实际执行条件，不自动判断冲突。'})
        if len({r['file'] for r in items}) > 1:
            findings.append({'code': 'cross_block_writes', 'severity': 'review', 'device': dev, 'locations': items})
    for entry in meta['files']:
        if not (root/entry['file_name']).is_file():
            findings.append({'code': 'missing_registered_file', 'severity': 'error', 'file': entry['file_name']})
    return dict(ok=True, tool='project_audit', findings=findings, finding_count=len(findings),
                unknown_instructions=unknown, unresolved_operands=unresolved,
                native_compiled=False, logic_verified=False,
                note='静态复核线索，不模拟扫描周期，不证明互锁、时序或机械动作正确。',
                **_coverage(blocks, skipped))


def project_export(project, dest):
    root, meta, blocks, skipped = source_blocks(project)
    before = inventory(root)
    out = Path(files.check_destination(str(root), dest))
    with tempfile.TemporaryDirectory(prefix='.as-export-', dir=out.parent) as tmp:
        stage = Path(tmp)/'export'
        stage.mkdir()
        entries = []
        for n, block in enumerate(blocks):
            # Sequential artifact names avoid interpreting a project name as a path.
            name = f'block-{n+1:03d}.il.txt'
            (stage/name).write_text(block['text'], 'utf-8')
            entries.append({'source_file': block['file'], 'sha256': block['sha256'], 'text_file': name})
        audit = project_audit(project)
        xref = project_xref(project)
        for name, data in [('audit.json', audit), ('xref.json', xref)]:
            (stage/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
        manifest = dict(format='autoshop-review-export/1', blocks=entries, index_sha256=meta['sha256'],
                        native_compiled=False, **_coverage(blocks, skipped))
        (stage/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
        (stage/'README.md').write_text('# 工程审查导出\n\n这是 UTF-8 指令文本、显式地址引用和静态复核线索，不是 AutoShop 工程或可下载程序。\n\n'
                                      'manifest.json 记录文件映射、哈希及未覆盖程序块；原配置和二进制工程未复制。\n', 'utf-8')
        if inventory(root) != before:
            raise ToolError('source_changed', '导出期间工程发生变化。')
        stage.rename(out)
    return dict(ok=True, tool='project_export', destination=str(out), manifest=manifest,
                source_unchanged=True, native_compiled=False)
