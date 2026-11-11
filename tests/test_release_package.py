"""Build and validate a fixture release without real environments or user data."""
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import build_skill_package as builder


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in builder.FIXED_FILES:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        (self.root/'manifest.json').write_text('{"version":"0.0.1"}')
        (self.root/'SKILL.md').write_text('---\nmetadata:\n  version: "0.0.1"\n---\n')
        (self.root/'README.md').write_text('版本：0.0.1。',encoding='utf-8')
        (self.root/'导入安装说明.md').write_text('enterprise-printer-hub-0.0.1-import.zip')
        (self.root/'scripts/test.py').write_text('pass\n')
        for name in ('.venv/secret.txt', '.git/config', 'data/queue.sqlite3', 'data/office-converter.json',
                     'deploy/office-converter/.env', '.cache/output.png', 'bin/SumatraPDF.exe'):
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'private')
        patcher=patch.object(builder,'ROOT',self.root)
        patcher.start();self.addCleanup(patcher.stop)

    def test_clean_versioned_archive_and_checksum(self):
        with contextlib.redirect_stdout(io.StringIO()):
            builder.main()
        archive=self.root/'dist/enterprise-printer-hub-0.0.1-import.zip'
        with zipfile.ZipFile(archive) as package:
            self.assertIsNone(package.testzip())
            self.assertEqual(len(package.namelist()),len(builder.FIXED_FILES)+1)
            self.assertNotIn('enterprise-printer-hub/bin/SumatraPDF.exe', package.namelist())
            for entry in ('bootstrap-windows.ps1', 'bootstrap-mac.sh'):
                self.assertIn('enterprise-printer-hub/scripts/' + entry, package.namelist())
        manifest=json.loads(archive.with_suffix('.sha256.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['archive'],archive.name)
        self.assertEqual(len(manifest['archive_sha256']),64)
        self.assertNotIn('data/queue.sqlite3',manifest['files'])
        self.assertNotIn('data/office-converter.json', manifest['files'])
        self.assertNotIn('deploy/office-converter/.env', manifest['files'])

    def test_inconsistent_version_is_rejected_before_writing(self):
        for name, contents in [('SKILL.md','metadata:\n  version: "2.7.1"'),
                                ('README.md','版本：2.7.1。'),
                                ('导入安装说明.md','enterprise-printer-hub-2.7.1-import.zip')]:
            path=self.root/name;original=path.read_bytes();path.write_text(contents,encoding='utf-8')
            with self.assertRaises(ValueError):
                builder.main()
            self.assertFalse((self.root/'dist').exists())
            path.write_bytes(original)


if __name__=='__main__':
    unittest.main()
