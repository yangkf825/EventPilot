# Source this file from Bash or Zsh; it does not install or download anything.
if [ -n "${ZSH_VERSION:-}" ]; then
    EP_PROJECT_ROOT="$(cd "$(dirname "${(%):-%N}")" && pwd)"
else
    EP_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -f "$EP_PROJECT_ROOT/.venv/bin/activate" ]; then
    . "$EP_PROJECT_ROOT/.venv/bin/activate"
else
    echo "No project virtualenv. Create it with: python3 -m venv '$EP_PROJECT_ROOT/.venv'" >&2
    echo "Then install: '$EP_PROJECT_ROOT/.venv/bin/python' -m pip install -r '$EP_PROJECT_ROOT/requirements.txt'" >&2
    return 1 2>/dev/null || exit 1
fi
export EP_CORPUS_ROOT="$EP_PROJECT_ROOT/corpus"
export PYTHONUNBUFFERED=1
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/eventpilot_v12_pycache_${UID:-user}}"
printf 'EventPilot v12 ready: %s\n' "$(command -v python)"
