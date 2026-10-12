#!/bin/zsh
set -euo pipefail
EP_ROOT="${0:A:h:h}"
if [[ -r "$EP_ROOT/.credential_file.env" ]]; then source "$EP_ROOT/.credential_file.env"; fi
EP_KEY_FILE="${EP_CREDENTIAL_FILE:-$EP_ROOT/.credentials.env}"
if [[ -e "$EP_KEY_FILE" ]]; then
  if [[ "${1:-}" == --replace ]]; then
    cp "$EP_KEY_FILE" "$EP_KEY_FILE.backup_$(date +%Y%m%d_%H%M%S)"
  else
    print "Existing credential file retained: $EP_KEY_FILE (use --replace to enter new keys)"
    exit 0
  fi
fi
umask 077
mkdir -p "${EP_KEY_FILE:h}"
EP_TEMP=$(mktemp "$EP_KEY_FILE.XXXXXX")
trap 'rm -f "$EP_TEMP"' EXIT
for EP_NAME in GATEWAY_DS_API_KEY GATEWAY_GLM_API_KEY GATEWAY_KIMI_API_KEY GATEWAY_GPT_API_KEY; do
  if [[ "${1:-}" == --replace ]]; then EP_KEY=""; else EP_KEY="${(P)EP_NAME:-}"; fi
  if [[ -z "$EP_KEY" ]]; then
    read -rs "EP_KEY?$EP_NAME (leave blank to disable): "
    print
  fi
  if [[ -n "$EP_KEY" ]]; then
    printf 'export %s=%s\n' "$EP_NAME" "${(qq)EP_KEY}" >> "$EP_TEMP"
  fi
  unset EP_KEY
done
mv "$EP_TEMP" "$EP_KEY_FILE"
chmod 600 "$EP_KEY_FILE"
print "Credentials saved locally; values are not printed."
