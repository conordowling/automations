# Inbox design

A local control center for my work, open in a pinned cmux workspace and updating live.
It tracks each piece of work as a workstream through its states, and my agents post
updates to it.

## Workstreams

- One workstream per Linear ticket. Its PRs, in any repo, are nested under it with their
  own states.
- A PR is linked by the ticket's Linear attachments, falling back to a `[PERS-123]` title
  prefix until Linear has synced. A `[PERS-TBD]` or unprefixed PR gets its own workstream
  (`pr:k-repo#98871`), flagged **needs ticket**, until it's linked.
- Done = ticket Done and all its PRs merged. Done workstreams drop off after 3 days.
- Order: needs me (unread events, commits ready to push, failing CI), then waiting on
  others, then idle.

## Architecture

- `bin/inbox-server.py`: Homebrew Python 3, stdlib only (`http.server`, `sqlite3`). Bound to
  `127.0.0.1:8765`. Run by a new launchd agent (`KeepAlive`), installed and synced like the
  others.
- Store: `~/.local/state/inbox/inbox.db`, SQLite in WAL mode.
  - `workstreams`: key, title, Linear status, snoozed-until, archived, last activity.
  - `prs`: repo, number, workstream, title, state, CI summary, review decision,
    unresolved threads, saved workspace ids, fixer state (idle, running, commits ready to
    push).
  - `events`: workstream, PR (optional), source, text, created time, read flag.
- Collectors, in the server process:
  - `gh`, every 2 min: my open and recently merged PRs, and direct review requests, in one
    GraphQL query.
  - Watcher state, every 30 s: fixer handled items, worktrees ahead of their PR, saved
    workspace ids. Changes become events.
  - Linear, every 30 min and on demand: `bin/inbox-linear-sync.sh` runs `claude -p` with the
    Runlayer Linear tools and writes `linear.json`, which the server checks against the
    expected shape and imports. Skipped while I'm away from the machine. No personal API
    key: integrations must use an org-owned identity, and Runlayer covers this.

## Agent updates

- `bin/inbox post <ticket|repo#num> "<text>" [--source <name>] [--notify]` writes straight to
  SQLite, so it works with the server down. `--notify` also sends a cmux and macOS
  notification.
- The watchers post when sessions start and finish, when the fixer is dispatched, when
  cleanup closes or keeps something, and when a review request arrives.
- The self-review, fixer and review prompts tell the Claude session to post its one-line
  result when it finishes.
- The server posts changes it sees itself: CI passing to failing, a new review, a merge, a
  Linear status change.

## Page

- One HTML file served by the server, no build step and no external scripts. Server-sent
  events re-render only the cards that changed and keep scroll position and expanded cards.
- Top strip: counts for needs me, waiting, reviews requested and needs ticket; GitHub and
  Linear freshness; a Refresh button.
- Workstream cards: title and Linear status, nested PR rows (state, CI, threads, fixer
  badge), the latest 3 events with a "show all" option; unread events highlighted.
- Review requests: their own section, oldest first, with age and size.

## Actions

Each button is a `POST` to localhost. Pushing, posting to GitHub and changing Linear stay
manual.

| Button | Does |
|---|---|
| Focus | Opens or focuses the PR's cmux workspace by its saved id |
| Open PR / ticket | Opens it in the cmux browser pane |
| Start self-review / fixer | Runs the existing watcher logic for that one PR now |
| Mark read, Snooze (1 day, until it changes), Archive | Local state only |

`bin/inbox open` creates or focuses a pinned `📥 Inbox` workspace with a browser pane on the
server. The installer runs it once.

## Failure handling

- Server crash: launchd restarts it, and the page shows "reconnecting…" and re-subscribes.
- `gh` failures: keep the last good data and show "GitHub stale since <time>". Back off
  until the rate-limit reset. Post one event after 3 failed polls in a row.
- Linear failures: same, with the last sync time and the error. A malformed `linear.json`
  never overwrites good data.
- Shell scripts use the `{ ...; exit; }` wrapping so an edit mid-run can't break a run.

## Security

Local only, but any website open in my browser can send requests to localhost, and some
buttons start Claude sessions. So:

- Bound to `127.0.0.1`.
- Action endpoints need a random token from `~/.local/state/inbox/token`, which the server
  puts in the page it serves, and reject a `Host` or `Origin` other than `localhost:8765`.
- Ticket, comment and PR text is HTML-escaped. Text from Linear and GitHub that reaches the
  fixer stays labelled as data, not instructions.

## Verification

No unit tests for now. Each stage gets a live check against real data: start the server,
open it in cmux, post an event, and confirm it appears without a reload.

## Rollout

Each stage is usable on its own and gets its own commit, push and `install.sh` run.

1. Server, store, `gh` collector and the read-only page in cmux.
2. Watcher-state collector, `inbox post`, and posts from the watchers and their prompts.
3. Action buttons.
4. Linear sync.

Out of scope for v1: Slack, Calendar, pushing and posting from the inbox, and workstreams I
define myself.
