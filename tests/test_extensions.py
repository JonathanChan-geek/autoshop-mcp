import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autoshop_mcp import analysis, compiler, core, hcp, il, server, workflows
from fixtures import create_project


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root/'source'
        create_project(self.src)
        self.original = compiler.inventory(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, name, **args):
        return core.run_tool(name, dict(project=str(self.src), **args))

    def write_il(self, text):
        file = self.src/'DEMO.IL'
        doc = il.parse(file.read_bytes())
        file.write_bytes(doc.render(text))

    def patches(self, edits=None):
        return [dict(file='DEMO.IL', expected_sha256=compiler.sha(self.src/'DEMO.IL'),
                     edits=edits or [dict(old_text='OUT\t\t Y1\n',new_text='OUT\t\t Y2\n')])]

    def test_search_context_limit_and_partial_coverage(self):
        result = self.call('project_search', query='out', limit=1, context=1)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['total'], 2)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['matches'][0]['line'], 2)
        self.assertFalse(result['complete_source_coverage'])
        self.assertEqual(result['skipped_blocks'][0]['reason'], 'requires_ld_conversion')
        self.assertFalse(self.call('project_search', query='x', limit=True)['ok'])

    def test_reference_exactness_comments_strings_and_unknowns(self):
        self.write_il('// OUT Y1\nLD X10\nOUT Y1\nLD X100\nMOV "//Y1" D0\nAXISFOO D10 Y2\nMOV D10Z0 D20\n')
        result = self.call('project_xref', device='x010')
        self.assertEqual(len(result['references']), 1)
        self.assertEqual(result['references'][0]['device'], 'X10')
        result = self.call('project_xref', device='Y1')
        self.assertEqual(len(result['references']), 1)
        self.assertEqual(result['references'][0]['access'], 'write')
        self.assertIn('AXISFOO', result['unknown_instructions'])
        self.assertTrue(result['unresolved_operands'])
        self.assertFalse(result['indirect_and_implicit_coverage'])
        self.assertFalse(self.call('project_xref', device='X18')['ok'])
        self.assertEqual(self.call('project_xref',device='D0')['references'][0]['access'], 'write')

    def test_audit_multiple_out_is_review_not_proof(self):
        self.write_il('LD X0\nOUT Y1\nLD X1\nOUT Y1\nSET M0\nRST M0\n')
        result = self.call('project_audit')
        self.assertEqual(result['findings'][0]['code'], 'multiple_out')
        self.assertEqual(result['findings'][0]['severity'], 'review')
        self.assertFalse(result['logic_verified'])
        self.assertFalse(result['native_compiled'])

    def test_export_source_unchanged_and_no_binary_project(self):
        dest = self.root/'export'
        result = self.call('project_export', dest=str(dest))
        self.assertTrue(result['ok'], result)
        self.assertEqual(compiler.inventory(self.src), self.original)
        self.assertFalse(list(dest.glob('*.hcp')))
        self.assertEqual((dest/'block-001.il.txt').read_text('utf-8'), il.parse((self.src/'DEMO.IL').read_bytes()).text)
        manifest = json.loads((dest/'manifest.json').read_text('utf-8'))
        self.assertFalse(manifest['complete_source_coverage'])

    def test_batch_simultaneous_edits_and_preservation(self):
        changes = self.patches([dict(old_text='Y1\n',new_text='Y3\n'),dict(old_text='Y3\n',new_text='Y4\n')])
        dest = self.root/'edited'
        result = self.call('il_batch_patch_copy', patches=changes, dest=str(dest))
        self.assertTrue(result['ok'], result)
        self.assertEqual(compiler.inventory(self.src), self.original)
        text = il.parse((dest/'DEMO.IL').read_bytes()).text
        self.assertIn('OUT\t\t Y3\n', text)
        self.assertIn('OUT\t\t Y4\n', text)
        self.assertEqual((dest/'MAIN.dat').read_bytes(), (self.src/'MAIN.dat').read_bytes())
        self.assertFalse((dest/'Output.prg').exists())
        self.assertEqual((dest/result['quarantined']['Output.prg']).read_bytes(), b'synthetic stale output')

    def test_batch_rollback_on_second_file_or_overlap(self):
        for changes in [self.patches()+[dict(file='OTHER.IL',expected_sha256='0'*64,edits=[])],
                        self.patches([dict(old_text='Y1\n',new_text='Y2\n'),dict(old_text='OUT\t\t Y1\n',new_text='RST Y1\n')])]:
            dest = self.root/'must-not-exist'
            result = self.call('il_batch_patch_copy', patches=changes, dest=str(dest))
            self.assertFalse(result['ok'], result)
            self.assertFalse(dest.exists())
        self.assertEqual(compiler.inventory(self.src), self.original)

    def test_batch_bad_hash_and_nested_destination(self):
        changes = self.patches()
        changes[0]['expected_sha256'] = '0'*64
        self.assertFalse(self.call('il_batch_patch_copy', patches=changes, dest=str(self.root/'bad'))['ok'])
        self.assertFalse(self.call('il_batch_patch_copy', patches=self.patches(), dest=str(self.src/'nested'))['ok'])

    def test_batch_two_registered_files_commit_together(self):
        index=self.src/'demo.hcp'
        text=hcp.decode(index.read_bytes()).decode('utf-16').replace('</project>',
            '<file id="2"><FileName>OTHER.IL</FileName><FileType>0</FileType><ProgType>1</ProgType><Encrypted>0</Encrypted></file></project>')
        raw=text.encode('utf-16')
        index.write_bytes(bytes(((v+hcp.HCP_KEY[(i+1)%11])&255)^(i&255 if i%2==0 else 0) for i,v in enumerate(raw)))
        (self.src/'OTHER.IL').write_bytes((self.src/'DEMO.IL').read_bytes())
        changes=self.patches()
        changes.append(dict(changes[0],file='OTHER.IL',edits=[dict(old_text='Y1\n',new_text='Y4\n')]))
        dest=self.root/'both'
        result=self.call('il_batch_patch_copy',patches=changes,dest=str(dest))
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['patches']),2)
        self.assertIn('Y2\n',il.parse((dest/'DEMO.IL').read_bytes()).text)
        self.assertIn('Y4\n',il.parse((dest/'OTHER.IL').read_bytes()).text)

    def test_source_change_during_batch_is_not_published(self):
        real=workflows.shutil.copyfile
        def mutate(source,dest):
            result=real(source,dest)
            (self.src/'MAIN.dat').write_bytes(b'concurrent change')
            return result
        dest=self.root/'concurrent'
        with patch.object(workflows.shutil,'copyfile',side_effect=mutate):
            result=self.call('il_batch_patch_copy',patches=self.patches(),dest=str(dest))
        self.assertFalse(result['ok'])
        self.assertFalse(dest.exists())

    def test_protected_sources_are_excluded(self):
        index = self.src/'demo.hcp'
        xml = hcp.decode(index.read_bytes()).decode('utf-16').replace('<project>', '<project><AllEncrypted>1</AllEncrypted>')
        raw=xml.encode('utf-16')
        index.write_bytes(bytes(((v+hcp.HCP_KEY[(i+1)%11])&255)^(i&255 if i%2==0 else 0) for i,v in enumerate(raw)))
        result = self.call('project_search', query='OUT')
        self.assertEqual(result['matches'], [])
        self.assertTrue(all(s['reason'] == 'protected' for s in result['skipped_blocks']))
        self.assertFalse(self.call('il_batch_patch_copy', patches=self.patches(), dest=str(self.root/'bad'))['ok'])

    def test_failed_native_pipeline_publishes_diagnostics_only(self):
        fake = dict(ok=False,error={'code':'native_compile_failed'},diagnostics=[{'severity':'error','text':'bad instruction'}])
        dest = self.root/'failed'
        with patch.object(workflows, 'compile_copy', return_value=fake):
            result = self.call('project_build_copy', patches=self.patches(), dest=str(dest))
        self.assertFalse(result['ok'])
        self.assertEqual([p.name for p in dest.iterdir()], ['build-report.json'])
        self.assertEqual(result['stages'][-1]['diagnostics'][0]['text'], 'bad instruction')
        self.assertEqual(compiler.inventory(self.src), self.original)

    def test_source_changed_during_export_is_not_published(self):
        real = analysis.project_audit
        def mutate(project):
            (self.src/'MAIN.dat').write_bytes(b'changed during analysis')
            return real(project)
        dest=self.root/'export'
        with patch.object(analysis, 'project_audit', side_effect=mutate):
            result=self.call('project_export',dest=str(dest))
        self.assertFalse(result['ok'])
        self.assertFalse(dest.exists())

    def test_mcp_nested_patch_schema(self):
        result=asyncio.run(server.list_tools(None,None))
        tool=next(t for t in result.tools if t.name=='il_batch_patch_copy')
        self.assertEqual(tool.input_schema['properties']['patches']['items']['type'],'object')


if __name__ == '__main__':
    unittest.main()
