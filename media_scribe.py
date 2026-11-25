#!/usr/bin/env python3
"""
Download a Google Drive video when accessible, then transcribe it with
faster-whisper.

Private Google Drive files require either public link access or a Netscape-format
cookie file exported from a browser session that can access the file.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from collections import defaultdict
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, unquote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


DRIVE_ID_RE = re.compile(r"(?:/d/|id=)([-\w]{20,})")
CONFIRM_RE = re.compile(r"confirm=([0-9A-Za-z_]+)")
FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?')
FORM_RE = re.compile(r"<form\b[^>]*\baction=[\"']([^\"']+)[\"'][^>]*>(.*?)</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
ATTR_RE = re.compile(r"\b([a-zA-Z_:][-a-zA-Z0-9_:.]*)=[\"']([^\"']*)[\"']")
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
ENGLISH_LANGUAGE_CODES = {"en", "eng", "english"}
DEFAULT_PLEX_TOKEN_FILE = Path(__file__).resolve().with_name("plex-token")


def available_cpu_count() -> int:
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0))
        except OSError:
            pass
    return os.cpu_count() or 1


def available_cpu_ids() -> set[int] | None:
    if hasattr(os, "sched_getaffinity"):
        try:
            return set(os.sched_getaffinity(0))
        except OSError:
            pass
    count = os.cpu_count()
    if count:
        return set(range(count))
    return None


def physical_cpu_count() -> int:
    cpu_ids = available_cpu_ids()
    if not cpu_ids:
        return available_cpu_count()

    cores = set()
    topology_root = Path("/sys/devices/system/cpu")
    for cpu_id in cpu_ids:
        topology = topology_root / f"cpu{cpu_id}" / "topology"
        try:
            package_id = (topology / "physical_package_id").read_text().strip()
            core_id = (topology / "core_id").read_text().strip()
        except OSError:
            return available_cpu_count()
        cores.add((package_id, core_id))

    return len(cores) or available_cpu_count()


def default_cpu_threads() -> int:
    return max(1, physical_cpu_count() * 80 // 100)


def default_cpu_threads_help() -> str:
    physical_cpus = physical_cpu_count()
    cpu_threads = max(1, physical_cpus * 80 // 100)
    return (
        "CPU inference threads for faster-whisper. Defaults to 80%% of "
        f"available physical CPU cores ({cpu_threads} of {physical_cpus})."
    )


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_args() -> argparse.Namespace:
    cpu_threads_default = default_cpu_threads()
    parser = argparse.ArgumentParser(
        description="Download an accessible video and transcribe it."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--drive-url", help="Google Drive share URL or uc download URL")
    src.add_argument("--url", help="Standard media URL supported by curl, e.g. http, https, sftp")
    src.add_argument("--video", help="Existing local video/audio file, or - to read from stdin")
    parser.add_argument(
        "--cookies",
        help="Netscape-format cookies.txt for private Google Drive files",
    )
    parser.add_argument(
        "--google-client-file",
        help=(
            "Google OAuth client JSON for headless Drive API fallback when "
            "cookies/public download are unavailable."
        ),
    )
    parser.add_argument(
        "--google-token-file",
        default=".google-drive-token.json",
        help="Google OAuth refresh-token cache. Defaults to .google-drive-token.json.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Directory for downloaded media and transcript files. Defaults to "
            "the input video directory for --video, or the current directory for URLs/stdin."
        ),
    )
    parser.add_argument(
        "--stdin-name",
        default="stdin.mkv",
        help="Filename to use for stdin input outputs. Defaults to stdin.mkv.",
    )
    from runpod_backend import add_arguments
    add_arguments(parser)
    parser.add_argument(
        "--model",
        default="medium",
        help="faster-whisper model name or local model path",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda"),
        help="Inference device",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="faster-whisper compute type, e.g. int8, float16, default",
    )
    parser.add_argument(
        "--cpu-threads",
        type=positive_int,
        default=cpu_threads_default,
        help=default_cpu_threads_help(),
    )
    parser.add_argument(
        "--num-workers",
        type=positive_int,
        default=1,
        help=(
            "Parallel faster-whisper workers for concurrent transcription calls. "
            "Defaults to 1."
        ),
    )
    parser.add_argument(
        "--language",
        help="Optional language code, e.g. en. Omit for auto-detection.",
    )
    progress = parser.add_mutually_exclusive_group()
    progress.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        default=None,
        help="Print transcription progress to stderr. Defaults on when stderr is a terminal.",
    )
    progress.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Disable transcription progress output.",
    )
    parser.add_argument(
        "--vad-min-silence-duration-ms",
        type=positive_int,
        default=700,
        help="VAD silence duration needed to split speech chunks. Defaults to 700.",
    )
    parser.add_argument(
        "--vad-speech-pad-ms",
        type=positive_int,
        default=250,
        help="VAD padding added before/after detected speech. Defaults to 250.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download the media file; do not transcribe.",
    )
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument(
        "--keep",
        action="store_true",
        help="Keep the input video (the default; retained for compatibility).",
    )
    retention.add_argument(
        "--delete",
        action="store_true",
        help="Delete the input video only after successful transcription.",
    )
    parser.add_argument(
        "--diarize",
        action="store_true",
        help="Run speaker diarization and add speaker labels to transcript output.",
    )
    parser.add_argument(
        "--diarization-model",
        default="pyannote/speaker-diarization-3.1",
        help="pyannote.audio diarization model",
    )
    parser.add_argument(
        "--hf-token",
        help="Hugging Face token. Takes precedence over --hf-token-file and env vars.",
    )
    parser.add_argument(
        "--hf-token-file",
        help=(
            "File containing a Hugging Face token. Used when --hf-token is not set; "
            "env vars HF_TOKEN/HUGGINGFACE_TOKEN are used after this."
        ),
    )
    parser.add_argument(
        "--speaker-map",
        help="Comma-separated speaker names, e.g. SPEAKER_00=Alice,SPEAKER_01=Bob",
    )
    parser.add_argument(
        "--remap-speakers",
        action="store_true",
        help=(
            "Apply --speaker-map to an existing diarized JSON transcript and refresh "
            "outputs without running transcription or diarization."
        ),
    )
    parser.add_argument(
        "--subtitle-reading-cps",
        type=positive_float,
        default=14.0,
        help=(
            "Characters per second for readable SRT/VTT cue durations. "
            "Defaults to 14."
        ),
    )
    parser.add_argument(
        "--subtitle-min-duration",
        type=positive_float,
        default=1.2,
        help="Minimum SRT/VTT cue duration.",
    )
    parser.add_argument(
        "--subtitle-max-duration",
        type=positive_float,
        default=7.0,
        help="Maximum SRT/VTT cue duration.",
    )
    parser.add_argument(
        "--subtitle-gap",
        type=non_negative_float,
        default=0.15,
        help="Minimum gap in seconds before the next SRT/VTT cue when possible.",
    )
    parser.add_argument(
        "--num-speakers",
        type=int,
        help="Known speaker count for diarization.",
    )
    parser.add_argument(
        "--min-speakers",
        type=int,
        help="Minimum speaker count when the exact count is unknown.",
    )
    parser.add_argument(
        "--max-speakers",
        type=int,
        help="Maximum speaker count when the exact count is unknown.",
    )
    parser.add_argument(
        "--plex-url",
        nargs="?",
        const="http://localhost:32400",
        help="Upload subtitles instead of saving outputs. Without a URL, uses http://localhost:32400.",
    )
    parser.add_argument(
        "--plex-token-file",
        default=str(DEFAULT_PLEX_TOKEN_FILE),
        help="Plex server owner's token file. Defaults to plex-token beside this script.",
    )
    parser.add_argument("--plex-retry", metavar="DIRECTORY", help="Retry retained Plex uploads without retranscribing.")
    parser.add_argument(
        "--plex-skip-uploaded-english",
        action="store_true",
        help="Skip if Plex has a server-managed English subtitle matching the generated filename.",
    )
    parser.add_argument("--force", action="store_true", help="Skip the input completeness check before transcription.")
    args = parser.parse_args()
    if args.progress is None:
        args.progress = sys.stderr.isatty()
    return args


def drive_file_id(url: str) -> str:
    match = DRIVE_ID_RE.search(url)
    if not match:
        raise SystemExit(f"Could not find a Google Drive file id in: {url}")
    return match.group(1)


def generic_url_filename(url: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    if not name or name in (".", ".."):
        name = "download"
    return name


def download_url_file(url: str, output_dir: Path) -> Path:
    curl = shutil.which("curl")
    if not curl:
        raise SystemExit("--url requires curl to download standard URLs")

    path = output_dir / generic_url_filename(url)
    command = [
        curl,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--output",
        str(path),
        url,
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise SystemExit(f"URL download failed: {detail}")
    if not path.exists() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise SystemExit(f"URL download produced no data: {url}")
    return path


def resolve_output_dir(args: argparse.Namespace, video_path: Path | None = None) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    if video_path is not None:
        return video_path.parent
    return Path.cwd()


def is_stdin_video(args: argparse.Namespace) -> bool:
    return args.video == "-"


def stdin_source_path(args: argparse.Namespace) -> Path:
    name = Path(args.stdin_name).name
    if not name or name in (".", ".."):
        raise SystemExit("--stdin-name must be a filename, not a directory")
    return Path(name)


def language_code(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    return cleaned.lower() or None


def is_english_language(value: str | None) -> bool:
    code = language_code(value)
    return bool(
        code
        and (
            code in ENGLISH_LANGUAGE_CODES
            or code.startswith("en-")
            or code.startswith("en_")
        )
    )


def transcript_stem(video_path: Path, language: str | None) -> str:
    code = language_code(language)
    if not code:
        return video_path.stem
    return f"{video_path.stem}.{code}"


def find_existing_transcript_json(
    video_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> Path:
    candidates = []
    if args.language:
        candidates.append(output_dir / f"{transcript_stem(video_path, args.language)}.json")

    prefix = f"{video_path.stem}."
    language_coded = sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and path.suffix == ".json"
        and path.name.startswith(prefix)
        and path.stem != video_path.stem
    )
    candidates.extend(language_coded)
    candidates.append(output_dir / f"{video_path.stem}.json")

    seen = set()
    existing = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            existing.append(path)

    if not existing:
        if args.language:
            return output_dir / f"{transcript_stem(video_path, args.language)}.json"
        return output_dir / f"{video_path.stem}.json"
    if not args.language and len(language_coded) > 1:
        formatted = ", ".join(str(path) for path in language_coded)
        raise SystemExit(
            "Multiple language-coded transcript JSON files found. "
            f"Pass --language to choose one: {formatted}"
        )
    return existing[0]


def make_opener(cookies_path: str | None):
    cookie_jar = MozillaCookieJar()
    if cookies_path:
        cookie_jar.load(cookies_path, ignore_discard=True, ignore_expires=True)
    return build_opener(HTTPCookieProcessor(cookie_jar))


def request(opener, url: str):
    return opener.open(
        Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
                )
            },
        )
    )


def request_or_die(opener, url: str):
    try:
        return request(opener, url)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = summarize_html(body)
        raise SystemExit(
            f"Download request failed: HTTP {exc.code} {exc.reason}: {detail}"
        ) from exc
    except URLError as exc:
        raise SystemExit(f"Download request failed: {exc}") from exc


def filename_from_headers(headers, fallback: str) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = FILENAME_RE.search(disposition)
    if match:
        return Path(unquote(match.group(1))).name

    if Path(fallback).suffix:
        return Path(fallback).name

    content_type = headers.get_content_type()
    ext = mimetypes.guess_extension(content_type) or ".bin"
    return fallback + ext


def looks_like_login_or_html(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(2048).lower()
    return (
        b"<html" in head
        or b"accounts.google.com" in head
        or b"service login" in head
        or b"sign in" in head
    )


def summarize_html(body: str) -> str:
    title = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
    if title:
        text = html.unescape(title.group(1))
    else:
        text = re.sub(r"<[^>]+>", " ", body)
        text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500] or "no readable HTML error text"


def is_login_html(body: str) -> bool:
    return (
        "accounts.google.com" in body
        or "ServiceLogin" in body
        or "signin" in body.lower()
    )


def drive_confirmation_url(body: str, base_url: str) -> str | None:
    for action, form_body in FORM_RE.findall(body):
        if "download" not in action and "uc" not in action:
            continue

        params = {}
        for input_tag in INPUT_RE.findall(form_body):
            attrs = {
                key.lower(): html.unescape(value)
                for key, value in ATTR_RE.findall(input_tag)
            }
            if "name" in attrs:
                params[attrs["name"]] = attrs.get("value", "")

        if "confirm" in params:
            return urljoin(base_url, html.unescape(action)) + "?" + urlencode(params)

    match = CONFIRM_RE.search(body)
    if match:
        separator = "&" if "?" in base_url else "?"
        return base_url + f"{separator}confirm={match.group(1)}"

    return None


def save_response(response, output_dir: Path, fallback_name: str) -> Path:
    name = filename_from_headers(response.headers, fallback_name)
    path = output_dir / name
    with path.open("wb") as fh:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    return path


def load_google_client(client_path: str) -> dict[str, str]:
    path = Path(client_path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise SystemExit(f"Google OAuth client file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse Google OAuth client file {path}: {exc}") from exc

    config = raw.get("installed") or raw.get("web") or raw
    client_id = config.get("client_id")
    if not client_id:
        raise SystemExit(f"Google OAuth client file has no client_id: {path}")

    client = {"client_id": client_id}
    client_secret = config.get("client_secret")
    if client_secret:
        client["client_secret"] = client_secret
    return client


def post_form_json(url: str, data: dict[str, str]) -> tuple[int, dict]:
    body = urlencode(data).encode("utf-8")
    request_obj = Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request_obj) as response:
            payload = response.read().decode("utf-8", "replace")
            return response.status, json.loads(payload)
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            raise SystemExit(
                f"Google OAuth request failed: HTTP {exc.code} {exc.reason}: "
                f"{payload[:500]}"
            ) from exc
    except URLError as exc:
        raise SystemExit(f"Google OAuth request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Google OAuth returned invalid JSON: {exc}") from exc


def load_google_token(token_path: Path) -> dict | None:
    if not token_path.exists():
        return None
    try:
        with token_path.open("r", encoding="utf-8") as fh:
            token = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse Google OAuth token file {token_path}: {exc}") from exc

    if not isinstance(token, dict):
        raise SystemExit(f"Google OAuth token file is not a JSON object: {token_path}")
    return token


def save_google_token(token_path: Path, token: dict) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with token_path.open("w", encoding="utf-8") as fh:
        json.dump(token, fh, indent=2)
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def apply_token_expiry(token: dict) -> dict:
    if "expires_in" in token:
        token["expires_at"] = time.time() + float(token["expires_in"]) - 60
    return token


def token_has_valid_access_token(token: dict) -> bool:
    return bool(token.get("access_token")) and time.time() < float(token.get("expires_at", 0))


def client_token_payload(client: dict[str, str]) -> dict[str, str]:
    payload = {"client_id": client["client_id"]}
    if client.get("client_secret"):
        payload["client_secret"] = client["client_secret"]
    return payload


def refresh_google_access_token(client: dict[str, str], token: dict) -> dict:
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise SystemExit("Google OAuth token cache has no refresh_token; delete it and reauthorize.")

    payload = {
        **client_token_payload(client),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    status, response = post_form_json(GOOGLE_TOKEN_URL, payload)
    if status != 200:
        detail = response.get("error_description") or response.get("error") or response
        raise SystemExit(f"Could not refresh Google OAuth token: {detail}")

    token.update(apply_token_expiry(response))
    token["refresh_token"] = refresh_token
    return token


def run_google_device_flow(client: dict[str, str]) -> dict:
    status, response = post_form_json(
        GOOGLE_DEVICE_CODE_URL,
        {
            "client_id": client["client_id"],
            "scope": GOOGLE_DRIVE_READONLY_SCOPE,
        },
    )
    if status != 200:
        detail = response.get("error_description") or response.get("error") or response
        raise SystemExit(f"Could not start Google OAuth device flow: {detail}")

    verification_url = response.get("verification_url")
    user_code = response.get("user_code")
    device_code = response.get("device_code")
    if not verification_url or not user_code or not device_code:
        raise SystemExit(f"Google OAuth device flow returned incomplete response: {response}")

    verification_complete = response.get("verification_url_complete")
    print("Google OAuth authorization required.", file=sys.stderr)
    if verification_complete:
        print(f"Open: {verification_complete}", file=sys.stderr)
    else:
        print(f"Open: {verification_url}", file=sys.stderr)
        print(f"Code: {user_code}", file=sys.stderr)

    interval = int(response.get("interval", 5))
    expires_at = time.time() + int(response.get("expires_in", 1800))
    poll_payload = {
        **client_token_payload(client),
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }

    while time.time() < expires_at:
        time.sleep(interval)
        status, token = post_form_json(GOOGLE_TOKEN_URL, poll_payload)
        if status == 200:
            return apply_token_expiry(token)

        error = token.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise SystemExit("Google OAuth authorization was denied.")
        if error == "expired_token":
            raise SystemExit("Google OAuth device code expired; rerun the command.")

        detail = token.get("error_description") or error or token
        raise SystemExit(f"Google OAuth authorization failed: {detail}")

    raise SystemExit("Google OAuth device code expired; rerun the command.")


def google_access_token(args: argparse.Namespace) -> str:
    if not args.google_client_file:
        raise SystemExit("--google-client-file is required for Google OAuth fallback")

    client = load_google_client(args.google_client_file)
    token_path = Path(args.google_token_file)
    token = load_google_token(token_path)

    if token and token_has_valid_access_token(token):
        return token["access_token"]
    if token and token.get("refresh_token"):
        token = refresh_google_access_token(client, token)
        save_google_token(token_path, token)
        return token["access_token"]

    token = run_google_device_flow(client)
    save_google_token(token_path, token)
    return token["access_token"]


def google_api_json(access_token: str, url: str) -> dict:
    request_obj = Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request_obj) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body[:500]
        raise SystemExit(
            f"Google Drive API request failed: HTTP {exc.code} {exc.reason}: {detail}"
        ) from exc
    except URLError as exc:
        raise SystemExit(f"Google Drive API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Google Drive API returned invalid JSON: {exc}") from exc


def download_drive_file_oauth(url: str, output_dir: Path, args: argparse.Namespace) -> Path:
    file_id = drive_file_id(url)
    access_token = google_access_token(args)

    metadata_query = urlencode(
        {
            "fields": "name,mimeType,size,capabilities",
            "supportsAllDrives": "true",
        }
    )
    metadata = google_api_json(
        access_token,
        f"{GOOGLE_DRIVE_FILES_URL}/{quote(file_id)}?{metadata_query}",
    )
    if metadata.get("capabilities", {}).get("canDownload") is False:
        raise SystemExit(f"Google Drive file cannot be downloaded: {metadata.get('name', file_id)}")

    download_query = urlencode(
        {
            "alt": "media",
            "supportsAllDrives": "true",
            "acknowledgeAbuse": "true",
        }
    )
    request_obj = Request(
        f"{GOOGLE_DRIVE_FILES_URL}/{quote(file_id)}?{download_query}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urlopen(request_obj) as response:
            return save_response(response, output_dir, metadata.get("name") or f"drive-{file_id}")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body[:500]
        raise SystemExit(
            f"Google Drive API download failed: HTTP {exc.code} {exc.reason}: {detail}"
        ) from exc
    except URLError as exc:
        raise SystemExit(f"Google Drive API download failed: {exc}") from exc


def download_drive_file(url: str, output_dir: Path, cookies_path: str | None) -> Path:
    file_id = drive_file_id(url)
    opener = make_opener(cookies_path)
    direct_urls = [
        f"https://drive.usercontent.google.com/u/0/uc?id={file_id}&export=download",
        f"https://drive.google.com/uc?export=download&id={file_id}",
    ]
    last_html_summary = None
    saw_login = False

    for direct_url in direct_urls:
        response = request_or_die(opener, direct_url)

        if response.headers.get_content_type() != "text/html":
            break

        body = response.read().decode("utf-8", "replace")
        last_html_summary = summarize_html(body)
        saw_login = saw_login or is_login_html(body)
        confirmation_url = drive_confirmation_url(body, response.geturl())
        if not confirmation_url:
            continue

        response = request_or_die(opener, confirmation_url)
        if response.headers.get_content_type() != "text/html":
            break

        body = response.read().decode("utf-8", "replace")
        last_html_summary = summarize_html(body)
        saw_login = saw_login or is_login_html(body)
    else:
        if saw_login:
            raise SystemExit(
                "Google Drive requires login for this file. Make the file accessible "
                "to anyone with the link, or rerun with --cookies cookies.txt from a "
                "browser session that can access it."
            )
        raise SystemExit(
            "Google Drive returned HTML instead of a file: "
            + (last_html_summary or "no readable HTML error text")
        )

    path = save_response(response, output_dir, f"drive-{file_id}")
    if looks_like_login_or_html(path):
        path.unlink(missing_ok=True)
        raise SystemExit(
            "Downloaded content was an HTML login/error page, not media. "
            "The Drive file is not accessible with the supplied credentials."
        )
    return path


def format_timestamp(seconds: float, decimal: str = ",") -> str:
    millis = round(seconds * 1000)
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{decimal}{millis:03}"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02}m{secs:02}s"
    if minutes:
        return f"{minutes}m{secs:02}s"
    return f"{secs}s"


def segment_text(seg: dict, include_timestamp: bool = False) -> str:
    text = seg["text"].strip()
    speaker = seg.get("speaker")
    if speaker and include_timestamp:
        return f"[{format_timestamp(seg['start'], '.')}] {speaker}: {text}"
    if speaker:
        return f"{speaker}: {text}"
    return text


def write_txt(path: Path, segments: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for seg in segments:
            fh.write(segment_text(seg, include_timestamp=bool(seg.get("speaker"))) + "\n")


def write_srt(path: Path, segments: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for idx, seg in enumerate(segments, start=1):
            fh.write(f"{idx}\n")
            fh.write(
                f"{format_timestamp(seg['start'])} --> "
                f"{format_timestamp(seg['end'])}\n"
            )
            fh.write(segment_text(seg) + "\n\n")


def readable_subtitle_segments(
    segments: list[dict],
    reading_cps: float | None,
    min_duration: float,
    max_duration: float,
    gap: float,
) -> list[dict]:
    if reading_cps is None:
        return segments

    readable = []
    for idx, seg in enumerate(segments):
        interval_start = float(seg["start"])
        interval_end = float(seg["end"])
        text = " ".join(segment_text(seg).split())
        duration = len(text) / reading_cps if text else min_duration
        duration = min(max(duration, min_duration), max_duration)

        if idx + 1 < len(segments):
            next_start = float(segments[idx + 1]["start"])
            next_limit = next_start - gap
            interval_end = next_limit if next_limit > interval_start else next_start

        start = max(interval_start, interval_end - duration)
        end = interval_end

        if end <= start:
            end = start + 0.1

        readable_seg = dict(seg)
        readable_seg["start"] = start
        readable_seg["end"] = end
        readable.append(readable_seg)

    return readable


def write_vtt(path: Path, segments: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write("WEBVTT\n\n")
        for seg in segments:
            fh.write(
                f"{format_timestamp(seg['start'], '.')} --> "
                f"{format_timestamp(seg['end'], '.')}\n"
            )
            fh.write(segment_text(seg) + "\n\n")


def write_transcript_outputs(
    output_dir: Path,
    stem: str,
    segments: list[dict],
    include_speaker_samples: bool,
    args: argparse.Namespace | None = None,
) -> None:
    subtitle_segments = readable_subtitle_segments(
        segments,
        args.subtitle_reading_cps if args else None,
        args.subtitle_min_duration if args else 1.2,
        args.subtitle_max_duration if args else 7.0,
        args.subtitle_gap if args else 0.15,
    )
    write_txt(output_dir / f"{stem}.txt", segments)
    write_srt(output_dir / f"{stem}.srt", subtitle_segments)
    write_vtt(output_dir / f"{stem}.vtt", subtitle_segments)
    if include_speaker_samples:
        write_speaker_samples(output_dir / f"{stem}.speakers.txt", segments)


def parse_speaker_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}

    mapping = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise SystemExit(
                f"Bad --speaker-map item {item!r}; expected SPEAKER_00=Name"
            )
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(
                f"Bad --speaker-map item {item!r}; speaker and name are required"
            )
        mapping[key] = value
    return mapping


def resolve_hf_token(args: argparse.Namespace) -> str | None:
    if args.hf_token:
        return args.hf_token.strip()

    if args.hf_token_file:
        token_path = Path(args.hf_token_file)
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(
                f"Could not read Hugging Face token file {token_path}: {exc}"
            ) from exc

        if not token:
            raise SystemExit(f"Hugging Face token file is empty: {token_path}")
        return token

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
    return token.strip() if token else None


def decode_media_audio(media_input, sample_rate: int = 16_000):
    import av
    import numpy as np
    from av.audio.resampler import AudioResampler

    # Own the decoding boundary because faster-whisper 1.2.1 passes an option
    # removed in PyAV 19. Arrays bypass that decoder on both supported versions.
    source = str(media_input) if isinstance(media_input, Path) else media_input
    chunks = []
    with av.open(source, mode="r") as container:
        audio_streams = list(container.streams.audio)
        if not audio_streams:
            raise SystemExit(f"No audio stream found in: {media_input}")
        resampler = AudioResampler(format="flt", layout="mono", rate=sample_rate)
        frames = iter(container.decode(audio_streams[0]))
        while True:
            try:
                frame = next(frames)
            except StopIteration:
                break
            except av.error.InvalidDataError:
                # Match faster-whisper's tolerance of isolated damaged frames.
                continue
            frame.pts = None
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray().reshape(-1))
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
    if not chunks:
        raise SystemExit(f"No decodable audio frames found in: {media_input}")
    return np.concatenate(chunks).astype(np.float32, copy=False)


def load_audio_for_diarization(video_path: Path, sample_rate: int = 16_000) -> dict:
    try:
        import torch
    except ImportError as exc:
        raise SystemExit(
            "Diarization needs torch. Install dependencies with:\n"
            "  ./bin/python -m pip install -r requirements.txt "
            "-r requirements-diarization.txt"
        ) from exc
    waveform = torch.from_numpy(decode_media_audio(video_path, sample_rate)).unsqueeze(0)
    return {"waveform": waveform, "sample_rate": sample_rate}


def iter_diarization_turns(diarization) -> Iterable[dict]:
    # pyannote.audio 4 returns a DiarizeOutput wrapper. Its exclusive
    # diarization is intended for downstream transcription alignment.
    annotation = getattr(diarization, "exclusive_speaker_diarization", None)
    if annotation is None:
        annotation = getattr(diarization, "speaker_diarization", None)
    if annotation is None:
        annotation = diarization

    if not hasattr(annotation, "itertracks"):
        raise SystemExit(
            "Unsupported diarization output from pyannote.audio: "
            f"{type(diarization).__name__}"
        )

    for turn, _, speaker in annotation.itertracks(yield_label=True):
        yield {"start": turn.start, "end": turn.end, "speaker": speaker}


def diarize(video_path: Path, args: argparse.Namespace) -> list[dict]:
    hf_token = resolve_hf_token(args)
    if not hf_token:
        raise SystemExit(
            "--diarize requires a Hugging Face token. Use --hf-token-file, "
            "--hf-token, HF_TOKEN, or HUGGINGFACE_TOKEN. The pyannote diarization "
            "model also requires accepting its terms on Hugging Face before first use."
        )

    matplotlib_cache = Path("/tmp/matplotlib-cache")
    matplotlib_cache.mkdir(exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*torchcodec is not installed correctly.*",
                category=UserWarning,
            )
            from pyannote.audio import Pipeline
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: pyannote.audio. Install the optional diarization "
            "dependencies with:\n"
            "  ./bin/python -m pip install -r requirements-diarization.txt"
        ) from exc

    try:
        pipeline = Pipeline.from_pretrained(
            args.diarization_model,
            use_auth_token=hf_token,
        )
    except TypeError:
        pipeline = Pipeline.from_pretrained(
            args.diarization_model,
            token=hf_token,
        )

    kwargs = {}
    if args.num_speakers is not None:
        kwargs["num_speakers"] = args.num_speakers
    if args.min_speakers is not None:
        kwargs["min_speakers"] = args.min_speakers
    if args.max_speakers is not None:
        kwargs["max_speakers"] = args.max_speakers

    diarization_input = load_audio_for_diarization(video_path)
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"std\(\): degrees of freedom is <= 0\..*",
            category=UserWarning,
        )
        diarization = pipeline(diarization_input, **kwargs)
    return list(iter_diarization_turns(diarization))


def overlap_seconds(left: dict, right: dict) -> float:
    return max(0.0, min(left["end"], right["end"]) - max(left["start"], right["start"]))


def assign_speakers(
    segments: list[dict],
    speaker_turns: list[dict],
    speaker_map: dict[str, str],
) -> list[dict]:
    for seg in segments:
        by_speaker = defaultdict(float)
        for turn in speaker_turns:
            overlap = overlap_seconds(seg, turn)
            if overlap > 0:
                by_speaker[turn["speaker"]] += overlap

        if by_speaker:
            speaker = max(by_speaker.items(), key=lambda item: item[1])[0]
            seg["speaker_label"] = speaker
            seg["speaker"] = speaker_map.get(speaker, speaker)
        else:
            seg["speaker_label"] = "UNKNOWN"
            seg["speaker"] = speaker_map.get("UNKNOWN", "UNKNOWN")

    return segments


def write_speaker_samples(path: Path, segments: Iterable[dict], limit: int = 5) -> None:
    samples = defaultdict(list)
    for seg in segments:
        label = seg.get("speaker_label")
        if not label or len(samples[label]) >= limit:
            continue
        samples[label].append(seg)

    with path.open("w", encoding="utf-8") as fh:
        for label in sorted(samples):
            fh.write(f"{label}\n")
            for seg in samples[label]:
                fh.write(
                    f"  [{format_timestamp(seg['start'], '.')}] "
                    f"{seg['text'].strip()}\n"
                )
            fh.write("\n")


def remap_existing_speakers(
    video_path: Path,
    output_dir: Path,
    speaker_map: dict[str, str],
    args: argparse.Namespace,
) -> Path:
    if not speaker_map:
        raise SystemExit("--remap-speakers requires --speaker-map")

    json_path = find_existing_transcript_json(video_path, output_dir, args)
    try:
        with json_path.open("r", encoding="utf-8") as fh:
            transcript = json.load(fh)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"Existing transcript JSON not found: {json_path}. "
            "Run once with --diarize before using --remap-speakers."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse transcript JSON {json_path}: {exc}") from exc

    segments = transcript.get("segments")
    if not isinstance(segments, list):
        raise SystemExit(f"Transcript JSON has no segments list: {json_path}")

    if not any(seg.get("speaker_label") for seg in segments if isinstance(seg, dict)):
        raise SystemExit(
            f"Transcript JSON has no speaker_label values to remap: {json_path}"
        )

    for seg in segments:
        if not isinstance(seg, dict):
            raise SystemExit(f"Transcript JSON contains a non-object segment: {json_path}")
        label = seg.get("speaker_label")
        if label:
            seg["speaker"] = speaker_map.get(label, label)

    stem = transcript_stem(video_path, transcript.get("language") or args.language)
    target_json_path = output_dir / f"{stem}.json"
    write_transcript_outputs(
        output_dir,
        stem,
        segments,
        include_speaker_samples=True,
        args=args,
    )
    transcript["segments"] = segments
    transcript["diarized"] = True
    transcript["speaker_map"] = speaker_map
    with target_json_path.open("w", encoding="utf-8") as fh:
        json.dump(transcript, fh, ensure_ascii=False, indent=2)

    return target_json_path


def print_transcription_progress(
    current: float,
    total: float | None,
    force: bool = False,
    label: str = "Transcribing",
) -> float:
    now = time.monotonic()
    if not force and now - getattr(print_transcription_progress, "last_update", 0.0) < 1.0:
        return now

    print_transcription_progress.last_update = now
    started_at = getattr(print_transcription_progress, "started_at", now)
    if total and total > 0:
        percent = min(100.0, max(0.0, current / total * 100.0))
        estimate = "est --"
        end_time = "end --"
        if current > 0:
            elapsed = max(0.0, now - started_at)
            estimated_total = elapsed / max(current / total, 0.000001)
            remaining = max(0.0, estimated_total - elapsed)
            estimate = f"est {format_duration(estimated_total)}"
            end_time = "end " + time.strftime("%H:%M:%S", time.localtime(time.time() + remaining))
        message = (
            f"\r{label}: {percent:5.1f}% "
            f"({format_timestamp(current, '.')} / {format_timestamp(total, '.')}) "
            f"| {estimate} | {end_time}"
        )
    else:
        message = f"\r{label}: {format_timestamp(current, '.')}"

    print(message, end="", file=sys.stderr, flush=True)
    if force:
        print(file=sys.stderr, flush=True)
    return now


def collect_whisper_segments(
    segments_iter,
    duration: float | None,
    show_progress: bool,
    progress_label: str,
) -> list[dict]:
    segments = []
    last_segment_end = 0.0
    if show_progress:
        print_transcription_progress.last_update = 0.0
        print_transcription_progress.started_at = time.monotonic()
        print_transcription_progress(0.0, duration, label=progress_label)

    for seg in segments_iter:
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
        last_segment_end = max(last_segment_end, float(seg.end))
        if show_progress:
            print_transcription_progress(last_segment_end, duration, label=progress_label)

    if show_progress:
        print_transcription_progress(
            duration or last_segment_end,
            duration,
            force=True,
            label=progress_label,
        )
    return segments


def transcribe(
    media_input,
    source_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    if getattr(args, "backend", "local") == "runpod":
        from runpod_backend import transcribe as transcribe_runpod
        transcribe_runpod(media_input, source_path, output_dir, args)
        return
    speaker_turns = None
    speaker_map = None
    if args.diarize:
        if not isinstance(media_input, Path):
            raise SystemExit("--diarize is not supported with stdin input")
        speaker_turns = diarize(media_input, args)
        speaker_map = parse_speaker_map(args.speaker_map)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: faster-whisper. Install it with:\n"
            "  python3 -m pip install --user faster-whisper"
        ) from exc

    kwargs = {}
    if args.compute_type != "default":
        kwargs["compute_type"] = args.compute_type
    if args.device != "auto":
        kwargs["device"] = args.device
    kwargs["cpu_threads"] = args.cpu_threads
    kwargs["num_workers"] = args.num_workers

    model = WhisperModel(args.model, **kwargs)
    audio = decode_media_audio(media_input)
    segments_iter, info = model.transcribe(
        audio,
        language=args.language,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": args.vad_min_silence_duration_ms,
            "speech_pad_ms": args.vad_speech_pad_ms,
        },
    )
    segments = collect_whisper_segments(
        segments_iter,
        info.duration,
        args.progress,
        "Transcribing",
    )
    if args.diarize:
        segments = assign_speakers(segments, speaker_turns, speaker_map)

    stem = transcript_stem(source_path, info.language)
    write_transcript_outputs(
        output_dir,
        stem,
        segments,
        include_speaker_samples=args.diarize,
        args=args,
    )
    with (output_dir / f"{stem}.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "source": str(source_path),
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "diarized": args.diarize,
                "segments": segments,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )

    if is_english_language(info.language):
        return

    translate_iter, translate_info = model.transcribe(
        audio,
        language=info.language,
        task="translate",
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": args.vad_min_silence_duration_ms,
            "speech_pad_ms": args.vad_speech_pad_ms,
        },
    )
    translated_segments = collect_whisper_segments(
        translate_iter,
        translate_info.duration,
        args.progress,
        "Translating",
    )
    if args.diarize:
        translated_segments = assign_speakers(
            translated_segments,
            speaker_turns,
            speaker_map,
        )

    english_stem = transcript_stem(source_path, "en")
    write_transcript_outputs(
        output_dir,
        english_stem,
        translated_segments,
        include_speaker_samples=False,
        args=args,
    )
    with (output_dir / f"{english_stem}.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "source": str(source_path),
                "language": "en",
                "source_language": info.language,
                "source_language_probability": info.language_probability,
                "duration": translate_info.duration,
                "diarized": args.diarize,
                "translated": True,
                "segments": translated_segments,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )


def plex_directory_outside_media(path: Path) -> bool:
    # TMPDIR may itself point into the tree that the sync job deletes.
    media = Path("/media")
    return not (
        path.absolute().is_relative_to(media)
        or path.resolve().is_relative_to(media.resolve())
    )


def find_plex_video(server, source: Path):
    matches = {}
    for section in server.library.sections():
        if section.type not in ("movie", "show"):
            continue
        if not any(source.is_relative_to(Path(location)) for location in section.locations):
            continue
        libtype = "episode" if section.type == "show" else "movie"
        for item in section.all(libtype=libtype):
            parts = list(item.iterParts())
            if any(part.file == str(source) for part in parts):
                matches[str(item.ratingKey)] = (item, parts)
    if len(matches) != 1:
        raise SystemExit(f"Expected one Plex item for {source}; found {len(matches)}")
    item, parts = next(iter(matches.values()))
    # Uploads attach to the item, not to a particular edition or stacked part.
    if len(parts) != 1:
        raise SystemExit("Plex upload requires an item with a single media file/version")
    return item


def has_plex_uploaded_english(item, source: Path) -> bool:
    expected_title = transcript_stem(source, "en")
    # Plex has no upload-provenance flag; match its managed stream and our title.
    return any(
        is_english_language(getattr(stream, "languageCode", None))
        and (getattr(stream, "key", None) or "").startswith("/library/streams/")
        and getattr(stream, "title", None) == expected_title
        for stream in item.subtitleStreams()
    )


def save_plex_manifest(directory: Path, manifest: dict) -> None:
    temporary = directory / "upload.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "upload.json")


def check_media_completeness(path: Path, args: argparse.Namespace) -> None:
    if args.force:
        return

    def reject(reason):
        raise SystemExit(f"Media completeness check failed for {path}: {reason}. Use --force to bypass.")

    def seconds(value):
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    # Read only packet metadata, keeping memory use independent of file length.
    # Do not print raw probe diagnostics, which may contain embedded metadata.
    try:
        before = path.stat()
        command = ["ffprobe", "-v", "warning"]
        header = subprocess.run(
            [*command, "-show_streams", "-show_format", "-of", "json", "-i", str(path)],
            capture_output=True, check=True,
        )
        metadata = json.loads(header.stdout)
        streams = metadata.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"
                      and not s.get("disposition", {}).get("attached_pic")), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if audio is None:
            reject("no audio stream found")
        selected = video or audio
        last_end = None
        first_start = None
        packet_count = 0
        with tempfile.TemporaryFile() as diagnostics:
            with subprocess.Popen(
                [*command, "-select_streams", str(selected["index"]), "-show_packets",
                 "-show_entries", "packet=pts_time,dts_time,duration_time", "-of", "compact",
                 "-i", str(path)],
                stdout=subprocess.PIPE, stderr=diagnostics, text=True,
                encoding="utf-8", errors="replace",
            ) as probe:
                for line in probe.stdout:
                    if not line.startswith("packet|"):
                        continue
                    packet_count += 1
                    fields = dict(field.split("=", 1) for field in line.strip().split("|")[1:] if "=" in field)
                    timestamp = seconds(fields.get("pts_time"))
                    if timestamp is None:
                        timestamp = seconds(fields.get("dts_time"))
                    if timestamp is not None:
                        end = timestamp + max(0, seconds(fields.get("duration_time")) or 0)
                        last_end = end if last_end is None else max(last_end, end)
                        first_start = timestamp if first_start is None else min(first_start, timestamp)
                if probe.wait():
                    reject("ffprobe could not finish scanning packets")
            diagnostics.seek(0)
            # FFmpeg can report truncation as a warning and still exit zero.
            truncation = re.compile(rb"(?:file ended prematurely|partial file|truncated|packet corrupt|invalid data found)", re.I)
            if truncation.search(header.stderr) or any(truncation.search(line) for line in diagnostics):
                reject("ffprobe reported truncated or corrupt data")
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            reject("input changed during the scan")
    except FileNotFoundError:
        reject("input disappeared or ffprobe is missing (install FFmpeg)")
    except (OSError, ValueError, subprocess.CalledProcessError):
        reject("could not read media with ffprobe")

    if not packet_count:
        reject("no media packets found")
    if video is not None and last_end is not None:
        container = metadata.get("format", {})
        duration = seconds(video.get("duration"))
        start = seconds(video.get("start_time"))
        if duration is None:
            duration = seconds(container.get("duration"))
            start = seconds(container.get("start_time"))
        if duration is not None and duration > 0:
            observed = last_end - (first_start if start is None else start)
            # Container duration can include modest audio tails and rounding.
            if duration - observed > max(2.0, duration * 0.02):
                reject(f"video packets end at {observed:.1f}s of {duration:.1f}s declared")


def run_plex_upload(args: argparse.Namespace) -> int:
    if not args.video or is_stdin_video(args):
        raise SystemExit("Plex upload requires --video with a local media path (not stdin)")
    if args.output_dir or args.download_only or args.remap_speakers or args.delete:
        raise SystemExit("Plex upload cannot use --output-dir, --download-only, --remap-speakers, or --delete")
    parsed = urlparse(args.plex_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise SystemExit("--plex-url must be an HTTP(S) server URL without credentials")
    try:
        token = Path(args.plex_token_file).read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit("Could not read Plex token file") from None
    if not token:
        raise SystemExit("Plex token file is empty")
    try:
        from plexapi.server import PlexServer
    except ImportError:
        raise SystemExit("Plex upload requires PlexAPI; install requirements.txt") from None

    source = Path(os.path.abspath(args.video))
    if not args.plex_retry and not source.is_file():
        raise SystemExit(f"Local input does not exist: {source}")
    try:
        server = PlexServer(args.plex_url, token, timeout=30)
        item = find_plex_video(server, source)
        # An English-only partial success must not prevent retrying other languages.
        if args.plex_skip_uploaded_english and not args.plex_retry and has_plex_uploaded_english(item, source):
            print(f"Skipping: uploaded English subtitle already exists for {source}", file=sys.stderr)
            return 0
    except Exception as exc:
        # HTTP exceptions can contain authenticated URLs; never print their text.
        raise SystemExit(f"Plex connection or library lookup failed ({type(exc).__name__}); check URL, token, and access") from None

    if args.plex_retry:
        directory = Path(args.plex_retry).absolute()
        if not plex_directory_outside_media(directory):
            raise SystemExit("Plex retry directory must be outside /media")
        try:
            manifest = json.loads((directory / "upload.json").read_text(encoding="utf-8"))
            if (
                manifest["source"] != str(source)
                or manifest["server"] != server.machineIdentifier
                or manifest["rating_key"] != str(item.ratingKey)
                or not manifest["subtitles"]
            ):
                raise ValueError("target mismatch")
            for name in manifest["artifacts"]:
                if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
                    raise ValueError("invalid artifact name")
            for subtitle in manifest["subtitles"]:
                name = subtitle["name"]
                if Path(name).name != name or not name.endswith(".srt"):
                    raise ValueError("invalid subtitle name")
                if not isinstance(subtitle["uploaded"], bool):
                    raise ValueError("invalid upload state")
                if not (directory / name).is_file():
                    raise ValueError("missing subtitle")
        except (OSError, ValueError, KeyError, TypeError):
            raise SystemExit("Invalid Plex retry manifest or target mismatch; retained files were not changed") from None
    else:
        check_media_completeness(source, args)
        temporary_root = Path(tempfile.gettempdir())
        if not plex_directory_outside_media(temporary_root):
            temporary_root = Path("/tmp")
        if not plex_directory_outside_media(temporary_root):
            raise SystemExit("No temporary directory outside /media; set TMPDIR to a safe location")
        directory = Path(tempfile.mkdtemp(prefix="media-scribe-plex-", dir=temporary_root))
        manifest = None

    successful = False
    try:
        if manifest is None:
            transcribe(source, source, directory, args)
            # Plex rejects empty SRTs, which Whisper can produce for silent audio.
            subtitles = sorted(path for path in directory.glob("*.srt") if path.stat().st_size)
            if not subtitles:
                raise SystemExit("Transcription produced no non-empty subtitles to upload")
            manifest = {
                "source": str(source),
                "server": server.machineIdentifier,
                "rating_key": str(item.ratingKey),
                "artifacts": [path.name for path in directory.iterdir()],
                "subtitles": [{"name": path.name, "uploaded": False} for path in subtitles],
            }
            save_plex_manifest(directory, manifest)
        for subtitle in manifest["subtitles"]:
            if subtitle["uploaded"]:
                continue
            if not (directory / subtitle["name"]).stat().st_size:
                raise SystemExit(f"Cannot upload empty subtitle: {subtitle['name']}")
            try:
                # Preserve the language-coded filename Plex uses for subtitle metadata.
                item.uploadSubtitles(str(directory / subtitle["name"]))
            except Exception as exc:
                raise SystemExit(f"Plex subtitle upload failed ({type(exc).__name__})") from None
            subtitle["uploaded"] = True
            save_plex_manifest(directory, manifest)
            print(f"Uploaded subtitle: {subtitle['name']}", file=sys.stderr)
        successful = True
    finally:
        if not successful:
            print(f"Plex temporary outputs retained at: {directory}", file=sys.stderr)
            if (directory / "upload.json").exists():
                print(f"Retry without transcription using --plex-retry {str(directory)!r} with the same Plex and video options", file=sys.stderr)
    # A retained directory may have acquired unrelated files before the retry.
    for name in {*manifest["artifacts"], "upload.json", "upload.json.tmp"}:
        (directory / name).unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        print(f"Uploads complete; directory retained because it could not be removed: {directory}", file=sys.stderr)
    return 0


def main() -> int:
    args = parse_args()
    if args.subtitle_min_duration > args.subtitle_max_duration:
        raise SystemExit(
            "--subtitle-min-duration cannot be greater than --subtitle-max-duration"
        )

    if args.plex_url:
        return run_plex_upload(args)
    if args.plex_token_file != str(DEFAULT_PLEX_TOKEN_FILE) or args.plex_retry or args.plex_skip_uploaded_english:
        raise SystemExit("--plex-token-file, --plex-retry, and --plex-skip-uploaded-english require --plex-url")

    if args.remap_speakers and not args.video:
        raise SystemExit("--remap-speakers requires --video")
    if is_stdin_video(args):
        if args.diarize:
            raise SystemExit("--diarize is not supported with stdin input")
        if args.download_only:
            raise SystemExit("--download-only is not supported with stdin input")
        if args.remap_speakers:
            raise SystemExit("--remap-speakers is not supported with stdin input")

    temporary_media_input_path = None
    if args.video:
        if is_stdin_video(args):
            source_path = stdin_source_path(args)
            output_dir = resolve_output_dir(args)
            output_dir.mkdir(parents=True, exist_ok=True)
            print(f"Reading video from stdin as: {source_path.name}", file=sys.stderr)
            suffix = source_path.suffix or ".media"
            with tempfile.NamedTemporaryFile(
                dir=output_dir,
                prefix=f".{source_path.stem}.",
                suffix=suffix,
                delete=False,
            ) as tmp_fh:
                shutil.copyfileobj(sys.stdin.buffer, tmp_fh)
                temporary_media_input_path = Path(tmp_fh.name)
            media_input = temporary_media_input_path
            remove_input_after_transcription = False
        else:
            video_path = Path(args.video)
            if not video_path.exists():
                raise SystemExit(f"Local input does not exist: {video_path}")
            source_path = video_path
            media_input = video_path
            output_dir = resolve_output_dir(args, video_path)
            output_dir.mkdir(parents=True, exist_ok=True)
            remove_input_after_transcription = True
    elif args.drive_url:
        output_dir = resolve_output_dir(args)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            video_path = download_drive_file(args.drive_url, output_dir, args.cookies)
        except SystemExit as exc:
            if not args.google_client_file:
                raise
            print(
                f"Public/cookie Drive download failed; trying Google OAuth fallback: {exc}",
                file=sys.stderr,
            )
            video_path = download_drive_file_oauth(args.drive_url, output_dir, args)
        print(f"Downloaded: {video_path}", file=sys.stderr)
        source_path = video_path
        media_input = video_path
        remove_input_after_transcription = True
    else:
        output_dir = resolve_output_dir(args)
        output_dir.mkdir(parents=True, exist_ok=True)
        video_path = download_url_file(args.url, output_dir)
        print(f"Downloaded: {video_path}", file=sys.stderr)
        source_path = video_path
        media_input = video_path
        remove_input_after_transcription = True

    if args.download_only:
        return 0

    if args.remap_speakers:
        json_path = remap_existing_speakers(
            source_path,
            output_dir,
            parse_speaker_map(args.speaker_map),
            args,
        )
        print(f"Remapped speakers in: {json_path}", file=sys.stderr)
        print(f"Wrote transcripts under: {output_dir}", file=sys.stderr)
        return 0

    try:
        check_media_completeness(media_input, args)
        transcribe(media_input, source_path, output_dir, args)
    finally:
        if temporary_media_input_path:
            temporary_media_input_path.unlink(missing_ok=True)
    print(f"Wrote transcripts under: {output_dir}", file=sys.stderr)
    if remove_input_after_transcription and args.delete:
        try:
            source_path.unlink()
        except OSError as exc:
            raise SystemExit(f"Could not remove input video {source_path}: {exc}") from exc
        print(f"Removed input video: {source_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
