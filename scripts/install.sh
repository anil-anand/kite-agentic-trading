#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dry_run=false

usage() {
  cat <<'EOF'
Install Kite Agentic Trading from the current checkout.

Usage:
  ./scripts/install.sh [--dry-run]

Options:
  --dry-run  Show the commands without running them.
  --help     Show this help message.
EOF
}

fail() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

run() {
  printf '+ %s\n' "$*"
  if [[ "$dry_run" == false ]]; then
    "$@"
  fi
}

main() {
  for arg in "$@"; do
    case "$arg" in
      --dry-run) dry_run=true ;;
      --help|-h)
        usage
        return 0
        ;;
      *)
        usage >&2
        fail "unknown option: $arg"
        ;;
    esac
  done

  local platform="${INSTALLER_UNAME_OVERRIDE:-$(uname -s)}"
  case "$platform" in
    Darwin|Linux) ;;
    *) fail "this installer supports macOS or Linux, not $platform" ;;
  esac

  local command_name
  for command_name in node npm uv python3; do
    command -v "$command_name" >/dev/null 2>&1 || \
      fail "required command not found: $command_name"
  done

  cd "$repo_root"
  run npm install
  run uv sync
  run npm run dist

  printf '\nInstallation complete. Packaged artifacts are in %s/release/.\n' \
    "$repo_root"
}

main "$@"
