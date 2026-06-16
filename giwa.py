#!/usr/bin/env python3
"""GIWA (Redmine) one-shot query tool — zero dependencies, Python standard library only.

Usage:
    ./giwa overview      # Global issue overview (open/closed, by project, by status, by assignee)

Configuration:
    Set the following in the .env file at the project root:
        GIWA_URL=https://your-redmine-address
        GIWA_KEY=your_api_key
"""

import json
import os
import sys
import urllib.request
import urllib.error
from collections import Counter

# ---------- Terminal colors ----------
_TTY = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if _TTY else str(text)


def bold(t):  return c(t, "1")
def dim(t):   return c(t, "2")
def red(t):   return c(t, "31")
def green(t): return c(t, "32")
def yellow(t):return c(t, "33")
def cyan(t):  return c(t, "36")


# ---------- Configuration loading ----------
def _env_cfg():
    """Read .env from the project directory; environment variables take precedence."""
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(here, ".env")
    cfg = {}
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GIWA_URL", "GIWA_KEY", "GIWA_EXTRA_TASKS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


def load_env():
    cfg = _env_cfg()
    url, key = cfg.get("GIWA_URL"), cfg.get("GIWA_KEY")
    if not url or not key:
        die(
            "Missing configuration. Please set GIWA_URL and GIWA_KEY in the .env file.\n"
            "  You can copy .env.example to .env and fill in your API key."
        )
    return url.rstrip("/"), key


def extra_task_ids():
    """Persistent tasks (internal/client meetings etc.; not necessarily assigned to me, but should appear in the time-entry selection list)."""
    val = _env_cfg().get("GIWA_EXTRA_TASKS", "")
    return [int(x) for x in val.replace(" ", "").split(",") if x.strip().isdigit()]


def gitlab_cfg():
    c = _env_cfg()
    return c.get("GITLAB_URL", "").rstrip("/"), c.get("GITLAB_TOKEN", "")


def gitlab_get(gurl, gtok, path):
    """GitLab read-only GET (/api/v4 prefix, PRIVATE-TOKEN header)."""
    req = urllib.request.Request(gurl + "/api/v4" + path, headers={"PRIVATE-TOKEN": gtok})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def die(msg, code=1):
    print(red("✗ ") + msg, file=sys.stderr)
    sys.exit(code)


# ---------- API ----------
def api_get(url, key, path):
    req = urllib.request.Request(url + path, headers={"X-Redmine-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            die("Authentication failed: invalid API key or insufficient permissions. Please check GIWA_KEY in .env.")
        die(f"Request error (HTTP {e.code}): {path}")
    except urllib.error.URLError as e:
        die(f"Could not connect to GIWA ({url}): {e.reason}")
    except TimeoutError:
        die("Request timed out. Please retry later or check your network.")


def api_post(url, key, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="POST",
        headers={"X-Redmine-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


def api_put(url, key, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="PUT",
        headers={"X-Redmine-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


def api_delete(url, key, path):
    req = urllib.request.Request(
        url + path, method="DELETE",
        headers={"X-Redmine-API-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            return {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}")


def open_in_code(path, hint=""):
    import shutil, subprocess
    if shutil.which("code"):
        subprocess.run(["code", path])
        print(dim(f"   Opened in VS Code. {hint}"))
    else:
        print(dim(f"   'code' command not found; please open manually: {path}"))


def fetch_all_issues(url, key):
    """Fetch all issues with pagination (including closed)."""
    issues = []
    offset = 0
    while True:
        d = api_get(url, key, f"/issues.json?status_id=*&limit=100&offset={offset}")
        issues += d["issues"]
        total = d["total_count"]
        offset += 100
        sys.stderr.write(f"\r  Fetching… {min(offset, total)}/{total}")
        sys.stderr.flush()
        if offset >= total:
            break
    sys.stderr.write("\r" + " " * 40 + "\r")
    sys.stderr.flush()
    return issues


# ---------- Output helpers ----------
def table(rows, indent="  "):
    """rows: [(count, label)] -> aligned print."""
    if not rows:
        print(indent + dim("(none)"))
        return
    w = max(len(str(r[0])) for r in rows)
    for count, label in rows:
        print(f"{indent}{cyan(str(count).rjust(w))}  {label}")


def section(title):
    print("\n" + bold(title))


# ---------- Command: overview ----------
def cmd_overview(url, key, rest=None):
    print(dim(f"GIWA: {url}"))
    issues = fetch_all_issues(url, key)
    total = len(issues)

    by_proj = Counter()
    by_status = Counter()
    by_assignee = Counter()
    open_closed = Counter()
    status_is_closed = {}

    for i in issues:
        by_proj[i["project"]["name"]] += 1
        st = i["status"]["name"]
        by_status[st] += 1
        status_is_closed[st] = i["status"].get("is_closed", False)
        a = (i.get("assigned_to") or {}).get("name", "(unassigned)")
        by_assignee[a] += 1
        if i["status"].get("is_closed"):
            open_closed["closed"] += 1
        else:
            open_closed["open"] += 1

    print("\n" + bold(f"📊 Issue overview — {total} total"))

    section("Open / Closed")
    table([
        (green(open_closed["open"]), green("Open")),
        (dim(open_closed["closed"]), dim("Closed")),
    ])

    section("📁 By project")
    table([(v, k) for k, v in by_proj.most_common()])

    section("🏷️  By status")
    table([
        (v, (dim(k) if status_is_closed.get(k) else yellow(k)))
        for k, v in by_status.most_common()
    ])

    section("👥 By assignee (Top 15)")
    table([(v, k) for k, v in by_assignee.most_common(15)])
    print()


# ---------- Command: mine ----------
# Pending-status ordering (smaller = higher up); resolved ones like Resuelta go last
_STATUS_ORDER = {
    "Crítica": 0, "Urgente": 0,  # (fallback; normally use priority)
    "En curso": 1, "Nueva": 2, "Bloqueada": 3, "Feedback": 4,
    "Resuelta": 90, "Passed": 91, "Definition": 50,
}
_PRIORITY_ORDER = {"Crítica": 0, "Urgente": 1, "Alta": 2, "Normal": 3, "Baja": 4}


def cmd_mine(url, key, rest=None):
    issues = []
    offset = 0
    while True:
        d = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=open&limit=100&offset={offset}")
        issues += d["issues"]
        total = d["total_count"]
        offset += 100
        if offset >= total:
            break

    # Split into "pending" and "resolved, pending close (Resuelta/Passed)"
    done_states = {"Resuelta", "Passed"}
    pending = [i for i in issues if i["status"]["name"] not in done_states]
    resolved = [i for i in issues if i["status"]["name"] in done_states]

    def issue_link(i):
        return f"[#{i['id']}]({url}/issues/{i['id']})"

    def sort_key(i):
        st = i["status"]["name"]
        pr = i["priority"]["name"]
        return (_STATUS_ORDER.get(st, 60), _PRIORITY_ORDER.get(pr, 5), i["id"])

    from collections import defaultdict
    lines = []
    lines.append("# My issues (open)\n")
    lines.append(f"> Source: {url} · **{len(issues)}** total (To do {len(pending)} · Resolved, pending close {len(resolved)})\n")

    def render_group(title, items):
        lines.append(f"\n## {title} ({len(items)})\n")
        by_proj = defaultdict(list)
        for i in items:
            by_proj[i["project"]["name"]].append(i)
        for proj in sorted(by_proj, key=lambda p: -len(by_proj[p])):
            lines.append(f"\n### {proj}\n")
            lines.append("| Issue | Status | Priority | Due | Subject |")
            lines.append("|---|---|---|---|---|")
            for i in sorted(by_proj[proj], key=sort_key):
                due = i.get("due_date") or ""
                subj = i["subject"].replace("|", "\\|")
                lines.append(
                    f"| {issue_link(i)} | {i['status']['name']} | {i['priority']['name']} | {due} | {subj} |"
                )

    if pending:
        render_group("🔧 To do", pending)
    if resolved:
        render_group("✅ Resolved, pending close", resolved)

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "MINE.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(bold(f"📝 Exported {len(issues)} issues → ") + cyan(out_path))
    print(f"   {green('To do ' + str(len(pending)))} · {dim('Resolved, pending close ' + str(len(resolved)))}")

    open_in_code(out_path, "Press Cmd+Shift+V to preview; links are clickable.")


# ---------- Command: timesheet ----------
def cmd_timesheet(url, key, rest=None):
    """Launch the local web calendar week view; drag blocks to log time. See timesheet_web.py."""
    rest = rest or []
    port = 8765
    if "--port" in rest:
        try:
            port = int(rest[rest.index("--port") + 1])
        except (IndexError, ValueError):
            die("--port must be followed by a port number, e.g. ./giwa timesheet --port 8790")
    import timesheet_web
    gurl, gtok = gitlab_cfg()
    try:
        timesheet_web.serve(url, key, api_get, api_post, port, extra_ids=extra_task_ids(),
                            gitlab_url=gurl, gitlab_token=gtok, gitlab_get=gitlab_get,
                            api_put=api_put, api_delete=api_delete)
    except RuntimeError as e:
        die(str(e))


# ---------- Entry point ----------
COMMANDS = {
    "overview": cmd_overview,
    "mine": cmd_mine,
    "timesheet": cmd_timesheet,
}


def usage():
    print("GIWA one-shot query tool\n")
    print("Usage: ./giwa <command>\n")
    print("Commands:")
    print("  overview              Global issue overview (open/closed, by project, by status, by assignee)")
    print("  mine                  My open issues, exported to MINE.md with links")
    print("  timesheet [--port N]  Open the local web calendar week view; drag blocks to log time and submit to GIWA")
    print()


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        sys.exit(0)
    cmd = args[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        die(f"Unknown command: {cmd}\n  Run ./giwa --help to see available commands.")
    url, key = load_env()
    fn(url, key, args[1:])


if __name__ == "__main__":
    main()
