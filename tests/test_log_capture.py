from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from orchestra_kit.log_capture import TRUNCATION_MARKER, sanitized_output

class LogCaptureTests(unittest.TestCase):
    def test_no_secret_output_is_bounded_and_marked_while_writer_finishes(self):
        with tempfile.TemporaryDirectory() as temp, patch(
                'orchestra_kit.log_capture.MAX_RETAINED_BYTES', 128):
            path = Path(temp) / 'output.log'
            with sanitized_output(path, []) as output:
                output.write(b'x' * 4096)
            retained = path.read_bytes()
            self.assertTrue(output.overflowed)
            self.assertLessEqual(len(retained), 128)
            self.assertTrue(retained.endswith(TRUNCATION_MARKER))

    def test_redacted_output_stays_secret_free_when_bounded(self):
        value = 'chunk-boundary-secret'
        with tempfile.TemporaryDirectory() as temp, patch(
                'orchestra_kit.log_capture.MAX_RETAINED_BYTES', 160):
            path = Path(temp) / 'output.log'
            with sanitized_output(path, [value]) as output:
                output.write(b'x' * 90 + value[:8].encode())
                output.write(value[8:].encode() + b'y' * 4096)
            retained = path.read_bytes()
            self.assertTrue(output.overflowed)
            self.assertLessEqual(len(retained), 160)
            self.assertNotIn(value.encode(), retained)
            self.assertIn(b'[REDACTED]', retained)
            self.assertTrue(retained.endswith(TRUNCATION_MARKER))

    def test_chunk_boundaries_and_json_escapes_do_not_disclose_credentials(self):
        value='demo-quote-"-token'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'output.log'
            with sanitized_output(path,[value]) as output:
                for chunk in [b'prefix '+b'x'*65520, value[:7].encode(),value[7:].encode(),b' suffix\n',json.dumps(value).encode()]:
                    output.write(chunk)
            text=path.read_text()
            self.assertNotIn(value,text)
            self.assertNotIn(json.dumps(value)[1:-1],text)
            self.assertEqual(text.count('[REDACTED]'),2)
            self.assertTrue(text.endswith('"[REDACTED]"'))

    def test_detached_continuous_writer_cannot_hold_capture_open(self):
        import subprocess,sys
        code='\n'.join(['import os,tempfile,threading,time','from pathlib import Path','from orchestra_kit.log_capture import sanitized_output','with tempfile.TemporaryDirectory() as root:', ' with sanitized_output(Path(root)/"log",["fake-credential"]) as out:', '  fd=os.dup(out.fileno())', '  def flood():', '   try:', '    while True: os.write(fd,b"x"*4096)', '   except OSError: pass', '   finally: os.close(fd)', '  threading.Thread(target=flood,daemon=True).start()', '  time.sleep(.01)', ' print("closed")'])
        env={**__import__('os').environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        result=subprocess.run([sys.executable,'-c',code],env=env,text=True,capture_output=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('closed',result.stdout)
