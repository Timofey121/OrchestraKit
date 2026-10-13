from types import SimpleNamespace
from unittest.mock import patch
import unittest
import tempfile
import os
from pathlib import Path

class FileMetadataTests(unittest.TestCase):
    def info(self, **changes):
        fields=dict(st_dev=1,st_ino=2,st_size=3,st_mtime_ns=4,st_ctime_ns=5,st_birthtime_ns=5)
        fields.update(changes)
        return SimpleNamespace(**fields)

    def test_windows_cross_view_uses_common_birthtime_but_same_view_detects_change(self):
        from orchestra_kit import file_metadata as metadata
        path=self.info();descriptor=self.info(st_ctime_ns=8)
        with patch.object(metadata,'_is_windows',return_value=True):
            self.assertTrue(metadata.stat_matches_fstat(path,descriptor))
            self.assertNotEqual(metadata.metadata_signature(path),metadata.metadata_signature(descriptor))
            for changes in ({'st_ino':9},{'st_size':9},{'st_mtime_ns':9},{'st_birthtime_ns':9}):
                self.assertFalse(metadata.stat_matches_fstat(path,self.info(**changes)))

    def test_posix_ctime_difference_is_still_rejected(self):
        from orchestra_kit import file_metadata as metadata
        with patch.object(metadata,'_is_windows',return_value=False):
            self.assertFalse(metadata.stat_matches_fstat(self.info(),self.info(st_ctime_ns=8)))

    def test_fingerprint_accepts_cross_view_difference_but_rejects_fd_mutation(self):
        from orchestra_kit import file_metadata as metadata
        from orchestra_kit.fingerprint import fingerprint_paths
        real_fstat = os.fstat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'source.py').write_text('value = 1\n')
            calls = 0
            def changed_ctime(fd):
                nonlocal calls
                calls += 1
                info = real_fstat(fd)
                values = {name:getattr(info,name) for name in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode')}
                if hasattr(info,'st_birthtime_ns'): values['st_birthtime_ns']=info.st_birthtime_ns
                values['st_ctime_ns'] += 17
                return SimpleNamespace(**values)
            with patch.object(metadata,'_is_windows',return_value=True), patch('orchestra_kit.fingerprint.os.fstat',side_effect=changed_ctime):
                self.assertEqual(fingerprint_paths(root,['source.py'])['files']['source.py']['state'],'regular')
            def mutated_fd(fd):
                info = changed_ctime(fd)
                info.st_ctime_ns += calls
                return info
            with patch.object(metadata,'_is_windows',return_value=True), patch('orchestra_kit.fingerprint.os.fstat',side_effect=mutated_fd):
                with self.assertRaisesRegex(ValueError,'unreadable'):
                    fingerprint_paths(root,['source.py'])
