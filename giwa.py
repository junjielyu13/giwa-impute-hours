#!/usr/bin/env python3
"""GIWA (Redmine) 一键查询工具 — 零依赖，仅用 Python 标准库。

用法:
    ./giwa overview      # 全局工单总览（开/关、按项目、按状态、按负责人）

配置:
    在项目根目录的 .env 文件中设置:
        GIWA_URL=https://你的-redmine-地址
        GIWA_KEY=你的_api_key
"""

import json
import os
import sys
import urllib.request
import urllib.error
from collections import Counter

# ---------- 终端颜色 ----------
_TTY = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if _TTY else str(text)


def bold(t):  return c(t, "1")
def dim(t):   return c(t, "2")
def red(t):   return c(t, "31")
def green(t): return c(t, "32")
def yellow(t):return c(t, "33")
def cyan(t):  return c(t, "36")


# ---------- 配置加载 ----------
def _env_cfg():
    """读取项目目录 .env，环境变量优先。"""
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
            "缺少配置。请在 .env 文件里设置 GIWA_URL 和 GIWA_KEY。\n"
            "  可以复制 .env.example 为 .env 后填入你的 API key。"
        )
    return url.rstrip("/"), key


def extra_task_ids():
    """常驻任务（内部/客户开会等，未必分配给我，但要出现在工时选择列表里）。"""
    val = _env_cfg().get("GIWA_EXTRA_TASKS", "")
    return [int(x) for x in val.replace(" ", "").split(",") if x.strip().isdigit()]


def gitlab_cfg():
    c = _env_cfg()
    return c.get("GITLAB_URL", "").rstrip("/"), c.get("GITLAB_TOKEN", "")


def gitlab_get(gurl, gtok, path):
    """GitLab 只读 GET（/api/v4 前缀，PRIVATE-TOKEN 头）。"""
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
            die("认证失败：API key 无效或没有权限。请检查 .env 里的 GIWA_KEY。")
        die(f"请求出错 (HTTP {e.code}): {path}")
    except urllib.error.URLError as e:
        die(f"无法连接 GIWA ({url})：{e.reason}")
    except TimeoutError:
        die("请求超时，请稍后重试或检查网络。")


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


def open_in_code(path, hint=""):
    import shutil, subprocess
    if shutil.which("code"):
        subprocess.run(["code", path])
        print(dim(f"   已用 VS Code 打开。{hint}"))
    else:
        print(dim(f"   未找到 code 命令，请手动打开：{path}"))


def fetch_all_issues(url, key):
    """分页抓取全部工单（含已关闭）。"""
    issues = []
    offset = 0
    while True:
        d = api_get(url, key, f"/issues.json?status_id=*&limit=100&offset={offset}")
        issues += d["issues"]
        total = d["total_count"]
        offset += 100
        sys.stderr.write(f"\r  抓取中… {min(offset, total)}/{total}")
        sys.stderr.flush()
        if offset >= total:
            break
    sys.stderr.write("\r" + " " * 40 + "\r")
    sys.stderr.flush()
    return issues


# ---------- 输出辅助 ----------
def table(rows, indent="  "):
    """rows: [(count, label)] -> 对齐打印。"""
    if not rows:
        print(indent + dim("(无)"))
        return
    w = max(len(str(r[0])) for r in rows)
    for count, label in rows:
        print(f"{indent}{cyan(str(count).rjust(w))}  {label}")


def section(title):
    print("\n" + bold(title))


# ---------- 命令: overview ----------
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
        a = (i.get("assigned_to") or {}).get("name", "(未分配)")
        by_assignee[a] += 1
        if i["status"].get("is_closed"):
            open_closed["closed"] += 1
        else:
            open_closed["open"] += 1

    print("\n" + bold(f"📊 工单总览 — 共 {total} 个"))

    section("开 / 关")
    table([
        (green(open_closed["open"]), green("进行中")),
        (dim(open_closed["closed"]), dim("已关闭")),
    ])

    section("📁 按项目")
    table([(v, k) for k, v in by_proj.most_common()])

    section("🏷️  按状态")
    table([
        (v, (dim(k) if status_is_closed.get(k) else yellow(k)))
        for k, v in by_status.most_common()
    ])

    section("👥 按负责人 (Top 15)")
    table([(v, k) for k, v in by_assignee.most_common(15)])
    print()


# ---------- 命令: mine ----------
# 待处理状态优先级（越小越靠前），Resuelta 等已解决的排最后
_STATUS_ORDER = {
    "Crítica": 0, "Urgente": 0,  # (兜底，正常用 priority)
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

    # 分「待处理」与「已解决待关闭(Resuelta/Passed)」
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
    lines.append("# 我的工单 (open)\n")
    lines.append(f"> 来源：{url} · 共 **{len(issues)}** 个（待处理 {len(pending)} · 已解决待关闭 {len(resolved)}）\n")

    def render_group(title, items):
        lines.append(f"\n## {title} ({len(items)})\n")
        by_proj = defaultdict(list)
        for i in items:
            by_proj[i["project"]["name"]].append(i)
        for proj in sorted(by_proj, key=lambda p: -len(by_proj[p])):
            lines.append(f"\n### {proj}\n")
            lines.append("| 工单 | 状态 | 优先级 | 截止 | 标题 |")
            lines.append("|---|---|---|---|---|")
            for i in sorted(by_proj[proj], key=sort_key):
                due = i.get("due_date") or ""
                subj = i["subject"].replace("|", "\\|")
                lines.append(
                    f"| {issue_link(i)} | {i['status']['name']} | {i['priority']['name']} | {due} | {subj} |"
                )

    if pending:
        render_group("🔧 待处理", pending)
    if resolved:
        render_group("✅ 已解决待关闭", resolved)

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.join(here, "MINE.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(bold(f"📝 已导出 {len(issues)} 个工单 → ") + cyan(out_path))
    print(f"   {green('待处理 ' + str(len(pending)))} · {dim('已解决待关闭 ' + str(len(resolved)))}")

    open_in_code(out_path, "按 Cmd+Shift+V 预览，链接可点击。")


# ---------- 命令: timesheet ----------
def cmd_timesheet(url, key, rest=None):
    """启动本地网页·日历周视图，拖块记工时。见 timesheet_web.py。"""
    rest = rest or []
    port = 8765
    if "--port" in rest:
        try:
            port = int(rest[rest.index("--port") + 1])
        except (IndexError, ValueError):
            die("--port 后面要跟端口号，例如 ./giwa timesheet --port 8790")
    import timesheet_web
    gurl, gtok = gitlab_cfg()
    try:
        timesheet_web.serve(url, key, api_get, api_post, port, extra_ids=extra_task_ids(),
                            gitlab_url=gurl, gitlab_token=gtok, gitlab_get=gitlab_get)
    except RuntimeError as e:
        die(str(e))


# ---------- 入口 ----------
COMMANDS = {
    "overview": cmd_overview,
    "mine": cmd_mine,
    "timesheet": cmd_timesheet,
}


def usage():
    print("GIWA 一键查询工具\n")
    print("用法: ./giwa <命令>\n")
    print("命令:")
    print("  overview              全局工单总览（开/关、按项目、按状态、按负责人）")
    print("  mine                  我名下未关闭的工单，导出带链接的 MINE.md")
    print("  timesheet [--port N]  打开本地网页·日历周视图，拖块记工时并提交到 GIWA")
    print()


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        sys.exit(0)
    cmd = args[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        die(f"未知命令: {cmd}\n  运行 ./giwa --help 查看可用命令。")
    url, key = load_env()
    fn(url, key, args[1:])


if __name__ == "__main__":
    main()
