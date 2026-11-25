"""Protect media and retry state without contacting Plex or downloading models."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import media_scribe as scribe


class PlexUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'episode.mkv'
        self.source.write_bytes(b'video')
        self.token = self.root / 'token'
        self.token.write_text('secret-token\n')
        self.item = Mock(ratingKey='42')
        self.item.iterParts.return_value = [SimpleNamespace(file=str(self.source))]
        self.item.subtitleStreams.return_value = []
        self.section = Mock(type='show', locations=[str(self.root)])
        self.section.all.return_value = [self.item]
        self.server = Mock(machineIdentifier='server-id')
        self.server.library.sections.return_value = [self.section]
        self.factory = self.enterContext(patch('plexapi.server.PlexServer', return_value=self.server))
        self.check_media = self.enterContext(patch.object(scribe, 'check_media_completeness'))
        self.transcribe = self.enterContext(patch.object(scribe, 'transcribe', side_effect=self.make_outputs))
        self.enterContext(patch.object(scribe.tempfile, 'gettempdir', return_value=str(self.root)))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.directory = None

    def make_outputs(self, media, source, directory, args):
        self.directory = directory
        for name in ('episode.de.srt', 'episode.en.srt', 'episode.de.txt', 'episode.de.json'):
            (directory / name).write_text('subtitle')

    def run_upload(self, *extra):
        argv = ['media_scribe.py', '--video', str(self.source), '--plex-url',
                'http://plex:32400', '--plex-token-file', str(self.token), *extra]
        with patch('sys.argv', argv):
            return scribe.main()

    def test_default_token_path_is_beside_script_not_working_directory(self):
        with contextlib.chdir(self.root), patch('sys.argv', ['media_scribe.py', '--video', str(self.source)]):
            args = scribe.parse_args()
        self.assertEqual(Path(args.plex_token_file), Path(scribe.__file__).resolve().with_name('plex-token'))

    def test_plex_url_without_value_defaults_to_localhost(self):
        with patch('sys.argv', ['media_scribe.py', '--video', str(self.source), '--plex-url']):
            self.assertEqual(scribe.parse_args().plex_url, 'http://localhost:32400')

    def test_upload_is_opt_in(self):
        with patch('sys.argv', ['media_scribe.py', '--video', str(self.source)]):
            self.assertIsNone(scribe.parse_args().plex_url)

    def test_upload_uses_default_token_without_override(self):
        with patch.object(scribe, 'DEFAULT_PLEX_TOKEN_FILE', self.token), \
                patch('sys.argv', ['media_scribe.py', '--video', str(self.source),
                                   '--plex-url', 'http://plex:32400']):
            self.assertEqual(scribe.main(), 0)
        self.factory.assert_called_once_with('http://plex:32400', 'secret-token', timeout=30)

    def test_skip_matches_only_managed_english_with_expected_title(self):
        cases = [
            ('eng', '/library/streams/1', 'episode.en', True),
            ('en', '/library/streams/1', 'episode.en', True),
            ('hrv', '/library/streams/1', 'episode.hr', False),
            ('eng', None, 'episode.en', False),
            ('eng', '/library/streams/1', None, False),
            ('eng', '/library/streams/1', 'another.en', False),
        ]
        for language, key, title, expected in cases:
            with self.subTest(language=language, key=key, title=title):
                self.item.subtitleStreams.return_value = [SimpleNamespace(
                    languageCode=language, key=key, title=title)]
                self.assertEqual(scribe.has_plex_uploaded_english(self.item, self.source), expected)

    def test_uploaded_english_skips_transcription_and_upload(self):
        self.item.subtitleStreams.return_value = [SimpleNamespace(
            languageCode='eng', key='/library/streams/1', title='episode.en')]
        self.assertEqual(self.run_upload('--plex-skip-uploaded-english'), 0)
        self.transcribe.assert_not_called()
        self.item.uploadSubtitles.assert_not_called()
        self.assertIsNone(self.directory)

    def test_other_language_does_not_skip(self):
        self.item.subtitleStreams.return_value = [SimpleNamespace(
            languageCode='deu', key='/library/streams/1', title='episode.de')]
        self.assertEqual(self.run_upload('--plex-skip-uploaded-english'), 0)
        self.transcribe.assert_called_once()

    def test_skip_does_not_prevent_partial_retry(self):
        self.item.uploadSubtitles.side_effect = [None, RuntimeError()]
        with self.assertRaises(SystemExit):
            self.run_upload()
        self.item.subtitleStreams.return_value = [SimpleNamespace(
            languageCode='eng', key='/library/streams/1', title='episode.en')]
        self.item.uploadSubtitles.reset_mock(side_effect=True)
        self.assertEqual(self.run_upload('--plex-skip-uploaded-english', '--plex-retry', str(self.directory)), 0)
        self.item.uploadSubtitles.assert_called_once()

    def test_uploads_all_languages_and_cleans_up(self):
        self.assertEqual(self.run_upload(), 0)
        names = [Path(call.args[0]).name for call in self.item.uploadSubtitles.call_args_list]
        self.assertEqual(names, ['episode.de.srt', 'episode.en.srt'])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.directory.exists())
        self.assertEqual(list(self.root.glob('*.srt')), [])
        self.section.all.assert_called_once_with(libtype='episode')
        self.factory.assert_called_once_with('http://plex:32400', 'secret-token', timeout=30)

    def test_failed_upload_retains_state_and_retry_skips_uploaded_language(self):
        self.item.uploadSubtitles.side_effect = [None, RuntimeError('secret-token')]
        with self.assertRaisesRegex(SystemExit, 'upload failed') as error:
            self.run_upload()
        self.assertNotIn('secret-token', str(error.exception))
        self.assertTrue(self.directory.exists())
        manifest = json.loads((self.directory / 'upload.json').read_text())
        self.assertEqual([sub['uploaded'] for sub in manifest['subtitles']], [True, False])
        self.item.uploadSubtitles.reset_mock(side_effect=True)
        self.transcribe.reset_mock()
        self.check_media.reset_mock()
        # Cleanup must not remove unrelated files placed in the retained directory.
        unrelated = self.directory / 'notes'
        unrelated.write_text('keep me')
        self.assertEqual(self.run_upload('--plex-retry', str(self.directory)), 0)
        self.transcribe.assert_not_called()
        self.assertEqual(Path(self.item.uploadSubtitles.call_args.args[0]).name, 'episode.en.srt')
        self.assertEqual(unrelated.read_text(), 'keep me')
        self.assertFalse((self.directory / 'upload.json').exists())

    def test_incomplete_media_does_not_transcribe(self):
        self.check_media.side_effect = SystemExit('incomplete')
        with self.assertRaisesRegex(SystemExit, 'incomplete'):
            self.run_upload()
        self.transcribe.assert_not_called()

    def test_retry_rejects_changed_server(self):
        self.item.uploadSubtitles.side_effect = RuntimeError()
        with self.assertRaises(SystemExit):
            self.run_upload()
        self.server.machineIdentifier = 'other-server'
        with self.assertRaisesRegex(SystemExit, 'target mismatch'):
            self.run_upload('--plex-retry', str(self.directory))
        self.assertTrue((self.directory / 'upload.json').exists())

    def test_no_match_does_not_transcribe(self):
        self.item.iterParts.return_value = [SimpleNamespace(file='/other/video.mkv')]
        with self.assertRaisesRegex(SystemExit, 'found 0'):
            self.run_upload()
        self.transcribe.assert_not_called()

    def test_ambiguous_match_does_not_transcribe(self):
        other = Mock(ratingKey='43')
        other.iterParts.return_value = [SimpleNamespace(file=str(self.source))]
        self.section.all.return_value.append(other)
        with self.assertRaisesRegex(SystemExit, 'found 2'):
            self.run_upload()
        self.transcribe.assert_not_called()

    def test_multiple_versions_are_rejected(self):
        self.item.iterParts.return_value.append(SimpleNamespace(file='/other/version.mkv'))
        with self.assertRaisesRegex(SystemExit, 'single media file'):
            self.run_upload()
        self.transcribe.assert_not_called()

    def test_incompatible_options_are_rejected(self):
        for flags in (['--delete'], ['--output-dir', str(self.root)],
                      ['--download-only'], ['--remap-speakers']):
            with self.subTest(flags=flags), self.assertRaisesRegex(SystemExit, 'cannot use'):
                self.run_upload(*flags)
        self.transcribe.assert_not_called()

    def test_connection_errors_do_not_expose_token(self):
        self.factory.side_effect = RuntimeError('http://plex/?X-Plex-Token=secret-token')
        with self.assertRaises(SystemExit) as error:
            self.run_upload()
        self.assertNotIn('secret-token', str(error.exception))
        self.assertNotIn('secret-token', self.stderr.getvalue())
        self.transcribe.assert_not_called()

    def test_empty_subtitles_are_not_uploaded(self):
        def silence(media, source, directory, args):
            self.directory = directory
            (directory / 'episode.en.srt').write_text('')
        self.transcribe.side_effect = silence
        with self.assertRaisesRegex(SystemExit, 'no non-empty subtitles'):
            self.run_upload()
        self.item.uploadSubtitles.assert_not_called()
        self.assertTrue(self.directory.exists())

    def test_transcription_failure_retains_directory(self):
        def fail(*args):
            self.make_outputs(*args)
            raise RuntimeError('transcription failed')
        self.transcribe.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'transcription failed'):
            self.run_upload()
        self.assertTrue(self.directory.exists())
        self.assertIn(str(self.directory), self.stderr.getvalue())
        self.item.uploadSubtitles.assert_not_called()

    def test_media_temp_directory_falls_back_to_tmp(self):
        with patch.object(scribe.tempfile, 'gettempdir', return_value='/media/tmp'), \
                patch.object(scribe.tempfile, 'mkdtemp', side_effect=RuntimeError('stop')) as create:
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                self.run_upload()
        self.assertEqual(create.call_args.kwargs['dir'], Path('/tmp'))

    def test_media_retry_directory_rejected(self):
        with self.assertRaisesRegex(SystemExit, 'outside /media'):
            self.run_upload('--plex-retry', '/media/retry')

    def test_retry_rejects_path_traversal(self):
        self.item.uploadSubtitles.side_effect = RuntimeError()
        with self.assertRaises(SystemExit):
            self.run_upload()
        path = self.directory / 'upload.json'
        manifest = json.loads(path.read_text())
        manifest['artifacts'].append('../episode.mkv')
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(SystemExit, 'Invalid Plex retry manifest'):
            self.run_upload('--plex-retry', str(self.directory))
        self.assertTrue(self.source.exists())


if __name__ == '__main__':
    unittest.main()
