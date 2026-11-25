#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s FULL_PATH_REGEX | --regex-file FILE | --pick [DIRECTORY]\n' "${0##*/}" >&2
    exit 2
}

patterns=()
pick_root=
case ${1-} in
    --pick)
        [[ $# -le 2 ]] || usage
        pick_root=${2-/media}
        [[ -d $pick_root ]] || { printf 'Search directory does not exist: %s\n' "$pick_root" >&2; exit 2; }
        command -v fzf >/dev/null || { printf 'Missing dependency: fzf\n' >&2; exit 2; }
        # Resolve before changing directory so caller-relative roots remain valid.
        IFS= read -r -d '' pick_root < <(cd -P -- "$pick_root" && printf '%s\0' "$PWD") || exit 2
        ;;
    --regex-file)
        [[ $# -eq 2 && -f $2 && -r $2 ]] || usage
        while IFS= read -r line || [[ -n $line ]]; do
            line=${line%$'\r'}
            [[ $line =~ ^[[:space:]]*(#|$) ]] || patterns+=("$line")
        done < "$2"
        ;;
    *)
        [[ $# -eq 1 && -n $1 ]] || usage
        patterns+=("$1")
        ;;
esac

# Validate the entire list before starting any transcription.
for pattern in "${patterns[@]}"; do
    if [[ '' =~ $pattern ]]; then
        :
    elif [[ $? -eq 2 ]]; then
        printf 'Invalid extended regular expression: %s\n' "$pattern" >&2
        exit 2
    fi
done

invocation_dir=$PWD
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

transcribe_file() {
    local fname=$1
    shift
    printf 'Checking %s\n' "$fname"
    # Plex, not a sync-deleted sidecar, determines whether work is complete.
    .venv/bin/python media_scribe.py \
        --hf-token-file hf_token.txt \
        --video "$fname" \
        --plex-url "${PLEX_URL:-http://localhost:32400}" \
        --plex-skip-uploaded-english "$@" </dev/null
}

if [[ -n $pick_root ]]; then
    # Materialize candidates first: a cancelled picker must not hide traversal
    # failures or start transcription from an incomplete search.
    pick_dir=$(mktemp -d)
    trap 'rm -rf -- "$pick_dir"' EXIT
    find "$pick_root" -type f \( \
        -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.m4v' -o \
        -iname '*.avi' -o -iname '*.mov' -o -iname '*.webm' -o \
        -iname '*.mpg' -o -iname '*.mpeg' -o -iname '*.ts' -o \
        -iname '*.m2ts' -o -iname '*.wmv' \
    \) -print0 > "$pick_dir/candidates" || exit 1
    if fzf --read0 --print0 --no-multi --prompt='Subtitle via Runpod> ' \
        < "$pick_dir/candidates" > "$pick_dir/selection"; then
        IFS= read -r -d '' fname < "$pick_dir/selection" || exit 1
        transcribe_file "$fname" --backend runpod --runpod-token-file runpod-token
        exit $?
    else
        status=$?
        # fzf uses 1 for no match and 130 for interactive cancellation.
        [[ $status -eq 1 || $status -eq 130 ]] && exit 0
        exit "$status"
    fi
fi

# A leading slash expresses an absolute search path even without '^'. Other
# unanchored expressions need the invocation directory as their search scope.
search_root_for() {
    .venv/bin/python - "$1" "$invocation_dir" <<'PY'
import sys

pattern, fallback = sys.argv[1:]

def fixed_root(pattern):
    # Treat absolute-path input as rooted so it does not search the caller's cwd.
    if pattern.startswith('/'):
        pattern = '^' + pattern
    if not pattern.startswith('^/'):
        return fallback
    # A top-level alternative can bypass the apparent leading prefix.
    depth = 0
    escaped = False
    in_class = False
    for char in pattern[1:]:
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
        elif in_class:
            if char == ']':
                in_class = False
        elif char == '[':
            # Complex bracket syntax is deliberately handled conservatively:
            # a pipe anywhere in such an expression disables narrowing.
            if '|' in pattern:
                return fallback
            in_class = True
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        elif char == '|' and depth == 0:
            return fallback

    literal = ''
    index = 1
    while index < len(pattern):
        char = pattern[index]
        if char == '\\':
            index += 1
            # Bash ERE also accepts shell-style escaped spaces in user patterns.
            if index >= len(pattern) or pattern[index] not in r'.^$*+?()[]{}|\\ ':
                break
            char = pattern[index]
        elif char in '.^$*+?()[]{}|':
            # A quantified slash may disappear, joining two path components.
            if char in '*?+{' and literal.endswith('/'):
                literal = literal[:-1]
            break
        literal += char
        index += 1
    # Do not include a partial filename or a partly fixed directory component.
    return literal.rsplit('/', 1)[0] or '/'

sys.stdout.write(fixed_root(pattern) + '\0')
PY
}

declare -A seen=()
result=0
for pattern in "${patterns[@]}"; do
    if ! IFS= read -r -d '' search_root < <(search_root_for "$pattern"); then
        printf 'Could not resolve search directory for: %s\n' "$pattern" >&2
        result=2
        continue
    fi
    if [[ ! -d $search_root ]]; then
        printf 'Search directory does not exist: %s\n' "$search_root" >&2
        result=2
        continue
    fi
    printf 'Searching %s\n' "$search_root"

    # NUL delimiters preserve unusual filenames; wait detects failed traversal.
    exec {find_fd}< <(find "$search_root" -type f -print0)
    find_pid=$!
    while IFS= read -r -d '' fname <&"$find_fd"; do
        [[ $fname =~ $pattern ]] || continue
        [[ -z ${seen["$fname"]+x} ]] || continue
        seen["$fname"]=1
        # A failed file must not starve other matches on every scheduled run.
        transcribe_file "$fname" || result=1
    done
    exec {find_fd}<&-
    wait "$find_pid" || result=1
done
exit "$result"
