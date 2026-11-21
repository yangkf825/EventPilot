#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
MODEL="${1:-gateway_ds}"
MODE="${2:-full}"
METHOD="${EP_METHOD:-direct}"
export EP_METHODS="$METHOD"
exec zsh scripts/start_methods.zsh "$MODEL" custom "$MODE"
