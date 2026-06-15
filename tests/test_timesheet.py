#!/usr/bin/env python3
"""Playwright UI test + screenshot generator for the timesheet web page.

It mocks every /api/* response with fake, non-sensitive data, so it needs no
Redmine server, runs deterministically in CI, and the screenshot it produces
(docs/timesheet.png) leaks nothing real.

Setup (dev-only; the tool itself stays zero-dependency):
    python3 -m venv .venv && source .venv/bin/activate
    pip install playwright && playwright install chromium

Run:
    python tests/test_timesheet.py            # asserts + writes docs/timesheet.png
"""

import datetime
import json
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright, expect

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from timesheet_web import HTML_PAGE  # noqa: E402

PXH, START_H = 44, 7  # must match the constants in the page

# --- Fake week + data (nothing real) ---------------------------------------
_today = datetime.date.today()
_monday = _today - datetime.timedelta(days=_today.weekday())
DAYS = [(_monday + datetime.timedelta(days=i)).isoformat() for i in range(5)]

MOCK_INIT = {
    "base": "https://redmine.example.com",
    "week_offset": 0,
    "week_start": DAYS[0],
    "week_end": DAYS[4],
    "week_num": _monday.isocalendar()[1],
    "days": [{"date": d} for d in DAYS],
    "all_tasks": [
        {"id": 101, "subject": "Fix login crash on Android", "tracker": "Incidencia",
         "project": "Mobile App", "projcode": "Mobile App", "status": "En curso"},
        {"id": 102, "subject": "Implement dark mode", "tracker": "Task",
         "project": "Mobile App", "projcode": "Mobile App", "status": "Nueva"},
        {"id": 103, "subject": "Internal meeting", "tracker": "Epic", "label": "Internal meeting",
         "project": "Team", "projcode": "Team", "status": "En curso"},
        {"id": 104, "subject": "Landing page redesign", "tracker": "Task",
         "project": "Website", "projcode": "Website", "status": "Nueva"},
    ],
    "recent": [
        {"id": 201, "subject": "Update dependencies", "tracker": "Task",
         "project": "Mobile App", "projcode": "Mobile App", "status": "Resuelta", "updated": DAYS[1]},
    ],
    "gitlab_tasks": [
        {"id": 101, "subject": "Fix login crash on Android", "tracker": "Incidencia",
         "project": "Mobile App", "projcode": "Mobile App", "status": "En curso"},
    ],
    "gitlab": {
        "enabled": True,
        "days": DAYS,
        "byday": {
            DAYS[0]: [{"type": "push", "repo": "acme/mobile-app", "branch": "feature/GIWA101",
                       "count": 12, "title": "fix: handle null auth token",
                       "repo_url": "https://gitlab.example.com/acme/mobile-app",
                       "branch_url": "https://gitlab.example.com/acme/mobile-app/-/tree/feature/GIWA101"}],
            DAYS[2]: [{"type": "mr", "repo": "acme/mobile-app", "action": "opened",
                       "title": "Add dark mode support", "repo_url": "https://gitlab.example.com/acme/mobile-app",
                       "url": "https://gitlab.example.com/acme/mobile-app/-/merge_requests/42"}],
        },
    },
    "existing": [
        {"issue_id": 102, "date": DAYS[0], "hours": 2.5, "subject": "Implement dark mode", "projcode": "Mobile App"},
        {"issue_id": 104, "date": DAYS[1], "hours": 4, "subject": "Landing page redesign", "projcode": "Website"},
    ],
}
MOCK_ISSUE = {"id": 27509, "subject": "Manually entered task", "tracker": "Task",
              "project": "Mobile App", "projcode": "Mobile App", "status": "Nueva"}


def _install_routes(page):
    page.route("https://demo.local/", lambda r: r.fulfill(content_type="text/html", body=HTML_PAGE))
    page.route(re.compile(r".*/api/init.*"),
               lambda r: r.fulfill(content_type="application/json", body=json.dumps(MOCK_INIT)))
    page.route(re.compile(r".*/api/issue.*"),
               lambda r: r.fulfill(content_type="application/json", body=json.dumps(MOCK_ISSUE)))
    page.route(re.compile(r".*/api/(ping|close|submit).*"),
               lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))


def _min_to_y(m):
    return (m - START_H * 60) / 60 * PXH


def main():
    out = ROOT / "docs" / "timesheet.png"
    out.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 980}, locale="en-US")
        _install_routes(page)
        page.goto("https://demo.local/")

        # 1) Calendar renders: 5 day columns + already-logged chips
        page.wait_for_selector(".dayhead")
        assert page.locator(".dayhead").count() == 5, "expected 5 weekday columns"
        assert page.locator(".allday .chip").count() == 2, "expected 2 already-logged chips"
        expect(page.locator("#title")).to_have_text("GIWA Time Calendar")

        # 2) GitLab panel shows clickable links into GitLab
        assert page.locator(".gp-item a.repo").count() >= 1, "repo link missing"
        assert page.locator('.gp-item a[href*="/merge_requests/"]').count() >= 1, "MR link missing"

        # 3) Drag on Wednesday's timeline to create a block, pick a task
        grid = page.locator(f"#grid-{DAYS[2]}")
        box = grid.bounding_box()
        x = box["x"] + box["width"] / 2
        page.mouse.move(x, box["y"] + _min_to_y(10 * 60))     # 10:00
        page.mouse.down()
        page.mouse.move(x, box["y"] + _min_to_y(13 * 60), steps=8)  # 13:00
        page.mouse.up()
        expect(page.locator("#popup")).to_be_visible()
        # manual-ID option exists
        assert page.locator('#popupTask option[value="__manual__"]').count() == 1, "manual-id option missing"
        page.select_option("#popupTask", "101")
        page.click("#popup .btn-primary")
        expect(page.locator("#popup")).to_be_hidden()
        assert page.locator(".grid .block:not(.preview)").count() == 1, "created block not rendered"

        # ---- screenshot for the README (English, with a freshly created block) ----
        # Un-stick the footer so the full-page capture doesn't overlap the stats rows.
        page.evaluate("document.querySelector('footer').style.position = 'static'")
        page.screenshot(path=str(out), full_page=True)
        print(f"screenshot written: {out.relative_to(ROOT)}")

        # 4) Language switch works (do this after the screenshot so the image stays English)
        page.select_option("#langSel", "zh")
        expect(page.locator("#title")).to_have_text("GIWA 工时日历")
        page.select_option("#langSel", "es")
        expect(page.locator("#title")).to_have_text("Calendario de horas GIWA")

        browser.close()
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
