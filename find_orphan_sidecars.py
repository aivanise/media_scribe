#!/usr/bin/env python3
"""
Find sidecar files from a local directory or lsjson -R listing that no longer
have a matching video file in the same directory.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections import defaultdict
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable


if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

DEFAULT_VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".avi",
    ".divx",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

DEFAULT_SIDECAR_EXTENSIONS = {
    ".ass",
    ".idx",
    ".jpeg",
    ".jpg",
    ".json",
    ".nfo",
    ".png",
    ".srt",
    ".ssa",
    ".sub",
    ".txt",
    ".vtt",
    ".webp",
    ".xml",
}

LANGUAGE_SUFFIXES = {
    "ba",
    "bs",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "eng",
    "es",
    "fi",
    "fr",
    "hr",
    "hu",
    "it",
    "ja",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sr",
    "sv",
    "tr",
    "zh",
}

SIDE_IMAGE_SUFFIXES = (
    "-backdrop",
    "-banner",
    "-clearart",
    "-clearlogo",
    "-fanart",
    "-landscape",
    "-logo",
    "-poster",
    "-thumb",
    ".backdrop",
    ".banner",
    ".clearart",
    ".clearlogo",
    ".fanart",
    ".landscape",
    ".logo",
    ".poster",
    ".thumb",
)

SUBTITLE_EXTENSIONS = {".ass", ".idx", ".srt", ".ssa", ".sub", ".vtt"}


def parse_csv_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def normalize_extensions(values: Iterable[str]) -> set[str]:
    normalized = set()
    for value in values:
        value = value.strip().lower()
        if not value:
            continue
        normalized.add(value if value.startswith(".") else f".{value}")
    return normalized


def split_path(path: str) -> tuple[str, str, str]:
    parsed = PurePosixPath(path)
    directory = "" if str(parsed.parent) == "." else str(parsed.parent)
    suffix = parsed.suffix.lower()
    stem = parsed.name[: -len(parsed.suffix)] if parsed.suffix else parsed.name
    return directory, stem, suffix


def strip_language_suffix(stem: str) -> str:
    head, dot, suffix = stem.rpartition(".")
    if dot and suffix.lower() in LANGUAGE_SUFFIXES:
        return head
    return stem


def strip_side_image_suffix(stem: str) -> str:
    lower_stem = stem.lower()
    for suffix in SIDE_IMAGE_SUFFIXES:
        if lower_stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def media_stem_for_sidecar(stem: str, extension: str) -> str:
    if extension in SUBTITLE_EXTENSIONS or extension in {".json", ".txt"}:
        stem = strip_language_suffix(stem)
    if extension in {".jpeg", ".jpg", ".png", ".webp"}:
        stem = strip_side_image_suffix(stem)
    return stem


def is_video(entry: dict, video_extensions: set[str]) -> bool:
    path = entry.get("Path") or entry.get("Name") or ""
    _, _, extension = split_path(path)
    mime_type = str(entry.get("MimeType") or "").lower()
    return extension in video_extensions or mime_type.startswith("video/")


def directory_depth(directory: str, root: Path) -> int:
    relative = os.path.relpath(directory, root)
    if relative == ".":
        return 0
    return len(Path(relative).parts)


def scan_directory(path: str, *, depth: int | None = None) -> list[dict]:
    root = Path(path)
    entries: list[dict] = []

    try:
        def on_error(exc: OSError) -> None:
            raise exc

        walk = os.walk(root, onerror=on_error)
        for directory, dirnames, filenames in walk:
            current_depth = directory_depth(directory, root)
            if depth is not None and current_depth >= depth:
                dirnames.clear()

            dirnames.sort()
            for filename in sorted(filenames):
                file_path = Path(directory) / filename
                entries.append(
                    {
                        "Path": file_path.as_posix(),
                        "Name": filename,
                        "IsDir": False,
                    }
                )
    except OSError as exc:
        raise SystemExit(f"failed to scan {path}: {exc}") from exc

    return entries


def load_entries(paths: list[str], *, depth: int | None = None) -> list[dict]:
    if not paths:
        paths = ["-"]

    if len(paths) > 1:
        entries: list[dict] = []
        for path in paths:
            input_path = Path(path)
            if not input_path.is_dir():
                raise SystemExit(
                    "multiple inputs are supported only for local directories"
                )
            entries.extend(scan_directory(path, depth=depth))
        return entries

    path = paths[0]
    if path != "-":
        input_path = Path(path)
        if input_path.is_dir():
            return scan_directory(path, depth=depth)

    try:
        if path == "-":
            data = json.load(sys.stdin)
        else:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"failed to read {path}: {exc}") from exc

    if not isinstance(data, list):
        raise SystemExit("expected lsjson output to be a JSON array")

    return [entry for entry in data if isinstance(entry, dict)]


def find_orphans(
    entries: list[dict],
    *,
    sidecar_extensions: set[str],
    video_extensions: set[str],
    include_all_non_video: bool,
) -> list[dict]:
    video_stems: set[tuple[str, str]] = set()
    candidates_by_media: dict[tuple[str, str], list[dict]] = defaultdict(list)

    files = [entry for entry in entries if not entry.get("IsDir")]

    for entry in files:
        path = entry.get("Path") or entry.get("Name")
        if not isinstance(path, str) or not path:
            continue
        if not is_video(entry, video_extensions):
            continue
        directory, stem, _ = split_path(path)
        video_stems.add((directory, stem))

    for entry in files:
        path = entry.get("Path") or entry.get("Name")
        if not isinstance(path, str) or not path:
            continue
        if is_video(entry, video_extensions):
            continue

        directory, stem, extension = split_path(path)
        if not include_all_non_video and extension not in sidecar_extensions:
            continue

        media_stem = media_stem_for_sidecar(stem, extension)
        candidates_by_media[(directory, media_stem)].append(entry)

    orphans: list[dict] = []
    for key in sorted(candidates_by_media):
        if key in video_stems:
            continue
        orphans.extend(
            sorted(
                candidates_by_media[key],
                key=lambda entry: str(entry.get("Path") or entry.get("Name") or ""),
            )
        )

    return orphans


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List sidecar files from a local directory or lsjson -R JSON "
            "listing when no video with the same media stem exists in the "
            "same directory."
        )
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        help=(
            "Local directory, directories, or one lsjson -R output file to "
            "inspect. Omit the argument or use - for JSON from stdin."
        ),
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        help=(
            "Maximum directory depth to scan for local filesystem inputs. "
            "Use 0 for only the input directory's files, 1 for one child "
            "directory level, and omit for unlimited recursion."
        ),
    )
    parser.add_argument(
        "--video-ext",
        default=",".join(sorted(DEFAULT_VIDEO_EXTENSIONS)),
        help="Comma-separated video extensions. Defaults cover common video files.",
    )
    parser.add_argument(
        "--sidecar-ext",
        default=",".join(sorted(DEFAULT_SIDECAR_EXTENSIONS)),
        help="Comma-separated cleanup-candidate extensions.",
    )
    parser.add_argument(
        "--all-non-video",
        action="store_true",
        help="Consider every non-video file a cleanup candidate, not just sidecar extensions.",
    )
    output = parser.add_mutually_exclusive_group()
    output.set_defaults(output="text")
    output.add_argument(
        "--text",
        dest="output",
        action="store_const",
        const="text",
        help="Print one orphan path per line. This is the default.",
    )
    output.add_argument(
        "--null",
        dest="output",
        action="store_const",
        const="null",
        help="Print NUL-delimited orphan paths for xargs -0.",
    )
    output.add_argument(
        "--json",
        dest="output",
        action="store_const",
        const="json",
        help="Print full lsjson entries for orphan files as JSON.",
    )
    return parser


def entry_path(entry: dict) -> str:
    return str(entry.get("Path") or entry.get("Name") or "")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.depth is not None and args.depth < 0:
        raise SystemExit("--depth must be greater than or equal to 0")

    entries = load_entries(args.inputs, depth=args.depth)
    orphans = find_orphans(
        entries,
        sidecar_extensions=normalize_extensions(parse_csv_set(args.sidecar_ext)),
        video_extensions=normalize_extensions(parse_csv_set(args.video_ext)),
        include_all_non_video=args.all_non_video,
    )

    if args.output == "json":
        json.dump(orphans, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif args.output == "null":
        for entry in orphans:
            sys.stdout.write(entry_path(entry))
            sys.stdout.write("\0")
    else:
        for entry in orphans:
            print(entry_path(entry))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
