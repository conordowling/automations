#!/bin/bash
# Opens the morning brief (from 08:00) and end-of-day parking report (from
# 16:30) as cmux workspaces on weekdays. Runs often; a per-day stamp makes each
# run at most once, and a cutoff skips a brief that would be too late to matter.
# Usage: daily-rhythm-watcher.sh [--force morning|eod]
set -u
export CMUX_QUIET=1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

CM=/Applications/cmux.app/Contents/Resources/bin/cmux
STATE_DIR="$HOME/.local/state/daily-rhythm"
mkdir -p "$STATE_DIR"
log() { echo "$(date '+%F %T') $*"; }

# kind  start  cutoff  (HHMM)
SCHEDULE="morning 0800 1200
eod 1630 2000"

today=$(date +%F); dow=$(date +%u); now=$(date +%H%M)
force=""; [ "${1:-}" = "--force" ] && force=${2:-}
[ -z "$force" ] && [ "$dow" -gt 5 ] && exit 0

export CMUX_SOCKET_PATH
CMUX_SOCKET_PATH=$(cat "$HOME/Library/Application Support/cmux/last-socket-path" 2>/dev/null)

printf '%s\n' "$SCHEDULE" | while read -r kind start cutoff; do
  [ -n "$force" ] && [ "$force" != "$kind" ] && continue
  stamp="$STATE_DIR/$kind-$today"
  if [ -z "$force" ]; then
    [ -e "$stamp" ] && continue
    # 10#: HHMM like 0800 is not octal.
    [ $((10#$now)) -lt $((10#$start)) ] && continue
    if [ $((10#$now)) -ge $((10#$cutoff)) ]; then
      touch "$stamp"; log "$kind: past cutoff $cutoff, skipped for $today"; continue
    fi
  fi

  # Am I at the machine? launchd also fires during DarkWake (lid shut, no user),
  # where the network and cmux look fine but the Mac sleeps again mid-run and
  # the typed session command is lost. Keyboard/mouse idle time tells them apart.
  if [ -z "$force" ]; then
    idle=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')
    [ "${idle:-0}" -gt 300 ] && { log "$kind: away (idle ${idle}s), will retry"; continue; }
  fi

  # Online and cmux reachable? If not, a later run retries until the cutoff.
  gh api user -q .login >/dev/null 2>&1 || { log "$kind: offline, will retry"; continue; }
  "$CM" ping >/dev/null 2>&1 || { log "$kind: cmux not reachable, will retry"; continue; }

  case $kind in morning) name="☀️ Morning brief $today"; label="Morning brief";; eod) name="🌙 End of day $today"; label="End-of-day parking";; esac
  layout=$(jq -nc --arg cmd "$HOME/.local/bin/daily-brief-session.sh $kind" '{pane:{surfaces:[{type:"terminal",command:$cmd}]}}')
  if out=$("$CM" new-workspace --name "$name" --cwd "$HOME/Klaviyo/Repos" --layout "$layout" --focus false 2>&1); then
    ws=$(echo "$out" | awk '/^OK/ {print $2}')
    [ -z "$force" ] && touch "$stamp"
    [ -n "$ws" ] && "$CM" set-status daily_brief "$label" --icon sun.max --color "#007aff" --priority 85 --workspace "$ws" >/dev/null 2>&1
    "$CM" notify --title "$label ready" --body "Claude is putting it together in cmux" ${ws:+--workspace "$ws"} >/dev/null 2>&1
    osascript -e "display notification \"Claude is putting it together in cmux\" with title \"$label\"" >/dev/null 2>&1
    log "$kind: opened ($out)"
  else
    log "$kind: failed to open: $out"
  fi
done
exit 0
