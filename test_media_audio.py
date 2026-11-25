"""Guard the audio boundary against PyAV/faster-whisper API incompatibilities."""
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

import media_scribe as scribe


def wav_buffer(rate=16000, channels=1):
    samples = np.full((rate, channels), 8192, dtype='<i2')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())
    buffer.seek(0)
    return buffer


class AudioTests(unittest.TestCase):
    def test_mono_float32(self):
        audio = scribe.decode_media_audio(wav_buffer())
        self.assertEqual(audio.shape, (16000,))
        self.assertEqual(audio.dtype, np.float32)
        np.testing.assert_allclose(audio, 0.25, atol=1e-5)

    def test_stereo_resampling_and_flush(self):
        audio = scribe.decode_media_audio(wav_buffer(48000, 2))
        self.assertEqual(audio.shape, (16000,))
        self.assertTrue(np.isfinite(audio).all())

    def test_path_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'audio.wav'
            path.write_bytes(wav_buffer().getvalue())
            self.assertEqual(len(scribe.decode_media_audio(path)), 16000)

    def test_no_audio_stream(self):
        container = SimpleNamespace(streams=SimpleNamespace(audio=[]))
        with patch('av.open') as open_media:
            open_media.return_value.__enter__.return_value = container
            with self.assertRaisesRegex(SystemExit, 'No audio stream'):
                scribe.decode_media_audio('empty')
            open_media.return_value.__exit__.assert_called_once()

    def test_translation_reuses_decoded_array(self):
        args = SimpleNamespace(diarize=False, compute_type='default', device='auto',
                               cpu_threads=1, num_workers=1, model='tiny', language='de',
                               vad_min_silence_duration_ms=500, vad_speech_pad_ms=400,
                               progress=False)
        info = SimpleNamespace(duration=1, language='de', language_probability=1)
        audio = np.zeros(16000, dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory, \
             patch('faster_whisper.WhisperModel') as model_class, \
             patch.object(scribe, 'decode_media_audio', return_value=audio) as decode, \
             patch.object(scribe, 'collect_whisper_segments', return_value=[]), \
             patch.object(scribe, 'write_transcript_outputs'):
            model = model_class.return_value
            model.transcribe.side_effect = [(iter([]), info), (iter([]), info)]
            scribe.transcribe(Path('input.mkv'), Path('input.mkv'), Path(directory), args)
            decode.assert_called_once_with(Path('input.mkv'))
            self.assertEqual(model.transcribe.call_count, 2)
            for call in model.transcribe.call_args_list:
                self.assertIs(call.args[0], audio)
            self.assertEqual(model.transcribe.call_args_list[1].kwargs['task'], 'translate')


if __name__ == '__main__':
    unittest.main()
