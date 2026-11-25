# Media Scribe

`media_scribe.py` downloads an accessible media URL or Google Drive
recording, or uses a local media file, transcribes it with `faster-whisper`, and
can optionally add speaker labels with `pyannote.audio`.

## Fresh Machine Install

Install OS packages first. On Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
```

Create a virtual environment. Do not install these packages into the system
Python; Debian and Ubuntu mark it as externally managed.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Install diarization support only if you need speaker labels:

```bash
python -m pip install -r requirements-diarization.txt
```

Verify the basic tools:

```bash
python media_scribe.py --help
ffmpeg -version
```

## Audio decoding compatibility

The application decodes the first audio stream with PyAV to a 16 kHz mono
float32 NumPy array before calling faster-whisper. This avoids faster-whisper
1.2.1's obsolete `metadata_errors` argument, which PyAV 19 no longer accepts.
Translation reuses the same decoded array. Diarization uses the shared decoder
but still performs its own decode pass.

The decoder is tested with PyAV 16.0.0 and 19.0.2; normal installs select PyAV 19.
Major-version bounds in `requirements.txt` prevent untested API changes from
being installed silently. They are not a guarantee of future compatibility.

Run the audio and translation-boundary tests without downloading a model:

```bash
.venv/bin/python -m unittest -v test_media_audio
```

## Hugging Face Token

Diarization requires a Hugging Face token and access to the pyannote diarization
model. Accept the model terms on Hugging Face before first use.

Store the token in a local file:

```bash
printf '%s\n' 'hf_your_token_here' > hf-token
chmod 600 hf-token
```

Use it with:

```bash
python media_scribe.py \
  --video recording.first5min.mp4 \
  --model small \
  --compute-type int8 \
  --diarize \
  --hf-token-file hf-token
```

Token precedence is:

```text
--hf-token
--hf-token-file
HF_TOKEN
HUGGINGFACE_TOKEN
```

Do not commit `hf-token` or any file containing credentials.

## Finding Orphan Sidecars

`find_orphan_sidecars.py` can scan a local directory recursively and print
sidecar files that no longer have a matching video file in the same directory:

```bash
python find_orphan_sidecars.py /path/to/media
```

Limit local filesystem traversal depth, or scan more than one directory:

```bash
python find_orphan_sidecars.py --depth 1 /path/to/media
python find_orphan_sidecars.py /path/to/movies /path/to/shows
```

It also still accepts `lsjson -R` JSON from a file or stdin:

```bash
python find_orphan_sidecars.py listing.json
rclone lsjson -R remote:path | python find_orphan_sidecars.py -
```

## Google Drive Access

Private Google Drive files require browser cookies from an account that can
access the recording. Export cookies in Netscape `cookies.txt` format.

Treat `cookies.txt` like a password. It can contain many unrelated browser
sessions if exported broadly.

Download and transcribe:

```bash
python media_scribe.py \
  --drive-url 'https://drive.google.com/file/d/FILE_ID/view?usp=drive_web' \
  --cookies cookies.txt \
  --model small \
  --compute-type int8
```

If cookies are not available, use Google OAuth device flow as a headless
fallback. In Google Cloud, enable the Google Drive API and create an OAuth client
for TVs and limited-input devices. Save the downloaded client JSON locally, then
run:

```bash
python media_scribe.py \
  --drive-url 'https://drive.google.com/file/d/FILE_ID/view?usp=drive_web' \
  --google-client-file google-client.json \
  --model small \
  --compute-type int8
```

The first OAuth run prints a Google URL and code. Open it on another device,
authorize Drive read access, and the script caches a refresh token in
`.google-drive-token.json` for later runs. Treat the token file like a password
and do not commit it.

## Standard URLs

Use `--url` for normal media URLs supported by `curl`, including HTTP, HTTPS,
FTP, SCP, and SFTP:

```bash
python media_scribe.py \
  --url 'https://example.com/path/video.mkv' \
  --model small \
  --compute-type int8
```

For SFTP:

```bash
python media_scribe.py \
  --url 'sftp://user@example.com/path/video.mkv' \
  --model small \
  --compute-type int8
```

The downloaded filename comes from the URL path, and downloads default to the
current directory unless `--output-dir` is set.

For an already downloaded file:

```bash
python media_scribe.py \
  --video recording.mp4 \
  --model small \
  --compute-type int8
```

For local input files, outputs default to the same directory as the input video.
For URL downloads, downloads and transcripts default to the current directory
unless `--output-dir` is set.

To stream a video through stdin without saving the compressed video file:

```bash
ssh host 'cat /path/video.mkv' | python media_scribe.py \
  --video - \
  --stdin-name video.mkv \
  --model small \
  --compute-type int8
```

Stdin outputs default to the current directory. `--stdin-name` controls the
transcript basename, so the example writes files like `video.de.srt`. Stdin mode
does not support `--diarize` or `--remap-speakers`.

The input video is kept by default. Add `--delete` to remove it only after
successful transcription. `--keep` remains accepted for compatibility and cannot
be combined with `--delete`.

When stderr is a terminal, transcription progress is shown by default, including
estimated total duration and local finish time. Use `--progress` to force
progress in non-terminal runs, or `--no-progress` to suppress it.

## Speaker Labels

First run diarization without a speaker map:

```bash
python media_scribe.py \
  --video recording.first5min.mp4 \
  --model small \
  --compute-type int8 \
  --diarize \
  --hf-token-file hf-token
```

Inspect the generated speaker samples:

```bash
less recording.first5min.de.speakers.txt
```

Then remap the generated labels to names:

```bash
python media_scribe.py \
  --video recording.first5min.mp4 \
  --remap-speakers \
  --speaker-map 'SPEAKER_00=Alice,SPEAKER_01=Bob'
```

This refreshes the existing language-coded `.txt`, `.srt`, `.vtt`,
`.speakers.txt`, and `.json` files without running transcription or diarization
again.

`SPEAKER_00` is not stable across different recordings. Verify the samples for
each video before reusing a speaker map.

## Subtitle Display Timing

By default, `.srt` and `.vtt` use readable cue timings at 14 characters per
second, aligned to the end of each available subtitle interval. This keeps
subtitles visible only long enough to read instead of filling long silent gaps
for newly generated transcript outputs.

Useful tuning options:

```bash
--subtitle-reading-cps 14     # default
--subtitle-reading-cps 16     # faster, shorter cue display
--subtitle-reading-cps 20     # faster reading, shorter cue display
--subtitle-min-duration 1.2
--subtitle-max-duration 7.0
--subtitle-gap 0.15
```

Speech detection uses faster-whisper VAD by default:

```bash
--vad-min-silence-duration-ms 700
--vad-speech-pad-ms 250
```

If you know the number of speakers, pass it explicitly:

```bash
--num-speakers 4
```

## Model and Compute Type

Default settings:

```bash
--model medium --compute-type int8
```

Faster CPU option:

```bash
--model small --compute-type int8
```

Highest practical CPU quality, slower:

```bash
--model large-v3 --compute-type int8
```

On CPU, useful compute types are usually `int8`, `int8_float32`, `int16`, and
`float32`. `int8` is the normal choice for long meetings because it is fastest
and uses less memory. The model size usually affects quality more than compute
type.

CPU transcription defaults to 80% of the physical CPU cores available to the
process. Override it when needed:

```bash
--cpu-threads 16
```

`--num-workers` controls parallel faster-whisper workers for concurrent
transcription calls. For this script's normal single-file mode, keep the default
unless you are deliberately trading more memory for parallel model work:

```bash
--num-workers 2
```

Use CUDA only if the NVIDIA driver and CUDA runtime are compatible on the
machine.

## Batch Subtitling

Pass a quoted full-path extended regular expression to `subtitle.sh`:

```bash
./subtitle.sh '/media/series/Example Show/.*\.mkv$'
```

Expressions starting with `/` or `^/` search beneath their fixed directory
prefix. Other expressions, and top-level alternatives without a shared root,
search beneath the invocation directory. Quote the expression to prevent shell
expansion. Each match is uploaded to Plex using the token file beside
`media_scribe.py`; generated outputs are temporary and stay outside `/media`.
The default server is `http://localhost:32400`. Override it when needed:

```bash
PLEX_URL=http://plex.example.com:32400 ./subtitle.sh '/media/series/Show/.*\.mkv$'
```

The batch script passes `--plex-skip-uploaded-english`: it skips only when Plex
has a server-managed English subtitle with the expected generated title
(`<video-stem>.en`). Other languages, embedded subtitles, and ordinary adjacent
sidecars do not trigger the skip. Plex does not expose reliable upload provenance;
a downloaded subtitle with the same managed title could also match. An explicit
`--plex-retry` bypasses this skip to finish any remaining language uploads.

## Input completeness check

Before transcription, `ffprobe` scans media packets without decoding. It rejects
obvious truncation, missing media packets, and files that change during the scan.
For video, it also checks the last packet timestamp against the declared duration
when available. This adds a full-file read and cannot prove that a file matches
the original. Install FFmpeg for `ffprobe`.

Use `--force` to bypass the check for a known-good input that fails validation:

```bash
.venv/bin/python media_scribe.py --video input.mkv --force
```

Download-only, speaker remapping, already-uploaded Plex skips, and Plex upload
retries do not scan the input because they do not transcribe it.

## Test Clips

Create a five-minute clip for testing without re-encoding:

```bash
ffmpeg -y -hide_banner \
  -ss 0 -t 300 \
  -i recording.mp4 \
  -map 0 -c copy -avoid_negative_ts make_zero \
  recording.first5min.mp4
```

## Upload subtitles to Plex

Plex upload mode uploads every generated subtitle language instead of leaving
transcripts next to the video. It requires a local `--video` path that exactly
matches Plex's indexed path, and a server-owner token in `plex-token` beside
`media_scribe.py`, independent of the working directory. This file is gitignored;
protect it with `chmod 600 plex-token`. Override its location with
`--plex-token-file /secure/plex-token`. Obtain the token from the `X-Plex-Token` value
in Plex Web's **Get Info → View XML** request. Tokens normally do not need
scheduled refresh, but must be replaced if revoked or invalidated. Do not commit
the token file or share its contents.

```bash
python media_scribe.py \
  --video '/media/series/Show/episode.mkv' \
  --plex-url http://plex:32400
```

Use `--plex-url` without a value for `http://localhost:32400`. Omit the flag
entirely for normal non-upload operation.

The server and unique full-path match are checked before transcription. Matching
currently enumerates movies or episodes in the relevant library sections; large
libraries can make this preflight slow. Multiple media versions or stacked parts
are rejected because Plex uploads attach to the item rather than a specific file.
Generated filenames retain their language codes (for example `.de.srt` and
`.en.srt`) when uploaded through PlexAPI. A live test on Plex 1.43.5 verified
English and Croatian uploads with the correct language labels. Automated tests
mock Plex rather than contacting a live server.

Temporary outputs use a private `media-scribe-plex-*` directory in the system
temporary directory. If that location is under `/media`, `/tmp` is used instead;
resolved paths are also checked. The input video is never deleted in upload mode.
After successful uploads, generated files and the temporary directory are removed.
Empty SRTs (for example, when no speech is detected) are not uploaded. On failure,
outputs are retained and their location is printed. Nothing is written alongside
the video. Plex stores uploaded subtitles in its own application
data; that data must also be outside the synchronized `/media` tree.

Retry an upload failure without retranscribing by repeating the command with:

```bash
  --plex-retry /tmp/media-scribe-plex-EXAMPLE
```

The retained manifest verifies the server, item, and source path and tracks each
successful upload, so completed languages are skipped on retry. A lost response
or a crash between Plex accepting an upload and saving that checkpoint can still
cause a duplicate; inspect Plex before retrying an uncertain upload. A failure
before the manifest is written requires a new transcription run. Retained
transcripts may be sensitive; remove failed-job directories when no longer needed.

Upload mode rejects stdin/URL sources, `--output-dir`, `--delete`,
`--download-only`, and `--remap-speakers`. Ordinary non-upload behavior is
unchanged. `subtitle.sh` uses upload mode with the uploaded-English completion
check described above.

Run the upload and input-retention tests without contacting Plex:

```bash
.venv/bin/python -m unittest -v test_plex_upload test_input_retention
```

## Transcribe media in a remote LXC container

`transcribe_remote_plex.sh` streams audio over SSH and copies a single
resulting SRT back beside the source video. Set the SSH destination and
container explicitly; the SSH account needs access to the container.

```bash
PLEX_SSH_HOST=media.example.com PLEX_CONTAINER=media \
  ./transcribe_remote_plex.sh '/media/series/Example Show/episode.mkv'
```

OpenSSH configuration supplies the username and port unless overridden by the
SSH destination or `PLEX_SSH_PORT`, respectively. Set `PLEX_LXC` if `lxc` is
not in the remote PATH, or `PLEX_MEDIA_ROOT` to change the interactive search root.

## Outputs

For local videos, transcription writes files next to the input video unless
`--output-dir` is set. For URL downloads and stdin, the default output directory is
the current directory.

The input video is kept unless `--delete` is specified, in which case it is
removed only after successful transcription. `--download-only` and
`--remap-speakers` do not remove the video, even with `--delete`.
Stdin mode does not create an input video file to remove.

The detected language code is inserted before the transcript extension:

```text
video.de.txt
video.de.srt
video.de.vtt
video.de.json
video.de.speakers.txt   only when --diarize is used
```

When the detected language is not English, the script also writes an English
translation using the same basename:

```text
video.en.txt
video.en.srt
video.en.vtt
video.en.json
```

The original-language files are kept, so a German recording, for example,
produces both `video.de.*` and `video.en.*` transcript outputs.

## License

[MIT](LICENSE).
