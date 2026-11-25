#!/bin/bash
# Prints repos/worktrees under ~/Klaviyo/Repos with uncommitted changes or
# commits that exist on no remote. Only repos with a github.com/klaviyo remote
# are considered. Read-only.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
REPOS_DIR="$HOME/Klaviyo/Repos"
seen=""
exec 2>/dev/null
for repo in "$REPOS_DIR"/*/; do
  [ -e "$repo/.git" ] || continue
  git -C "$repo" remote -v | grep -qiE 'github\.com[:/]klaviyo/' || continue
  # Only main checkouts: worktree list from a main repo covers its worktrees.
  git -C "$repo" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print substr($0,10)}' | while read -r wt; do
    [ -d "$wt" ] || continue
    case "$seen" in *"|$wt|"*) continue;; esac
    seen="$seen|$wt|"
    branch=$(git -C "$wt" symbolic-ref --short -q HEAD || echo "(detached)")
    dirty=$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if up=$(git -C "$wt" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
      ahead=$(git -C "$wt" rev-list --count "$up..HEAD" 2>/dev/null); basis="ahead of $up"
      list=$(git -C "$wt" log --oneline "$up..HEAD" 2>/dev/null | head -5)
    else
      ahead=$(git -C "$wt" rev-list --count HEAD --not --remotes 2>/dev/null); basis="on no remote (no upstream)"
      list=$(git -C "$wt" log --oneline HEAD --not --remotes 2>/dev/null | head -5)
    fi
    if [ "${dirty:-0}" -gt 0 ] || [ "${ahead:-0}" -gt 0 ]; then
      echo "## $wt  [branch: $branch]"
      echo "   uncommitted files: $dirty"
      echo "   unpushed commits: $ahead ($basis)"
      [ -n "$list" ] && echo "$list" | sed 's/^/     /'
      [ "${dirty:-0}" -gt 0 ] && git -C "$wt" status --porcelain | head -8 | sed 's/^/     /'
      echo
    fi
  done
done
echo "(scan finished $(date '+%F %T'))"
