"""Temporary Runpod Pods keep CUDA and cloud credentials out of local inference."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile

API_URL = "https://api.runpod.io/graphql"
IMAGE = "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
REMOTE_FIELDS = (
    "model", "compute_type", "cpu_threads", "num_workers", "language",
    "vad_min_silence_duration_ms", "vad_speech_pad_ms", "subtitle_reading_cps",
    "subtitle_min_duration", "subtitle_max_duration", "subtitle_gap",
)


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def add_arguments(parser):
    group = parser.add_argument_group("optional Runpod GPU backend")
    group.add_argument("--backend", choices=("local", "runpod"), default="local")
    group.add_argument("--runpod-token-file", help="API token file; otherwise RUNPOD_API_KEY")
    group.add_argument("--runpod-gpu", default="NVIDIA GeForce RTX 4090")
    group.add_argument("--runpod-max-hourly-rate", type=positive_number, default=2.0,
                       help="Maximum quoted total Pod hourly rate (default: 2 USD)")
    group.add_argument("--runpod-max-seconds", type=positive_number, default=3600,
                       help="Maximum allocation lifetime, including setup (default: 3600)")


def api(token, query, variables=None, timeout=30):
    request = Request(API_URL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    if result.get("errors"):
        raise RuntimeError(f"Runpod API: {result['errors']}")
    return result["data"]


def terminate(token, pod_id):
    for attempt in range(5):
        try:
            api(token, "mutation($id: String!) { podTerminate(input: {podId: $id}) }", {"id": pod_id})
            return
        except Exception:
            if attempt == 4:
                raise
            time.sleep(2)


def watchdog():
    # A separate session survives CLI interruption; no cloud token enters the Pod.
    token, pod_id, deadline = json.loads(sys.stdin.read())
    time.sleep(max(0, deadline - time.time()))
    terminate(token, pod_id)


def suppress_hf_anonymous_warning():
    message = ("You are sending unauthenticated requests to the HF Hub. "
               "Please set a HF_TOKEN to enable higher rate limits and faster downloads.")

    class AnonymousDownloadNotice(logging.Filter):
        def filter(self, record):
            # Suppress only the expected anonymous-download notice, not actual
            # throttling, download failures, or unrelated Hub warnings.
            return not (record.levelno == logging.WARNING and
                        record.getMessage() in (message, "Warning: " + message))

    notice_filter = AnonymousDownloadNotice()
    logging.getLogger("huggingface_hub.utils._http").addFilter(notice_filter)
    return notice_filter


def remote_config(args):
    config = {name: getattr(args, name) for name in REMOTE_FIELDS}
    config.update(backend="local", device="cuda", diarize=False, progress=args.progress)
    return config


def install_results(archive, output_dir, source_path):
    # Validate the entire response before writing: a remote host is not trusted
    # to choose local paths, symlinks, or arbitrary output filenames.
    from media_scribe import transcript_stem
    with zipfile.ZipFile(archive) as bundle:
        files = {}
        total = 0
        for item in bundle.infolist():
            total += item.file_size
            if total > 100 * 1024 * 1024 or len(files) >= 100:
                raise RuntimeError("Runpod results exceed safety limits")
            if item.filename != Path(item.filename).name or item.filename in files:
                raise RuntimeError("Invalid Runpod output filename")
            if item.is_dir() or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Invalid Runpod output entry")
            files[item.filename] = bundle.read(item)
        expected = set()
        for name, data in files.items():
            if not name.endswith(".json"):
                continue
            record = json.loads(data)
            language = record.get("language")
            if not isinstance(language, str) or not language.isalpha() or len(language) > 32:
                raise RuntimeError("Invalid Runpod language")
            if not isinstance(record.get("segments"), list):
                raise RuntimeError("Missing Runpod segments")
            stem = transcript_stem(source_path, language)
            expected.update(stem + suffix for suffix in (".json", ".srt", ".vtt", ".txt"))
            if name != stem + ".json":
                raise RuntimeError("Unexpected Runpod transcript filename")
        if not expected or set(files) != expected:
            raise RuntimeError("Incomplete or unexpected Runpod outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as target:
                temporary = Path(target.name)
                try:
                    target.write(data)
                    target.flush()
                    os.replace(temporary, output_dir / name)
                finally:
                    temporary.unlink(missing_ok=True)


def transcribe(media_input, source_path, output_dir, args):
    if args.diarize:
        raise SystemExit("--backend runpod does not support --diarize; use local")
    if Path(args.model).exists() or "/" in args.model or "\\" in args.model:
        raise SystemExit("Runpod requires a faster-whisper model name, not a local path or repository")
    token = (Path(args.runpod_token_file).read_text().strip() if args.runpod_token_file
             else os.environ.get("RUNPOD_API_KEY", "").strip())
    if not token:
        raise SystemExit("Runpod requires --runpod-token-file or RUNPOD_API_KEY")
    # Extract before allocating so cloud time isn't spent waiting on decoding.
    with tempfile.TemporaryDirectory(prefix="media-scribe-runpod-") as directory:
        work = Path(directory)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(work / "key")],
                       check=True, timeout=30)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(media_input),
                        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac",
                        str(work / "audio.flac")], check=True, timeout=args.runpod_max_seconds)
        config = remote_config(args)
        (work / "config.json").write_text(json.dumps({"args": config, "source": str(source_path)}))
        (work / "worker.py").write_text(
            "import argparse,json,zipfile\nfrom pathlib import Path\nimport media_scribe\nimport runpod_backend\n"
            "runpod_backend.suppress_hf_anonymous_warning()\n"
            "c=json.loads(Path('/tmp/config.json').read_text())\n"
            "out=Path('/tmp/results');out.mkdir()\n"
            "media_scribe.transcribe(Path('/tmp/audio.flac'),Path(c['source']),out,argparse.Namespace(**c['args']))\n"
            "with zipfile.ZipFile('/tmp/results.zip','w',zipfile.ZIP_DEFLATED) as z:\n"
            " for f in out.iterdir(): z.write(f,f.name)\n")
        deadline = time.time() + args.runpod_max_seconds
        def remaining():
            seconds = deadline - time.time()
            if seconds <= 0:
                raise TimeoutError("Runpod lifetime exceeded")
            return seconds
        def command(cmd):
            subprocess.run(cmd, check=True, timeout=remaining())
        quote = api(token, "query($id: String!) { gpuTypes(input: {id: $id}) { lowestPrice(input: {gpuCount: 1, secureCloud: true}) { uninterruptablePrice } } }",
                    {"id": args.runpod_gpu})["gpuTypes"]
        rates = [g["lowestPrice"]["uninterruptablePrice"] for g in quote if g.get("lowestPrice")]
        if not rates or rates[0] is None or rates[0] > args.runpod_max_hourly_rate:
            raise RuntimeError("Runpod GPU unavailable or quote exceeds hourly limit")
        pod_id = None
        guard = None
        try:
            pod = api(token, "mutation($input: PodFindAndDeployOnDemandInput!) { podFindAndDeployOnDemand(input: $input) { id costPerHr } }", {"input": {
                "cloudType": "SECURE", "gpuCount": 1, "gpuTypeId": args.runpod_gpu,
                "name": "media-scribe", "imageName": IMAGE, "containerDiskInGb": 25,
                "volumeInGb": 0, "ports": "22/tcp", "supportPublicIp": True,
                "env": [{"key": "PUBLIC_KEY", "value": (work / "key.pub").read_text().strip()}],
            }})["podFindAndDeployOnDemand"]
            pod_id = pod["id"]
            print(f"Runpod {pod_id}: ${pod['costPerHr']}/hour; terminate manually if cleanup fails", file=sys.stderr)
            guard = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--watchdog"],
                                     stdin=subprocess.PIPE, start_new_session=True)
            guard.stdin.write(json.dumps([token, pod_id, deadline]).encode())
            guard.stdin.close()
            if pod["costPerHr"] > args.runpod_max_hourly_rate:
                raise RuntimeError("Allocated Runpod rate exceeds hourly limit")
            options = ["-i", str(work / "key"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                       "-o", "UserKnownHostsFile=" + str(work / "known_hosts"), "-o", "ConnectTimeout=10"]
            while True:
                remaining()
                pod = api(token, "query($id: String!) { pod(input: {podId: $id}) { runtime { ports { ip isIpPublic privatePort publicPort } } } }", {"id": pod_id})["pod"]
                ports = ((pod or {}).get("runtime") or {}).get("ports") or []
                port = next((p for p in ports if p["isIpPublic"] and p["privatePort"] == 22), None)
                if port:
                    host = "root@" + port["ip"]
                    ssh = ["ssh", *options, "-p", str(port["publicPort"]), host]
                    scp = ["scp", *options, "-P", str(port["publicPort"])]
                    if subprocess.run(ssh + ["true"], timeout=min(15, remaining()),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                        break
                time.sleep(min(5, remaining()))
            command(scp + [str(work / n) for n in ("audio.flac", "config.json", "worker.py")] +
                    [str(Path(__file__).with_name("media_scribe.py")), str(Path(__file__).resolve()), host + ":/tmp/"])
            command(ssh + ["python -m venv /tmp/venv && /tmp/venv/bin/pip install --quiet faster-whisper==1.2.1 'av>=19.0.2,<20' ctranslate2==4.9.0 && cd /tmp && /tmp/venv/bin/python worker.py"])
            command(scp + [host + ":/tmp/results.zip", str(work / "results.zip")])
            install_results(work / "results.zip", output_dir, source_path)
        finally:
            if pod_id:
                try:
                    terminate(token, pod_id)
                except Exception as exc:
                    raise RuntimeError(f"Runpod cleanup failed; terminate Pod {pod_id} manually") from exc
                else:
                    if guard and guard.poll() is None:
                        guard.terminate()
                    print(f"Runpod {pod_id} terminated", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1:] == ["--watchdog"]:
        watchdog()
