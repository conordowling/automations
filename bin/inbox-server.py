#!/opt/homebrew/bin/python3
"""Local inbox server: keeps a SQLite store of my workstreams (a Linear ticket and its PRs,
plus events from GitHub and my agents) current, and serves a page that polls /api/state
with an ETag (cmux's WebKit browser buffers server-sent events). Bound to 127.0.0.1. See docs/plans/2026-11-27-inbox-design.md.

Env: INBOX_PORT (8765), INBOX_DB (~/.local/state/inbox/inbox.db).
"""
import hashlib
import hmac
import secrets
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("INBOX_PORT", "8765"))
DB = Path(os.environ.get("INBOX_DB", Path.home() / ".local/state/inbox/inbox.db"))
ASSETS = Path(__file__).resolve().parents[1] / "share/inbox"
GH_INTERVAL = 120
WATCH_INTERVAL = 30
WT_ROOT = Path.home() / ".local/share/pr-fixer-watcher/worktrees"
WATCHER_WS = {kind: Path.home() / f".local/state/pr-{kind}-watcher/workspaces"
              for kind in ("authored", "review", "fixer")}
DONE_HIDE_AFTER = timedelta(days=3)
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}
BIN = Path(__file__).resolve().parent
CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
STARTERS = {"self-review": "pr-authored-watcher", "fixer": "pr-fixer-watcher", "review": "pr-review-watcher"}
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")

SCHEMA = """
create table if not exists workstreams (
  key text primary key, title text, linear_status text, linear_url text,
  snoozed_until text, archived integer default 0, updated_at text);
create table if not exists prs (
  ref text primary key, repo_full text, num integer, workstream text, title text, url text,
  state text, is_draft integer, ci text, review_decision text, unresolved integer,
  additions integer, deletions integer, head_oid text, updated_at text, merged_at text);
create table if not exists reviews (
  ref text primary key, repo_full text, num integer, title text, url text, author text,
  created_at text, additions integer, deletions integer);
create table if not exists events (
  id integer primary key, workstream text, pr_ref text, source text, text text,
  created_at text, read integer default 0);
create index if not exists events_ws on events(workstream, created_at);
create table if not exists meta (key text primary key, value text);
"""
# Columns added after the first release; migrate() adds any an existing db lacks.
PR_EXTRA_COLUMNS = {"bot_unresolved": "integer", "fixer_state": "text", "fixer_ahead": "integer",
                    "workspaces": "text", "threads_checked_at": "text", "threads_seen_updated_at": "text"}


def migrate(db):
    db.executescript(SCHEMA)
    have = {r["name"] for r in db.execute("pragma table_info(prs)")}
    for col, typ in PR_EXTRA_COLUMNS.items():
        if col not in have:
            db.execute(f"alter table prs add column {col} {typ}")


def log(msg):
    print(f"{datetime.now():%F %T} {msg}", flush=True)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    db = sqlite3.connect(DB, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    # fetchall: a pragma statement left unfinished holds a read snapshot open, and then
    # this connection never sees other connections' commits.
    db.execute("pragma journal_mode=wal").fetchall()
    db.execute("pragma busy_timeout=5000").fetchall()
    return db


def set_meta(db, **kv):
    for k, v in kv.items():
        db.execute("insert or replace into meta values (?, ?)", (k, v))


def add_event(db, workstream, pr_ref, source, text):
    db.execute(
        "insert into events (workstream, pr_ref, source, text, created_at) values (?,?,?,?,?)",
        (workstream, pr_ref, source, text, now()),
    )


# --- GitHub -----------------------------------------------------------------------------

TICKET = re.compile(r"^\[?([A-Z][A-Z0-9]*-\d+)\]?")
TICKET_PREFIX = re.compile(r"^\[?[A-Z][A-Z0-9]*-[A-Z0-9]+\]?:?\s*")

# GraphQL charges by how many nodes a query could return, so the searches skip review
# threads (with them, one poll cost ~300 of the 5,000 points an hour). Threads are fetched
# separately, only for PRs that changed (THREADS_QUERY), at about 1 point per PR.
PR_FIELDS = """... on PullRequest { number title url isDraft state createdAt updatedAt mergedAt
  additions deletions reviewDecision headRefOid author { login } repository { nameWithOwner }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }"""
GH_QUERY = """query($open: String!, $merged: String!, $reviews: String!) {
  open: search(query: $open, type: ISSUE, first: 50) { nodes { %s } }
  merged: search(query: $merged, type: ISSUE, first: 30) { nodes { %s } }
  reviews: search(query: $reviews, type: ISSUE, first: 30) { nodes { %s } }
  rateLimit { cost remaining resetAt } }""" % (PR_FIELDS, PR_FIELDS, PR_FIELDS)
THREADS_FIELDS = """reviewThreads(first: 100) { nodes { isResolved isOutdated
  comments(last: 1) { nodes { author { login __typename } } } } }"""
THREADS_BATCH = 20
THREADS_MAX_AGE = timedelta(minutes=15)  # resolving a thread may not change a PR's updatedAt


def gh(*args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or f"gh exited {out.returncode}")
    return out.stdout


def short_title(title):
    return TICKET_PREFIX.sub("", title) or title


def workstream_key(pr):
    m = TICKET.match(pr["title"])
    if m and not m.group(1).endswith("-TBD"):
        return m.group(1)
    return f"pr:{pr['repository']['nameWithOwner'].split('/')[1]}#{pr['number']}"


def open_threads(threads, login):
    """Unresolved, current threads whose last reply isn't mine, as (human, bot) counts."""
    human = bot = 0
    for t in threads:
        last = (t["comments"]["nodes"] or [{}])[0].get("author") or {}
        if t["isResolved"] or t["isOutdated"] or last.get("login") == login:
            continue
        if last.get("__typename") == "Bot":
            bot += 1
        else:
            human += 1
    return human, bot


def pr_row(pr):
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    repo_full = pr["repository"]["nameWithOwner"]
    return {
        "ref": f"{repo_full.split('/')[1]}#{pr['number']}",
        "repo_full": repo_full,
        "num": pr["number"],
        "workstream": workstream_key(pr),
        "title": pr["title"],
        "url": pr["url"],
        "state": pr["state"].lower(),
        "is_draft": int(pr["isDraft"]),
        "ci": (rollup or {}).get("state") or "NONE",
        "review_decision": pr["reviewDecision"] or "",
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "head_oid": pr["headRefOid"],
        "updated_at": pr["updatedAt"],
        "merged_at": pr["mergedAt"],
    }


CI_WORDS = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "erroring", "PENDING": "running"}


def pr_events(old, new):
    """What changed on a PR since the last poll, as event texts."""
    if old is None:
        return ["PR opened"] if new["state"] == "open" else []
    out = []
    if new["state"] == "merged" and old["state"] != "merged":
        out.append("merged")
    elif new["state"] == "closed" and old["state"] != "closed":
        out.append("closed without merging")
    if new["ci"] != old["ci"] and new["ci"] in ("SUCCESS", "FAILURE", "ERROR"):
        out.append(f"CI {CI_WORDS[new['ci']]}")
    if new["review_decision"] != old["review_decision"] and new["review_decision"]:
        out.append(new["review_decision"].replace("_", " ").lower())
    if old["is_draft"] and not new["is_draft"] and new["state"] == "open":
        out.append("marked ready for review")
    return out


def poll_github(db, login):
    since = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = json.loads(gh(
        "api", "graphql", "-f", f"query={GH_QUERY}",
        "-f", f"open=is:pr is:open archived:false author:{login}",
        "-f", f"merged=is:pr is:merged author:{login} merged:>={since}",
        "-f", f"reviews=is:pr is:open archived:false user-review-requested:{login}",
    ))
    if "errors" in data:
        raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
    d = data["data"]
    mine = [pr_row(p) for p in d["open"]["nodes"] + d["merged"]["nodes"] if p]
    seen = {r["ref"] for r in mine}
    first_run = db.execute("select count(*) from prs").fetchone()[0] == 0

    db.execute("begin")
    for row in mine:
        old = db.execute("select * from prs where ref = ?", (row["ref"],)).fetchone()
        if not first_run:
            for text in pr_events(old, row):
                add_event(db, row["workstream"], row["ref"], "github", text)
        cols = ",".join(row)
        updates = ",".join(f"{c}=excluded.{c}" for c in row if c != "ref")
        db.execute(f"insert into prs ({cols}) values ({','.join('?' * len(row))}) "
                   f"on conflict(ref) do update set {updates}", list(row.values()))
        ws = db.execute("select updated_at from workstreams where key = ?", (row["workstream"],)).fetchone()
        if ws is None:
            db.execute("insert into workstreams (key, title, updated_at) values (?,?,?)",
                       (row["workstream"], short_title(row["title"]), row["updated_at"]))
        elif row["updated_at"] > (ws["updated_at"] or ""):
            db.execute("update workstreams set updated_at = ? where key = ?",
                       (row["updated_at"], row["workstream"]))
    # A PR that left both searches (closed long ago, or merged >14 days ago) is dropped.
    for (ref,) in db.execute("select ref from prs").fetchall():
        if ref not in seen:
            db.execute("delete from prs where ref = ?", (ref,))
    db.execute("delete from workstreams where key not in (select workstream from prs) "
               "and linear_status is null")

    db.execute("delete from reviews")
    for p in d["reviews"]["nodes"]:
        if not p:
            continue
        repo_full = p["repository"]["nameWithOwner"]
        ref = f"{repo_full.split('/')[1]}#{p['number']}"
        db.execute("insert into reviews values (?,?,?,?,?,?,?,?,?)", (
            ref, repo_full, p["number"], p["title"], p["url"], (p["author"] or {}).get("login", ""),
            p["createdAt"], p["additions"], p["deletions"]))
    db.execute("commit")
    poll_threads(db, login, first_run)
    set_meta(db, gh_synced_at=now(), gh_error="", gh_error_since="", gh_failures="0")
    return d["rateLimit"]


def poll_threads(db, login, first_run):
    """Refresh thread counts for open PRs that changed since their last check, or whose
    counts are older than THREADS_MAX_AGE."""
    stale = (datetime.now(timezone.utc) - THREADS_MAX_AGE).isoformat()
    due = db.execute(
        "select ref, repo_full, num, workstream, unresolved from prs where state = 'open' and "
        "(threads_checked_at is null or threads_checked_at < ? or threads_seen_updated_at is not updated_at)",
        (stale,)).fetchall()
    for i in range(0, len(due), THREADS_BATCH):
        batch = due[i:i + THREADS_BATCH]
        parts = [f'p{n}: repository(owner: {json.dumps(pr["repo_full"].split("/")[0])}, '
                 f'name: {json.dumps(pr["repo_full"].split("/")[1])}) '
                 f'{{ pullRequest(number: {pr["num"]}) {{ updatedAt {THREADS_FIELDS} }} }}'
                 for n, pr in enumerate(batch)]
        data = json.loads(gh("api", "graphql", "-f", "query={ %s }" % " ".join(parts)))
        if "errors" in data and not data.get("data"):
            raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
        db.execute("begin")
        for n, pr in enumerate(batch):
            node = ((data.get("data") or {}).get(f"p{n}") or {}).get("pullRequest")
            if not node:
                continue
            human, bot = open_threads(node["reviewThreads"]["nodes"], login)
            if not first_run and pr["unresolved"] is not None and human > pr["unresolved"]:
                k = human - pr["unresolved"]
                add_event(db, pr["workstream"], pr["ref"], "github",
                          f"{k} new unresolved thread{'s' if k > 1 else ''}")
            db.execute("update prs set unresolved = ?, bot_unresolved = ?, threads_checked_at = ?, "
                       "threads_seen_updated_at = ? where ref = ?",
                       (human, bot, now(), node["updatedAt"], pr["ref"]))
        db.execute("commit")


def github_loop():
    db = connect()
    login = None
    while True:
        wait = GH_INTERVAL
        try:
            login = login or gh("api", "user", "-q", ".login").strip()
            rate = poll_github(db, login)
            if rate["remaining"] < 50:
                reset = datetime.fromisoformat(rate["resetAt"].replace("Z", "+00:00"))
                wait = max(wait, (reset - datetime.now(timezone.utc)).total_seconds() + 5)
                log(f"rate limit low ({rate['remaining']}); waiting {wait:.0f}s")
        except Exception as e:  # keep the last good data; say what went wrong
            if db.in_transaction:
                db.execute("rollback")
            failures = int((db.execute("select value from meta where key='gh_failures'").fetchone() or ["0"])[0] or 0) + 1
            since = (db.execute("select value from meta where key='gh_error_since'").fetchone() or [""])[0] or now()
            set_meta(db, gh_error=str(e)[:300], gh_error_since=since, gh_failures=str(failures))
            if failures == 3:
                add_event(db, "inbox", None, "inbox", f"GitHub polling failing since {since}: {str(e)[:200]}")
            log(f"github poll failed ({failures}): {e}")
            # "...for <ip>. Authenticated requests get a higher rate limit": gh fell back to
            # anonymous because launchd couldn't read the keychain this time. That clears on
            # its own, so retry as usual; only a real rate limit gets the long backoff.
            if "rate limit" in str(e).lower() and "Authenticated requests" not in str(e):
                wait = 900
        time.sleep(wait)


# --- Watcher state -----------------------------------------------------------------------

def git(wt, *args):
    out = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, timeout=90)
    return out.returncode, out.stdout.strip()


def fixer_worktree(wt, head_oid, fetched, cache):
    """(state, commits ahead of the PR) for a fixer worktree. The patch comparison is
    cached by (HEAD, PR head): git is slow in k-repo, and launchd throttles our I/O."""
    # Tracked files only: a full status scans untracked files and takes ~45 s in k-repo.
    if git(wt, "status", "--porcelain", "--untracked-files=no")[1]:
        return "dirty", 0
    head = git(wt, "rev-parse", "HEAD")[1]
    if (wt, head, head_oid) not in cache:
        result = fixer_ahead(wt, head_oid, fetched)
        if result is None:  # couldn't compare (PR head not fetched): try again next poll
            return "idle", 0
        cache[(wt, head, head_oid)] = result
    return cache[(wt, head, head_oid)]


def fixer_ahead(wt, head_oid, fetched):
    if git(wt, "cat-file", "-e", f"{head_oid}^{{commit}}")[0]:
        if head_oid in fetched:  # fetch a missing PR head once, not every 30 s
            return None
        fetched.add(head_oid)
        git(wt, "fetch", "-q", "origin", head_oid)
    # Count by patch (git cherry), not hash: after a rebase of the PR, commits already on it
    # have new hashes. If the PR head isn't an ancestor of HEAD, pushing would undo the
    # rebase, so that's "diverged", not "ready to push".
    rc, out = git(wt, "cherry", head_oid, "HEAD")
    if rc:
        return None
    ahead = sum(l.startswith("+") for l in out.splitlines())
    if not ahead:
        return "idle", 0
    on_top = git(wt, "merge-base", "--is-ancestor", head_oid, "HEAD")[0] == 0
    return ("ready_to_push" if on_top else "diverged"), ahead


FIXER_WORDS = {"ready_to_push": "{n} fixer commit{s} ready to push",
               "diverged": "{n} fixer commit{s} not on the PR, which was rebased since: rebase the worktree before pushing",
               "dirty": "fixer worktree has uncommitted changes"}


def poll_watchers(db, fetched, cache):
    # Git (and the odd fetch) runs before the write transaction, so it never holds the
    # db lock while other writers, like `inbox post`, wait.
    updates = []
    for pr in db.execute("select ref, repo_full, num, workstream, head_oid, state, fixer_state from prs").fetchall():
        key = pr["repo_full"].replace("/", "__") + f"__{pr['num']}"
        ws = {}
        for kind, d in WATCHER_WS.items():
            try:
                ws[kind] = (d / key).read_text().strip()
            except OSError:
                pass
        wt = WT_ROOT / f"{pr['repo_full'].split('/')[1]}-{pr['num']}"
        try:
            state, ahead = (fixer_worktree(wt, pr["head_oid"], fetched, cache)
                            if wt.is_dir() and pr["state"] == "open" else (None, 0))
        except subprocess.TimeoutExpired:
            log(f"git timed out in {wt}; keeping its last state")
            state, ahead = pr["fixer_state"], 0
        updates.append((pr, state, ahead, json.dumps(ws) if ws else None))
    db.execute("begin")
    for pr, state, ahead, ws in updates:
        if state != pr["fixer_state"] and state in FIXER_WORDS:
            add_event(db, pr["workstream"], pr["ref"], "fixer",
                      FIXER_WORDS[state].format(n=ahead, s="s" if ahead > 1 else ""))
        db.execute("update prs set fixer_state = ?, fixer_ahead = ?, workspaces = ? where ref = ?",
                   (state, ahead, ws, pr["ref"]))
    db.execute("commit")


def watcher_loop():
    db = connect()
    fetched, cache = set(), {}
    while True:
        try:
            poll_watchers(db, fetched, cache)
        except Exception as e:
            if db.in_transaction:
                db.execute("rollback")
            log(f"watcher poll failed: {e}")
        time.sleep(WATCH_INTERVAL)


# --- State for the page -----------------------------------------------------------------

def bucket(prs):
    open_prs = [p for p in prs if p["state"] == "open"]
    if not open_prs:
        return "done"
    if any(p["ci"] in ("FAILURE", "ERROR") or p["review_decision"] == "CHANGES_REQUESTED"
           or p["unresolved"] or p["fixer_state"] in ("ready_to_push", "diverged", "dirty") for p in open_prs):
        return "needs_you"
    if all(p["is_draft"] for p in open_prs):
        return "draft"
    return "waiting"


def short_names():
    """Repo short names shared with the PR watchers (share/pr-watchers/repo-short-names)."""
    try:
        lines = (ASSETS.parent / "pr-watchers/repo-short-names").read_text().splitlines()
    except OSError:
        return {}
    return dict(l.split()[:2] for l in lines if l.strip() and not l.startswith("#") and len(l.split()) > 1)


def is_snoozed(db, w):
    """'t:<iso>' hides until that time; 'c:<iso>' hides until a new event after it."""
    until = w["snoozed_until"] or ""
    if until.startswith("t:"):
        return until[2:] > now()
    if until.startswith("c:"):
        return db.execute("select 1 from events where workstream = ? and created_at > ?",
                          (w["key"], until[2:])).fetchone() is None
    return False


def state(db):
    prs = {}
    for p in db.execute("select * from prs order by num"):
        prs.setdefault(p["workstream"], []).append(dict(p))
    cutoff = (datetime.now(timezone.utc) - DONE_HIDE_AFTER).isoformat()
    workstreams = []
    for w in db.execute("select * from workstreams order by updated_at desc"):
        w = dict(w)
        w["snoozed"] = is_snoozed(db, w)
        w["prs"] = prs.get(w["key"], [])
        w["bucket"] = bucket(w["prs"])
        if w["bucket"] == "done" and (w["updated_at"] or "") < cutoff:
            continue
        w["events"] = [dict(e) for e in db.execute(
            "select * from events where workstream = ? order by id desc limit 20", (w["key"],))]
        w["unread"] = db.execute("select count(*) from events where workstream = ? and read = 0",
                                 (w["key"],)).fetchone()[0]
        workstreams.append(w)
    reviews = []
    for r in db.execute("select * from reviews order by created_at"):
        r = dict(r)
        r["events"] = [dict(e) for e in db.execute(
            "select * from events where workstream = ? order by id desc limit 3", (f"review:{r['ref']}",))]
        reviews.append(r)
    meta = {k: v for k, v in db.execute("select key, value from meta")}
    inbox_events = [dict(e) for e in db.execute(
        "select * from events where workstream = 'inbox' order by id desc limit 5")]
    return {"workstreams": workstreams, "reviews": reviews, "meta": meta,
            "inbox_events": inbox_events, "short_names": short_names()}


# --- Actions (POST /api/<name>, token-checked) ---------------------------------------------

def token():
    path = DB.parent / "token"
    try:
        return path.read_text().strip()
    except OSError:
        t = secrets.token_urlsafe(32)
        path.write_text(t)
        path.chmod(0o600)
        return t


TOKEN = None


def cmux(*args):
    env = dict(os.environ, CMUX_QUIET="1")
    try:
        env["CMUX_SOCKET_PATH"] = (Path.home() / "Library/Application Support/cmux/last-socket-path").read_text().strip()
    except OSError:
        pass
    return subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=15, env=env)


def pr_target(db, ref):
    """(owner/repo, num, workspaces) for one of my PRs or a review request, by repo#num."""
    row = db.execute("select repo_full, num, workspaces from prs where ref = ?", (ref,)).fetchone()
    if row:
        return row["repo_full"], row["num"], json.loads(row["workspaces"] or "{}")
    row = db.execute("select repo_full, num from reviews where ref = ?", (ref,)).fetchone()
    if row:
        key = row["repo_full"].replace("/", "__") + f"__{row['num']}"
        try:
            ws = {"review": (WATCHER_WS["review"] / key).read_text().strip()}
        except OSError:
            ws = {}
        return row["repo_full"], row["num"], ws
    raise ValueError(f"unknown PR {ref}")


def act_focus(db, body):
    _, _, ws = pr_target(db, body["ref"])
    for kind in ([body["kind"]] if body.get("kind") else ["fixer", "authored", "review"]):
        if ws.get(kind):
            out = cmux("workspace", "select", ws[kind])
            if out.returncode == 0:
                return f"focused the {kind} workspace"
    raise ValueError("no open workspace for this PR")


def act_start(db, body):
    script = STARTERS.get(body.get("kind"))
    if not script:
        raise ValueError("kind must be self-review, fixer or review")
    repo_full, num, _ = pr_target(db, body["ref"])
    log_path = Path.home() / f".local/state/{script}/watcher.log"
    with open(log_path, "a") as out:
        subprocess.Popen([str(BIN / f"{script}.sh"), "--only", f"{repo_full}#{num}"],
                         stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return f"starting {body['kind']}…"


def workstream_arg(db, body):
    key = body.get("workstream", "")
    if not db.execute("select 1 from workstreams where key = ?", (key,)).fetchone():
        raise ValueError(f"unknown workstream {key}")
    return key


def act_read(db, body):
    if body.get("all"):
        db.execute("update events set read = 1 where read = 0")
        return "marked everything read"
    db.execute("update events set read = 1 where workstream = ?", (workstream_arg(db, body),))
    return "marked read"


def act_snooze(db, body):
    key, mode = workstream_arg(db, body), body.get("until")
    value = {"day": "t:" + (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"),
             "change": "c:" + now(), "off": None}
    if mode not in value:
        raise ValueError("until must be day, change or off")
    db.execute("update workstreams set snoozed_until = ? where key = ?", (value[mode], key))
    return "unsnoozed" if mode == "off" else "snoozed"


def act_archive(db, body):
    on = 1 if body.get("on", True) else 0
    db.execute("update workstreams set archived = ? where key = ?", (on, workstream_arg(db, body)))
    return "archived" if on else "restored"


ACTIONS = {"focus": act_focus, "start": act_start, "read": act_read, "snooze": act_snooze,
           "archive": act_archive}


# --- HTTP -------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def allowed(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self.send_error(403)
            return False
        return True

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Another site open in the browser can send requests to localhost, and some actions
        # start Claude sessions: require our origin and the token that only our page has.
        if not self.allowed():
            return
        origin = self.headers.get("Origin")
        if (origin and origin not in ALLOWED_ORIGINS) or not hmac.compare_digest(
                self.headers.get("X-Inbox-Token", ""), TOKEN):
            self.send_error(403)
            return
        action = ACTIONS.get(self.path.removeprefix("/api/"))
        if not action:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        db = connect()
        try:
            body = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
            result = {"ok": True, "message": action(db, body)}
            code = 200
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            result, code = {"ok": False, "message": str(e)}, 400
        finally:
            db.close()
        self.send(code, json.dumps(result).encode(), "application/json")

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == "/":
            page = (ASSETS / "index.html").read_text().replace("__INBOX_TOKEN__", TOKEN)
            self.send(200, page.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            db = connect()
            try:
                body = json.dumps(state(db)).encode()
            finally:
                db.close()
            etag = '"%s"' % hashlib.sha1(body).hexdigest()
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    migrate(connect())
    global TOKEN
    TOKEN = token()
    threading.Thread(target=github_loop, daemon=True).start()
    threading.Thread(target=watcher_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(f"inbox on http://localhost:{PORT} (db {DB}, assets {ASSETS})")
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
