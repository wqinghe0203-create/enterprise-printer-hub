#!/bin/zsh
# Invoke with /bin/zsh; zsh/system provides an OS lock even before Python exists.
set -eu
cd -- "${0:A:h:h}"

find_python() {
    local candidate
    for candidate in .venv/bin/python "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
        "$(command -v python3 || true)" /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
        /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
        if [[ -n "$candidate" && "$candidate" != /usr/bin/python3 && -x "$candidate" ]] && \
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' >/dev/null 2>&1; then
            print -r -- "$candidate"
            return 0
        fi
    done
    return 1
}

fail() {
    print -r -- "{\"status\":\"${2:-failed}\",\"phase\":\"system_python\",\"message\":\"$1\"}"
    exit 1
}

python_bin=$(find_python) || {
    brew_bin=$(command -v brew || true)
    if [[ -z "$brew_bin" && -x /opt/homebrew/bin/brew ]]; then brew_bin=/opt/homebrew/bin/brew; fi
    if [[ -z "$brew_bin" && -x /usr/local/bin/brew ]]; then brew_bin=/usr/local/bin/brew; fi
    [[ -n "$brew_bin" ]] || fail 'Install Python from https://www.python.org/downloads/macos/ and retry.'
    [[ -d data ]] || mkdir -p data || [[ -d data ]] || fail 'Cannot create the skill data directory.'
    touch data/bootstrap-python.lock || fail 'Cannot write the Python setup lock.'
    zmodload zsh/system || fail 'Cannot lock Python setup. Install Python manually and retry.'
    zsystem flock -t 3 -f lock_fd data/bootstrap-python.lock || fail 'Python setup is still running. Retry later.' busy
    if ! python_bin=$(find_python); then
        print -u2 -- 'Installing system Python with Homebrew. This is separate from starting the console.'
        "$brew_bin" install python@3.12 >&2 || fail 'Homebrew Python installation failed. Check the output and retry.'
        prefix=$("$brew_bin" --prefix python@3.12)
        export PATH="$prefix/bin:$PATH"
        python_bin=$(find_python) || fail 'Python installation incomplete. Check Homebrew and retry.'
    fi
    zsystem flock -u "$lock_fd"
}
exec "$python_bin" scripts/bootstrap.py "$@"
