"""Keep absolute-path batch searches independent of the invocation directory."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script_dir = self.root / 'script'
        script_dir.mkdir()
        self.script = script_dir / 'subtitle.sh'
        shutil.copyfile(Path(__file__).with_name('subtitle.sh'), self.script)
        python = script_dir / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.cwd = self.root / 'caller'
        self.cwd.mkdir()
        self.media = self.root / 'Example Show With Spaces'
        self.media.mkdir()
        self.video = self.media / 'episode.mkv'
        self.video.touch()
        # A sidecar must no longer suppress the Plex-side completion check.
        self.video.with_suffix('.en.srt').touch()
        (script_dir / 'media_scribe.py').write_text(
            "import json, sys\nprint('ARGS ' + json.dumps(sys.argv[1:]))\n"
            "assert sys.stdin.read() == ''\n"
        )

    def run_script(self, pattern, plex_url=None, extra=(), env_overrides=None):
        env = dict(os.environ)
        env.pop('PLEX_URL', None)
        if plex_url is not None:
            env['PLEX_URL'] = plex_url
        env.update(env_overrides or {})
        return subprocess.run(
            ['bash', str(self.script), pattern, *extra], cwd=self.cwd, env=env,
            capture_output=True, text=True, timeout=10,
        )

    def transcriber_args(self, result):
        return [json.loads(line[5:]) for line in result.stdout.splitlines() if line.startswith('ARGS ')]

    def picker_env(self, status=0):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir(exist_ok=True)
        picker = bin_dir / 'fzf'
        picker.write_text(
            '#!' + sys.executable + '\n'
            'import os, sys\n'
            'assert {"--read0", "--print0", "--no-multi"} <= set(sys.argv)\n'
            'data = sys.stdin.buffer.read().split(b"\\0")\n'
            'wanted = os.fsencode(os.environ["PICK_FILE"])\n'
            'assert wanted in data\n'
            'assert not any(name.endswith(b".srt") for name in data)\n'
            'status = int(os.environ["PICK_STATUS"])\n'
            'if not status: sys.stdout.buffer.write(wanted + b"\\0")\n'
            'sys.exit(status)\n'
        )
        picker.chmod(0o755)
        return {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                'PICK_FILE': str(self.video), 'PICK_STATUS': str(status)}

    def test_pick_uses_runpod_and_caller_relative_directory(self):
        relative = os.path.relpath(self.media, self.cwd)
        result = self.run_script('--pick', extra=(relative,), env_overrides=self.picker_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        args, = self.transcriber_args(result)
        self.assertEqual(args[args.index('--video') + 1], str(self.video))
        self.assertEqual(args[args.index('--backend') + 1], 'runpod')
        self.assertEqual(args[args.index('--runpod-token-file') + 1], 'runpod-token')
        self.assertIn('--plex-skip-uploaded-english', args)

    def test_pick_preserves_newline_filename(self):
        self.video = self.media / 'odd\nname.MKV'
        self.video.touch()
        result = self.run_script('--pick', extra=(str(self.media),), env_overrides=self.picker_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        args, = self.transcriber_args(result)
        self.assertEqual(args[args.index('--video') + 1], str(self.video))

    def test_pick_cancellation_does_no_work(self):
        for status in (1, 130):
            result = self.run_script('--pick', extra=(str(self.media),), env_overrides=self.picker_env(status))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.transcriber_args(result), [])

    def test_pick_error_is_not_cancellation(self):
        result = self.run_script('--pick', extra=(str(self.media),), env_overrides=self.picker_env(2))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.transcriber_args(result), [])

    def test_pick_propagates_transcription_failure(self):
        self.script.with_name('media_scribe.py').write_text('raise SystemExit(7)\n')
        result = self.run_script('--pick', extra=(str(self.media),), env_overrides=self.picker_env())
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_pick_missing_directory_does_no_work(self):
        result = self.run_script('--pick', extra=(str(self.root / 'missing'),))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.transcriber_args(result), [])

    def test_pick_defaults_to_media(self):
        env = self.picker_env()
        find = self.root / 'bin' / 'find'
        find.write_text('#!' + sys.executable + '\nimport os,sys\n'
                        'assert sys.argv[1] == "/media"\n'
                        'sys.stdout.buffer.write(os.fsencode(os.environ["PICK_FILE"]) + b"\\0")\n')
        find.chmod(0o755)
        result = self.run_script('--pick', env_overrides=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.transcriber_args(result)), 1)

    def test_pick_traversal_failure_does_no_work(self):
        env = self.picker_env()
        find = self.root / 'bin' / 'find'
        find.write_text('#!/bin/sh\nexit 1\n')
        find.chmod(0o755)
        result = self.run_script('--pick', extra=(str(self.media),), env_overrides=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.transcriber_args(result), [])

    def test_absolute_path_without_anchor(self):
        result = self.run_script(str(self.media) + '/.*mkv')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'Searching {self.media}\n', result.stdout)
        args, = self.transcriber_args(result)
        self.assertEqual(args[args.index('--video') + 1], str(self.video))
        self.assertEqual(args[args.index('--plex-url') + 1], 'http://localhost:32400')
        self.assertIn('--plex-skip-uploaded-english', args)
        self.assertNotIn('--backend', args)

    def test_anchored_absolute_path(self):
        result = self.run_script('^' + str(self.media) + r'/.*\.mkv$')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'Searching {self.media}\n', result.stdout)
        self.assertEqual(len(self.transcriber_args(result)), 1)

    def test_server_can_be_overridden(self):
        plex_url = 'http://plex.example.com:32400'
        result = self.run_script(str(self.media) + r'/.*\.mkv$', plex_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        args, = self.transcriber_args(result)
        self.assertEqual(args[args.index('--plex-url') + 1], plex_url)

    def test_multiple_files_do_not_consume_traversal_input(self):
        (self.media / 'second.mkv').touch()
        result = self.run_script(str(self.media) + r'/.*\.mkv$')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.transcriber_args(result)), 2)

    def test_relative_expression_keeps_invocation_directory(self):
        result = self.run_script(r'.*\.mkv$')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'Searching {self.cwd}\n')

    def test_top_level_alternative_keeps_invocation_directory(self):
        result = self.run_script(str(self.media) + r'/.*mkv|other')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f'Searching {self.cwd}\n')

    def test_pattern_file_preserves_escaped_spaces_and_deduplicates(self):
        patterns = self.cwd / 'patterns.txt'
        escaped = str(self.media).replace(' ', r'\ ')
        other = self.root / 'Other Show'
        other.mkdir()
        (other / 'two.mkv').touch()
        patterns.write_text(f'# comment\r\n\r\n{escaped}/.*mkv\r\n{self.media}/episode\\.mkv$\n{other}/.*mkv')
        result = self.run_script('--regex-file', extra=('patterns.txt',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'Searching {self.media}\n', result.stdout)
        args = self.transcriber_args(result)
        self.assertEqual([a[a.index('--video') + 1] for a in args], [str(self.video), str(other / 'two.mkv')])

    def test_invalid_file_pattern_prevents_all_work(self):
        patterns = self.cwd / 'patterns.txt'
        patterns.write_text(f'{self.media}/.*mkv\n[\n')
        result = self.run_script('--regex-file', extra=('patterns.txt',))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.transcriber_args(result), [])

    def test_invalid_expression_is_rejected(self):
        result = self.run_script('[')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Invalid extended regular expression:', result.stderr)


if __name__ == '__main__':
    unittest.main()
