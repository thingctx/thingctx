#!/usr/bin/env bash
# Guard against committing disallowed substrings into this public repo.
#
# The list of disallowed substrings is provided out-of-band and is not stored in
# this repo. It lives in a gitignored file (.security/banned-substrings.txt) or is
# provided via $THINGCTX_BANNED_FILE. When no list is present this hook is a safe
# no-op, so contributors without it are unaffected.
#
# Usage:
#   check_no_internal_leaks.sh [FILE ...]           scan the given files (pre-commit)
#   check_no_internal_leaks.sh --range BASE...HEAD  scan added lines in a range (CI)
set -euo pipefail

SELF="scripts/check_no_internal_leaks.sh"

# Resolve the banned-substring list, first match wins: an explicit override, a
# per-repo gitignored file, then a machine-wide file (covers every clone and
# worktree at once). Absent everywhere, this hook is a no-op.
BANNED_FILE=""
for cand in \
  "${THINGCTX_BANNED_FILE:-}" \
  ".security/banned-substrings.txt" \
  "${XDG_CONFIG_HOME:-$HOME/.config}/thingctx/banned-substrings.txt"; do
  if [ -n "$cand" ] && [ -f "$cand" ]; then
    BANNED_FILE="$cand"
    break
  fi
done
[ -n "$BANNED_FILE" ] || exit 0

patterns="$(grep -vE '^[[:space:]]*(#|$)' "$BANNED_FILE" || true)"
[ -n "$patterns" ] || exit 0

status=0
match() { grep -inF -f <(printf '%s\n' "$patterns") "$@"; }

if [ "${1:-}" = "--range" ]; then
  range="${2:?usage: --range BASE...HEAD}"
  added="$(git diff "$range" -U0 -- . ":(exclude).security/**" ":(exclude)$SELF" \
    | grep -E '^\+' || true)"
  if printf '%s' "$added" | match >/dev/null 2>&1; then
    echo "ERROR: internal/private string found in added lines:" >&2
    printf '%s' "$added" | match >&2 || true
    status=1
  fi
else
  for f in "$@"; do
    case "$f" in
      .security/* | "$SELF") continue ;;
    esac
    [ -f "$f" ] || continue
    if match "$f" >/dev/null 2>&1; then
      echo "ERROR: internal/private string in $f:" >&2
      match "$f" >&2 || true
      status=1
    fi
  done
fi

if [ "$status" -ne 0 ]; then
  echo "" >&2
  echo "Commit blocked: remove the internal/private references above," >&2
  echo "or update your local allowlist if this is a false positive." >&2
fi
exit "$status"
