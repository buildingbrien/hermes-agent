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
    p="${p%$'\r'}"                             # strip CR (series checked out CRLF on Windows)
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

# 3) overlay fork-only addon files. An addon never replaces a file upstream (or a
#    patch) already put there — that would silently swap out upstream code or
#    tests — so a collision fails the build. One routed exception:
#    runtime-addons/tests/conftest.py, the addon suite's hermetic guard, would
#    land on upstream's tests/conftest.py; it is installed as the runtime ROOT
#    conftest.py instead, and .lucaryin-addon-tests lists the addon test files it
#    guards (upstream's tests are untouched by it).
if [ -d "$ADDONS" ]; then
  clobbered=""
  addon_tests=""
  while IFS= read -r f; do
    f="${f#./}"
    case "$f" in */__pycache__/*|*.pyc|*/.pytest_cache/*|.DS_Store|*/.DS_Store) continue ;; esac
    dest="$f"
    [ "$f" = "tests/conftest.py" ] && dest="conftest.py"
    if [ -e "$OUT/$dest" ]; then
      clobbered="$clobbered $f"
      continue
    fi
    mkdir -p "$(dirname "$OUT/$dest")"
    cp "$ADDONS/$f" "$OUT/$dest"
    case "$f" in tests/*/test_*.py|tests/test_*.py) addon_tests="$addon_tests$f"$'\n' ;; esac
  done < <(cd "$ADDONS" && find . -type f | LC_ALL=C sort)
  if [ -n "$clobbered" ]; then
    echo "[build-runtime] addon file(s) would replace an upstream/patched file:$clobbered" >&2
    exit 5
  fi
  {
    echo "# Written by build-runtime.sh: the fork-owned addon tests (runtime-addons/tests)."
    echo "# conftest.py at this root applies its hermetic guard to exactly these files."
    printf '%s' "$addon_tests"
  } > "$OUT/.lucaryin-addon-tests"
fi

echo "[build-runtime] done: upstream $TAG + $applied patch(es) + addons"

if [ "${1:-}" = "--check" ]; then
  # Sanity: the runtime must at least import-shape correctly (key serving files present).
  for f in run_agent.py cli.py model_tools.py agent/turn_context.py; do
    [ -f "$OUT/$f" ] || { echo "[build-runtime] --check: missing $f in materialized runtime" >&2; exit 4; }
  done
  echo "[build-runtime] --check ok"
fi
