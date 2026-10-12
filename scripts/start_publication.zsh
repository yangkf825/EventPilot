#!/bin/zsh
set -euo pipefail
EP_ROOT="${0:A:h:h}"
cd "$EP_ROOT"
source scripts/activate_official.zsh
if [[ -z "${EP_CREDENTIAL_FILE:-}" && -r "$EP_ROOT/.credential_file.env" ]]; then source "$EP_ROOT/.credential_file.env"; fi
EP_KEY_FILE="${EP_CREDENTIAL_FILE:-$EP_ROOT/.credentials.env}"
if [[ -r "$EP_KEY_FILE" ]]; then source "$EP_KEY_FILE"; fi
EP_MODELS="${1:-gateway_ds,gateway_glm,gateway_kimi,gateway_gpt}"
EP_OUTPUT="${2:-${EP_PUBLICATION_RUN_ROOT:-$EP_ROOT/runs/publication_v6_$(date +%Y%m%d_%H%M%S)}}"
EP_OPTIONS=()
if [[ -n "${3:-}" ]]; then
  [[ "$3" == resume ]] || { print -u2 'Third argument must be resume';exit 2; }
  EP_OPTIONS+=(--resume)
fi
if [[ -n "${4:-}" ]]; then
  [[ "$4" == retry-infra && "${3:-}" == resume ]] || { print -u2 'retry-infra requires resume';exit 2; }
  EP_OPTIONS+=(--retry-infra)
fi
[[ -z "${EP_PREP_MODEL:-}" ]] || EP_OPTIONS+=(--prep-model "$EP_PREP_MODEL")
[[ -z "${EP_JUDGE_MODEL:-}" ]] || EP_OPTIONS+=(--judge-model "$EP_JUDGE_MODEL")
[[ -z "${EP_MODEL_ID:-}" ]] || EP_OPTIONS+=(--model-id "$EP_MODEL_ID")
[[ -z "${EP_PREPARATION_FROM:-}" ]] || EP_OPTIONS+=(--preparation-from-run "$EP_PREPARATION_FROM")
[[ -z "${EP_PREP_WORKERS:-}" ]] || EP_OPTIONS+=(--prep-workers "$EP_PREP_WORKERS")
export EP_RUN_ROOT="${EP_OUTPUT:A}"
EP_LOCAL_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/eventpilot"
EP_RUN_ID=$(print -rn -- "$EP_RUN_ROOT" | shasum -a 256 | cut -d' ' -f1)
export EP_STORAGE_JOURNAL="$EP_LOCAL_STATE/pending/$EP_RUN_ID"
mkdir -p "$EP_STORAGE_JOURNAL"
"$EA_OFFICIAL_PYTHON" scripts/recover_pending_storage.py --run-root "$EP_RUN_ROOT" --journal "$EP_STORAGE_JOURNAL"
mkdir -p "$EP_RUN_ROOT/logs"
print "Formal v6 result directory: $EP_RUN_ROOT"
print 'Casewise protocol: qualified groups run immediately; unavailable cases are recorded; no global 90% halt.'
"$EA_OFFICIAL_PYTHON" -u scripts/run_publication.py --only "$EP_MODELS" --run-root "$EP_RUN_ROOT" "${EP_OPTIONS[@]}" \
  2>&1 | tee -a "$EP_RUN_ROOT/logs/publication.log"
