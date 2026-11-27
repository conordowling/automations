#!/bin/bash
# Opens a cmux workspace running a Claude self-review for each newly opened
# PR you authored (draft or ready), then notifies via cmux and macOS.
set -u
export CMUX_QUIET=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

CM=/Applications/cmux.app/Contents/Resources/bin/cmux
STATE_DIR="$HOME/.local/state/pr-authored-watcher"
SEEN="$STATE_DIR/seen.txt"
REPOS_DIR="$HOME/Klaviyo/Repos"
mkdir -p "$STATE_DIR"

log() { echo "$(date '+%F %T') $*"; }

# Short name for a repo in workspace names, from share/pr-watchers/repo-short-names.
short_repo() {
  awk -v r="$1" '!/^#/ && $1==r {print $2; f=1; exit} END {if (!f) print r}' \
    "$HOME/.local/share/pr-watchers/repo-short-names" 2>/dev/null || echo "$1"
}

# PR title without a leading ticket id ("[PERS-123]: ", "PERS-123 "), for workspace names.
short_title() {
  local t
  t=$(printf '%s' "$1" | sed -E 's/^\[?[A-Z][A-Z0-9]*-[A-Z0-9]+\]?:?[[:space:]]*//')
  echo "${t:-$1}"
}

LOGIN=$(gh api user -q .login) || { log "gh auth failed"; exit 1; }
CURRENT=$(gh search prs --state=open --limit 200 --json number,repository,title,isDraft \
  -q '.[] | "\(.repository.nameWithOwner)#\(.number)\t\(.title)\t\(if .isDraft then "draft" else "ready" end)"' \
  -- "author:$LOGIN" "archived:false") || { log "search failed"; exit 1; }

# First run: baseline everything currently open, open nothing.
if [ ! -f "$SEEN" ]; then
  printf '%s\n' "$CURRENT" | cut -f1 | sed '/^$/d' > "$SEEN"
  log "baselined $(wc -l < "$SEEN") PRs"
  exit 0
fi

export CMUX_SOCKET_PATH
CMUX_SOCKET_PATH=$(cat "$HOME/Library/Application Support/cmux/last-socket-path" 2>/dev/null)

printf '%s\n' "$CURRENT" | sed '/^$/d' | while IFS=$'\t' read -r ref title state; do
  grep -qxF "$ref" "$SEEN" && continue
  repo_full=${ref%#*}; num=${ref##*#}; repo=${repo_full#*/}
  url="https://github.com/$repo_full/pull/$num"

  if ! "$CM" ping >/dev/null 2>&1; then
    log "cmux not reachable; will retry $ref next run"
    continue
  fi

  cwd="$REPOS_DIR/$repo"; [ -d "$cwd" ] || cwd="$REPOS_DIR"
  prompt="Self-review my GitHub PR $url ($state). Use gh pr view and gh pr diff, and read surrounding code in this repo as needed. Review it as a skeptical reviewer would and give me a list of anything that should be fixed before reviewers see it, in priority order (most severe first), with file:line references and a short reason for each. This is read-only: DO NOT post comments, reviews, or approvals on GitHub, and do not push or modify anything."

  # Left: Claude self-review. Right: the PR in a cmux browser pane.
  cmd="cd $(printf '%q' "$cwd") && claude $(printf '%q' "$prompt")"
  layout=$(jq -nc --arg cmd "$cmd" --arg url "$url" '{direction:"horizontal",split:0.5,children:[
    {pane:{surfaces:[{type:"terminal",command:$cmd}]}},
    {pane:{surfaces:[{type:"browser",url:$url}]}}]}')

  if out=$("$CM" new-workspace --name "📝 [$(short_repo "$repo")] $(short_title "$title")" --cwd "$cwd" \
        --layout "$layout" --focus false 2>&1); then
    ws=$(echo "$out" | awk '/^OK/ {print $2}')
    [ -n "$ws" ] && "$CM" set-status pr_authored "self-review ($state)" --icon pencil --color "#34c759" --priority 90 --workspace "$ws" >/dev/null 2>&1
    "$CM" notify --title "PR opened: $repo #$num ($state)" --body "$title" ${ws:+--workspace "$ws"} >/dev/null 2>&1
    osascript -e "display notification \"${title//\"/\\\"}\" with title \"PR opened: $repo #$num ($state)\" subtitle \"Claude self-review opened in cmux\"" >/dev/null 2>&1
    echo "$ref" >> "$SEEN"
    log "opened $ref ($out)"
  else
    log "failed to open $ref: $out"
  fi
done
