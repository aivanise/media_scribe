#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Create a Python virtualenv and install media_scribe dependencies.

Options:
  --with-diarization     Also install optional pyannote diarization deps.
  --venv DIR             Virtualenv directory. Defaults to .venv.
  --python PATH          Python executable to use. Defaults to python3.
  --skip-apt             Do not try to install missing apt packages.
  -h, --help             Show this help.

Environment:
  VENV_DIR               Alternative virtualenv directory.
  PYTHON                 Alternative Python executable.
EOF
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
venv_dir=${VENV_DIR:-$script_dir/.venv}
python_cmd=${PYTHON:-python3}
with_diarization=0
skip_apt=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-diarization|--diarization)
      with_diarization=1
      ;;
    --venv)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--venv requires a directory" >&2
        exit 2
      fi
      venv_dir=$1
      ;;
    --venv=*)
      venv_dir=${1#--venv=}
      ;;
    --python)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--python requires an executable path" >&2
        exit 2
      fi
      python_cmd=$1
      ;;
    --python=*)
      python_cmd=${1#--python=}
      ;;
    --skip-apt)
      skip_apt=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

install_apt_packages() {
  local -a packages=( "$@" )
  local apt_cmd=()

  if [[ ${#packages[@]} -eq 0 ]]; then
    return
  fi

  if [[ $skip_apt -eq 1 ]]; then
    echo "Skipping apt install for missing packages: ${packages[*]}" >&2
    return
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "apt-get not found. Install missing packages manually: ${packages[*]}" >&2
    return
  fi

  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    apt_cmd=( apt-get )
  elif command -v sudo >/dev/null 2>&1; then
    apt_cmd=( sudo apt-get )
  else
    echo "Missing packages: ${packages[*]}" >&2
    echo "Run as root or install sudo, then run:" >&2
    echo "  apt-get update && apt-get install -y ${packages[*]}" >&2
    return
  fi

  echo "Installing missing apt packages: ${packages[*]}"
  "${apt_cmd[@]}" update
  "${apt_cmd[@]}" install -y "${packages[@]}"
}

missing_apt_packages=()
if ! command -v ffmpeg >/dev/null 2>&1; then
  missing_apt_packages+=( ffmpeg )
fi
if ! command -v fzf >/dev/null 2>&1; then
  missing_apt_packages+=( fzf )
fi
install_apt_packages "${missing_apt_packages[@]}"

if ! command -v "$python_cmd" >/dev/null 2>&1; then
  echo "Python executable not found: $python_cmd" >&2
  echo "Install python3 and python3-venv, or pass --python PATH." >&2
  exit 1
fi

if ! "$python_cmd" -m venv --help >/dev/null 2>&1; then
  echo "Python venv module is unavailable for: $python_cmd" >&2
  echo "On Debian/Ubuntu, install it with: sudo apt install python3-venv" >&2
  exit 1
fi

requirements_file=$script_dir/requirements.txt
diarization_requirements_file=$script_dir/requirements-diarization.txt

if [[ ! -f "$requirements_file" ]]; then
  echo "Missing requirements file: $requirements_file" >&2
  exit 1
fi

if [[ $with_diarization -eq 1 && ! -f "$diarization_requirements_file" ]]; then
  echo "Missing diarization requirements file: $diarization_requirements_file" >&2
  exit 1
fi

echo "Creating virtualenv: $venv_dir"
"$python_cmd" -m venv "$venv_dir"

venv_python=$venv_dir/bin/python
if [[ ! -x "$venv_python" ]]; then
  echo "Virtualenv Python was not created at: $venv_python" >&2
  exit 1
fi

echo "Upgrading pip tooling"
"$venv_python" -m pip install --upgrade pip setuptools wheel

echo "Installing base dependencies"
"$venv_python" -m pip install -r "$requirements_file"

if [[ $with_diarization -eq 1 ]]; then
  echo "Installing optional diarization dependencies"
  "$venv_python" -m pip install -r "$diarization_requirements_file"
fi

echo
echo "Install complete."
echo "Use: $venv_python $script_dir/media_scribe.py --help"
if [[ $with_diarization -eq 0 ]]; then
  echo "Diarization deps were skipped. Re-run with --with-diarization if needed."
fi
