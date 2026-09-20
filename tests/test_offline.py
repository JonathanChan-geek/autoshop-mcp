import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile
from autoshop_mcp import core, hcp, il

from fixtures import create_project
NAME = 'DEMO.IL'
def hashes(p):
    return {f.relative_to(p).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest() for f in p.rglob('*') if f.is_file()}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='autoshop-验证-')
        self.root = Path(self.tmp.name)
        self.src = self.root / '原始工程'
        create_project(self.src)
        self.original = hashes(self.src)
    def tearDown(self):
        if hasattr(self, 'tmp'): self.tmp.cleanup()
    def call(self, name, **kw): return core.run_tool(name, kw)
    def patch(self, **overrides):
        p = dict(project=str(self.src), file=NAME, expected_sha256=self.original[NAME],
                 old_text='OUT\t\t Y1\n', new_text='OUT\t\t Y2\n', dest=str(self.root/'修改副本'))
        p.update(overrides)
        return self.call('il_patch_copy', **p)
    def test_inspect_read(self):
        r=self.call('project_inspect',project=str(self.src)); self.assertTrue(r['ok'],r)
        self.assertEqual(r['summary']['editable_il'],[NAME])
        r=self.call('il_read',project=str(self.src),file=NAME);self.assertTrue(r['ok'],r)
        self.assertIn('OUT\t\t Y1',r['text'])
    def test_patch_diff_package(self):
        r=self.patch();self.assertTrue(r['ok'],r);dst=Path(r['dest_project'])
        self.assertEqual(hashes(self.src),self.original)
        a=il.parse((self.src/NAME).read_bytes());b=il.parse((dst/NAME).read_bytes())
        self.assertEqual(a.header,b.header);self.assertEqual(a.footer,b.footer)
        self.assertEqual(b.text,a.text.replace('OUT\t\t Y1\n','OUT\t\t Y2\n'))
        self.assertFalse((dst/'Output.prg').exists())
        for n in ['MAIN.dat','MAIN.mon','CANLink.prg']:self.assertEqual((dst/n).read_bytes(),(self.src/n).read_bytes())
        d=self.call('project_diff',before=str(self.src),after=str(dst));self.assertTrue(d['ok'],d)
        self.assertEqual(d['counts']['changed'],1);self.assertEqual(d['config_changes'],[])
        z=self.root/'交付.zip';r=self.call('package_project',project=str(dst),out_zip=str(z));self.assertTrue(r['ok'],r)
        self.assertFalse(r['native_compiled'])
        with zipfile.ZipFile(z) as f:
            self.assertIsNone(f.testzip());self.assertNotIn('Output.prg',f.namelist())
            for n in ['MAIN.dat','MAIN.mon','CANLink.prg']:self.assertEqual(f.read(n),(self.src/n).read_bytes())
    def test_wrong_hash(self): self.assertFalse(self.patch(expected_sha256='0'*64)['ok'])
    def test_ambiguous_missing(self):
        for old in ['LD','NO_SUCH_TEXT']:self.assertFalse(self.patch(old_text=old)['ok'])
    def test_ld_unsupported(self):
        self.assertFalse(self.call('il_read',project=str(self.src),file='MAIN.LD')['ok'])
    def test_paths(self):
        for n in ['../MAIN.LD','C:\\outside.IL','..\\outside.IL']:
            self.assertFalse(self.call('il_read',project=str(self.src),file=n)['ok'])
        self.assertFalse(self.patch(dest=str(self.src/'nested'))['ok'])
        self.assertFalse(self.patch(dest=str(self.src))['ok'])
    def test_bad_header_and_crlf(self):
        p=self.src/NAME;b=bytearray(p.read_bytes());b[143]=0;p.write_bytes(b)
        self.assertFalse(self.call('il_read',project=str(self.src),file=NAME)['ok'])
        self.assertFalse(self.patch(new_text='LD Y0\r\nOUT Y1')['ok'])
    def test_stale_override_rejected(self):
        self.assertFalse(self.patch(quarantine=False)['ok'])
        self.assertFalse(self.call('package_project',project=str(self.src),out_zip=str(self.root/'x.zip'),include_stale=True)['ok'])
    def test_probe_truth(self):
        r=self.call('native_compile_probe',install_dir=str(self.root/'absent'));self.assertFalse(r.get('native_compile_available',False))
    def test_source_ref_traversal(self):
        p=self.src/'demo.hcp';raw=hcp.decode(p.read_bytes()).decode('utf-16').replace('<FileName>MAIN.LD</FileName>','<FileName>../MAIN.LD</FileName>')
        key=hcp.HCP_KEY;b=raw.encode('utf-16');p.write_bytes(bytes(((v+key[(i+1)%11])&255)^(i&255 if i%2==0 else 0) for i,v in enumerate(b)))
        self.assertFalse(self.call('project_inspect',project=str(self.src))['ok'])
    def test_symlinks(self):
        for directory in (False,True):
            target=self.root/('outside-dir' if directory else 'outside.txt')
            target.mkdir() if directory else target.write_text('outside')
            link=self.src/'link'
            try:os.symlink(target,link,target_is_directory=directory)
            except OSError:self.skipTest('OS does not allow creating symlink')
            self.assertFalse(self.call('project_inspect',project=str(self.src))['ok'])
            link.unlink()
    def test_mcp_transport(self):
        from mcp.client.stdio import stdio_client, StdioServerParameters
        from mcp.client.session import ClientSession
        async def run():
            async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','autoshop_mcp.server'], env={'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')})) as (reader,writer):
                async with ClientSession(reader,writer,read_timeout_seconds=15) as client:
                    await client.initialize()
                    tools=await client.list_tools();self.assertEqual(len(tools.tools),len(core.TOOL_SPECS))
                    result=await client.call_tool('capabilities',{});self.assertFalse(result.is_error)
                    body=json.loads(result.content[0].text);self.assertEqual(body['native_compile']['backend'], 'vendor-dll-x86-child')
                    result=await client.call_tool('il_read',{'project':str(self.src),'file':NAME});self.assertFalse(result.is_error)
                    result=await client.call_tool('il_read',{'project':str(self.src),'file':'../bad'});self.assertTrue(result.is_error)
                    result=await client.call_tool('project_xref',{'project':str(self.src),'device':'Y1'})
                    self.assertFalse(result.is_error,result)
                    body=json.loads(result.content[0].text)
                    self.assertEqual(body['references'][0]['access'],'write')
                    result=await client.call_tool('il_batch_patch_copy',{'project':str(self.src),'dest':str(self.root/'mcp-patched'),
                        'patches':[{'file':NAME,'expected_sha256':self.original[NAME],
                                    'edits':[{'old_text':'OUT\t\t Y1\n','new_text':'OUT\t\t Y2\n'}]}]})
                    self.assertFalse(result.is_error,result)


        asyncio.run(run())


if __name__=='__main__':unittest.main()
