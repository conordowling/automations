#!/bin/bash
# Installs these automations for the current user: symlinks bin/ and share/ into
# ~/.local, renders launchd/*.plist.in into ~/Library/LaunchAgents and (re)loads
# only the agents whose plist changed. Safe to rerun. The automations-sync agent
# reruns it with --sync whenever the repo changes, so edits go live by themselves.
# Usage: install.sh [--sync]
set -u
REPO=$(cd "$(dirname "$0")" && pwd -P)
SYNC=""; [ "${1:-}" = "--sync" ] && SYNC=1
BIN="$HOME/.local/bin"; SHARE="$HOME/.local/share"; AGENTS="$HOME/Library/LaunchAgents"
STATE="$HOME/.local/state/automations"; BACKUP="$STATE/replaced-$(date +%Y%m%d-%H%M%S)"
SELF=com.conordowling.automations-sync
DOMAIN="gui/$(id -u)"
log() { echo "$(date '+%F %T') $*"; }
mkdir -p "$BIN" "$SHARE" "$AGENTS" "$STATE"

if [ -z "$SYNC" ]; then
  for c in git gh jq claude; do command -v "$c" >/dev/null || log "warning: '$c' is not on PATH; the watchers need it"; done
  [ -d /Applications/cmux.app ] || log "warning: cmux is not installed in /Applications; the watchers open their sessions in it"
fi

# link SRC DEST: point DEST at SRC, moving a real file that is in the way aside first.
link() {
  [ "$(readlink "$2" 2>/dev/null)" = "$1" ] && return
  if [ -e "$2" ] && [ ! -L "$2" ]; then mkdir -p "$BACKUP"; mv "$2" "$BACKUP/"; log "moved old $2 to $BACKUP"; fi
  mkdir -p "$(dirname "$2")"
  ln -sfn "$1" "$2" && log "linked $2"
}

for f in "$REPO"/bin/*; do [ -f "$f" ] && link "$f" "$BIN/$(basename "$f")"; done
(cd "$REPO/share" && find . -type f ! -name '.*') | while read -r f; do link "$REPO/share/${f#./}" "$SHARE/${f#./}"; done
# Drop links to files that were deleted or renamed in the repo.
find "$BIN" "$SHARE" -maxdepth 3 -type l 2>/dev/null | while read -r l; do
  case $(readlink "$l") in "$REPO"/*) [ -e "$l" ] || { rm -f "$l"; log "removed stale link $l"; };; esac
done

# Directories (for added/removed files) plus the plist templates (often edited in
# place, which a directory watch doesn't see). Scripts are symlinked, so they
# need no watch: an edit is live on the next run.
watch_paths() {
  { echo "$REPO/bin"; find "$REPO/share" -type d; echo "$REPO/launchd"; ls "$REPO"/launchd/*.plist.in; } |
    sed 's#.*#    <string>&</string>#'
}
render() {
  watch_paths > "$STATE/watch-paths"
  sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$REPO#g" "$1" |
    awk -v f="$STATE/watch-paths" '$0 == "__WATCH_PATHS__" { while ((getline l < f) > 0) print l; next } { print }'
}
is_loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }
unload() {
  launchctl bootout "$DOMAIN/$1" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do is_loaded "$1" || return 0; sleep 0.5; done
}

labels=""
for t in "$REPO"/launchd/*.plist.in; do
  label=$(basename "$t" .plist.in); labels="$labels $label "
  dest="$AGENTS/$label.plist"; new="$STATE/$label.plist.new"
  render "$t" > "$new"
  plutil -lint -s "$new" || { log "$label: invalid plist, left the installed one alone"; rm -f "$new"; continue; }
  for k in StandardOutPath StandardErrorPath; do
    p=$(plutil -extract "$k" raw "$new" 2>/dev/null) && mkdir -p "$(dirname "$p")"
  done
  loaded=""; is_loaded "$label" && loaded=1
  if cmp -s "$new" "$dest" && [ -n "$loaded" ]; then rm -f "$new"; continue; fi
  mv "$new" "$dest"
  if [ "$label" = "$SELF" ] && [ -n "$SYNC" ] && [ -n "$loaded" ]; then
    # Unloading the sync agent would kill this very run, so a detached shell
    # does it (AbandonProcessGroup in the plist lets it outlive us).
    nohup /bin/sh -c "sleep 2; launchctl bootout $DOMAIN/$label; sleep 1; launchctl bootstrap $DOMAIN '$dest'" >/dev/null 2>&1 &
    log "$label: plist changed, reload scheduled"; continue
  fi
  [ -n "$loaded" ] && unload "$label"
  if launchctl bootstrap "$DOMAIN" "$dest"; then log "$label: loaded"; else log "$label: failed to load"; fi
done

# Unload agents whose template was removed from the repo.
for label in $(cat "$STATE/agents" 2>/dev/null); do
  case $labels in *" $label "*) continue;; esac
  unload "$label"; rm -f "$AGENTS/$label.plist"; log "$label: removed"
done
echo $labels > "$STATE/agents"
[ -z "$SYNC" ] && log "done: $(echo $labels | wc -w | tr -d ' ') agents from $REPO"
exit 0
