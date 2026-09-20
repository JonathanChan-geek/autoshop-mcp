"""Integration gates: actual vendor compiler, user-provided private fixtures."""
import json
import re
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autoshop_mcp import compiler, core, hcp, il

FIXTURE_A = os.environ.get("AUTOSHOP_TEST_FIXTURE")
FIXTURE_B = os.environ.get("AUTOSHOP_SECOND_FIXTURE")
UNLOADER = Path(FIXTURE_A) if FIXTURE_A else None
LOADER = Path(FIXTURE_B) if FIXTURE_B else None


@unittest.skipUnless(UNLOADER is not None and LOADER is not None and UNLOADER.is_dir() and LOADER.is_dir() and compiler.runtime_status()["available"], "Requires matching runtime and AUTOSHOP_TEST_FIXTURE / AUTOSHOP_SECOND_FIXTURE")
class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-test-")
        self.root = Path(self.tmp.name)
        self.src = self.root / "原工程"
        shutil.copytree(UNLOADER, self.src)
        self.before = compiler.inventory(self.src)

    def tearDown(self):
        self.assertEqual(compiler.inventory(self.src), self.before)
        self.tmp.cleanup()

    def compile(self, src=None, name="result"):
        return core.run_tool("native_compile_copy", {"project": str(src or self.src), "dest": str(self.root / name)})

    def output_edit(self):
        for item in hcp.project_meta_from_dir(str(self.src))["files"]:
            if item["file_type"] != 0:
                continue
            name = item["file_name"]
            text = il.parse((self.src / name).read_bytes()).text
            for match in re.finditer(r"(?m)^OUT[ \t]+Y([0-7]+)\n", text):
                old = match.group(0)
                if text.count(old) == 1:
                    replacement = "Y1" if match[1] == "0" else "Y0"
                    new = re.sub(r"Y[0-7]+", replacement, old)
                    return name, old, new
        self.skipTest("First fixture needs an unencrypted IL block with a unique OUT Y instruction")

    def test_both_baselines_are_byte_identical_from_empty_cache(self):
        for n, source in enumerate([self.src, LOADER]):
            with self.subTest(source=str(source)):
                original = compiler.inventory(source)
                result = self.compile(source, str(n))
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["output_sha256"], original["Output.prg"])
                self.assertEqual(result["fresh_block_coverage"], len(result["blocks"]))
                self.assertEqual(result["passes"][-1]["missing_blocks"], [])
                self.assertEqual(compiler.inventory(source), original)
                self.assertTrue(result["configuration_unchanged"])

    def test_bad_instruction_rejects_even_with_old_output_and_cache(self):
        bad = self.root / "bad"
        shutil.copytree(self.src, bad)
        name, old, _ = self.output_edit()
        f = bad / name
        doc = il.parse(f.read_bytes())
        f.write_bytes(il.render(doc.text.replace(old, old.replace("OUT", "INVALID_PLC_OPCODE", 1)), doc.header, doc.footer))
        (bad / "Compile").mkdir(exist_ok=True)
        (bad / "Compile" / "MAIN.compile").write_bytes(b"STALE")
        result = self.compile(bad)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "native_compile_failed")
        self.assertTrue(any("INVALID_PLC_OPCODE" in d["text"] for d in result["diagnostics"]))
        self.assertFalse((self.root / "result" / "project").exists())
        self.assertFalse((self.root / "result" / "compiled-project.zip").exists())

    def test_valid_edit_changes_machine_code_and_preserves_config(self):
        name, old, new = self.output_edit()
        edited = self.root / "edited"
        result = core.run_tool("il_patch_copy", dict(project=str(self.src), file=name,
            expected_sha256=self.before[name], old_text=old,
            new_text=new, dest=str(edited)))
        self.assertTrue(result["ok"], result)
        result = self.compile(edited)
        self.assertTrue(result["ok"], result)
        self.assertNotEqual(result["output_sha256"], self.before["Output.prg"])
        self.assertTrue(result["configuration_unchanged"])
        self.assertEqual((Path(result["project"]) / "MAIN.dat").read_bytes(), (self.src / "MAIN.dat").read_bytes())

    def test_unknown_version_rejected_before_loading(self):
        fake = self.root / "fake-install"
        fake.mkdir()
        (fake / "Converter.dll").write_bytes(b"not the validated vendor build")
        result = core.run_tool("native_compile_copy", dict(project=str(self.src), dest=str(self.root / "result"), install_dir=str(fake)))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "native_runtime_rejected")
        self.assertFalse((self.root / "result").exists())
        direct = subprocess.run([str(compiler.NATIVE / "host.exe"), "no-manifest", str(self.src), str(fake), "none.xml", hcp.project_meta_from_dir(str(self.src))["index_file"]], capture_output=True)
        self.assertEqual(direct.returncode, 20)
        self.assertNotIn(b"mfc_app_constructed", direct.stdout)

    def test_false_success_without_new_artifact_is_rejected(self):
        fake = subprocess.CompletedProcess([], 0, b"compile_finished w=2 l=0\n", b"")
        with patch.object(compiler.subprocess, "run", return_value=fake):
            result = self.compile()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "native_result_missing")

    def test_configuration_mutation_is_rejected(self):
        real = subprocess.run
        def tamper(cmd, **kwargs):
            result = real(cmd, **kwargs)
            (Path(cmd[2]) / "MAIN.dat").write_bytes(b"unexpected overwrite")
            return result
        with patch.object(compiler.subprocess, "run", side_effect=tamper):
            result = self.compile()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "configuration_changed")
        self.assertFalse((self.root / "result" / "project").exists())

    def test_all_ld_blocks_convert_equivalently_on_both_real_projects(self):
        for group, original in enumerate([self.src, LOADER]):
            current = original
            expected = compiler.sha(original / "Output.prg")
            for n, item in enumerate(hcp.project_meta_from_dir(str(original))["files"]):
                if item["file_type"] != 1:
                    continue
                with self.subTest(project=group, block=item["file_name"]):
                    result = core.run_tool("ld_to_il_copy", dict(project=str(current), file=item["file_name"], dest=str(self.root / f"convert-{group}-{n}")))
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["output_sha256"], expected)
                    current = Path(result["project"])
                    read = core.run_tool("il_read", dict(project=str(current), file=result["new_file"]))
                    self.assertTrue(read["ok"], read)
                    self.assertTrue(read["text"])

    def test_complete_conversion_workflow_has_full_analysis_coverage(self):
        result = core.run_tool('project_convert_all_copy', dict(project=str(self.src), dest=str(self.root/'all-il')))
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['output_sha256'], self.before['Output.prg'])
        self.assertTrue(result['binary_equivalent'])
        audit = core.run_tool('project_audit', dict(project=result['project']))
        self.assertTrue(audit['complete_source_coverage'], audit['skipped_blocks'])
        self.assertGreater(len(audit['analyzed_blocks']), 1)
        self.assertEqual((Path(result['project'])/'MAIN.dat').read_bytes(), (self.src/'MAIN.dat').read_bytes())

    def test_batch_build_and_bad_build_publish_correctly(self):
        name, old, new = self.output_edit()
        for label, replacement, succeeds in [('valid',new,True), ('invalid',old.replace('OUT','INVALID_PLC_OPCODE',1),False)]:
            with self.subTest(label=label):
                dest = self.root/label
                result = core.run_tool('project_build_copy', dict(project=str(self.src), dest=str(dest),
                    patches=[dict(file=name, expected_sha256=self.before[name],
                                  edits=[dict(old_text=old,new_text=replacement)])]))
                self.assertEqual(result['ok'], succeeds, result)
                self.assertEqual((dest/'project').exists(), succeeds)
                self.assertEqual((dest/'compiled-project.zip').exists(), succeeds)
                if succeeds:
                    self.assertNotEqual(result['output_sha256'], self.before['Output.prg'])
                    self.assertTrue(result['configuration_unchanged'])
                else:
                    self.assertFalse(result['native_compiled'])
                    self.assertTrue(any('INVALID_PLC_OPCODE' in d['text'] for d in result['stages'][-1]['diagnostics']))

    def test_il_to_ld_equivalent_or_explicitly_rejected_on_both_projects(self):
        for group, source in enumerate([self.src, LOADER]):
            successful = 0
            for n, item in enumerate(hcp.project_meta_from_dir(str(source))['files']):
                if item['file_type'] != 0:
                    continue
                with self.subTest(project=group, file=item['file_name']):
                    result = core.run_tool('il_to_ld_copy', dict(project=str(source), file=item['file_name'],
                                          dest=str(self.root/f'reverse-{group}-{n}')))
                    if not result['ok']:
                        self.assertIn(result['error']['code'], ['conversion_roundtrip_failed','conversion_not_equivalent'], result)
                        self.assertFalse((self.root/f'reverse-{group}-{n}'/'compiled'/'project').exists())
                        self.assertFalse((self.root/f'reverse-{group}-{n}'/'compiled'/'compiled-project.zip').exists())
                        continue
                    successful += 1
                    self.assertTrue(result['il_roundtrip_equal'])
                    self.assertEqual(result['output_sha256'], compiler.sha(source/'Output.prg'))
                    delivered = Path(result['project'])
                    self.assertFalse((delivered/item['file_name']).exists())
                    self.assertTrue((delivered/result['new_file']).is_file())
                    registered = hcp.project_meta_from_dir(str(delivered))['files']
                    self.assertEqual(next(f for f in registered if f['id']==item['id'])['file_type'], 1)
            self.assertGreater(successful, 0, 'Each real project needs an actually accepted reverse conversion')

    def test_symbols_chinese_append_update_and_fresh_reload(self):
        read = core.run_tool('symbols_read', dict(project=str(self.src)))
        self.assertTrue(read['ok'], read)
        changes = [dict(index=-1, name='验证符号', address='M6000', comment='中文注释\n第二行')]
        added = core.run_tool('symbols_patch_copy', dict(project=str(self.src), dest=str(self.root/'symbols-add'),
                             expected_sha256=read['sha256'], changes=changes))
        self.assertTrue(added['ok'], added)
        self.assertEqual(added['output_sha256'], self.before['Output.prg'])
        second = core.run_tool('symbols_read', dict(project=added['project']))
        self.assertEqual(second['rows'][-1]['name'], '验证符号')
        self.assertEqual(second['rows'][-1]['comment'], '中文注释\n第二行')
        changes[0].update(index=second['rows'][-1]['index'], name='ModifiedSymbol', comment='更新注释')
        updated = core.run_tool('symbols_patch_copy', dict(project=added['project'], dest=str(self.root/'symbols-update'),
                               expected_sha256=second['sha256'], changes=changes))
        self.assertTrue(updated['ok'], updated)
        third = core.run_tool('symbols_read', dict(project=updated['project']))
        self.assertEqual(third['rows'][-1]['name'], 'ModifiedSymbol')
        self.assertEqual(third['rows'][-1]['comment'], '更新注释')
        self.assertEqual(len(third['rows']), len(read['rows'])+1)
        final = compiler.inventory(Path(updated['project']))
        self.assertEqual([n for n,v in self.before.items() if not compiler.derived(n) and final.get(n)!=v], ['VarList.gdt'])

    def test_symbol_configuration_mutation_never_publishes(self):
        from autoshop_mcp import symbols
        real = symbols._run
        def tamper(work, root, runtime, meta, xml, request=None):
            rows = real(work, root, runtime, meta, xml, request)
            if request:
                (work/'MAIN.dat').write_bytes(b'changed')
            return rows
        with patch.object(symbols, '_run', side_effect=tamper):
            result = core.run_tool('symbols_patch_copy', dict(project=str(self.src), dest=str(self.root/'rejected-symbols'),
                         expected_sha256=self.before['VarList.gdt'],
                         changes=[dict(index=-1,name='DemoSymbol',address='M6000',comment='test')]))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'configuration_changed')
        self.assertFalse((self.root/'rejected-symbols').exists())


if __name__ == "__main__":
    unittest.main()
