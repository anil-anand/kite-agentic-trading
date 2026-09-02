#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
installer="$repo_root/scripts/install.sh"

help_output="$($installer --help)"
[[ "$help_output" == *"Install Kite Agentic Trading"* ]]
[[ "$help_output" == *"--dry-run"* ]]

if INSTALLER_UNAME_OVERRIDE=FreeBSD "$installer" --dry-run >/tmp/kite-installer-test.out 2>&1; then
  printf 'installer unexpectedly accepted unsupported platform\n' >&2
  exit 1
fi

grep -q "macOS or Linux" /tmp/kite-installer-test.out
rm -f /tmp/kite-installer-test.out

printf 'installer tests passed\n'
