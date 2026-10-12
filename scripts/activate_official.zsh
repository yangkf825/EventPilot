#!/bin/zsh
# Source this file to use the official-task environment without changing old runs.
EA_OFFICIAL_PROJECT="${${(%):-%N}:A:h:h}"
if [[ -r "$EA_OFFICIAL_PROJECT/.official_runtime.env" ]]; then
  source "$EA_OFFICIAL_PROJECT/.official_runtime.env"
else
  if [[ -z "${EA_OFFICIAL_PYTHON:-}" ]]; then
    if [[ -x "$EA_OFFICIAL_PROJECT/.venv/bin/python" ]]; then
      export EA_OFFICIAL_PYTHON="$EA_OFFICIAL_PROJECT/.venv/bin/python"
    else
      export EA_OFFICIAL_PYTHON="$(command -v python3)"
    fi
  fi
fi
if [[ ! -x "${EA_OFFICIAL_PYTHON:-}" ]]; then
  print -u2 'Official Python is unavailable. Install the project .venv or set EA_OFFICIAL_PYTHON.'
  return 1
fi
"$EA_OFFICIAL_PYTHON" -c 'import sys, playwright, httpx; from importlib.metadata import version; assert sys.version_info >= (3,11); assert version("playwright")=="1.63.0", "Install the pinned Playwright 1.63.0 for the HAR replay adapter"' || return 1
print "Official-task environment ready. Python: $EA_OFFICIAL_PYTHON"
