#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 && $1 != -* ]]; then
  source_path=$1
  shift
else
  source_path=
fi
grep_pattern=
transcriber_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --grep)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--grep requires a pattern" >&2
        exit 2
      fi
      grep_pattern=$1
      ;;
    --grep=*)
      grep_pattern=${1#--grep=}
      ;;
    *)
      transcriber_args+=( "$1" )
      ;;
  esac
  shift
done

ssh_host=${PLEX_SSH_HOST:?Set PLEX_SSH_HOST to the SSH destination}
ssh_port=${PLEX_SSH_PORT:-}
ssh_options=()
if [[ -n "$ssh_port" ]]; then
  ssh_options=( -p "$ssh_port" )
fi
lxc=${PLEX_LXC:-lxc}
container=${PLEX_CONTAINER:?Set PLEX_CONTAINER to the LXC container name}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -n "${PLEX_TRANSCRIBER:-}" ]]; then
  transcriber_cmd=( "$PLEX_TRANSCRIBER" )
elif [[ -x "$script_dir/.venv/bin/python" ]]; then
  transcriber_cmd=( "$script_dir/.venv/bin/python" "$script_dir/media_scribe.py" )
else
  transcriber_cmd=( "$script_dir/media_scribe.py" )
fi
media_root=${PLEX_MEDIA_ROOT:-/media}
output_dir=.
for ((i = 0; i < ${#transcriber_args[@]}; i++)); do
  arg=${transcriber_args[$i]}
  case "$arg" in
    --output-dir)
      i=$((i + 1))
      if [[ $i -ge ${#transcriber_args[@]} ]]; then
        echo "--output-dir requires a value" >&2
        exit 2
      fi
      output_dir=${transcriber_args[$i]}
      ;;
    --output-dir=*)
      output_dir=${arg#--output-dir=}
      ;;
  esac
done

pick_source_path() {
  local encoded_media_root
  encoded_media_root=$(printf '%s' "$media_root" | base64 | tr -d '\n')

  local -a video_paths
  mapfile -t video_paths < <(
    ssh "${ssh_options[@]}" -- "$ssh_host" \
      "$quoted_lxc exec $quoted_container -- sh -c 'root=\$(printf %s \"\$1\" | base64 -d); find \"\$root\" -type f \( -iname \"*.3gp\" -o -iname \"*.avi\" -o -iname \"*.flv\" -o -iname \"*.m4v\" -o -iname \"*.mkv\" -o -iname \"*.mov\" -o -iname \"*.mp4\" -o -iname \"*.mpeg\" -o -iname \"*.mpg\" -o -iname \"*.ts\" -o -iname \"*.webm\" -o -iname \"*.wmv\" \) -print | sort' sh $encoded_media_root"
  )

  if [[ -n "$grep_pattern" ]]; then
    local -a filtered_paths=()
    local path
    for path in "${video_paths[@]}"; do
      if [[ $path == *"$grep_pattern"* ]]; then
        filtered_paths+=( "$path" )
      fi
    done
    video_paths=( "${filtered_paths[@]}" )
  fi

  if [[ ${#video_paths[@]} -eq 0 ]]; then
    if [[ -n "$grep_pattern" ]]; then
      echo "No video files found under $media_root in $container matching: $grep_pattern" >&2
    else
      echo "No video files found under $media_root in $container" >&2
    fi
    exit 1
  fi

  if [[ ! -t 0 ]]; then
    echo "SOURCE_PATH is required when stdin is not a terminal" >&2
    exit 2
  fi

  if command -v fzf >/dev/null 2>&1; then
    local -a dir_paths=()
    local -A seen_dirs=()
    local path dir
    for path in "${video_paths[@]}"; do
      dir=$(dirname "$path")
      if [[ -z "${seen_dirs[$dir]+x}" ]]; then
        seen_dirs[$dir]=1
        dir_paths+=( "$dir" )
      fi
    done

    local dir_query=
    if [[ -n "$grep_pattern" ]]; then
      for dir in "${dir_paths[@]}"; do
        if [[ $dir == *"$grep_pattern"* ]]; then
          dir_query=$grep_pattern
          break
        fi
      done
    fi

    local selected_dir=
    selected_dir=$(
      printf '%s\n' "${dir_paths[@]}" |
        fzf \
          --prompt="Directory> " \
          --height=80% \
          --layout=reverse \
          --border \
          --query="$dir_query" \
          --header="Select a directory under $media_root"
    ) || {
      echo "No directory selected" >&2
      exit 130
    }
    if [[ -z "$selected_dir" ]]; then
      echo "No directory selected" >&2
      exit 130
    fi

    local -a videos_in_dir=()
    for path in "${video_paths[@]}"; do
      if [[ $path == "$selected_dir"/* ]]; then
        videos_in_dir+=( "$path" )
      fi
    done
    if [[ ${#videos_in_dir[@]} -eq 0 ]]; then
      echo "No video files found in selected directory: $selected_dir" >&2
      exit 1
    fi

    local selected_video=
    selected_video=$(
      printf '%s\n' "${videos_in_dir[@]}" |
        fzf \
          --prompt="Video> " \
          --height=80% \
          --layout=reverse \
          --border \
          --query="$grep_pattern" \
          --header="Select a video from $selected_dir"
    ) || {
      echo "No video selected" >&2
      exit 130
    }
    if [[ -z "$selected_video" ]]; then
      echo "No video selected" >&2
      exit 130
    fi

    printf '%s\n' "$selected_video"
    return
  fi

  if [[ -n "$grep_pattern" ]]; then
    printf 'Video files under %s in %s matching "%s":\n' "$media_root" "$container" "$grep_pattern" >&2
  else
    printf 'Video files under %s in %s:\n' "$media_root" "$container" >&2
  fi
  local i
  for i in "${!video_paths[@]}"; do
    printf '%4d) %s\n' "$((i + 1))" "${video_paths[$i]}" >&2
  done

  local choice
  while true; do
    read -r -p "Select video [1-${#video_paths[@]}]: " choice
    if [[ $choice =~ ^[0-9]+$ && $choice -ge 1 && $choice -le ${#video_paths[@]} ]]; then
      printf '%s\n' "${video_paths[$((choice - 1))]}"
      return
    fi
    echo "Enter a number from 1 to ${#video_paths[@]}." >&2
  done
}

quoted_lxc=$(printf '%q' "$lxc")
quoted_container=$(printf '%q' "$container")

if [[ -z "$source_path" ]]; then
  source_path=$(pick_source_path)
fi

stdin_name=$(basename "$source_path")
stdin_stem=${stdin_name%.*}
if [[ -z "$stdin_stem" ]]; then
  stdin_stem=$stdin_name
fi
source_dir=$(dirname "$source_path")
encoded_source=$(printf '%s' "$source_path" | base64 | tr -d '\n')
marker=$(mktemp "${TMPDIR:-/tmp}/transcribe-remote-plex.XXXXXX")
trap 'rm -f "$marker"' EXIT

new_transcript_files=()
collect_new_transcript_files() {
  local path
  local -a candidates=()
  local -A seen=()

  shopt -s nullglob
  candidates=(
    "$output_dir/$stdin_stem".txt
    "$output_dir/$stdin_stem".srt
    "$output_dir/$stdin_stem".vtt
    "$output_dir/$stdin_stem".json
    "$output_dir/$stdin_stem".speakers.txt
    "$output_dir/$stdin_stem".*.txt
    "$output_dir/$stdin_stem".*.srt
    "$output_dir/$stdin_stem".*.vtt
    "$output_dir/$stdin_stem".*.json
    "$output_dir/$stdin_stem".*.speakers.txt
  )
  shopt -u nullglob

  new_transcript_files=()
  for path in "${candidates[@]}"; do
    if [[ -f "$path" && "$path" -nt "$marker" && -z "${seen[$path]+x}" ]]; then
      seen[$path]=1
      new_transcript_files+=( "$path" )
    fi
  done
}

ssh "${ssh_options[@]}" -- "$ssh_host" \
  "$quoted_lxc exec $quoted_container -- sh -c 'src=\$(printf %s \"\$1\" | base64 -d); exec ffmpeg -hide_banner -loglevel error -i \"\$src\" -vn -ac 1 -ar 16000 -f wav pipe:1' sh $encoded_source" \
  | "${transcriber_cmd[@]}" --video - --stdin-name "$stdin_name" "${transcriber_args[@]}"

collect_new_transcript_files

srt_files=()
for srt in "${new_transcript_files[@]}"; do
  if [[ $srt == *.srt ]]; then
    srt_files+=( "$srt" )
  fi
done

if [[ ${#srt_files[@]} -eq 0 ]]; then
  echo "No newly written SRT file found for $stdin_name" >&2
  exit 1
fi

if [[ ${#srt_files[@]} -gt 1 ]]; then
  printf 'Multiple newly written SRT files found for %s:\n' "$stdin_name" >&2
  printf '  %s\n' "${srt_files[@]}" >&2
  exit 1
fi

srt_file=${srt_files[0]}
remote_srt_path=$source_dir/$(basename "$srt_file")
encoded_remote_srt_path=$(printf '%s' "$remote_srt_path" | base64 | tr -d '\n')

ssh "${ssh_options[@]}" -- "$ssh_host" \
  "$quoted_lxc exec $quoted_container -- sh -c 'dest=\$(printf %s \"\$1\" | base64 -d); cat > \"\$dest\"' sh $encoded_remote_srt_path" \
  < "$srt_file"
echo "Copied SRT back to: $remote_srt_path" >&2

if [[ ${#new_transcript_files[@]} -gt 0 ]]; then
  rm -f -- "${new_transcript_files[@]}"
  printf 'Removed local artefacts:\n' >&2
  printf '  %s\n' "${new_transcript_files[@]}" >&2
fi
