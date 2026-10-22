#!/bin/bash
# One isolated `claude -p` call on the user's subscription.
# Usage: claude_p.sh <model> <system-prompt-file> <user-message-file> <out.json>
# The OAuth token comes from the macOS keychain and is never printed.
set -euo pipefail
MODEL="$1"; SYSTEM="$2"; INPUT="$3"; OUT="$4"
CFG=$(mktemp -d); trap 'rm -rf "$CFG"' EXIT
OUT=$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT"); SYSTEM=$(cd "$(dirname "$SYSTEM")" && pwd)/$(basename "$SYSTEM")
INPUT=$(cd "$(dirname "$INPUT")" && pwd)/$(basename "$INPUT")
cd "$CFG"  # no instruction files from parent folders (evidence/run-1)
echo '{}' > "$CFG/settings.json"
TOKEN=$(security find-generic-password -s "Claude Code-credentials" -a "$USER" -w | python3 -c 'import json,sys; print(json.load(sys.stdin)["claudeAiOauth"]["accessToken"])')
env -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH \
    -u CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH -u ANTHROPIC_BASE_URL -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_API_KEY \
    CLAUDE_CONFIG_DIR="$CFG" CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
    claude -p --model "$MODEL" --system-prompt-file "$SYSTEM" --tools "" --max-turns 1 --output-format json \
    < "$INPUT" > "$OUT.part"
mv "$OUT.part" "$OUT"
