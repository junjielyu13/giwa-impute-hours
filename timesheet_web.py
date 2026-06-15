"""GIWA 工时表 —— 本地网页·日历周视图。

像日历一样：列=周一~周五，纵轴=时间。在某天的时间轴上拖出一个块 = 在某任务上花的时间，
块的时长换算成工时。GIWA 只存「日期+小时数」不存几点，故时间轴仅作直观排布之用；
已记录的工时以每列顶部的灰色卡片显示（不可改、不会重复提交）。

被 giwa.py 的 `timesheet` 命令调用，依赖注入 api_get / api_post。
"""

import datetime
import http.server
import json
import re
import threading
import time
import urllib.parse
import webbrowser

ACTIVITY_OTHERS = 17  # 用户惯用的工时活动类型 (Others)
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _proj_code(name):
    """项目代号：取「 - 描述」前的部分。如「ABC-12345 - 某模块…」→ ABC-12345；无「 - 」则原样保留。"""
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
        """本周 GitLab 活动（按天）+ 从分支/MR 提取的 GIWA 工单号集合。只读。"""
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
                others.setdefault(d, []).append({"type": "mr", "repo": repo, "action": e.get("action_name", ""), "title": title})
                m = re.search(r"giwa[-_]?(\d+)", title, re.I)
                if m:
                    giwa_ids.add(int(m.group(1)))
        byday = {}
        for (d, repo, branch), a in pushagg.items():
            byday.setdefault(d, []).append({"type": "push", "repo": repo, "branch": branch, "count": a["count"], "title": a["title"]})
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

        # 跨项目自动发现「内部/客户会议」Epic（subject 含 Tareas internas/externas），改成友好标题
        meeting = []
        try:
            md = api_get(url, key, "/issues.json?subject=~Tareas&status_id=*&limit=100")
            for i in md["issues"]:
                m = re.search(r"tareas\s+(internas|externas)\b", i["subject"].lower())
                if not m:
                    continue
                proj = i["project"]["name"]
                kind = "Tarea internal" if m.group(1) == "internas" else "Tarea external"
                meeting.append({"id": i["id"], "subject": i["subject"], "project": proj, "projcode": _proj_code(proj),
                                "tracker": i["tracker"]["name"], "status": i["status"]["name"], "label": kind})
        except Exception:
            pass

        # 手动常驻任务（.env GIWA_EXTRA_TASKS）
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

        # 会议/常驻任务排最前，去重
        seen, merged = set(), []
        for t in meeting + manual + all_tasks:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            merged.append(t)
        all_tasks = merged
        subj_of = {t["id"]: t["subject"] for t in all_tasks}

        # 最近 7 天我处理/编辑过的工单（任何状态），放选择列表最上方
        recent = []
        try:
            since = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
            rc = api_get(url, key, f"/issues.json?assigned_to_id=me&status_id=*&updated_on=%3E%3D{since}&sort=updated_on:desc&limit=30")
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

        # GitLab：本周活动 + 从 PR/分支提取的 GIWA 工单（做成选择列表的 gitlab 分组）
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

        # 给已记录工时补项目代号（左侧统计按项目分组用）
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
            "week_label": f"{days[0]} ~ {days[4]}（第 {days[0].isocalendar()[1]} 周）",
            "days": [{"date": d.isoformat(), "label": WEEKDAY_CN[d.weekday()]} for d in days],
            "all_tasks": all_tasks,
            "recent": recent,
            "gitlab_tasks": gitlab_tasks,
            "gitlab": gl_panel,
            "existing": existing,
        }

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
        raise RuntimeError(f"端口 {port} 被占用，换一个： ./giwa timesheet --port 8790")

    # 心跳看门狗：页面每 3 秒 ping 一次，关掉标签页后 8 秒内收不到心跳即自动停服务。
    state = {"last": None}
    HEARTBEAT_TIMEOUT = 8.0

    def watchdog():
        while True:
            time.sleep(2)
            if state["last"] is not None and time.monotonic() - state["last"] > HEARTBEAT_TIMEOUT:
                srv.shutdown()
                return

    u = f"http://127.0.0.1:{port}/"
    print(f"🗓️  工时日历已启动： {u}")
    print("   在某天时间轴上拖出时间块 → 选任务 → 点「提交到 GIWA」。")
    print("   关闭浏览器标签页即自动停止服务（也可在此按 Ctrl+C）。")
    threading.Thread(target=watchdog, daemon=True).start()
    webbrowser.open(u)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    print("\n已关闭工时日历服务（页面已关闭或手动停止）。")


HTML_PAGE = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GIWA 工时日历</title>
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
  /* all-day (已记录) row */
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
  .gp-giwa { background:var(--accent); color:#fff; border-radius:3px; padding:0 4px; font-weight:600; text-decoration:none; }
  #result { padding:0 20px; }
  .msg { padding:9px 13px; border-radius:8px; margin:6px 0; font-size:14px; }
  .msg.ok { background:#e6f6ec; color:var(--ok); }
  .msg.err { background:#fdecea; color:#c0392b; }
</style>
</head>
<body>
<header>
  <h1 id="title">GIWA 工时日历</h1>
  <span class="wk" id="weekLabel">加载中…</span>
  <div class="weeknav">
    <button onclick="changeWeek(-1)">◀ 上周</button>
    <button onclick="changeWeek(0,true)">本周</button>
    <button onclick="changeWeek(1)">下周 ▶</button>
  </div>
</header>
<div id="cal" class="cal"></div>
<div id="bottom">
  <div id="stats" class="bcol">
    <h3>🧮 本周工时统计</h3>
    <div id="statsBody"></div>
  </div>
  <div id="gitarea" class="bcol">
    <h3>📦 本周 GitLab 活动</h3>
    <div id="gpBody">加载中…</div>
  </div>
</div>
<div id="result"></div>
<footer>
  <span class="grand" id="grand">合计 0h</span>
  <span class="hint">在某天时间轴上按住拖动 = 新建时间块（自动按 15 分钟吸附）。灰色卡片是已记录的工时。</span>
  <button class="btn btn-submit" id="submitBtn" onclick="submitAll()">提交到 GIWA</button>
</footer>

<div id="overlay" onclick="closePopup()"></div>
<div id="popup">
  <h3>这段时间在做哪个任务？</h3>
  <div class="sub" id="popupRange"></div>
  <select id="popupTask"></select>
  <input id="popupComment" placeholder="备注（可选，留空自动用任务标题）">
  <div class="row">
    <button class="btn btn-ghost" onclick="closePopup()">取消</button>
    <button class="btn btn-primary" onclick="confirmBlock()">添加</button>
  </div>
</div>

<script>
const START_H = 7, END_H = 22, PXH = 44, SNAP = 15;
const TOTAL_MIN = (END_H - START_H) * 60;
let DATA = null, weekOffset = 0;
let blocks = [];          // 新建块 {bid, issue_id, subject, date, s, e, comment}
let bidSeq = 1;
let drag = null;          // {date, col, s, e, el}
let pending = null;       // 待确认的块范围

const fmt = m => String(Math.floor(m/60)).padStart(2,'0') + ':' + String(m%60).padStart(2,'0');
const minToY = m => (m - START_H*60) / 60 * PXH;
const yToMin = y => { let m = START_H*60 + y/PXH*60; return Math.max(START_H*60, Math.min(END_H*60, Math.round(m/SNAP)*SNAP)); };
// 时长格式化 / 解析
// 时长显示用 H.MM 记法：2h45m → "2.45"，30m → "0.30"，1h → "1"。提交 GIWA 时另用十进制小时。
const fmtDot = h => { if (!h) return '0'; const m = Math.round(h*60); const hh = Math.floor(m/60), mm = m%60; return mm ? hh + '.' + String(mm).padStart(2,'0') : '' + hh; };
const decH = h => +(h).toFixed(2);  // 提交给 GIWA 的十进制小时
const fmtColon = h => { const m = Math.round(h*60); return Math.floor(m/60) + ':' + String(m%60).padStart(2,'0'); };
function parseHM(s) {
  s = (s||'').trim().toLowerCase().replace(/h/g, ':').replace(/m/g, '').replace(/\s/g, '');
  if (!s) return null;
  // 点和冒号都作为「时:分」分隔符，分钟取字面值： 7:45 / 7.45 → 7h45m
  const sep = s.includes(':') ? ':' : (s.includes('.') ? '.' : null);
  if (sep) { const p = s.split(sep); return (parseInt(p[0],10)||0) + (p[1] ? (parseInt(p[1],10)||0)/60 : 0); }
  // 纯数字： 1~2 位=小时(8→8h)，3~4 位按 HMM(745→7h45m, 830→8h30m)
  if (/^\d+$/.test(s)) {
    if (s.length <= 2) return parseInt(s, 10);
    return (parseInt(s.slice(0,-2),10)||0) + (parseInt(s.slice(-2),10)||0)/60;
  }
  const v = parseFloat(s); return isNaN(v) ? null : v;
}
// 每日目标工时（按具体日期记在本地，每周独立、不共享）
function loadTargets() { try { return JSON.parse(localStorage.getItem('giwa_targets')) || {}; } catch(e) { return {}; } }
let TARGETS = loadTargets();
function setTarget(date, val) { const h = parseHM(val); if (h == null || isNaN(h) || h <= 0) delete TARGETS[date]; else TARGETS[date] = h; localStorage.setItem('giwa_targets', JSON.stringify(TARGETS)); recalc(); }

async function load() {
  document.getElementById('weekLabel').textContent = '加载中…';
  const r = await fetch('/api/init?week=' + weekOffset);
  DATA = await r.json();
  if (DATA.error) { document.getElementById('weekLabel').textContent = '出错: ' + DATA.error; return; }
  blocks = [];
  document.getElementById('result').innerHTML = '';
  render();
  renderGitlab();
  renderStats();
}
function changeWeek(d, reset) { weekOffset = reset ? 0 : weekOffset + d; load(); }

const giwaLink = s => (s || '').replace(/giwa[-_]?(\d+)/ig, (_, n) => `<a class="gp-giwa" href="${DATA.base}/issues/${n}" target="_blank">GIWA #${n}</a>`);

// 左侧：本周工时统计（已记录 + 新增，按天 & 按项目）
function renderStats() {
  const body = document.getElementById('statsBody'); if (!body) return;
  let grand = 0, newTot = 0, rows = '';
  DATA.days.forEach(d => {
    const ex = DATA.existing.filter(e => e.date === d.date).reduce((s,e)=>s+e.hours,0);
    const nw = blocks.filter(b => b.date === d.date).reduce((s,b)=>s+(b.e-b.s)/60,0);
    const tot = ex + nw; grand += tot; newTot += nw;
    const tg = TARGETS[d.date];
    rows += `<div class="st-row"><span>${d.label} ${d.date.slice(8)}</span><span>${tot?fmtDot(tot):'—'}${tg!=null?' / '+fmtDot(tg):''}</span></div>`;
  });
  const proj = {};
  DATA.existing.forEach(e => { const k = e.projcode||'?'; proj[k] = (proj[k]||0) + e.hours; });
  blocks.forEach(b => { const k = b.projcode||'?'; proj[k] = (proj[k]||0) + (b.e-b.s)/60; });
  const prows = Object.keys(proj).sort((a,b)=>proj[b]-proj[a])
    .map(k => `<div class="st-row"><span>${k}</span><span>${fmtDot(proj[k])}</span></div>`).join('') || '<div class="st-row"><span>—</span><span></span></div>';
  body.innerHTML = `<div class="st-sec">每天（已记录＋新增）</div>${rows}<div class="st-sec">按项目</div>${prows}<div class="st-total">合计 ${fmtDot(grand)}　·　新增 ${fmtDot(newTot)}</div>`;
}

function renderGitlab() {
  const body = document.getElementById('gpBody');
  const gl = DATA.gitlab;
  if (!gl || !gl.enabled) { body.innerHTML = '<span style="color:#999">未配置 GitLab（在 .env 设 GITLAB_URL / GITLAB_TOKEN）</span>'; return; }
  const wd = ['周日','周一','周二','周三','周四','周五','周六'];
  let html = '';
  (gl.days || []).forEach(date => {
    const items = gl.byday[date] || [];
    if (!items.length) return;
    const dow = wd[new Date(date + 'T00:00:00').getDay()];
    html += `<div class="gp-day"><h4>${date.slice(5)} ${dow}</h4>`;
    items.forEach(it => {
      if (it.type === 'push') {
        html += `<div class="gp-item">⬆ <span class="repo">${it.repo}</span> · <span class="br">${it.branch}</span> · ${it.count} commits<br>${giwaLink(it.branch)} ${(it.title||'').slice(0,55)}</div>`;
      } else if (it.type === 'mr') {
        html += `<div class="gp-item gp-mr">🔀 ${it.action} · <span class="repo">${it.repo}</span><br>${giwaLink((it.title||'').slice(0,60))}</div>`;
      }
    });
    html += '</div>';
  });
  body.innerHTML = html || '<span style="color:#999">本周暂无 GitLab 活动</span>';
}

function render() {
  document.getElementById('weekLabel').textContent = DATA.week_label;
  const cal = document.getElementById('cal');
  // 表头
  let html = '<div class="corner"></div>';
  DATA.days.forEach((d, idx) => {
    html += `<div class="dayhead"><div class="wd">${d.label}</div><div class="dt">${d.date.slice(8)}</div>` +
            `<div class="tot" id="tot-${d.date}"></div>` +
            `<input class="target" id="tg-${d.date}" placeholder="目标工时" title="可选：填当天应上班时长。7:45 / 7.45 / 745 都=7h45m，8=8h。只做提醒，不限制。" onchange="setTarget('${d.date}', this.value)"></div>`;
  });
  // 已记录（全天行）
  html += '<div class="allday" style="border-left:0"></div>';
  DATA.days.forEach(d => {
    const exs = DATA.existing.filter(e => e.date === d.date);
    let chips = exs.map(e => `<div class="chip" title="#${e.issue_id} ${e.subject}">已记 #${e.issue_id} · ${e.hours}h</div>`).join('');
    html += `<div class="allday">${chips}</div>`;
  });
  // 时间网格
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
  // 回填已保存的每日目标
  DATA.days.forEach(d => { const inp = document.getElementById('tg-' + d.date); if (inp && TARGETS[d.date] != null) inp.value = fmtColon(TARGETS[d.date]); });
  // 小时线 + 拖拽绑定
  DATA.days.forEach(d => {
    const g = document.getElementById('grid-' + d.date);
    for (let h = START_H; h <= END_H; h++) { const l = document.createElement('div'); l.className='hourline'; l.style.top = minToY(h*60)+'px'; g.appendChild(l); }
    g.addEventListener('mousedown', startDrag);
  });
  renderBlocks();
  recalc();
}

function startDrag(ev) {
  if (ev.target.closest('.block')) return;   // 点在已有块上不新建
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
  if (b - a < SNAP) return;                 // 太短忽略
  pending = { date: dd.date, s: a, e: b };
  openPopup(ev);
}

function positionBlock(el, s, e) {
  el.style.top = minToY(s) + 'px';
  el.style.height = Math.max(minToY(e) - minToY(s), 2) + 'px';
}

function openPopup(ev) {
  const sel = document.getElementById('popupTask');
  // 按项目分组（optgroup）；项目按任务数多→少排，组内会议置顶、其余按编号新→旧
  const byProj = {};
  DATA.all_tasks.forEach(t => { (byProj[t.project] = byProj[t.project] || []).push(t); });
  const projNames = Object.keys(byProj).sort((a, b) => byProj[b].length - byProj[a].length || a.localeCompare(b));
  let opts = '<option value="">选择任务…</option>';
  // gitlab：本周 PR/分支关联的 GIWA 任务，置于最顶
  if (DATA.gitlab_tasks && DATA.gitlab_tasks.length) {
    opts += '<optgroup label="🦊 gitlab（本周 PR/分支关联）">';
    DATA.gitlab_tasks.forEach(t => { opts += `<option value="${t.id}">[${t.tracker||'—'}] #${t.id} · ${(t.label||t.subject).slice(0,40)} · ${t.projcode}</option>`; });
    opts += '</optgroup>';
  }
  // 最近 7 天处理过的
  if (DATA.recent && DATA.recent.length) {
    opts += '<optgroup label="🕒 最近7天（你处理过的，任何状态）">';
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
function confirmBlock() {
  const id = parseInt(document.getElementById('popupTask').value);
  if (!id) { alert('请选择一个任务'); return; }
  const t = DATA.all_tasks.find(x => x.id === id) || (DATA.recent || []).find(x => x.id === id) || (DATA.gitlab_tasks || []).find(x => x.id === id);
  blocks.push({ bid: bidSeq++, issue_id: id, subject: t ? (t.label || t.subject) : '', projcode: t ? t.projcode : '',
                date: pending.date, s: pending.s, e: pending.e, comment: document.getElementById('popupComment').value.trim() });
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
    el.innerHTML = `<div class="rsz top"></div><span class="x" onclick="delBlock(${b.bid})" title="删除">×</span>` +
      `<span class="dur">${fmtDot((b.e-b.s)/60)} ${fmt(b.s)}–${fmt(b.e)}</span> 【${b.projcode}】#${b.issue_id}<br>` +
      `<span style="opacity:.9">${b.subject.slice(0,26)}</span><div class="rsz bot"></div>`;
    el.addEventListener('mousedown', e => blockMouseDown(e, b.bid));
    g.appendChild(el);
  });
}
function delBlock(bid) { blocks = blocks.filter(b => b.bid !== bid); renderBlocks(); recalc(); }

// 拖动整块移动 / 拖上下边缘拉长缩短（15 分钟吸附）
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
        el.title = Math.abs(diff) < 0.01 ? '正好达标' : (diff < 0 ? `还差 ${fmtDot(-diff)}` : `超出 ${fmtDot(diff)}`);
      }
    } else {
      if (el) { el.textContent = tot ? fmtDot(tot) : ''; el.title = ''; }
    }
    if (el) el.className = 'tot ' + cls;
    if (g) g.style.background = bg;
  });
  const newTot = blocks.reduce((s,b)=>s+(b.e-b.s)/60,0);
  document.getElementById('grand').textContent = `本周合计 ${fmtDot(grand)}（新增 ${fmtDot(newTot)}）`;
  renderStats();
}

async function submitAll() {
  if (!blocks.length) { alert('还没有新建时间块。在某天时间轴上拖动即可。'); return; }
  const entries = blocks.map(b => ({ issue_id: b.issue_id, date: b.date, hours: decH((b.e-b.s)/60), comment: b.comment }));
  const total = entries.reduce((s,e)=>s+e.hours,0);
  if (!confirm(`将向 GIWA 写入 ${entries.length} 条工时，合计 ${fmtDot(total)}（GIWA 记 ${total.toFixed(2)}h）。确认提交？`)) return;
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = '提交中…';
  const r = await fetch('/api/submit', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ entries }) });
  const res = await r.json();
  const box = document.getElementById('result');
  box.innerHTML = '';
  if (res.error) box.innerHTML = `<div class="msg err">提交失败: ${res.error}</div>`;
  else {
    const ok = res.filter(x=>x.ok), bad = res.filter(x=>!x.ok);
    if (ok.length) box.innerHTML += `<div class="msg ok">✓ 成功提交 ${ok.length} 条工时，已转为「已记录」。</div>`;
    bad.forEach(b => box.innerHTML += `<div class="msg err">✗ #${b.issue_id} ${b.date} ${b.hours}h — ${b.error}</div>`);
    // 成功的转入 existing（变灰卡片），从新建块移除
    ok.forEach(e => {
      DATA.existing.push({ issue_id:e.issue_id, date:e.date, hours:e.hours, subject:(DATA.all_tasks.find(t=>t.id===e.issue_id)||{}).subject||'' });
      blocks = blocks.filter(b => !(b.issue_id===e.issue_id && b.date===e.date && Math.abs((b.e-b.s)/60 - e.hours) < 0.001));
    });
    render();
  }
  btn.disabled = false; btn.textContent = '提交到 GIWA';
}

load();

// 心跳：每 3 秒告诉服务端页面还开着；关闭/离开页面时通知服务端自动退出
setInterval(() => { fetch('/api/ping').catch(()=>{}); }, 3000);
window.addEventListener('pagehide', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
window.addEventListener('beforeunload', () => { try { navigator.sendBeacon('/api/close'); } catch(e){} });
</script>
</body>
</html>
'''
