"""Portable protocol/validation tests. Actual native persistence is in test_native."""
import unittest
from autoshop_mcp import symbols
from autoshop_mcp.errors import ToolError


class SymbolTests(unittest.TestCase):
    def test_protocol_strict_completeness(self):
        row = 'symbol\t0\t44656d6f\t4d30\t-\t0\n'
        log = 'symbols_count=1\n'+row+'symbols_finished=1\n'
        self.assertEqual(symbols._parse(log)[0]['name'], 'Demo')
        for bad in [log.replace('count=1','count=2'), log.replace('finished=1',''),
                    log.replace('44656d6f','zz'), log.replace('symbol\t0','symbol\t2')]:
            with self.subTest(bad=bad), self.assertRaises(ToolError):
                symbols._parse(bad)

    def test_unicode_protocol_and_duplicates(self):
        change = dict(index=-1,name='测试名称',address='M100',comment='第一行\n第二行')
        rows, protocol = symbols._plan([], [change])
        self.assertEqual(rows[0]['name'], change['name'])
        self.assertTrue(protocol.isascii())
        with self.assertRaises(ToolError):
            symbols._plan(rows, [change])

    def test_invalid_edits_rejected(self):
        base = dict(index=-1,name='Demo',address='M100',comment='')
        for delta in [dict(index=True),dict(index=0),dict(name='has space'),dict(comment='\0'),
                      dict(comment='😀'),dict(address='X8'),dict(comment='x'*4096)]:
            with self.subTest(delta=delta), self.assertRaises(ToolError):
                symbols._plan([], [dict(base,**delta)])

    def test_equivalent_address_spellings_are_duplicate(self):
        for address, alias in [('M1','M001'), ('X10','X010')]:
            rows = [dict(index=0,name='Existing',address=address,comment='',check_result=0)]
            with self.subTest(address=address), self.assertRaises(ToolError):
                symbols._plan(rows, [dict(index=-1,name='New',address=alias,comment='')])
