#!/usr/bin/env bash
# LIFECYCLE: permanent
# Check and repair this checkout's remote refspecs.
#
# A remote-tracking refspec that names a branch the remote no longer has makes every
# plain `git fetch` fail outright: git reports "couldn't find remote ref
# refs/heads/<gone>" and fetches nothing at all. The branch prune on 2026-09-15 removed
# 716 remote branches and left three such refspecs behind in remote.origin.fetch, so
# `git pull` on main had been failing silently -- the command was run, the error was
# scrolled past, and main stayed stale.
#
#   dev/git-hygiene.sh check    report dead refspecs and stale tracking refs, exit 1 if any
#   dev/git-hygiene.sh repair   drop the dead refspecs, then delete their tracking refs
#
# The default remote is `origin`; pass a second argument to name another one.
set -euo pipefail

# The checkout to inspect is the one the caller is in, not the one this script lives in:
# a fixture clone must be able to exercise it, and a developer may run it from a worktree.
if ! ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  printf 'error: not inside a Git worktree\n' >&2
  exit 2
fi
cd "$ROOT"

mode="${1:-check}"
remote="${2:-origin}"
case "$mode" in
  check | repair) ;;
  *) printf 'usage: dev/git-hygiene.sh [check|repair] [remote]\n' >&2; exit 2 ;;
esac

git config --get "remote.$remote.url" >/dev/null ||
  { printf 'error: %s is not a configured remote\n' "$remote" >&2; exit 2; }

heads="$(git ls-remote --heads "$remote" | awk '{print $2}')"

# A refspec's source side may be a glob; only exact sources can be judged dead.
# Counters, not "${#array[@]}": bash 3.2 treats an empty array expansion as unbound
# under `set -u`, which aborted the repair halfway on macOS.
dead_refspecs=()
dead_count=0
while IFS= read -r refspec; do
  [ -n "$refspec" ] || continue
  source="${refspec%%:*}"
  source="${source#+}"
  case "$source" in
    *'*'*) continue ;;
  esac
  if ! printf '%s\n' "$heads" | grep -qx -- "$source"; then
    dead_refspecs+=("$refspec")
    dead_count=$((dead_count + 1))
  fi
done < <(git config --get-all "remote.$remote.fetch" || true)

# Tracking refs whose branch is gone, regardless of how the refspec is written.
stale_refs=()
stale_count=0
while IFS= read -r ref; do
  [ -n "$ref" ] || continue
  branch="${ref#refs/remotes/$remote/}"
  case "$branch" in
    HEAD) continue ;;
  esac
  if ! printf '%s\n' "$heads" | grep -qx -- "refs/heads/$branch"; then
    stale_refs+=("$ref")
    stale_count=$((stale_count + 1))
  fi
done < <(git for-each-ref --format='%(refname)' "refs/remotes/$remote")

printf 'remote %s: %s head(s), %s refspec(s)\n' \
  "$remote" "$(printf '%s\n' "$heads" | grep -c . || true)" \
  "$(git config --get-all "remote.$remote.fetch" | grep -c . || true)"

if [ "$dead_count" -eq 0 ] && [ "$stale_count" -eq 0 ]; then
  printf 'OK: no dead refspec and no stale tracking ref\n'
  exit 0
fi

printf 'dead refspec(s) — every plain fetch fails while one exists:\n'
if [ "$dead_count" -gt 0 ]; then
  for refspec in "${dead_refspecs[@]}"; do printf '  %s\n' "$refspec"; done
fi
printf 'stale tracking ref(s):\n'
if [ "$stale_count" -gt 0 ]; then
  for ref in "${stale_refs[@]}"; do printf '  %s\n' "$ref"; done
fi

if [ "$mode" = check ]; then
  printf 'Run: dev/git-hygiene.sh repair %s\n' "$remote"
  exit 1
fi

if [ "$dead_count" -gt 0 ]; then
  surviving=()
  surviving_count=0
  while IFS= read -r refspec; do
    [ -n "$refspec" ] || continue
    keep=1
    for dead in "${dead_refspecs[@]}"; do
      [ "$refspec" = "$dead" ] && keep=0
    done
    if [ "$keep" = 1 ]; then
      surviving+=("$refspec")
      surviving_count=$((surviving_count + 1))
    fi
  done < <(git config --get-all "remote.$remote.fetch")
  git config --unset-all "remote.$remote.fetch"
  if [ "$surviving_count" -gt 0 ]; then
    for refspec in "${surviving[@]}"; do
      git config --add "remote.$remote.fetch" "$refspec"
    done
  fi
  printf 'dropped %s dead refspec(s); %s kept\n' "$dead_count" "$surviving_count"
fi

if [ "$stale_count" -gt 0 ]; then
  for ref in "${stale_refs[@]}"; do
    git update-ref -d "$ref"
  done
fi
printf 'deleted %s stale tracking ref(s)\n' "$stale_count"
exec "$0" check "$remote"
