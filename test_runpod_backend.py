"""Cloud failures must not leak Pods or credentials into remote execution."""
import argparse
import json
import logging
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import media_scribe as scribe
import runpod_backend as backend


class RunpodTests(unittest.TestCase):
    def args(self, *extra):
        with patch('sys.argv', ['media_scribe.py', '--video', 'film.mkv', *extra]):
            return scribe.parse_args()

    def test_local_is_default_and_dispatch_is_opt_in(self):
        self.assertEqual(self.args().backend, 'local')
        args = self.args('--backend', 'runpod')
        with patch.object(backend, 'transcribe') as remote:
            scribe.transcribe(Path('film.mkv'), Path('film.mkv'), Path('.'), args)
            remote.assert_called_once()

    def test_limits_require_finite_positive_values(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.args('--runpod-max-seconds', value)

    def test_remote_config_excludes_credentials(self):
        args = self.args('--backend', 'runpod', '--hf-token', 'secret', '--runpod-token-file', 'secret-file')
        config = backend.remote_config(args)
        self.assertEqual(config['device'], 'cuda')
        self.assertEqual(config['backend'], 'local')
        self.assertFalse(any('token' in key or 'plex' in key for key in config))

    def test_only_anonymous_hub_notice_is_suppressed(self):
        logger = logging.getLogger('huggingface_hub.utils._http')
        notice_filter = backend.suppress_hf_anonymous_warning()
        self.addCleanup(logger.removeFilter, notice_filter)
        message = ('You are sending unauthenticated requests to the HF Hub. '
                   'Please set a HF_TOKEN to enable higher rate limits and faster downloads.')
        with self.assertLogs(logger, level='WARNING') as captured:
            logger.warning(message)
            logger.warning('Warning: ' + message)
            logger.warning('Rate limited. Waiting before retry.')
            logger.warning('Download failed')
            logger.error(message)
        self.assertEqual([record.getMessage() for record in captured.records],
                         ['Rate limited. Waiting before retry.', 'Download failed', message])
        other = logging.getLogger('media_scribe')
        with self.assertLogs(other, level='WARNING') as captured:
            other.warning(message)
        self.assertEqual(captured.records[0].getMessage(), message)

    def test_rejects_diarization_before_provisioning(self):
        with patch.object(backend, 'api') as api, self.assertRaises(SystemExit):
            backend.transcribe(Path('film.mkv'), Path('film.mkv'), Path('.'), self.args('--diarize'))
        api.assert_not_called()

    def test_results_validate_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            archive = base / 'results.zip'
            out = base / 'out'
            with zipfile.ZipFile(archive, 'w') as z:
                for suffix in ('.json', '.srt', '.vtt', '.txt'):
                    data = json.dumps({'language': 'en', 'segments': []}) if suffix == '.json' else 'text'
                    z.writestr('film.en' + suffix, data)
            backend.install_results(archive, out, Path('film.mkv'))
            self.assertEqual(len(list(out.iterdir())), 4)
            for name in ('../escape.txt', '/absolute.txt', 'unexpected.txt'):
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(name, 'bad')
                with self.assertRaises(RuntimeError):
                    backend.install_results(archive, out, Path('film.mkv'))
            self.assertFalse((base / 'escape.txt').exists())

    def test_lifecycle_and_remote_pyav_requirement(self):
        for failure in (False, True, 'timeout', 'rate'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                token = base / 'token'; token.write_text('private-token')
                args = self.args('--backend', 'runpod', '--runpod-token-file', str(token))
                calls = []
                def run(cmd, **kwargs):
                    calls.append(cmd)
                    if cmd[0] == 'scp' and any(path.endswith('/worker.py') for path in cmd):
                        worker = next(Path(path) for path in cmd if path.endswith('/worker.py'))
                        self.assertIn('runpod_backend.suppress_hf_anonymous_warning()', worker.read_text())
                        self.assertIn(str(Path(backend.__file__).resolve()), cmd)
                    if cmd[0] == 'ssh-keygen':
                        Path(cmd[-1] + '.pub').write_text('ssh-ed25519 test')
                    if failure in (True, 'timeout') and cmd[0] == 'ssh' and 'pip install' in cmd[-1]:
                        if failure == 'timeout':
                            raise subprocess.TimeoutExpired(cmd, 1)
                        raise subprocess.CalledProcessError(1, cmd)
                    return subprocess.CompletedProcess(cmd, 0)
                replies = [
                    {'gpuTypes': [{'lowestPrice': {'uninterruptablePrice': 0.89}}]},
                    {'podFindAndDeployOnDemand': {'id': 'test-pod', 'costPerHr': 3 if failure == 'rate' else 0.89}},
                    {'pod': {'runtime': {'ports': [{'ip': '127.0.0.1', 'isIpPublic': True, 'privatePort': 22, 'publicPort': 1234}]}}},
                ]
                guard = MagicMock(); guard.poll.return_value = None
                with patch.object(backend, 'api', side_effect=replies), \
                     patch.object(backend.subprocess, 'run', side_effect=run), \
                     patch.object(backend.subprocess, 'Popen', return_value=guard), \
                     patch.object(backend, 'install_results') as install, \
                     patch.object(backend, 'terminate') as terminate:
                    if failure:
                        error = RuntimeError if failure == 'rate' else (subprocess.TimeoutExpired if failure == 'timeout' else subprocess.CalledProcessError)
                        with self.assertRaises(error):
                            backend.transcribe(base/'input', Path('film.mkv'), base/'out', args)
                        install.assert_not_called()
                    else:
                        backend.transcribe(base/'input', Path('film.mkv'), base/'out', args)
                        install.assert_called_once()
                        requirements = Path(__file__).with_name('requirements.txt').read_text().splitlines()
                        av_requirement, = [line.strip() for line in requirements if line.strip().startswith('av')]
                        setup, = [cmd[-1] for cmd in calls if cmd[0] == 'ssh' and 'pip install' in cmd[-1]]
                        # Shell redirection must not consume version bounds, and
                        # cloud installs must inherit local decoder safety fixes.
                        tokens = shlex.split(setup, posix=True)
                        self.assertIn(av_requirement, tokens)
                        self.assertIn(shlex.quote(av_requirement), setup)
                        self.assertNotIn('av==19.0.0', tokens)
                    terminate.assert_called_once_with('private-token', 'test-pod')
                    guard.terminate.assert_called_once()
                self.assertNotIn('private-token', str(calls))


if __name__ == '__main__':
    unittest.main()
