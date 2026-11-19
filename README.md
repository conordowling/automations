# automations

My macOS launchd automations: the daily morning brief and end-of-day report, and
the PR watchers. Each one opens a Claude session in a cmux workspace.

| Agent | Runs | Script |
|---|---|---|
| `daily-rhythm-watcher` | every 10 min; morning brief from 08:00, end-of-day report from 16:30, weekdays, when I'm at the machine | `bin/daily-rhythm-watcher.sh` → `bin/daily-brief-session.sh` |
| `pr-authored-watcher` | every 5 min | `bin/pr-authored-watcher.sh` |
| `pr-review-watcher` | every 5 min | `bin/pr-review-watcher.sh` |
| `pr-fixer-watcher` | every 5 min | `bin/pr-fixer-watcher.sh` |
| `automations-sync` | whenever this repo changes | `install.sh --sync` |

## Install

```sh
git clone <this repo> ~/repos/automations   # any location works
~/repos/automations/install.sh
```

Needs `git`, `gh` (logged in), `jq`, `claude` and the cmux app. The Claude
sessions use the Runlayer Linear, Slack and Google Calendar connectors on my
claude.ai account. The scripts expect work repos under `~/Klaviyo/Repos`.

## How edits go live

- `bin/*` and `share/**` are symlinked into `~/.local/bin` and `~/.local/share`,
  so an edit is live on the agent's next run.
- `launchd/*.plist.in` are templates (`__HOME__`, `__REPO__`). launchd only reads
  a plist when it's loaded, so the `automations-sync` agent watches the repo and
  reruns `install.sh --sync`. That re-renders the plists and reloads just the
  agents that changed. It also links new files and unloads or unlinks anything
  deleted from the repo. This includes changes from `git pull`.
- Sync activity is logged to `~/.local/state/automations/sync.log`.

Adding an agent: drop `launchd/<label>.plist.in` (and its script in `bin/`) into
the repo. It loads within a few seconds.

Runtime state (stamps, logs, generated briefs) stays outside the repo, in
`~/.local/state`, `~/.local/share/pr-fixer-watcher/worktrees` and `~/morning-briefs`.
