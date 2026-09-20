import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autoshop_mcp import compiler


class RuntimeTests(unittest.TestCase):
    def test_unconfigured_install_does_not_scan_system_libraries(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'glob', side_effect=AssertionError('unexpected system scan')):
            self.assertFalse(compiler.runtime_status()['available'])

    def test_unconfigured_install_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=True):
            result = compiler.runtime_status()
        self.assertFalse(result['available'])
        self.assertIsNone(result['install_dir'])
        self.assertIn('AUTOSHOP_INSTALL_DIR is not configured', result['mismatches'])

    def test_explicit_install_overrides_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            supplied = Path(tmp) / 'explicit'
            supplied.mkdir()
            with patch.dict(os.environ, {'AUTOSHOP_INSTALL_DIR': str(Path(tmp)/'env')}):
                result = compiler.runtime_status(str(supplied))
            self.assertEqual(result['install_dir'], str(supplied.resolve()))
            self.assertFalse(result['available'])
            self.assertIn('Converter.dll', result['mismatches'])

    def test_python_and_native_fingerprints_agree(self):
        header = (compiler.NATIVE/'profile.h').read_text('utf-8')
        for digest in compiler.PROFILE['files'].values():
            self.assertIn(digest, header)
        self.assertIn(compiler.PROFILE['mfc_sha256'], header)


if __name__ == '__main__':
    unittest.main()
