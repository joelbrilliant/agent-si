#!/bin/sh
# agent-si and agent-memory in one file. Read it before you run it: this top part is all the script there is, and
# below the __AGENT_SI_BUNDLE_PAYLOAD__ line sits a gzipped tar of the two wheels and their SHA256SUMS. It checks
# every wheel against SHA256SUMS before it installs anything, installs uv if needed (pinned, as install.sh does),
# installs both wheels as uv tools on uv's own Python, makes a brain, connects every agent tool it finds to the brain
# and to agent memory, and indexes your past agent sessions once. It installs and checks nothing by name from a package index. It reaches the network only for uv itself (astral.sh) when uv is missing.
#   sh install-agent-si.sh [--yes] [--principal NAME] [--name NAME] [--dir FOLDER] [--timezone ZONE]
#                                  [--no-connect]
# Run it again to upgrade the two tools; your brain is kept.
set -eu

UV_VERSION="0.11.6"  # pinned, at least two weeks old when chosen, bumped on purpose
UV_MIN="0.9.0"       # a uv on PATH older than this is passed over for the pinned copy
PYTHON="3.13"
HEADER_BYTES=@HEADER_BYTES@
MARKER="__AGENT_SI_BUNDLE_PAYLOAD__"
TMP=""

say() { printf '%s\n' "$*"; }
die() { printf 'install-agent-si: %s\n' "$1" >&2; exit "${2:-1}"; }

# True when version $1 is at least $2 (both X.Y.Z).
at_least() { [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n 1)" = "$2" ]; }

sha256() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else sha256sum "$1" | awk '{print $1}'; fi
}

usage() {
  say "usage: sh $0 [--yes] [--principal NAME] [--name NAME] [--dir FOLDER] [--timezone ZONE] [--no-connect]"
  say "  --yes          ask nothing; take every default"
  say "  --principal    your name (default: the account's full name; \"\" for none)"
  say "  --name         the brain's name (default work)"
  say "  --dir          the folder for your brains (default ~/Brains)"
  say "  --timezone     an IANA zone name (default: this machine's)"
  say "  --no-connect   install and make the brain, but don't change any agent tool's settings"
}

# Check the bundle against its own SHA256SUMS, setting SI and MEM to the two wheels' file names.
verify() {
  [ "$(head -c "$HEADER_BYTES" "$0" | tail -n 1)" = "$MARKER" ] || die "this file is damaged or was cut short; download it again and compare its SHA-256 with the one you were given"
  tail -c +"$((HEADER_BYTES + 1))" "$0" > "$TMP/payload.tgz" || die "can't read this file"
  gzip -t "$TMP/payload.tgz" 2>/dev/null || die "this file is damaged or was cut short (its archive doesn't unpack); download it again and compare its SHA-256 with the one you were given"
  mkdir "$TMP/files"
  gzip -dc "$TMP/payload.tgz" | tar -xf - -C "$TMP/files" 2>/dev/null || die "this file is damaged (its archive doesn't unpack); download it again"
  [ -f "$TMP/files/SHA256SUMS" ] || die "this file holds no SHA256SUMS; download it again"
  SI=""; MEM=""
  while read -r want name; do
    [ -n "$want" ] || continue
    name="${name#\*}"
    case "$name" in ""|*/*) die "the bundle's SHA256SUMS names $name; download it again" ;; esac
    [ -f "$TMP/files/$name" ] || die "the bundle lists $name but doesn't hold it; download it again"
    got="$(sha256 "$TMP/files/$name")"
    [ "$got" = "$want" ] || die "$name failed its SHA-256 check (expected $want, got $got), so nothing was installed; download the bundle again"
    case "$name" in agent_si-*.whl) SI="$name" ;; agent_memory-*.whl) MEM="$name" ;; esac
  done < "$TMP/files/SHA256SUMS"
  [ -n "$SI" ] && [ -n "$MEM" ] || die "the bundle is missing a wheel; download it again"
}

# Set UV to a usable uv, installing the pinned one when there isn't. The lines below are install.sh's, word for word
# (tests/test_bundle.py keeps them so).
ensure_uv() {
  command -v uv >/dev/null 2>&1 || command -v curl >/dev/null 2>&1 || die "uv isn't installed and curl isn't here to fetch it; install curl (Ubuntu: apt install curl ca-certificates) and run this again"
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
}

# Install wheel $2 as the uv tool whose command is $1, refusing a command that uv didn't install.
install_tool() {
  existing="$(command -v "$1" 2>/dev/null || true)"
  if [ -n "$existing" ] && [ "$existing" != "$BIN/$1" ]; then
    die "a $1 command is already installed at $existing and wasn't installed by uv; remove it, or put uv's $BIN first on PATH"
  fi
  "$UV" tool install --quiet --reinstall --managed-python --python "$PYTHON" "$TMP/files/$2" >/dev/null || die "uv tool install of $2 failed"
  if ! out="$("$BIN/$1" --version 2>&1)"; then
    printf '%s\n' "$out" >&2
    exit 5
  fi
  say "$out"
}

# One line per tool from agent-memory's one JSON line, or the line itself when it isn't the shape this expects.
memory_summary() {
  case "$1" in *'"results": []'*) say "  no agent tools found"; return ;; esac
  rows="$(printf '%s\n' "$1" | tr '}' '\n' | sed -n 's/.*"harness": "\([^"]*\)".*"smoke": "\([^"]*\)".*"status": "\([^"]*\)".*/  \1: \3, smoke test \2/p')"
  if [ -n "$rows" ]; then say "$rows"; else say "  $1"; fi
}

main() {
  YES=""; PRINCIPAL=""; HAVE_PRINCIPAL=""; NAME=""; DIR=""; ZONE=""; NO_CONNECT=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --yes) YES=1; shift ;;
      --no-connect) NO_CONNECT=1; shift ;;
      --principal) [ $# -ge 2 ] || die "--principal needs a name" 2; PRINCIPAL="$2"; HAVE_PRINCIPAL=1; shift 2 ;;
      --name) [ $# -ge 2 ] || die "--name needs a name" 2; NAME="$2"; shift 2 ;;
      --dir) [ $# -ge 2 ] || die "--dir needs a folder" 2; DIR="$2"; shift 2 ;;
      --timezone) [ $# -ge 2 ] || die "--timezone needs a zone name" 2; ZONE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option $1 (see --help)" 2 ;;
    esac
  done

  case "$(uname -s)" in Darwin|Linux) ;; *) die "agent-si runs on macOS and Linux only" ;; esac
  case "$(uname -m)" in x86_64|amd64|arm64|aarch64) ;; *) die "agent-si runs on x86_64 and arm64 only" ;; esac
  [ -f "$0" ] || die "this file can't be piped into sh; download it first, then run it: curl -LO https://github.com/joelbrilliant/agent-si/releases/latest/download/install-agent-si.sh && sh install-agent-si.sh"
  command -v shasum >/dev/null 2>&1 || command -v sha256sum >/dev/null 2>&1 || die "neither shasum nor sha256sum is here to check the bundle with"

  TMP="$(mktemp -d "${TMPDIR:-/tmp}/agent-si-install.XXXXXX")" || die "can't make a temporary folder"
  trap 'rm -rf "$TMP"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP

  verify
  VERSION_SI="${SI#agent_si-}"; VERSION_SI="${VERSION_SI%%-*}"
  VERSION_MEM="${MEM#agent_memory-}"; VERSION_MEM="${VERSION_MEM%%-*}"
  say "agent-si $VERSION_SI and agent-memory $VERSION_MEM: both wheels match their checksums"

  ensure_uv
  BIN="$("$UV" tool dir --bin)" || die "uv tool dir --bin failed"
  say "Installing both on uv's Python $PYTHON"
  install_tool brain "$SI"
  install_tool agent-memory "$MEM"
  "$UV" tool update-shell >/dev/null 2>&1 || true

  # The tools find each other and the agent tools through these, whether or not uv's folder is on PATH yet.
  UV_TOOL_BIN_DIR="$BIN"; export UV_TOOL_BIN_DIR
  PATH="$BIN:$PATH"; export PATH

  set -- setup
  [ -z "$YES" ] || set -- "$@" --yes
  [ -z "$HAVE_PRINCIPAL" ] || set -- "$@" --principal "$PRINCIPAL"
  [ -z "$NAME" ] || set -- "$@" --name "$NAME"
  [ -z "$DIR" ] || set -- "$@" --dir "$DIR"
  [ -z "$ZONE" ] || set -- "$@" --timezone "$ZONE"
  [ -z "$NO_CONNECT" ] || set -- "$@" --no-connect
  problems=""
  say ""
  code=0
  "$BIN/brain" "$@" || code=$?
  [ "$code" -ne 2 ] || die "brain setup didn't like its options (see above). Both tools are installed; run this file again with corrected options" 2
  [ "$code" -eq 0 ] || problems="$problems brain-setup"

  say ""
  if [ -z "$NO_CONNECT" ]; then
    say "Connecting agent memory:"
    code=0
    out="$("$BIN/agent-memory" connect all 2>&1)" || code=$?
    memory_summary "$out"
    if [ "$code" -eq 0 ]; then
      say "  undo: $BIN/agent-memory connect all --remove"
    else
      problems="$problems agent-memory-connect"
    fi
  fi
  say "Indexing your past agent sessions once (Claude Code, Codex, Grok). This can take a minute."
  "$BIN/agent-memory" session sync || problems="$problems session-sync"

  say ""
  if [ -n "$problems" ]; then
    say "Finished, but these steps failed:$problems. Nothing was rolled back. Fix what's printed above, then run this file again: it upgrades the tools and keeps your brain."
    exit 1
  fi
  say "Open a new terminal, start your agent and ask: read my brain brief."
}

main "$@"
exit $?
__AGENT_SI_BUNDLE_PAYLOAD__
