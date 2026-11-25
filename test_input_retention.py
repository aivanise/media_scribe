"""Prevent implicit deletion of source media, including on failed transcription."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import media_scribe as scribe


class InputRetentionTests(unittest.TestCase):
    def run_local(self, flags=(), failure=None):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.mkv'
            source.write_bytes(b'source media')
            with patch('sys.argv', ['media_scribe.py', '--video', str(source), *flags]), \
                    patch.object(scribe, 'check_media_completeness'), \
                    patch.object(scribe, 'transcribe', side_effect=failure), \
                    contextlib.redirect_stderr(io.StringIO()):
                if failure:
                    with self.assertRaises(RuntimeError):
                        scribe.main()
                else:
                    self.assertEqual(scribe.main(), 0)
            return source.exists()

    def test_default_keeps_input(self):
        self.assertTrue(self.run_local())

    def test_keep_remains_supported(self):
        self.assertTrue(self.run_local(['--keep']))

    def test_delete_removes_input_after_success(self):
        self.assertFalse(self.run_local(['--delete']))

    def test_delete_preserves_input_on_failure(self):
        self.assertTrue(self.run_local(['--delete'], RuntimeError('transcription failed')))

    def test_download_only_preserves_input_even_with_delete(self):
        self.assertTrue(self.run_local(['--download-only', '--delete']))

    def test_remap_preserves_input_even_with_delete(self):
        with patch.object(scribe, 'remap_existing_speakers', return_value=Path('input.json')):
            self.assertTrue(self.run_local(['--remap-speakers', '--delete']))

    def test_keep_and_delete_are_mutually_exclusive(self):
        with patch('sys.argv', ['media_scribe.py', '--video', 'input.mkv', '--keep', '--delete']), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                scribe.parse_args()
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
