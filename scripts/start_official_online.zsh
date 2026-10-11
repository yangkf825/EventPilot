#!/bin/zsh
# Keep the old start_online_v2.zsh unchanged for its frozen data and resumes.
set -euo pipefail
EA_OFFICIAL_PROJECT="${0:A:h:h}"
cd "$EA_OFFICIAL_PROJECT"
source ./scripts/activate_official.zsh
EA_OFFICIAL_MODE="${1:-baseline}"
EA_OFFICIAL_ALIAS="${2:-gateway_ds}"
EA_OFFICIAL_OUTPUT="${3:-runs/official_${EA_OFFICIAL_ALIAS}_${EA_OFFICIAL_MODE}_$(date +%Y%m%d_%H%M%S)}"
EA_OFFICIAL_RESUME="${4:-}"
EA_OFFICIAL_RETRY="${5:-}"
EA_OFFICIAL_CONFIG="${EA_OFFICIAL_CONFIG:-models.gateway.json}"
EA_OFFICIAL_AVAILABILITY="${EA_OFFICIAL_AVAILABILITY:-reports/official_sites/availability.json}"
EA_OFFICIAL_TASK_IDS="${EA_OFFICIAL_TASK_IDS:-d71be72aa25c3eab8eea47a0e60382e2,bf3b311cc8dce16d3de844f4b5875dfd,828c2d98616a9478d5864d847d5a1b28,d9a8689393effeed75ea0866e44e1def,3621b099326c7aebd2e2dac6be3b52d1}"
if [[ "$EA_OFFICIAL_MODE" != baseline && "$EA_OFFICIAL_MODE" != pilot ]]; then
  print -u2 'Usage: start_official_online.zsh baseline|pilot gateway_ds|gateway_glm|gateway_kimi|gateway_gpt [run-root] [resume] [retry-infra (pilot only)]'
  exit 2
fi
case "$EA_OFFICIAL_ALIAS" in
  gateway_ds) EA_OFFICIAL_KEY_ENV=GATEWAY_DS_API_KEY ;;
  gateway_glm) EA_OFFICIAL_KEY_ENV=GATEWAY_GLM_API_KEY ;;
  gateway_kimi) EA_OFFICIAL_KEY_ENV=GATEWAY_KIMI_API_KEY ;;
  gateway_gpt) EA_OFFICIAL_KEY_ENV=GATEWAY_GPT_API_KEY ;;
  *) print -u2 'Choose a configured gateway alias; custom configs use the Python entry.'; exit 2 ;;
esac
if [[ -z "${(P)EA_OFFICIAL_KEY_ENV:-}" ]]; then
  read -rs "EA_OFFICIAL_INPUT_KEY?${EA_OFFICIAL_ALIAS} API Key: "
  print
  export "${EA_OFFICIAL_KEY_ENV}=${EA_OFFICIAL_INPUT_KEY}"
  unset EA_OFFICIAL_INPUT_KEY
fi
EA_OFFICIAL_RESUME_ARGS=()
if [[ "$EA_OFFICIAL_RESUME" == resume ]]; then
  EA_OFFICIAL_RESUME_ARGS+=(--resume)
elif [[ -n "$EA_OFFICIAL_RESUME" ]]; then
  print -u2 'Fourth argument must be resume or omitted';exit 2
fi
if [[ "$EA_OFFICIAL_RETRY" == retry-infra && "$EA_OFFICIAL_MODE" == pilot && "$EA_OFFICIAL_RESUME" == resume ]]; then
  EA_OFFICIAL_RESUME_ARGS+=(--retry-infra)
elif [[ -n "$EA_OFFICIAL_RETRY" ]]; then
  print -u2 'retry-infra requires pilot mode and resume; omit the fifth argument for baseline';exit 2
fi
print "结果目录：$EA_OFFICIAL_OUTPUT"
"$EA_OFFICIAL_PYTHON" scripts/probe_gateway.py --config "$EA_OFFICIAL_CONFIG" --only "$EA_OFFICIAL_ALIAS" --timeout 60
if [[ "$EA_OFFICIAL_MODE" == baseline ]]; then
  "$EA_OFFICIAL_PYTHON" -u scripts/official_online.py baseline --config "$EA_OFFICIAL_CONFIG" --only "$EA_OFFICIAL_ALIAS" \
    --judge-model "$EA_OFFICIAL_ALIAS" \
    --task-ids "$EA_OFFICIAL_TASK_IDS" \
    --availability "$EA_OFFICIAL_AVAILABILITY" \
    --run-root "$EA_OFFICIAL_OUTPUT" --headless "${EA_OFFICIAL_RESUME_ARGS[@]}"
else
  EA_OFFICIAL_IMPORT_ARGS=()
  if [[ -n "${EA_OFFICIAL_PREPARATION_FROM:-}" ]]; then
    EA_OFFICIAL_IMPORT_ARGS+=(--preparation-from-run "$EA_OFFICIAL_PREPARATION_FROM")
  fi
  "$EA_OFFICIAL_PYTHON" -u scripts/run_official_online.py run --config "$EA_OFFICIAL_CONFIG" --only "$EA_OFFICIAL_ALIAS" \
    --data-dir data/official_online/events_pilot --experiments all --single-limit 10 --cf-groups 1 \
    --multi-limit 2 --repeats 1 --workers 1 --run-root "$EA_OFFICIAL_OUTPUT" --headless \
    "${EA_OFFICIAL_RESUME_ARGS[@]}" "${EA_OFFICIAL_IMPORT_ARGS[@]}"
fi
