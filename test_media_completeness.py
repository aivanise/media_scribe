"""Reject incomplete inputs before inference without decoding video in the check."""
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import media_scribe as scribe


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
class CompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.directory = Path(temp.name)
        cls.complete = cls.directory / 'complete.mkv'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=64x64:r=10',
                        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '8', '-c:v', 'mpeg4',
                        '-c:a', 'pcm_s16le', str(cls.complete)], check=True)
        data = cls.complete.read_bytes()
        cls.truncated = cls.directory / 'truncated.mkv'
        cls.truncated.write_bytes(data[:len(data) // 2])

    def test_complete_video_passes(self):
        scribe.check_media_completeness(self.complete, SimpleNamespace(force=False))

    def test_truncated_video_is_rejected(self):
        with self.assertRaisesRegex(SystemExit, 'truncated|video packets end'):
            scribe.check_media_completeness(self.truncated, SimpleNamespace(force=False))

    def test_audio_only_passes(self):
        audio = self.directory / 'audio.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.complete), '-vn', str(audio)], check=True)
        scribe.check_media_completeness(audio, SimpleNamespace(force=False))

    def test_cli_rejects_before_transcribing_and_force_bypasses(self):
        with patch('sys.argv', ['media_scribe.py', '--video', str(self.truncated)]), \
                patch.object(scribe, 'transcribe') as transcribe:
            with self.assertRaisesRegex(SystemExit, 'Use --force'):
                scribe.main()
            transcribe.assert_not_called()
        with patch('sys.argv', ['media_scribe.py', '--video', str(self.truncated), '--force']), \
                patch.object(scribe, 'transcribe') as transcribe, \
                patch.object(scribe.subprocess, 'run') as run:
            self.assertEqual(scribe.main(), 0)
            transcribe.assert_called_once()
            run.assert_not_called()

    def test_missing_probe_requires_explicit_override(self):
        with patch.object(scribe.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(SystemExit, 'ffprobe is missing'):
                scribe.check_media_completeness(self.complete, SimpleNamespace(force=False))


if __name__ == '__main__':
    unittest.main()
