#!/usr/bin/env bash
# build-runtime.sh — materialize the shipping runtime = pristine vendored upstream
# + the thin Lucaryin overlay (fold patches + addon files).
#
# The runtime is now `vendor/hermes-upstream` (pinned at UPSTREAM_TAG) plus a
# small overlay, instead of a divergent content-fork. This script assembles them
# into ./build/runtime, which is what the app bundles (release.yml points
# bundle-runtimes.sh at ./build/runtime) and what the bridge's runtime tests
# point HERMES_RUNTIME_DIR at.
#
# Usage:  ./build-runtime.sh [--check]
#   --check : also fail if any patch does not apply cleanly (CI gate)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$ROOT/vendor/hermes-upstream"
OUT="$ROOT/build/runtime"
PATCHES="$ROOT/runtime-patches"
ADDONS="$ROOT/runtime-addons"
TAG="$(cat "$ROOT/UPSTREAM_TAG" 2>/dev/null || echo '?')"

[ -d "$VENDOR" ] || { echo "no vendored upstream at $VENDOR — run 'git subtree add …' first" >&2; exit 2; }

echo "[build-runtime] materializing $TAG → $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"
cp -R "$VENDOR/." "$OUT/"                       # 1) pristine upstream at UPSTREAM_TAG

# 2) apply Lucaryin fold patches, in series order (git apply, no repo required)
applied=0
if [ -f "$PATCHES/series" ]; then
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    case "$p" in \#*) continue ;; esac        # allow comments in series
    if git apply --directory="build/runtime" -p1 "$PATCHES/$p" 2>/dev/null || \
       ( cd "$OUT" && git apply -p1 "$PATCHES/$p" ); then
      applied=$((applied+1))
    else
      echo "[build-runtime] PATCH FAILED to apply: $p" >&2
      exit 3
    fi
  done < "$PATCHES/series"
fi

# 3) overlay fork-only addon files (never conflict — absent upstream)
[ -d "$ADDONS" ] && cp -R "$ADDONS/." "$OUT/"

echo "[build-runtime] done: upstream $TAG + $applied patch(es) + addons"

if [ "${1:-}" = "--check" ]; then
  # Sanity: the runtime must at least import-shape correctly (key serving files present).
  for f in run_agent.py cli.py model_tools.py agent/turn_context.py; do
    [ -f "$OUT/$f" ] || { echo "[build-runtime] --check: missing $f in materialized runtime" >&2; exit 4; }
  done
  echo "[build-runtime] --check ok"
fi
