#!/bin/sh
# agent-si installer. Read it before you run it. It installs uv if needed, installs the
# brain as a uv tool on uv's own Python, checks it, and then asks five questions to make your first brain.
# It installs agent-si only. Agent memory is a separate tool with its own installer; the two work together.
# agent-si isn't on a package index, so this installs a wheel you have and never fetches one by name.
#   sh install.sh --from WHEEL
set -eu

UV_VERSION="0.11.6"  # pinned, at least two weeks old when chosen, bumped on purpose
UV_MIN="0.9.0"       # a uv on PATH older than this is passed over for the pinned copy
VERSION="1.17.1"
PYTHON="3.13"
FROM=""
BY_NAME="agent-si isn't on a package index, so this installer won't fetch it by name. Download install-agent-si.sh from https://github.com/joelbrilliant/agent-si/releases and run it, or pass --from with an agent-si wheel from that page"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$1" >&2; exit "${2:-1}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from) [ $# -ge 2 ] || die "--from needs a wheel" 2; FROM="$2"; shift 2 ;;
    --version) die "$BY_NAME" 2 ;;
    *) die "unknown option $1" 2 ;;
  esac
done
# By name, uv would install whoever registers agent-si on a package index, so only a wheel you hold is installed.
[ -n "$FROM" ] || die "$BY_NAME" 2

case "$(uname -s)" in Darwin|Linux) ;; *) die "agent-si runs on macOS and Linux only" ;; esac
case "$(uname -m)" in x86_64|amd64|arm64|aarch64) ;; *) die "agent-si runs on x86_64 and arm64 only" ;; esac

# True when version $1 is at least $2 (both X.Y.Z).
at_least() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)" = "$2" ]; }

UV="$(command -v uv 2>/dev/null || true)"
if [ -n "$UV" ]; then
  found="$("$UV" --version 2>/dev/null | awk '{print $2}')"
  at_least "${found:-0.0.0}" "$UV_MIN" || UV=""
fi
if [ -z "$UV" ]; then
  say "Installing uv $UV_VERSION from astral.sh"
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | env UV_NO_MODIFY_PATH=1 sh >/dev/null || die "uv didn't install"
  for candidate in "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$candidate" ] && UV="$candidate" && break
  done
  [ -n "$UV" ] || die "uv installed but can't be found"
fi

BIN="$("$UV" tool dir --bin)" || die "uv tool dir --bin failed"
existing="$(command -v brain 2>/dev/null || true)"
if [ -n "$existing" ] && [ "$existing" != "$BIN/brain" ]; then
  die "a brain command is already installed at $existing and wasn't installed by uv; remove it, or put uv's $BIN first on PATH"
fi

[ -f "$FROM" ] || die "no wheel at $FROM" 2
SPEC="$FROM"
say "Installing $SPEC on uv's Python $PYTHON"
"$UV" tool install --managed-python --python "$PYTHON" "$SPEC" >/dev/null || die "uv tool install failed"
"$UV" tool update-shell >/dev/null 2>&1 || true

if ! out="$("$BIN/brain" --version 2>&1)"; then
  printf '%s\n' "$out" >&2
  exit 5
fi
say "$out"

if (: </dev/tty) 2>/dev/null; then
  "$BIN/brain" init </dev/tty || die "brain init stopped"
else
  zone="UTC"
  link="$(readlink /etc/localtime 2>/dev/null || true)"
  case "$link" in *zoneinfo/*) zone="${link#*zoneinfo/}" ;; esac
  say "There's no terminal to ask questions in. Make your brain with:"
  say "  $BIN/brain init work --principal \"Your Name\" --timezone $zone"
fi
say "Open a new Terminal window, then ask your agent: read my brain brief."
