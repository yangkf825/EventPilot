#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
export EP_METHODS="${EP_METHODS:-${EP_METHOD:-direct}}"
exec zsh scripts/start_methods.zsh "${1:-gateway_ds}" context "${2:-full}"
