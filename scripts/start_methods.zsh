#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
source ./activate.sh
MODEL="${1:-gateway_ds}"
SCOPE="${2:-main}"
MODE="${3:-full}"
METHODS="${EP_METHODS:-all}"
case "$SCOPE" in
  main) EXPERIMENTS='main' ;;
  full) EXPERIMENTS='main,ablation,counterfactual,multi,baseline' ;;
  context) EXPERIMENTS='context_ablation_goal,context_ablation_state,context_ablation_multi' ;;
  custom) EXPERIMENTS="${EP_EXPERIMENTS:-main}" ;;
  *) print -u2 'Scope must be main, full, context, or custom'; exit 2 ;;
esac
FLAGS=()
case "$MODE" in
  full) ;;
  resume) FLAGS=(--resume --retry-infra) ;;
  plan) FLAGS=(--dry-run) ;;
  export) FLAGS=(--export-only) ;;
  *) print -u2 'Mode must be full, resume, plan, or export'; exit 2 ;;
esac
if [[ "$MODE" == resume || "$MODE" == export ]]; then
  if [[ -z "${EP_RUN_ROOT:-}" ]]; then
    print -u2 'Set EP_RUN_ROOT to the existing suite directory first.'; exit 2
  fi
fi
RUN_ROOT="${EP_RUN_ROOT:-$PWD/runs/${MODEL}_v12_${SCOPE}_${METHODS//,/_}}"
DELIVERIES="${EP_DELIVERIES:-sequential,batch,sequential_no_action}"
if [[ "$SCOPE" == context ]]; then DELIVERIES="${EP_CONTEXT_DELIVERIES:-sequential}"; fi
COMMON=(--config "${EP_MODELS_CONFIG:-$PWD/models.json}" --only "$MODEL" --run-root "$RUN_ROOT"
        --methods "$METHODS" --experiments "$EXPERIMENTS" --deliveries "$DELIVERIES"
        --workers "${EP_WORKERS:-8}" --api-concurrency "${EP_API_CONCURRENCY:-4}"
        --repeats "${EP_REPEATS:-1}" --candidate-limit "${EP_CANDIDATE_LIMIT:-50}"
        --max-commands "${EP_MAX_COMMANDS:-128}" --output-tokens "${EP_OUTPUT_TOKENS:-8192}"
        --max-model-requests "${EP_MAX_MODEL_REQUESTS:-1024}"
        --reflexion-trials "${EP_REFLEXION_TRIALS:-2}" --corpus-root "$PWD/corpus")
print "Results: $RUN_ROOT"
print "methods=$METHODS; scope=$SCOPE; workers=${EP_WORKERS:-8}; API concurrency=${EP_API_CONCURRENCY:-4}"
LAUNCH=()
if [[ "$MODE" != plan && "$MODE" != export ]] && command -v caffeinate >/dev/null 2>&1; then LAUNCH=(caffeinate -i); fi
"${LAUNCH[@]}" python -u scripts/run_methods.py "${COMMON[@]}" "${FLAGS[@]}"
