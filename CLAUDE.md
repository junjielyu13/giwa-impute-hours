# CLAUDE.md — GIWA 工作区说明

这个文件给 Claude 读。说明这个工作区是什么、怎么帮用户干活、有哪些规矩。

## 这是什么

**GIWA** 是一个基于 **Redmine** 搭建的项目管理平台（可能装了 Agile、Drive 等 RedmineUP 插件）。
实际地址放在 `.env` 的 `GIWA_URL`，不写死在代码或文档里。

本工作区的目的：通过 **Redmine REST API**，让用户用自然语言就能查询、分析、操作 GIWA 的工单/工时/项目等数据。

## 用户

- **当前用户**：从 `/users/current.json` 取（user id、登录名等以 API 返回为准）
- **权限**：通常为普通用户（`admin: false`）—— 用户管理、建项目等管理员操作多半会被拒；工单、工时、评论等日常操作有权限
- **语言**：用中文交流

## 配置

凭据存在项目根目录的 `.env`（已被 `.gitignore` 忽略，不进 git）：

```
GIWA_URL=https://<你的-redmine-地址>
GIWA_KEY=<API key>
```

API key 等同账号权限。**不要把 key 写进任何会进 git 的文件，也不要打印到对话里。** 调用时从 `.env` / 环境变量读。

## 工作方式

用户用大白话提需求，Claude 直接调 Redmine API 完成。两种模式：

1. **临时查询** —— 当场调 API 回答，不一定落进工具
2. **常用功能** —— 加进 `./giwa` CLI 工具（见下），以后一键跑

### ⚠️ 读 / 写 规矩（重要）

- 📖 **读操作**（查询、统计、导出、分析）→ 直接做
- ✍️ **写操作**（改状态/负责人/优先级、加评论、建/删工单、改工时、上传附件）
  → **先明确告诉用户「要改什么」，得到确认后才执行。** 这是用户的真实生产数据，绝不擅自修改。

## CLI 工具

`giwa.py`（Python 3 标准库，零依赖）+ `giwa` 包装脚本。子命令结构，在 `giwa.py` 的 `COMMANDS` 字典加函数即可扩展。

```bash
./giwa overview      # 全局工单总览（开/关、按项目、按状态、按负责人）
./giwa --help
```

已实现：
- `overview` — 全局工单总览
- `mine` — 我名下未关闭工单，导出带链接的 `MINE.md`，自动用 VS Code 打开
- `timesheet [--port N]` — 启动本地网页·**日历周视图**记工时（实现在 `timesheet_web.py`）。
  列=周一~周五、纵轴=时间；拖块新建工时、松手选任务，块时长换算成小时。
  顶部灰色卡片=已记录工时（读 `/time_entries.json` `from`/`to`，不可改、防重复提交）。
  提交走 POST `/time_entries.json`，活动固定 `activity_id=17` Others，备注留空自动用「tracker #id: subject」。
  注意：Redmine 工时只存日期+小时，不存时间点，时间轴仅作直观排布。
  每日可选「目标工时」：表头可填，合计显示「已填/目标」并按达标变背景色（绿/橙/红），仅提醒不强制；
  目标按具体日期存浏览器 localStorage（key=日期），每周独立不共享。
  页面有心跳（GET `/api/ping` 每 3s）+ 关闭信标（POST `/api/close`）；关掉标签页自动停服务，
  服务端 8s 心跳超时兜底退出。
  任务选择列表：除「分配给我的开放工单」外，还跨项目自动发现「内部/客户会议」Epic
  （`/issues.json?subject=~Tareas` 再正则筛 `tareas (internas|externas)`），改成友好标题
  「内部会议/客户会议」排在最前；另支持 `.env` 的 `GIWA_EXTRA_TASKS=id,id` 手动追加常驻任务。
  下拉与时间块：分组标题用完整项目名（区分名字相近的项目），时间块用项目代号 `split(" - ")[0]`。
  下拉按项目 `<optgroup>` 分组（项目按任务数排序），选项含 tracker 类型 `[Task]/[Epic]/...`。
  选择列表顶部还有两个特殊分组：`🦊 gitlab`（本周 PR/分支关联的 GIWA 任务）、`🕒 最近7天`
  （`assigned_to_id=me&status_id=*&updated_on>=7天前`，含已关闭，便于补工时）。

GitLab 集成（只读，`giwa.py` 的 `gitlab_cfg`/`gitlab_get`，配置在 `.env` 的 `GITLAB_URL`/`GITLAB_TOKEN`）：
  工时日历右下角浮动面板「📦 本周 GitLab 活动」按天列出 push（repo/分支/commit 数）与 MR；
  分支/MR 标题里的 `GIWA<编号>` 自动识别并链到 GIWA 工单，也用于生成上面的 gitlab 任务分组。
  实现：`timesheet_web.serve` 的 `gitlab_activity()` 调 `/api/v4/events?after=&before=`（按周），
  `/api/v4/projects/:id` 取 repo 名（带缓存）。token 建议只勾 read_api+read_user。严禁写操作。

规划中：`show #ID`（工单详情+评论）、`project NAME`、`due`（按截止排序）、`urgent`。

## Redmine API 速查

- **认证**：`X-Redmine-API-Key: <key>` 头，或 `?key=<key>`
- **格式**：`.json` / `.xml`
- **分页**：`limit`（最大 100）+ `offset`，大数据要循环
- **工单筛选**：`status_id`（`open`/`closed`/`*`）、`project_id`、`assigned_to_id`、`author_id`、`tracker_id`、`priority_id`、`cf_x`、`created_on`/`updated_on`（支持 `><`、范围）、`sort`
- **工单关联数据**：`?include=journals,attachments,relations,children,watchers`
- **主要资源**：issues、time_entries、projects、users、memberships、versions、wiki、attachments、issue_relations、issue_categories、groups、search、news、枚举类（statuses/trackers/priorities/roles，只读）
- **插件**：Agile、Checklists 等 RedmineUP 插件有各自独立 API（核心 API 之外），需要时单独验证
