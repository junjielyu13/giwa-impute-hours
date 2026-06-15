"""GIWA timesheet — a local web app with a weekly calendar view.

Like a calendar: columns = Mon–Fri, vertical axis = time. Drag a block on a day's
timeline = time spent on a task; the block duration is converted to logged hours.
Redmine only stores "date + hours" (no clock time), so the timeline is just for
intuitive layout. Already-logged hours show as grey cards atop each column
(read-only, never submitted twice).

Invoked by giwa.py's `timesheet` command, with api_get / api_post injected.
"""

import datetime
import http.server
import json
import re
import threading
import time
import urllib.parse
import webbrowser

ACTIVITY_OTHERS = 17  # the time-entry activity type the user normally uses (Others)


def _proj_code(name):
    """Project code: the part before " - description". e.g. "ABC-12345 - Some module…"
    -> ABC-12345; if there is no " - ", keep the name as-is."""
    return name.split(" - ")[0].strip()


def _week_dates(week_offset=0):
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(days=7 * week_offset)
    return [monday + datetime.timedelta(days=n) for n in range(5)]


def serve(url, key, api_get, api_post, port=8765, extra_ids=None,
          gitlab_url="", gitlab_token="", gitlab_get=None):
    extra_ids = extra_ids or []
    proj_cache = {}

    def gitlab_activity(week_offset):
        """This week's GitLab activity (by day) + the set of GIWA issue numbers
        extracted from branches/MRs. Read-only."""
        days = _week_dates(week_offset)
        if not (gitlab_token and gitlab_get):
            return {"enabled": False, "days": [d.isoformat() for d in days], "byday": {}}, set()
        start, end = days[0], days[0] + datetime.timedelta(days=6)
        after = (start - datetime.timedelta(days=1)).isoformat()
        before = (end + datetime.timedelta(days=1)).isoformat()
        events, page = [], 1
        try:
            while page <= 3:
                ev = gitlab_get(gitlab_url, gitlab_token, f"/events?after={after}&before={before}&per_page=100&page={page}")
                if not ev:
                    break
                events += ev
                if len(ev) < 100:
                    break
                page += 1
        except Exception:
            return {"enabled": True, "days": [d.isoformat() for d in days], "byday": {}}, set()

        def proj_name(pid):
            if pid not in proj_cache:
                try:
                    proj_cache[pid] = gitlab_get(gitlab_url, gitlab_token, f"/projects/{pid}")["path_with_namespace"]
                except Exception:
                    proj_cache[pid] = str(pid)
            return proj_cache[pid]

        giwa_ids, pushagg, others = set(), {}, {}
        for e in events:
            d = (e.get("created_at") or "")[:10]
            pid = e.get("project_id")
            repo = proj_name(pid) if pid else ""
            pd = e.get("push_data")
            if pd:
                branch = pd.get("ref") or ""
                a = pushagg.setdefault((d, repo, branch), {"count": 0, "title": pd.get("commit_title") or ""})
                a["count"] += pd.get("commit_count") or 0
                m = re.search(r"giwa[-_]?(\d+)", branch, re.I)
                if m:
                    giwa_ids.add(int(m.group(1)))
            elif e.get("target_type") == "MergeRequest":
                title = e.get("target_title") or ""
                iid = e.get("target_iid")
                repo_url = f"{gitlab_url}/{repo}" if repo else gitlab_url
                mr_url = f"{repo_url}/-/merge_requests/{iid}" if iid else repo_url
                others.setdefault(d, []).append({"type": "mr", "repo": repo, "repo_url": repo_url,
                                                 "action": e.get("action_name", ""), "title": title, "url": mr_url})
                m = re.search(r"giwa[-_]?(\d+)", title, re.I)
                if m:
                    giwa_ids.add(int(m.group(1)))
        byday = {}
        for (d, repo, branch), a in pushagg.items():
            repo_url = f"{gitlab_url}/{repo}" if repo else gitlab_url
            byday.setdefault(d, []).append({"type": "push", "repo": repo, "branch": branch,
                                            "count": a["count"], "title": a["title"], "repo_url": repo_url,
                                            "branch_url": f"{repo_url}/-/tree/{branch}" if branch else repo_url})
        for d, lst in others.items():
            byday.setdefault(d, []).extend(lst)
        return {"enabled": True, "days": [d.isoformat() for d in days], "byday": byday}, giwa_ids

    def init(week_offset):
        days = _week_dates(week_offset)
        issues, off = [], 0
        while True:
            d = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=open&limit=100&offset={off}")
            issues += d["issues"]
            tc = d["total_count"]
            off += 100
            if off >= tc:
                break

        all_tasks = [{
            "id": i["id"], "subject": i["subject"], "tracker": i["tracker"]["name"],
            "project": i["project"]["name"], "projcode": _proj_code(i["project"]["name"]),
            "status": i["status"]["name"],
        } for i in issues]

        # Auto-discover "internal/client meeting" Epics across projects (subject contains
        # Tareas internas/externas), and rename them to friendly titles.
        meeting = []
        try:
            md = api_get(url, key, "/issues.json?subject=~Tareas&status_id=*&limit=100")
            for i in md["issues"]:
                m = re.search(r"tareas\s+(internas|externas)\b", i["subject"].lower())
                if not m:
                    continue
                proj = i["project"]["name"]
                kind = "Internal meeting" if m.group(1) == "internas" else "Client meeting"
                meeting.append({"id": i["id"], "subject": i["subject"], "project": proj, "projcode": _proj_code(proj),
                                "tracker": i["tracker"]["name"], "status": i["status"]["name"], "label": kind})
        except Exception:
            pass

        # Manual persistent tasks (.env GIWA_EXTRA_TASKS)
        manual, have = [], {t["id"] for t in all_tasks} | {t["id"] for t in meeting}
        for tid in extra_ids:
            if tid in have:
                continue
            try:
                di = api_get(url, key, f"/issues/{tid}.json")["issue"]
                manual.append({"id": tid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                               "project": di["project"]["name"], "projcode": _proj_code(di["project"]["name"]),
                               "status": di["status"]["name"]})
                have.add(tid)
            except Exception:
                pass

        # Meetings/persistent tasks first, deduplicated
        seen, merged = set(), []
        for t in meeting + manual + all_tasks:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            merged.append(t)
        all_tasks = merged
        subj_of = {t["id"]: t["subject"] for t in all_tasks}

        # Issues I touched/edited during the selected week (any status), shown at the top of the list
        recent = []
        try:
            wk_from = days[0].isoformat()                                   # Monday of the selected week
            wk_to = (days[0] + datetime.timedelta(days=6)).isoformat()      # Sunday (covers the weekend too)
            rc = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=*&updated_on=%3E%3C{wk_from}%7C{wk_to}&sort=updated_on:desc&limit=50")
            for i in rc.get("issues", []):
                recent.append({"id": i["id"], "subject": i["subject"], "tracker": i["tracker"]["name"],
                               "project": i["project"]["name"], "projcode": _proj_code(i["project"]["name"]),
                               "status": i["status"]["name"], "updated": i["updated_on"][:10]})
        except Exception:
            pass

        start, end = days[0].isoformat(), days[4].isoformat()
        existing = []
        try:
            te = api_get(url, key, f"/time_entries.json?user_id=me&from={start}&to={end}&limit=100")
            agg = {}
            for t in te.get("time_entries", []):
                iss = (t.get("issue") or {}).get("id")
                if not iss:
                    continue
                agg[(iss, t["spent_on"])] = round(agg.get((iss, t["spent_on"]), 0) + t["hours"], 2)
            for (iss, date), hrs in agg.items():
                subj = subj_of.get(iss)
                if subj is None:
                    try:
                        subj = api_get(url, key, f"/issues/{iss}.json")["issue"]["subject"]
                    except Exception:
                        subj = ""
                existing.append({"issue_id": iss, "date": date, "hours": hrs, "subject": subj})
        except Exception:
            pass

        # GitLab: this week's activity + GIWA issues extracted from PRs/branches (the gitlab group in the task list)
        gl_panel, gl_ids = gitlab_activity(week_offset)
        known_all = {t["id"]: t for t in all_tasks}
        recent_map = {t["id"]: t for t in recent}
        gitlab_tasks = []
        for iid in sorted(gl_ids, reverse=True):
            t = known_all.get(iid) or recent_map.get(iid)
            if t is None:
                try:
                    di = api_get(url, key, f"/issues/{iid}.json")["issue"]
                    t = {"id": iid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                         "project": di["project"]["name"], "projcode": _proj_code(di["project"]["name"]),
                         "status": di["status"]["name"]}
                except Exception:
                    continue
            gitlab_tasks.append(t)

        # Attach project codes to already-logged entries (used by the left-side per-project stats)
        pcode = {t["id"]: t["projcode"] for t in (all_tasks + recent + gitlab_tasks)}
        ecache = {}
        for e in existing:
            iid = e["issue_id"]
            if iid not in pcode and iid not in ecache:
                try:
                    ecache[iid] = _proj_code(api_get(url, key, f"/issues/{iid}.json")["issue"]["project"]["name"])
                except Exception:
                    ecache[iid] = "?"
            e["projcode"] = pcode.get(iid) or ecache.get(iid, "?")

        return {
            "base": url,
            "week_offset": week_offset,
            # Structured week info; the client formats the localized label itself.
            "week_start": days[0].isoformat(),
            "week_end": days[4].isoformat(),
            "week_num": days[0].isocalendar()[1],
            "days": [{"date": d.isoformat()} for d in days],
            "all_tasks": all_tasks,
            "recent": recent,
            "gitlab_tasks": gitlab_tasks,
            "gitlab": gl_panel,
            "existing": existing,
        }

    def issue_brief(iid):
        """Look up a single issue for the manual-ID entry path (read-only)."""
        di = api_get(url, key, f"/issues/{iid}.json")["issue"]
        proj = di["project"]["name"]
        return {"id": iid, "subject": di["subject"], "tracker": di["tracker"]["name"],
                "project": proj, "projcode": _proj_code(proj), "status": di["status"]["name"]}

    def submit(entries):
        cache = {}

        def comment_for(iid, given):
            if given:
                return given
            if iid not in cache:
                try:
                    it = api_get(url, key, f"/issues/{iid}.json")["issue"]
                    cache[iid] = f"{it['tracker']['name']} #{iid}: {it['subject']}"
                except Exception:
                    cache[iid] = f"#{iid}"
            return cache[iid]

        results = []
        for e in entries:
            iid = int(e["issue_id"])
            date = e["date"]
            hours = round(float(e["hours"]), 2)
            comment = comment_for(iid, (e.get("comment") or "").strip())
            payload = {"time_entry": {"issue_id": iid, "hours": hours, "spent_on": date,
                                      "activity_id": ACTIVITY_OTHERS, "comments": comment}}
            try:
                api_post(url, key, "/time_entries.json", payload)
                results.append({"issue_id": iid, "date": date, "hours": hours, "ok": True})
            except Exception as ex:
                results.append({"issue_id": iid, "date": date, "hours": hours, "ok": False, "error": str(ex)})
        return results

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            if p.path == "/":
                self._send(200, HTML_PAGE, "text/html")
            elif p.path == "/api/init":
                q = urllib.parse.parse_qs(p.query)
                wk = int(q.get("week", ["0"])[0])
                try:
                    self._send(200, json.dumps(init(wk)))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            elif p.path == "/api/issue":
                q = urllib.parse.parse_qs(p.query)
                # Catch BaseException: the injected api_get calls die()/sys.exit on a
                # bad/nonexistent id, which raises SystemExit (not an Exception).
                try:
                    iid = int(q.get("id", ["0"])[0])
                    if iid <= 0:
                        raise ValueError("invalid id")
                    self._send(200, json.dumps(issue_brief(iid)))
                except BaseException as e:
                    self._send(404, json.dumps({"error": str(e) or "not found"}))
            elif p.path == "/api/ping":
                state["last"] = time.monotonic()
                self._send(200, "{}")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/submit":
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                try:
                    self._send(200, json.dumps(submit(body.get("entries", []))))
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}))
            elif self.path == "/api/close":
                self._send(200, "{}")
                threading.Thread(target=srv.shutdown, daemon=True).start()
            else:
                self._send(404, "not found", "text/plain")

    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise RuntimeError(f"Port {port} is in use, try another one: ./giwa timesheet --port 8790")

    # Heartbeat watchdog: the page pings every 3s; if no heartbeat arrives within 8s
    # of the tab closing, the server stops automatically.
    state = {"last": None}
    HEARTBEAT_TIMEOUT = 8.0

    def watchdog():
        while True:
            time.sleep(2)
            if state["last"] is not None and time.monotonic() - state["last"] > HEARTBEAT_TIMEOUT:
                srv.shutdown()
                return

    u = f"http://127.0.0.1:{port}/"
    print(f"🗓️  Time calendar started: {u}")
    print("   Drag a time block on a day's timeline → pick a task → click \"Submit to GIWA\".")
    print("   Closing the browser tab stops the service automatically (or press Ctrl+C here).")
    threading.Thread(target=watchdog, daemon=True).start()
    webbrowser.open(u)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    print("\nTime calendar service stopped (page closed or stopped manually).")


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GIWA Time Calendar</title>
<style>
  :root { --line:#e6e8ec; --accent:#e8482b; --ok:#1a8a3a; --new:#e8482b; --locked:#9aa0a8; }
  * { box-sizing: border-box; }
  body { font-family:-apple-system,"PingFang SC",system-ui,sans-serif; margin:0; background:#fff; color:#1d2129; }
  header { padding:14px 20px; display:flex; align-items:baseline; gap:14px; border-bottom:1px solid var(--line); }
  header h1 { font-size:22px; margin:0; font-weight:700; }
  header .wk { color:#8a9099; font-size:14px; }
  .weeknav { margin-left:auto; display:flex; align-items:center; gap:8px; }
  .weeknav button { background:#f0f1f3; border:0; border-radius:6px; padding:6px 12px; cursor:pointer; font-size:14px; }
  .weeknav button:hover { background:#e4e6e9; }
  .langsel { margin-left:10px; background:#f0f1f3; border:0; border-radius:6px; padding:6px 8px; cursor:pointer; font-size:13px; color:#333; }
  #content { position:relative; }
  #calLoading { display:none; position:absolute; inset:0; background:rgba(255,255,255,.72); z-index:40; flex-direction:column; align-items:center; justify-content:center; gap:14px; }
  #calLoading.on { display:flex; }
  #calLoading span { color:#8a9099; font-size:15px; font-weight:600; }
  .spin { width:42px; height:42px; border:4px solid #e6e8ec; border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .cal { display:grid; grid-template-columns:56px repeat(5,1fr); }
  .corner, .dayhead { border-bottom:1px solid var(--line); }
  .dayhead { padding:8px 6px; text-align:center; border-left:1px solid var(--line); }
  .dayhead .wd { font-size:13px; color:#6b7178; }
  .dayhead .dt { font-size:18px; font-weight:600; }
  .dayhead .tot { font-size:12px; color:#8a9099; margin-top:2px; min-height:14px; font-weight:600; }
  .dayhead .tot.met { color:var(--ok); }
  .dayhead .tot.under { color:#c87f0a; }
  .dayhead .tot.over { color:#c0392b; }
  .dayhead .target { width:88px; margin-top:4px; border:1px solid var(--line); border-radius:5px; padding:3px 4px; font-size:11px; text-align:center; color:#555; }
  .dayhead .target::placeholder { color:#bfc4cb; }
  /* all-day (already-logged) row */
  .allday { border-bottom:1px solid var(--line); min-height:26px; padding:3px; border-left:1px solid var(--line); }
  .allday .chip { background:#eef0f2; color:#6b7178; border-radius:4px; font-size:11px; padding:2px 5px; margin:2px 0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-left:3px solid var(--locked); }
  .gutlabel { font-size:11px; color:#9aa0a8; text-align:right; padding-right:6px; transform:translateY(-7px); }
  .grid { position:relative; border-left:1px solid var(--line); cursor:crosshair; }
  .hourline { position:absolute; left:0; right:0; border-top:1px solid #f0f1f3; }
  .block { position:absolute; left:3px; right:3px; background:var(--new); color:#fff; border-radius:5px; padding:3px 5px; font-size:11px; overflow:hidden; cursor:move; box-shadow:0 1px 2px rgba(0,0,0,.15); }
  .block .x { position:absolute; top:0; right:0; width:24px; height:24px; line-height:24px; text-align:center; cursor:pointer; font-weight:700; font-size:16px; opacity:.95; z-index:5; border-radius:0 5px 0 7px; background:rgba(0,0,0,.18); }
  .block .x:hover { background:rgba(0,0,0,.4); }
  .block .rsz { position:absolute; left:0; right:0; height:7px; cursor:ns-resize; z-index:2; }
  .block .rsz.top { top:0; right:24px; }
  .block .rsz.bot { bottom:0; }
  .block .x:hover { opacity:1; }
  .block .dur { font-weight:700; }
  .block.preview { opacity:.55; }
  /* popup */
  #overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.25); z-index:50; }
  #popup { position:fixed; z-index:51; background:#fff; border-radius:10px; box-shadow:0 8px 30px rgba(0,0,0,.25); padding:16px; width:360px; }
  #popup h3 { margin:0 0 4px; font-size:15px; }
  #popup .sub { color:#8a9099; font-size:12px; margin-bottom:10px; }
  #popup select, #popup input { width:100%; padding:8px; border:1px solid var(--line); border-radius:6px; font-size:13px; margin-bottom:10px; }
  #popup .row { display:flex; gap:8px; justify-content:flex-end; }
  .btn { border:0; border-radius:7px; padding:8px 16px; cursor:pointer; font-size:14px; font-weight:600; }
  .btn-ghost { background:#f0f1f3; color:#333; }
  .btn-primary { background:var(--accent); color:#fff; }
  footer { position:sticky; bottom:0; background:#fff; border-top:1px solid var(--line); padding:12px 20px; display:flex; align-items:center; gap:16px; }
  footer .grand { font-size:16px; font-weight:700; }
  footer .hint { color:#8a9099; font-size:12px; }
  .btn-submit { background:var(--accent); color:#fff; padding:11px 26px; font-size:15px; margin-left:auto; }
  .btn-submit:disabled { opacity:.5; cursor:default; }
  #bottom { display:flex; border-top:2px solid var(--line); margin-top:6px; }
  #bottom .bcol { flex:1; min-width:0; padding:14px 20px 24px; }
  #bottom #stats { border-right:1px solid var(--line); }
  #bottom h3 { margin:0 0 10px; font-size:14px; }
  .st-sec { font-size:11px; color:#8a9099; font-weight:600; margin:10px 0 4px; }
  .st-row { display:flex; justify-content:space-between; font-size:13px; padding:3px 0; border-bottom:1px solid #f3f4f6; }
  .st-total { margin-top:10px; font-size:15px; font-weight:700; }
  .gp-day h4 { margin:8px 0 3px; font-size:12px; color:#37404a; border-bottom:1px solid #eef0f2; padding-bottom:2px; }
  .gp-item { padding:3px 0; line-height:1.4; color:#444; font-size:12px; }
  .gp-item .repo { color:#37404a; font-weight:600; }
  .gp-item .br { color:#1a73c7; }
  .gp-item.gp-mr { color:#7b3ff2; }
  .gp-item a { text-decoration:none; }
  .gp-item a:hover { text-decoration:underline; }
  .gp-item a.mrlink { color:#7b3ff2; }
  .gp-giwa { background:var(--accent); color:#fff; border-radius:3px; padding:0 4px; font-weight:600; text-decoration:none; }
  #result { padding:0 20px; }
  .msg { padding:9px 13px; border-radius:8px; margin:6px 0; font-size:14px; }
  .msg.ok { background:#e6f6ec; color:var(--ok); }
  .msg.err { background:#fdecea; color:#c0392b; }
</style>
</head>
<body>
<header>
  <h1 id="title" data-i18n="title">GIWA Time Calendar</h1>
  <span class="wk" id="weekLabel">Loading…</span>
  <div class="weeknav">
    <button onclick="changeWeek(-1)" data-i18n="prevWeek">◀ Prev</button>
    <button onclick="changeWeek(0,true)" data-i18n="thisWeek">This week</button>
    <button onclick="changeWeek(1)" data-i18n="nextWeek">Next ▶</button>
  </div>
  <select class="langsel" id="langSel" onchange="setLang(this.value)" title="Language">
    <option value="en">English</option>
    <option value="zh">中文</option>
    <option value="es">Español</option>
    <option value="ca">Català</option>
  </select>
</header>
<div id="content">
<div id="cal" class="cal"></div>
<div id="bottom">
  <div id="stats" class="bcol">
    <h3 data-i18n="statsTitle">🧮 This week's hours</h3>
    <div id="statsBody"></div>
  </div>
  <div id="gitarea" class="bcol">
    <h3 data-i18n="gitlabTitle">📦 This week's GitLab activity</h3>
    <div id="gpBody">Loading…</div>
  </div>
</div>
<div id="result"></div>
<div id="calLoading"><div class="spin"></div><span data-i18n="loading">Loading…</span></div>
</div>
<footer>
  <span class="grand" id="grand"></span>
  <span class="hint" data-i18n="footerHint">Drag on a day's timeline to create a time block (snaps to 15 min). Grey cards are already-logged hours.</span>
  <button class="btn btn-submit" id="submitBtn" onclick="submitAll()" data-i18n="submit">Submit to GIWA</button>
</footer>

<div id="overlay" onclick="closePopup()"></div>
<div id="popup">
  <h3 data-i18n="popupTitle">Which task were you working on?</h3>
  <div class="sub" id="popupRange"></div>
  <select id="popupTask" onchange="onTaskSelChange()"></select>
  <input id="popupManualId" type="number" min="1" style="display:none" data-i18n-ph="manualPlaceholder" placeholder="GIWA ID, e.g. 27509" onkeydown="if(event.key==='Enter')confirmBlock()">
  <input id="popupComment" data-i18n-ph="commentPlaceholder" placeholder="Note (optional; blank = use task title)">
  <div class="row">
    <button class="btn btn-ghost" onclick="closePopup()" data-i18n="cancel">Cancel</button>
    <button class="btn btn-primary" onclick="confirmBlock()" data-i18n="add">Add</button>
  </div>
</div>

<script>
// ---------- i18n: 4 languages (English default), browser auto-detect + switcher ----------
const I18N = {
  en: {
    title: "GIWA Time Calendar",
    prevWeek: "◀ Prev", thisWeek: "This week", nextWeek: "Next ▶",
    loading: "Loading…", errPrefix: "Error: ",
    statsTitle: "🧮 This week's hours",
    gitlabTitle: "📦 This week's GitLab activity",
    footerHint: "Drag on a day's timeline to create a time block (snaps to 15 min). Grey cards are already-logged hours.",
    submit: "Submit to GIWA", submitting: "Submitting…",
    popupTitle: "Which task were you working on?",
    commentPlaceholder: "Note (optional; blank = use task title)",
    cancel: "Cancel", add: "Add", del: "Delete",
    targetPlaceholder: "Target h",
    targetTitle: "Optional: expected working hours for the day. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Reminder only, not enforced.",
    statsDaily: "Per day (logged + new)", statsByProject: "By project",
    chooseTask: "Choose a task…",
    manualOption: "✏️ Enter a GIWA ID manually…",
    manualPlaceholder: "GIWA ID, e.g. 27509",
    alertEnterId: "Please enter a valid GIWA ID",
    idNotFound: id => `GIWA #${id} not found (check the ID)`,
    grpGitlab: "🦊 gitlab (linked from this week's PRs/branches)",
    grpRecent: "🕒 This week (worked on by you, any status)",
    gitlabNotConfigured: "GitLab not configured (set GITLAB_URL / GITLAB_TOKEN in .env)",
    gitlabNoActivity: "No GitLab activity this week",
    alertChooseTask: "Please choose a task",
    alertNoBlocks: "No time blocks yet. Drag on a day's timeline to create one.",
    exactlyMet: "Exactly on target",
    dow: ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],
    weekN: n => `(week ${n})`,
    logged: (id, h) => `Logged #${id} · ${h}h`,
    statsTotal: (g, n) => `Total ${g} · new ${n}`,
    grandTotal: (g, n) => `Week total ${g} (new ${n})`,
    short: x => `${x} short`, over: x => `${x} over`,
    confirmSubmit: (count, disp, dec) => `Write ${count} time entries to GIWA, total ${disp} (GIWA records ${dec}h). Submit?`,
    submitFailed: e => `Submit failed: ${e}`,
    submitOk: n => `✓ Submitted ${n} entries; converted to "logged".`,
  },
  zh: {
    title: "GIWA 工时日历",
    prevWeek: "◀ 上周", thisWeek: "本周", nextWeek: "下周 ▶",
    loading: "加载中…", errPrefix: "出错: ",
    statsTitle: "🧮 本周工时统计",
    gitlabTitle: "📦 本周 GitLab 活动",
    footerHint: "在某天时间轴上按住拖动 = 新建时间块（自动按 15 分钟吸附）。灰色卡片是已记录的工时。",
    submit: "提交到 GIWA", submitting: "提交中…",
    popupTitle: "这段时间在做哪个任务？",
    commentPlaceholder: "备注（可选，留空自动用任务标题）",
    cancel: "取消", add: "添加", del: "删除",
    targetPlaceholder: "目标工时",
    targetTitle: "可选：填当天应上班时长。7:45 / 7.45 / 745 都=7h45m，8=8h。只做提醒，不限制。",
    statsDaily: "每天（已记录＋新增）", statsByProject: "按项目",
    chooseTask: "选择任务…",
    manualOption: "✏️ 手动输入 GIWA ID…",
    manualPlaceholder: "GIWA ID，例如 27509",
    alertEnterId: "请输入有效的 GIWA ID",
    idNotFound: id => `找不到 GIWA #${id}（请检查 ID）`,
    grpGitlab: "🦊 gitlab（本周 PR/分支关联）",
    grpRecent: "🕒 本周（你处理过的，任何状态）",
    gitlabNotConfigured: "未配置 GitLab（在 .env 设 GITLAB_URL / GITLAB_TOKEN）",
    gitlabNoActivity: "本周暂无 GitLab 活动",
    alertChooseTask: "请选择一个任务",
    alertNoBlocks: "还没有新建时间块。在某天时间轴上拖动即可。",
    exactlyMet: "正好达标",
    dow: ['周日','周一','周二','周三','周四','周五','周六'],
    weekN: n => `（第 ${n} 周）`,
    logged: (id, h) => `已记 #${id} · ${h}h`,
    statsTotal: (g, n) => `合计 ${g}　·　新增 ${n}`,
    grandTotal: (g, n) => `本周合计 ${g}（新增 ${n}）`,
    short: x => `还差 ${x}`, over: x => `超出 ${x}`,
    confirmSubmit: (count, disp, dec) => `将向 GIWA 写入 ${count} 条工时，合计 ${disp}（GIWA 记 ${dec}h）。确认提交？`,
    submitFailed: e => `提交失败: ${e}`,
    submitOk: n => `✓ 成功提交 ${n} 条工时，已转为「已记录」。`,
  },
  es: {
    title: "Calendario de horas GIWA",
    prevWeek: "◀ Ant.", thisWeek: "Esta semana", nextWeek: "Sig. ▶",
    loading: "Cargando…", errPrefix: "Error: ",
    statsTitle: "🧮 Horas de esta semana",
    gitlabTitle: "📦 Actividad GitLab de esta semana",
    footerHint: "Arrastra en la línea de tiempo de un día para crear un bloque (ajuste de 15 min). Las tarjetas grises son horas ya registradas.",
    submit: "Enviar a GIWA", submitting: "Enviando…",
    popupTitle: "¿En qué tarea trabajabas?",
    commentPlaceholder: "Nota (opcional; vacío = título de la tarea)",
    cancel: "Cancelar", add: "Añadir", del: "Eliminar",
    targetPlaceholder: "Horas obj.",
    targetTitle: "Opcional: horas previstas del día. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Solo recordatorio, no obligatorio.",
    statsDaily: "Por día (registrado + nuevo)", statsByProject: "Por proyecto",
    chooseTask: "Elige una tarea…",
    manualOption: "✏️ Introducir un ID de GIWA manualmente…",
    manualPlaceholder: "ID de GIWA, p. ej. 27509",
    alertEnterId: "Introduce un ID de GIWA válido",
    idNotFound: id => `No se encontró GIWA #${id} (revisa el ID)`,
    grpGitlab: "🦊 gitlab (vinculado a PRs/ramas de esta semana)",
    grpRecent: "🕒 Esta semana (en los que has trabajado, cualquier estado)",
    gitlabNotConfigured: "GitLab no configurado (define GITLAB_URL / GITLAB_TOKEN en .env)",
    gitlabNoActivity: "Sin actividad de GitLab esta semana",
    alertChooseTask: "Elige una tarea",
    alertNoBlocks: "Aún no hay bloques. Arrastra en la línea de tiempo de un día para crear uno.",
    exactlyMet: "Justo en el objetivo",
    dow: ['Dom','Lun','Mar','Mié','Jue','Vie','Sáb'],
    weekN: n => `(semana ${n})`,
    logged: (id, h) => `Registrado #${id} · ${h}h`,
    statsTotal: (g, n) => `Total ${g} · nuevo ${n}`,
    grandTotal: (g, n) => `Total semana ${g} (nuevo ${n})`,
    short: x => `faltan ${x}`, over: x => `${x} de más`,
    confirmSubmit: (count, disp, dec) => `Se escribirán ${count} entradas de tiempo en GIWA, total ${disp} (GIWA registra ${dec}h). ¿Confirmar?`,
    submitFailed: e => `Error al enviar: ${e}`,
    submitOk: n => `✓ Enviadas ${n} entradas; convertidas a "registrado".`,
  },
  ca: {
    title: "Calendari d'hores GIWA",
    prevWeek: "◀ Ant.", thisWeek: "Aquesta setmana", nextWeek: "Seg. ▶",
    loading: "Carregant…", errPrefix: "Error: ",
    statsTitle: "🧮 Hores d'aquesta setmana",
    gitlabTitle: "📦 Activitat GitLab d'aquesta setmana",
    footerHint: "Arrossega a la línia de temps d'un dia per crear un bloc (ajust de 15 min). Les targetes grises són hores ja registrades.",
    submit: "Envia a GIWA", submitting: "Enviant…",
    popupTitle: "En quina tasca treballaves?",
    commentPlaceholder: "Nota (opcional; buit = títol de la tasca)",
    cancel: "Cancel·la", add: "Afegeix", del: "Elimina",
    targetPlaceholder: "Hores obj.",
    targetTitle: "Opcional: hores previstes del dia. 7:45 / 7.45 / 745 = 7h45m, 8 = 8h. Només recordatori, no obligatori.",
    statsDaily: "Per dia (registrat + nou)", statsByProject: "Per projecte",
    chooseTask: "Tria una tasca…",
    manualOption: "✏️ Introduir un ID de GIWA manualment…",
    manualPlaceholder: "ID de GIWA, p. ex. 27509",
    alertEnterId: "Introdueix un ID de GIWA vàlid",
    idNotFound: id => `No s'ha trobat GIWA #${id} (revisa l'ID)`,
    grpGitlab: "🦊 gitlab (vinculat a PRs/branques d'aquesta setmana)",
    grpRecent: "🕒 Aquesta setmana (en què has treballat, qualsevol estat)",
    gitlabNotConfigured: "GitLab no configurat (defineix GITLAB_URL / GITLAB_TOKEN a .env)",
    gitlabNoActivity: "Sense activitat de GitLab aquesta setmana",
    alertChooseTask: "Tria una tasca",
    alertNoBlocks: "Encara no hi ha blocs. Arrossega a la línia de temps d'un dia per crear-ne un.",
    exactlyMet: "Just a l'objectiu",
    dow: ['Dg','Dl','Dt','Dc','Dj','Dv','Ds'],
    weekN: n => `(setmana ${n})`,
    logged: (id, h) => `Registrat #${id} · ${h}h`,
    statsTotal: (g, n) => `Total ${g} · nou ${n}`,
    grandTotal: (g, n) => `Total setmana ${g} (nou ${n})`,
    short: x => `falten ${x}`, over: x => `${x} de més`,
    confirmSubmit: (count, disp, dec) => `S'escriuran ${count} entrades de temps a GIWA, total ${disp} (GIWA registra ${dec}h). Confirmar?`,
    submitFailed: e => `Error en enviar: ${e}`,
    submitOk: n => `✓ Enviades ${n} entrades; convertides a "registrat".`,
  },
};
function detectLang() {
  const saved = localStorage.getItem('giwa_lang');
  if (saved && I18N[saved]) return saved;
  const navs = navigator.languages || [navigator.language || 'en'];
  for (const raw of navs) {
    const code = (raw || '').toLowerCase();
    if (code.startsWith('zh')) return 'zh';
    if (code.startsWith('ca')) return 'ca';   // check Catalan before Spanish
    if (code.startsWith('es')) return 'es';
    if (code.startsWith('en')) return 'en';
  }
  return 'en';
}
let LANG = detectLang();
let T = Object.assign({}, I18N.en, I18N[LANG]);
const dowName = date => T.dow[new Date(date + 'T00:00:00').getDay()];
function applyStatic() {
  document.documentElement.lang = LANG;
  document.title = T.title;
  document.querySelectorAll('[data-i18n]').forEach(el => { const k = el.dataset.i18n; if (T[k] != null) el.textContent = T[k]; });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { const k = el.dataset.i18nPh; if (T[k] != null) el.placeholder = T[k]; });
  const ls = document.getElementById('langSel'); if (ls) ls.value = LANG;
}
function setLang(l) {
  if (!I18N[l]) return;
  LANG = l; localStorage.setItem('giwa_lang', l);
  T = Object.assign({}, I18N.en, I18N[l]);
  applyStatic();
  if (DATA && !DATA.error) { render(); renderGitlab(); renderStats(); }
}
function updateWeekLabel() {
  if (DATA && !DATA.error) document.getElementById('weekLabel').textContent = `${DATA.week_start} ~ ${DATA.week_end} ${T.weekN(DATA.week_num)}`;
}

const START_H = 7, END_H = 22, PXH = 44, SNAP = 15;
const TOTAL_MIN = (END_H - START_H) * 60;
let DATA = null, weekOffset = 0;
let blocks = [];          // new blocks {bid, issue_id, subject, date, s, e, comment}
let bidSeq = 1;
let drag = null;          // {date, col, s, e, el}
let pending = null;       // pending block range awaiting confirmation

const fmt = m => String(Math.floor(m/60)).padStart(2,'0') + ':' + String(m%60).padStart(2,'0');
const minToY = m => (m - START_H*60) / 60 * PXH;
const yToMin = y => { let m = START_H*60 + y/PXH*60; return Math.max(START_H*60, Math.min(END_H*60, Math.round(m/SNAP)*SNAP)); };
// Duration formatting / parsing.
// Durations display in H.MM notation: 2h45m → "2.45", 30m → "0.30", 1h → "1". Decimal hours are used when submitting to GIWA.
const fmtDot = h => { if (!h) return '0'; const m = Math.round(h*60); const hh = Math.floor(m/60), mm = m%60; return mm ? hh + '.' + String(mm).padStart(2,'0') : '' + hh; };
const decH = h => +(h).toFixed(2);  // decimal hours submitted to GIWA
const fmtColon = h => { const m = Math.round(h*60); return Math.floor(m/60) + ':' + String(m%60).padStart(2,'0'); };
function parseHM(s) {
  s = (s||'').trim().toLowerCase().replace(/h/g, ':').replace(/m/g, '').replace(/\s/g, '');
  if (!s) return null;
  // Both '.' and ':' act as the "h:m" separator; minutes are taken literally: 7:45 / 7.45 → 7h45m
  const sep = s.includes(':') ? ':' : (s.includes('.') ? '.' : null);
  if (sep) { const p = s.split(sep); return (parseInt(p[0],10)||0) + (p[1] ? (parseInt(p[1],10)||0)/60 : 0); }
  // Plain digits: 1–2 digits = hours (8→8h), 3–4 digits as HMM (745→7h45m, 830→8h30m)
  if (/^\d+$/.test(s)) {
    if (s.length <= 2) return parseInt(s, 10);
    return (parseInt(s.slice(0,-2),10)||0) + (parseInt(s.slice(-2),10)||0)/60;
  }
  const v = parseFloat(s); return isNaN(v) ? null : v;
}
// Daily target hours (stored locally per specific date, independent per week, not shared)
function loadTargets() { try { return JSON.parse(localStorage.getItem('giwa_targets')) || {}; } catch(e) { return {}; } }
let TARGETS = loadTargets();
function setTarget(date, val) { const h = parseHM(val); if (h == null || isNaN(h) || h <= 0) delete TARGETS[date]; else TARGETS[date] = h; localStorage.setItem('giwa_targets', JSON.stringify(TARGETS)); recalc(); }

async function load() {
  const ld = document.getElementById('calLoading');
  ld.classList.add('on');
  document.getElementById('weekLabel').textContent = T.loading;
  try {
    const r = await fetch('/api/init?week=' + weekOffset);
    DATA = await r.json();
    if (DATA.error) { document.getElementById('weekLabel').textContent = T.errPrefix + DATA.error; return; }
    blocks = [];
    document.getElementById('result').innerHTML = '';
    render();
    renderGitlab();
    renderStats();
  } finally {
    ld.classList.remove('on');
  }
}
function changeWeek(d, reset) { weekOffset = reset ? 0 : weekOffset + d; load(); }

const giwaLink = s => (s || '').replace(/giwa[-_]?(\d+)/ig, (_, n) => `<a class="gp-giwa" href="${DATA.base}/issues/${n}" target="_blank">GIWA #${n}</a>`);

// Left panel: this week's hours (logged + new, by day & by project)
function renderStats() {
  const body = document.getElementById('statsBody'); if (!body) return;
  let grand = 0, newTot = 0, rows = '';
  DATA.days.forEach(d => {
    const ex = DATA.existing.filter(e => e.date === d.date).reduce((s,e)=>s+e.hours,0);
    const nw = blocks.filter(b => b.date === d.date).reduce((s,b)=>s+(b.e-b.s)/60,0);
    const tot = ex + nw; grand += tot; newTot += nw;
    const tg = TARGETS[d.date];
    rows += `<div class="st-row"><span>${dowName(d.date)} ${d.date.slice(8)}</span><span>${tot?fmtDot(tot):'—'}${tg!=null?' / '+fmtDot(tg):''}</span></div>`;
  });
  const proj = {};
  DATA.existing.forEach(e => { const k = e.projcode||'?'; proj[k] = (proj[k]||0) + e.hours; });
  blocks.forEach(b => { const k = b.projcode||'?'; proj[k] = (proj[k]||0) + (b.e-b.s)/60; });
  const prows = Object.keys(proj).sort((a,b)=>proj[b]-proj[a])
    .map(k => `<div class="st-row"><span>${k}</span><span>${fmtDot(proj[k])}</span></div>`).join('') || '<div class="st-row"><span>—</span><span></span></div>';
  body.innerHTML = `<div class="st-sec">${T.statsDaily}</div>${rows}<div class="st-sec">${T.statsByProject}</div>${prows}<div class="st-total">${T.statsTotal(fmtDot(grand), fmtDot(newTot))}</div>`;
}

function renderGitlab() {
  const body = document.getElementById('gpBody');
  const gl = DATA.gitlab;
  if (!gl || !gl.enabled) { body.innerHTML = `<span style="color:#999">${T.gitlabNotConfigured}</span>`; return; }
  let html = '';
  (gl.days || []).forEach(date => {
    const items = gl.byday[date] || [];
    if (!items.length) return;
    const dow = dowName(date);
    html += `<div class="gp-day"><h4>${date.slice(5)} ${dow}</h4>`;
    items.forEach(it => {
      if (it.type === 'push') {
        const repoEl = it.repo_url ? `<a class="repo" href="${it.repo_url}" target="_blank">${it.repo}</a>` : `<span class="repo">${it.repo}</span>`;
        const brEl = it.branch_url ? `<a class="br" href="${it.branch_url}" target="_blank">${it.branch}</a>` : `<span class="br">${it.branch}</span>`;
        html += `<div class="gp-item">⬆ ${repoEl} · ${brEl} · ${it.count} commits<br>${giwaLink(it.branch)} ${(it.title||'').slice(0,55)}</div>`;
      } else if (it.type === 'mr') {
        const repoEl = it.repo_url ? `<a class="repo" href="${it.repo_url}" target="_blank">${it.repo}</a>` : `<span class="repo">${it.repo}</span>`;
        const title = (it.title||'').slice(0,60);
        const titleEl = it.url ? `<a class="mrlink" href="${it.url}" target="_blank">${title}</a>` : title;
        const actEl = it.url ? `<a class="mrlink" href="${it.url}" target="_blank">${it.action}</a>` : it.action;
        html += `<div class="gp-item gp-mr">🔀 ${actEl} · ${repoEl}<br>${titleEl}</div>`;
      }
    });
    html += '</div>';
  });
  body.innerHTML = html || `<span style="color:#999">${T.gitlabNoActivity}</span>`;
}

function render() {
  updateWeekLabel();
  const cal = document.getElementById('cal');
  // Header row
  let html = '<div class="corner"></div>';
  DATA.days.forEach((d, idx) => {
    html += `<div class="dayhead"><div class="wd">${dowName(d.date)}</div><div class="dt">${d.date.slice(8)}</div>` +
            `<div class="tot" id="tot-${d.date}"></div>` +
            `<input class="target" id="tg-${d.date}" placeholder="${T.targetPlaceholder}" title="${T.targetTitle}" onchange="setTarget('${d.date}', this.value)"></div>`;
  });
  // Already-logged (all-day row)
  html += '<div class="allday" style="border-left:0"></div>';
  DATA.days.forEach(d => {
    const exs = DATA.existing.filter(e => e.date === d.date);
    let chips = exs.map(e => `<div class="chip" title="#${e.issue_id} ${e.subject}">${T.logged(e.issue_id, e.hours)}</div>`).join('');
    html += `<div class="allday">${chips}</div>`;
  });
  // Time grid
  const gh = TOTAL_MIN/60 * PXH;
  let gutter = '<div style="position:relative;height:' + gh + 'px">';
  for (let h = START_H; h <= END_H; h++) gutter += `<div class="gutlabel" style="position:absolute;top:${minToY(h*60)}px;right:6px">${h}:00</div>`;
  gutter += '</div>';
  html += gutter;
  DATA.days.forEach(d => {
    html += `<div class="grid" id="grid-${d.date}" data-date="${d.date}" style="height:${gh}px"></div>`;
  });
  cal.style.gridTemplateRows = 'auto auto 1fr';
  cal.innerHTML = html;
  // Restore saved daily targets
  DATA.days.forEach(d => { const inp = document.getElementById('tg-' + d.date); if (inp && TARGETS[d.date] != null) inp.value = fmtColon(TARGETS[d.date]); });
  // Hour lines + drag binding
  DATA.days.forEach(d => {
    const g = document.getElementById('grid-' + d.date);
    for (let h = START_H; h <= END_H; h++) { const l = document.createElement('div'); l.className='hourline'; l.style.top = minToY(h*60)+'px'; g.appendChild(l); }
    g.addEventListener('mousedown', startDrag);
  });
  renderBlocks();
  recalc();
}

function startDrag(ev) {
  if (ev.target.closest('.block')) return;   // clicking on an existing block doesn't create a new one
  const g = ev.currentTarget;
  const rect = g.getBoundingClientRect();
  const s = yToMin(ev.clientY - rect.top);
  drag = { date: g.dataset.date, g, rect, s, e: s };
  const el = document.createElement('div');
  el.className = 'block preview';
  g.appendChild(el);
  drag.el = el;
  positionBlock(el, s, s);
  document.addEventListener('mousemove', moveDrag);
  document.addEventListener('mouseup', endDrag);
  ev.preventDefault();
}
function moveDrag(ev) {
  if (!drag) return;
  drag.e = yToMin(ev.clientY - drag.rect.top);
  const a = Math.min(drag.s, drag.e), b = Math.max(drag.s, drag.e);
  positionBlock(drag.el, a, b);
  drag.el.innerHTML = `<span class="dur">${fmtDot((b-a)/60)}</span> ${fmt(a)}–${fmt(b)}`;
}
function endDrag(ev) {
  document.removeEventListener('mousemove', moveDrag);
  document.removeEventListener('mouseup', endDrag);
  if (!drag) return;
  const a = Math.min(drag.s, drag.e), b = Math.max(drag.s, drag.e);
  drag.el.remove();
  const dd = drag; drag = null;
  if (b - a < SNAP) return;                 // too short, ignore
  pending = { date: dd.date, s: a, e: b };
  openPopup(ev);
}

function positionBlock(el, s, e) {
  el.style.top = minToY(s) + 'px';
  el.style.height = Math.max(minToY(e) - minToY(s), 2) + 'px';
}

function openPopup(ev) {
  const sel = document.getElementById('popupTask');
  // Group by project (optgroup); projects sorted by task count desc; within a group meetings first, the rest by id newest→oldest
  const byProj = {};
  DATA.all_tasks.forEach(t => { (byProj[t.project] = byProj[t.project] || []).push(t); });
  const projNames = Object.keys(byProj).sort((a, b) => byProj[b].length - byProj[a].length || a.localeCompare(b));
  let opts = `<option value="">${T.chooseTask}</option>`;
  opts += `<option value="__manual__">${T.manualOption}</option>`;
  // gitlab: GIWA tasks linked to this week's PRs/branches, pinned at the top
  if (DATA.gitlab_tasks && DATA.gitlab_tasks.length) {
    opts += `<optgroup label="${T.grpGitlab}">`;
    DATA.gitlab_tasks.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label||t.subject).slice(0,40)} · ${t.projcode}</option>`; });
    opts += '</optgroup>';
  }
  // Touched in the last 7 days
  if (DATA.recent && DATA.recent.length) {
    opts += `<optgroup label="${T.grpRecent}">`;
    DATA.recent.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label||t.subject).slice(0,42)} · ${t.projcode}</option>`; });
    opts += '</optgroup>';
  }
  projNames.forEach(p => {
    const items = byProj[p].sort((a, b) => (a.label ? 0 : 1) - (b.label ? 0 : 1) || b.id - a.id);
    opts += `<optgroup label="${p}">`;
    items.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label || t.subject).slice(0,50)}</option>`; });
    opts += '</optgroup>';
  });
  sel.innerHTML = opts;
  document.getElementById('popupComment').value = '';
  const mid = document.getElementById('popupManualId');
  mid.value = ''; mid.style.display = 'none';
  document.getElementById('popupRange').textContent =
    `${pending.date}　${fmt(pending.s)}–${fmt(pending.e)}　(${fmtDot((pending.e-pending.s)/60)})`;
  const pop = document.getElementById('popup');
  document.getElementById('overlay').style.display = 'block';
  pop.style.display = 'block';
  let x = Math.min(ev.clientX, window.innerWidth - 380), y = Math.min(ev.clientY, window.innerHeight - 220);
  pop.style.left = Math.max(10, x) + 'px'; pop.style.top = Math.max(10, y) + 'px';
  sel.focus();
}
function closePopup() {
  document.getElementById('overlay').style.display = 'none';
  document.getElementById('popup').style.display = 'none';
  pending = null;
}
// Show the manual GIWA-ID input when "Enter a GIWA ID manually" is chosen
function onTaskSelChange() {
  const manual = document.getElementById('popupTask').value === '__manual__';
  const mid = document.getElementById('popupManualId');
  mid.style.display = manual ? 'block' : 'none';
  if (manual) mid.focus();
}
async function confirmBlock() {
  const pend = pending;            // capture before any await (closePopup nulls it)
  if (!pend) return;
  const selVal = document.getElementById('popupTask').value;
  let id, t;
  if (selVal === '__manual__') {
    id = parseInt(document.getElementById('popupManualId').value, 10);
    if (!id || id <= 0) { alert(T.alertEnterId); return; }
    // Validate the id exists and fetch its subject/project so the block looks right
    try {
      const r = await fetch('/api/issue?id=' + id);
      const d = await r.json();
      if (!r.ok || d.error) { alert(T.idNotFound(id)); return; }
      t = d;
    } catch (e) { alert(T.idNotFound(id)); return; }
  } else {
    id = parseInt(selVal);
    if (!id) { alert(T.alertChooseTask); return; }
    t = DATA.all_tasks.find(x => x.id === id) || (DATA.recent || []).find(x => x.id === id) || (DATA.gitlab_tasks || []).find(x => x.id === id);
  }
  blocks.push({ bid: bidSeq++, issue_id: id, subject: t ? (t.label || t.subject) : '', projcode: t ? t.projcode : '',
                date: pend.date, s: pend.s, e: pend.e, comment: document.getElementById('popupComment').value.trim() });
  closePopup();
  renderBlocks(); recalc();
}

function renderBlocks() {
  document.querySelectorAll('.grid .block:not(.preview)').forEach(x => x.remove());
  blocks.forEach(b => {
    const g = document.getElementById('grid-' + b.date);
    if (!g) return;
    const el = document.createElement('div');
    el.className = 'block';
    positionBlock(el, b.s, b.e);
    el.innerHTML = `<div class="rsz top"></div><span class="x" onclick="delBlock(${b.bid})" title="${T.del}">×</span>` +
      `<span class="dur">${fmtDot((b.e-b.s)/60)} ${fmt(b.s)}–${fmt(b.e)}</span> 【${b.projcode}】#${b.issue_id}<br>` +
      `<span style="opacity:.9">${b.subject.slice(0,26)}</span><div class="rsz bot"></div>`;
    el.addEventListener('mousedown', e => blockMouseDown(e, b.bid));
    g.appendChild(el);
  });
}
function delBlock(bid) { blocks = blocks.filter(b => b.bid !== bid); renderBlocks(); recalc(); }

// Drag the whole block to move it / drag the top/bottom edge to resize (15-min snap)
function blockMouseDown(ev, bid) {
  if (ev.target.classList.contains('x')) return;
  ev.stopPropagation(); ev.preventDefault();
  const b = blocks.find(x => x.bid === bid); if (!b) return;
  const mode = ev.target.classList.contains('rsz') ? (ev.target.classList.contains('top') ? 'top' : 'bot') : 'move';
  const startY = ev.clientY, s0 = b.s, e0 = b.e, dur = e0 - s0;
  function mm(e) {
    const dy = Math.round(((e.clientY - startY) / PXH * 60) / SNAP) * SNAP;
    if (mode === 'move') { let ns = Math.max(START_H*60, Math.min(s0 + dy, END_H*60 - dur)); b.s = ns; b.e = ns + dur; }
    else if (mode === 'top') { b.s = Math.max(START_H*60, Math.min(s0 + dy, b.e - SNAP)); }
    else { b.e = Math.min(END_H*60, Math.max(e0 + dy, b.s + SNAP)); }
    renderBlocks(); recalc();
  }
  function mu() { document.removeEventListener('mousemove', mm); document.removeEventListener('mouseup', mu); }
  document.addEventListener('mousemove', mm); document.addEventListener('mouseup', mu);
}

function recalc() {
  let grand = 0;
  DATA.days.forEach(d => {
    const ex = DATA.existing.filter(e => e.date === d.date).reduce((s,e)=>s+e.hours,0);
    const nw = blocks.filter(b => b.date === d.date).reduce((s,b)=>s+(b.e-b.s)/60,0);
    const tot = ex + nw;
    grand += tot;
    const tg = TARGETS[d.date];
    const el = document.getElementById('tot-' + d.date);
    const g = document.getElementById('grid-' + d.date);
    let cls = '', bg = '';
    if (tg != null) {
      const diff = tot - tg;
      if (Math.abs(diff) < 0.01) { cls = 'met'; bg = '#f3fbf5'; }
      else if (diff < 0) { cls = 'under'; bg = '#fff8ef'; }
      else { cls = 'over'; bg = '#fdf3f2'; }
      if (el) {
        el.textContent = `${fmtDot(tot)} / ${fmtDot(tg)}`;
        el.title = Math.abs(diff) < 0.01 ? T.exactlyMet : (diff < 0 ? T.short(fmtDot(-diff)) : T.over(fmtDot(diff)));
      }
    } else {
      if (el) { el.textContent = tot ? fmtDot(tot) : ''; el.title = ''; }
    }
    if (el) el.className = 'tot ' + cls;
    if (g) g.style.background = bg;
  });
  const newTot = blocks.reduce((s,b)=>s+(b.e-b.s)/60,0);
  document.getElementById('grand').textContent = T.grandTotal(fmtDot(grand), fmtDot(newTot));
  renderStats();
}

async function submitAll() {
  if (!blocks.length) { alert(T.alertNoBlocks); return; }
  const entries = blocks.map(b => ({ issue_id: b.issue_id, date: b.date, hours: decH((b.e-b.s)/60), comment: b.comment }));
  const total = entries.reduce((s,e)=>s+e.hours,0);
  if (!confirm(T.confirmSubmit(entries.length, fmtDot(total), total.toFixed(2)))) return;
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = T.submitting;
  const r = await fetch('/api/submit', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ entries }) });
  const res = await r.json();
  const box = document.getElementById('result');
  box.innerHTML = '';
  if (res.error) box.innerHTML = `<div class="msg err">${T.submitFailed(res.error)}</div>`;
  else {
    const ok = res.filter(x=>x.ok), bad = res.filter(x=>!x.ok);
    if (ok.length) box.innerHTML += `<div class="msg ok">${T.submitOk(ok.length)}</div>`;
    bad.forEach(b => box.innerHTML += `<div class="msg err">✗ #${b.issue_id} ${b.date} ${b.hours}h — ${b.error}</div>`);
    // Move successful ones into existing (grey cards) and remove them from the new blocks
    ok.forEach(e => {
      DATA.existing.push({ issue_id:e.issue_id, date:e.date, hours:e.hours, subject:(DATA.all_tasks.find(t=>t.id===e.issue_id)||{}).subject||'' });
      blocks = blocks.filter(b => !(b.issue_id===e.issue_id && b.date===e.date && Math.abs((b.e-b.s)/60 - e.hours) < 0.001));
    });
    render();
  }
  btn.disabled = false; btn.textContent = T.submit;
}

applyStatic();
load();

// Heartbeat: tell the server every 3s the page is still open; on close/leave, notify the server to exit automatically
setInterval(() => { fetch('/api/ping').catch(()=>{}); }, 3000);
window.addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
window.addEventListener('beforeunload', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
</script>
</body>
</html>
'''
