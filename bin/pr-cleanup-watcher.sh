#!/bin/bash
# When a PR the watchers opened workspaces for is merged or closed: closes its
# self-review, review and fixer workspaces, removes the fixer worktree and its
# fix/pr-<num> branch, and drops the watchers' state for it. A worktree with
# uncommitted changes or commits the PR never got is left alone (with its
# workspace) and reported once. The workspace you're looking at is skipped
# until you leave it.
# Read to the end before running, so editing this file mid-run can't break the run.
{
set -u
export CMUX_QUIET=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

CM=/Applications/cmux.app/Contents/Resources/bin/cmux
STATE_DIR="$HOME/.local/state/pr-cleanup-watcher"
FIXER="$HOME/.local/state/pr-fixer-watcher"
WS_DIRS="$HOME/.local/state/pr-authored-watcher/workspaces $HOME/.local/state/pr-review-watcher/workspaces $FIXER/workspaces"
WT_ROOT="$HOME/.local/share/pr-fixer-watcher/worktrees"
REPOS_DIR="$HOME/Klaviyo/Repos"
mkdir -p "$STATE_DIR/kept"

log() { echo "$(date '+%F %T') $*"; }

# One run at a time: removing a large worktree can outlast the interval.
LOCK="$STATE_DIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then rmdir "$LOCK"; mkdir "$LOCK" || exit 0; else exit 0; fi
fi
trap 'rmdir "$LOCK"' EXIT

export CMUX_SOCKET_PATH
CMUX_SOCKET_PATH=$(cat "$HOME/Library/Application Support/cmux/last-socket-path" 2>/dev/null)
"$CM" ping >/dev/null 2>&1 || { log "cmux not reachable"; exit 0; }
WORKSPACES=$("$CM" workspace list 2>/dev/null) || { log "workspace list failed"; exit 0; }

# Every PR some watcher keeps state for, as owner__repo__num.
keys=$( { ls $WS_DIRS 2>/dev/null; ls "$FIXER/handled" 2>/dev/null; } | grep -E '^[^_]+__.+__[0-9]+$' | sort -u)

for key in $keys; do
  owner=${key%%__*}; rest=${key#*__}; repo=${rest%__*}; num=${rest##*__}
  pr=$(gh pr view "$num" -R "$owner/$repo" --json state,headRefOid,title 2>/dev/null) || { log "gh pr view failed for $key"; continue; }
  state=$(jq -r .state <<<"$pr" | tr A-Z a-z)
  [ "$state" = open ] && continue

  # Keep a fixer worktree that holds work the PR doesn't have.
  wt="$WT_ROOT/${repo}-${num}"; repo_dir="$REPOS_DIR/$repo"; why=""
  if [ -d "$wt" ]; then
    head_oid=$(jq -r .headRefOid <<<"$pr")
    if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
      why="uncommitted changes"
    elif ! git -C "$wt" merge-base --is-ancestor HEAD "$head_oid" 2>/dev/null; then
      git -C "$wt" fetch -q origin "$head_oid" 2>/dev/null
      git -C "$wt" merge-base --is-ancestor HEAD "$head_oid" 2>/dev/null || why="commits the PR doesn't have"
    fi
  fi
  if [ -n "$why" ]; then
    if [ ! -f "$STATE_DIR/kept/$key" ]; then
      touch "$STATE_DIR/kept/$key"
      osascript -e "display notification \"Kept $wt: $why\" with title \"PR $state: $repo #$num\" subtitle \"Workspace cleanup skipped\"" >/dev/null 2>&1
      log "kept $key ($state): $why in $wt"
    fi
    continue
  fi

  # Close its workspaces, unless one is the selected workspace.
  busy=""; closed=0
  for d in $WS_DIRS; do
    f="$d/$key"; [ -f "$f" ] || continue
    ws=$(cat "$f")
    line=$(awk -v id="$ws" '{x=$1; if ($1=="*") x=$2; if (x==id) {print; exit}}' <<<"$WORKSPACES")
    if [ -z "$line" ]; then rm -f "$f"; continue; fi
    case $line in "*"*) busy=1; continue;; esac
    if "$CM" close-workspace --workspace "$ws" --force >/dev/null 2>&1; then rm -f "$f"; closed=$((closed + 1)); else log "failed to close $ws for $key"; busy=1; fi
  done
  [ -n "$busy" ] && { log "$key: workspace still selected or open; retrying next run"; continue; }

  # Checked clean above. rm is much faster than `git worktree remove` on a .venv.
  if [ -d "$wt" ]; then
    rm -rf "$wt" && git -C "$repo_dir" worktree prune || { log "worktree remove failed for $wt"; continue; }
  fi
  git -C "$repo_dir" branch -D "fix/pr-$num" >/dev/null 2>&1
  rm -f "$FIXER/handled/$key" "$STATE_DIR/kept/$key"
  title=$(jq -r .title <<<"$pr")
  [ "$closed" -gt 0 ] && osascript -e "display notification \"${title//\"/\\\"}\" with title \"PR $state: $repo #$num\" subtitle \"Closed $closed workspace(s)\"" >/dev/null 2>&1
  log "cleaned up $key ($state; closed $closed workspace(s))"
done
exit
}
