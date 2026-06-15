# AGENTS.md — GIWA Workspace Guide

This file is for AI coding agents to read. It explains what this workspace is, how to help the user get work done, and what the rules are. (Claude Code reads `CLAUDE.md`, which is a symlink to this file.)

## What this is

**GIWA** is a project-management platform built on **Redmine** (it may have RedmineUP plugins such as Agile, Drive, etc. installed).
The actual address lives in `GIWA_URL` in `.env`; it is not hard-coded in the code or docs.

The purpose of this workspace: via the **Redmine REST API**, let the user query, analyze, and operate on GIWA's data (issues / time entries / projects, etc.) using natural language.

## User

- **Current user**: taken from `/users/current.json` (user id, login, etc. as returned by the API)
- **Permissions**: usually a regular user (`admin: false`) — admin operations such as user management and creating projects will mostly be rejected; day-to-day operations on issues, time entries, comments, etc. are permitted
- **Language**: communicate in English

## Configuration

Credentials are stored in `.env` in the project root (already ignored by `.gitignore`, not committed to git):

```
GIWA_URL=https://<your-redmine-host>
GIWA_KEY=<API key>
```

The API key is equivalent to account permissions. **Do not write the key into any file that will be committed to git, and do not print it into the conversation.** Read it from `.env` / environment variables when making calls.

## How to work

The user states needs in plain language, and the agent calls the Redmine API directly to fulfill them. Two modes:

1. **Ad-hoc query** — call the API on the spot to answer; doesn't necessarily get baked into a tool
2. **Common feature** — add it to the `./giwa` CLI tool (see below) so it can be run with a single command later

### ⚠️ Read / Write rules (important)

- 📖 **Read operations** (query, statistics, export, analysis) → do them directly
- ✍️ **Write operations** (changing status / assignee / priority, adding comments, creating/deleting issues, editing time entries, uploading attachments)
  → **First clearly tell the user "what is going to change", and only execute after getting confirmation.** This is the user's real production data; never modify it without authorization.

## CLI tool

`giwa.py` (Python 3 standard library, zero dependencies) + the `giwa` wrapper script. Subcommand structure; extend it by adding a function to the `COMMANDS` dict in `giwa.py`.

```bash
./giwa overview      # Global issue overview (open/closed, by project, by status, by assignee)
./giwa --help
```

Already implemented:
- `overview` — global issue overview
- `mine` — open issues assigned to me; exports a linked `MINE.md` and opens it automatically in VS Code
- `timesheet [--port N]` — start a local web page · **calendar week view** for logging time (implemented in `timesheet_web.py`).
  Columns = Monday–Friday, vertical axis = time; drag a block to create a time entry, release to pick a task, the block's duration converts to hours.
  Gray cards at the top = already-recorded time entries (read from `/time_entries.json` `from`/`to`, not editable, prevents duplicate submission).
  Submission goes through POST `/time_entries.json`, the activity is fixed at `activity_id=17` Others, and an empty comment auto-fills with "tracker #id: subject".
  Note: Redmine time entries only store date + hours, not a point in time; the time axis is just for intuitive layout.
  Optional daily "target hours": fillable in the header; the total shows "logged/target" and changes background color by attainment (green/orange/red) — a reminder only, not enforced;
  targets are stored per specific date in browser localStorage (key = date), independent per week and not shared.
  The page has a heartbeat (GET `/api/ping` every 3s) + a close beacon (POST `/api/close`); closing the tab automatically stops the service,
  with an 8s server-side heartbeat timeout as a fallback to exit.
  The web UI supports 4 languages (English default, Chinese, Spanish, Catalan), with browser auto-detection and a toggle button.
  Task selection list: besides "open issues assigned to me", it also auto-discovers "internal/client meeting" Epics across projects
  (`/issues.json?subject=~Tareas`, then regex-filtered by `tareas (internas|externas)`), renamed to friendly titles
  "Internal Meeting / Client Meeting" pinned at the top; it also supports `GIWA_EXTRA_TASKS=id,id` in `.env` to manually add persistent tasks.
  The dropdown also has a "✏️ Enter a GIWA ID manually…" option: if the task isn't listed, pick it to reveal a number input; the id is validated/enriched via `/api/issue?id=N` before the block is added.
  Dropdown and time blocks: group headers use the full project name (to distinguish similarly named projects), time blocks use the project code `split(" - ")[0]`.
  The dropdown groups by project via `<optgroup>` (projects sorted by task count), and options include the tracker type `[Task]/[Epic]/...`.
  At the top of the selection list there are also two special groups: `🦊 gitlab` (GIWA tasks linked to this week's PRs/branches) and `🕒 this week`
  (issues you worked on during the *selected* week — `assigned_to_id=me&status_id=*&updated_on=><weekStart|weekEnd`, including closed ones, for catching up on time entries).

GitLab integration (read-only, `gitlab_cfg`/`gitlab_get` in `giwa.py`, configured via `GITLAB_URL`/`GITLAB_TOKEN` in `.env`):
  the floating panel "📦 This week's GitLab activity" at the bottom-right of the time calendar lists pushes (repo/branch/commit count) and MRs by day;
  repo / branch / MR are clickable links into GitLab (the server builds `repo_url`, `branch_url`, and the MR `url` from `target_iid`);
  `GIWA<number>` in branch/MR titles is auto-detected and linked to the GIWA issue, and is also used to generate the gitlab task group above.
  Implementation: `gitlab_activity()` in `timesheet_web.serve` calls `/api/v4/events?after=&before=` (by week),
  and `/api/v4/projects/:id` to get the repo name (with caching). The token should ideally only have read_api+read_user. Write operations are strictly forbidden.

Planned: `show #ID` (issue details + comments), `project NAME`, `due` (sorted by due date), `urgent`.

## Tests

`tests/test_timesheet.py` is a Playwright test for the web UI. It mocks every `/api/*` response (no Redmine server / API key needed) by route-interception, asserts the main behaviours (render, drag-to-create + popup, manual GIWA-ID option, GitLab links, language switcher), and regenerates `docs/timesheet.png` (the README screenshot) against mock data. Playwright is a dev-only dependency (`pip install playwright && playwright install chromium`); the tool itself stays zero-dependency. Keep the screenshot's data fake — never point it at the real instance, since the repo is public.

## Redmine API quick reference

- **Authentication**: `X-Redmine-API-Key: <key>` header, or `?key=<key>`
- **Format**: `.json` / `.xml`
- **Pagination**: `limit` (max 100) + `offset`; loop for large datasets
- **Issue filters**: `status_id` (`open`/`closed`/`*`), `project_id`, `assigned_to_id`, `author_id`, `tracker_id`, `priority_id`, `cf_x`, `created_on`/`updated_on` (supports `><` and ranges), `sort`
- **Issue associated data**: `?include=journals,attachments,relations,children,watchers`
- **Main resources**: issues, time_entries, projects, users, memberships, versions, wiki, attachments, issue_relations, issue_categories, groups, search, news, enumerations (statuses/trackers/priorities/roles, read-only)
- **Plugins**: RedmineUP plugins such as Agile, Checklists, etc. have their own separate APIs (outside the core API); verify separately when needed
