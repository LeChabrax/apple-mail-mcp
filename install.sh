#!/bin/bash
# Install this MCP server into Claude Desktop.
#
#   curl -fsSL https://raw.githubusercontent.com/LeChabrax/apple-mail-mcp/main/install.sh | bash
#
# Piping into bash is what makes this work at all on a Mac: a downloaded
# script file is quarantined and Gatekeeper refuses to open it, while a
# piped one never touches the filesystem. It also means you are running
# whatever that URL serves, so read it first if that matters to you.
#
# La version installee est la REF ci-dessous. `main` est l'etat livrable : on ne
# committe pas dessus, on y merge une branche terminee. Un merge vaut donc une
# livraison a tous les postes, au prochain demarrage de Claude Desktop.

set -eu

REF="${APPLE_MAIL_MCP_REF:-main}"
REPO="https://github.com/LeChabrax/apple-mail-mcp"
SERVER="apple-mail-mcp"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'

abort() { printf '%s\n' "${RED}$1${NC}" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || abort "This installer is macOS only."

CLAUDE_DIR="$HOME/Library/Application Support/Claude"
CONFIG="$CLAUDE_DIR/claude_desktop_config.json"
[ -d "$CLAUDE_DIR" ] || abort "Claude Desktop is not installed. Get it from https://claude.ai/download, then re-run."
printf '%s\n' "${GREEN}OK${NC} Claude Desktop found"

find_uvx() {
    local c
    for c in "$HOME/.local/bin/uvx" /opt/homebrew/bin/uvx /usr/local/bin/uvx "$(command -v uvx 2>/dev/null || true)"; do
        if [ -n "$c" ] && [ -x "$c" ]; then printf '%s' "$c"; return 0; fi
    done
    return 1
}

UVX="$(find_uvx || true)"
if [ -z "$UVX" ]; then
    printf '%s\n' "${YELLOW}..${NC} installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || abort "uv install failed. Check your connection and re-run."
    UVX="$(find_uvx || true)"
    [ -n "$UVX" ] || abort "uv installed but uvx is still not on disk."
fi
printf '%s\n' "${GREEN}OK${NC} uvx at $UVX"
UV="${UVX%x}"; [ -x "$UV" ] || UV="$UVX"

# Fetch and run once BEFORE writing any config: a config must not point at a
# server that does not start, and this call warms the cache. Without it the
# very first launch inside Claude Desktop pays for the download and build, and
# Claude Desktop may give up on the server before it finishes.
printf '%s\n' "${YELLOW}..${NC} fetching the server (slow the first time)"
"$UVX" --from "git+${REPO}@${REF}" "$SERVER" --help >/dev/null 2>&1 \
    || abort "The server does not start. Run this to see why:
  $UVX --from git+${REPO}@${REF} $SERVER --help"
printf '%s\n' "${GREEN}OK${NC} server runs"

if [ -f "$CONFIG" ]; then
    cp "$CONFIG" "${CONFIG}.bak-$(date +%Y%m%d-%H%M%S)"
    printf '%s\n' "${GREEN}OK${NC} existing config backed up"
fi

CONFIG_PATH="$CONFIG" SRV="$SERVER" CMD="$UVX" SOURCE="git+${REPO}@${REF}" \
"$UV" run --quiet --python 3.12 python - <<'PYEOF' || abort "Writing the config failed. Nothing was changed."
import json
import os
import sys

path = os.environ["CONFIG_PATH"]
server = os.environ["SRV"]

config = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Never overwrite a config we cannot parse: it may hold other MCP
        # servers we would not be able to put back.
        print("existing config is not readable, leaving it alone", file=sys.stderr)
        sys.exit(1)
if not isinstance(config, dict):
    print("existing config has an unexpected shape, leaving it alone", file=sys.stderr)
    sys.exit(1)

config.setdefault("mcpServers", {})
for old in ("apple-mail", "apple-mail-fast"):
    config["mcpServers"].pop(old, None)

# ABSOLUTE path to uvx. Claude Desktop starts its servers with the PATH of an
# app launched from Finder, where ~/.local/bin does not exist: a bare
# "command": "uvx" is never found and the server never starts.
config["mcpServers"][server] = {
    "command": os.environ["CMD"],
    "args": ["--from", os.environ["SOURCE"], server],
    "type": "stdio",
}

with open(path, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF

CONFIG_PATH="$CONFIG" SRV="$SERVER" "$UV" run --quiet --python 3.12 python - <<'PYEOF' \
    || abort "The server is not correctly declared after writing."
import json
import os
import sys

with open(os.environ["CONFIG_PATH"], encoding="utf-8") as f:
    c = json.load(f)
e = c.get("mcpServers", {}).get(os.environ["SRV"])
sys.exit(0 if e and e.get("command", "").startswith("/") else 1)
PYEOF
printf '%s\n' "${GREEN}OK${NC} config written and read back"

cat <<'EOF'

Done. Now:
  1. Quit Claude Desktop completely (Cmd-Q, not just closing the window)
  2. Start it again
  3. The first time it touches your mail, macOS asks for permission to
     control Mail. Say yes. It only asks once.
EOF
