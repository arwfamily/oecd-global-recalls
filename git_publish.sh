#!/usr/bin/env bash
# scripts/git_publish.sh — the ONLY place this repo talks to git.
#
# Contract: commit whatever is staged under the given paths and get it onto
# the target branch, or fail loudly having lost nothing.
#
# Why this exists: between 2026-07 and 2026-08 four runs lost work to git
# plumbing (shallow clone with no rebase base, push race between the daily
# and backfill jobs, `origin/HEAD` that does not exist, an interrupted
# rebase leaving a detached HEAD that poisoned every later step). Each was
# patched separately in three places. This file replaces all of them so
# there is one implementation to test and one to fix.
#
# Design rules:
#   1. Never assume HEAD is on a branch. Address the target by name.
#   2. Never use `git pull`. It needs an upstream; fetch+rebase does not.
#   3. Never name `origin/HEAD`. actions/checkout does not create it.
#   4. Clean any interrupted rebase before touching anything.
#   5. Unshallow before the first rebase, not after it fails.
#   6. Fail closed and say what is on disk, so the caller can still save it.
#
# Usage:  scripts/git_publish.sh "commit message" [path ...]
set -uo pipefail

MSG="${1:?commit message required}"
shift
PATHS=("$@")
[ ${#PATHS[@]} -eq 0 ] && PATHS=("data/")

BRANCH="${PUBLISH_BRANCH:-${GITHUB_REF_NAME:-main}}"
ATTEMPTS="${PUBLISH_ATTEMPTS:-4}"

log() { echo "[publish] $*"; }

# --- 4. an interrupted rebase poisons everything downstream -----------------
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  log "WARNING: an interrupted rebase was in progress - aborting it"
  git rebase --abort || git rebase --quit || true
fi

git config user.name  "${PUBLISH_NAME:-oecd-collector-bot}"
git config user.email "${PUBLISH_EMAIL:-actions@users.noreply.github.com}"

git add -- "${PATHS[@]}"
if git diff --cached --quiet; then
  log "nothing to commit"
  exit 0
fi

git commit -q -m "$MSG" || { log "ERROR: commit failed"; exit 1; }
log "committed: $MSG"

# --- 5. rebase needs history; do this once, up front ------------------------
if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
  log "shallow clone - unshallowing so a rebase has a base"
  git fetch --unshallow --quiet || log "WARNING: unshallow failed, continuing"
fi

for i in $(seq 1 "$ATTEMPTS"); do
  # --- 1. HEAD:<branch> works whether or not HEAD is attached ---------------
  if git push --quiet origin "HEAD:$BRANCH"; then
    log "pushed to $BRANCH on attempt $i"
    exit 0
  fi
  log "push rejected (attempt $i/$ATTEMPTS) - replaying onto origin/$BRANCH"

  # --- 2 & 3. named branch, fetch+rebase, never pull, never origin/HEAD -----
  if ! git fetch --quiet origin "$BRANCH"; then
    log "ERROR: cannot fetch origin/$BRANCH (network or auth)"
    break
  fi
  if ! git rebase --quiet "origin/$BRANCH"; then
    git rebase --abort || true
    log "ERROR: rebase onto origin/$BRANCH hit a conflict - not retrying"
    break
  fi
  sleep $(( i * 3 ))
done

# --- 6. fail closed, but tell the caller the work is on disk ----------------
log "ERROR: could not publish after $ATTEMPTS attempts."
log "The commit exists locally at $(git rev-parse --short HEAD)."
log "The workflow uploads data/ as an artifact, so nothing is lost:"
log "  Actions run page -> Artifacts -> download -> commit by hand."
exit 1
