# Distribution & Daily-Run Options

## Overview

FDE Audit has two moving parts, and it helps to keep them separate in your head:

1. **The capture app** - a lightweight, model-free Python program that each employee's machine runs during work sessions. Every 5 seconds it takes a screenshot and records a little metadata (which app, window title, timestamp), and it writes all of that to the **local disk and a local database** on that machine. There is no AI in this part. It is cheap, quiet, and it never phones home.

2. **The analysis pass** - a separate step, driven by Claude Code, that wakes up (typically once a day), samples and dedupes the day's frames so it isn't looking at 5,760 near-identical pictures, *reads* the screenshots with vision, writes structured activity logs, and ships the **derived text** - summaries, observations, per-app usage rollups - to the central PostgreSQL database.

The single most important rule of the whole system: **the raw screenshots never leave the machine.** Only the text Claude derives from them syncs to the central database.

Two design rules keep this trustworthy and maintainable:

- **Claude does not write raw SQL into the shared database.** It emits *structured output* (validated fields, not free-form database commands), and a small, deterministic command-line tool - `fde_audit ingest` - validates that output and inserts the rows. This means a misbehaving or confused AI can never corrupt or wander around the shared database; the CLI is the only thing with a key to the door.
- **The analysis logic is distributed as a versioned Claude Code slash command** - a file that lives in `.claude/commands/` (for example, `/analyze-session`) - *not* a prompt that each person copy-pastes. A pasted prompt drifts: within a month five people are running five slightly different versions and you can't compare their results. A versioned command file is one source of truth that you update once and push to everyone.

## The three distribution methods (ranked)

There are three ways to get the analysis to actually run on each person's machine every day. They differ mainly in *who or what presses the button.*

| Method | How it runs | No human needed? | Best for |
| --- | --- | --- | --- |
| 1. Manual paste | A person opens Claude Code and runs `/analyze-session` at end of day | No - depends on people remembering | A tiny first pilot (2–5 willing people) |
| **2. ⭐ Scheduled headless (PRIMARY)** | **Windows Task Scheduler runs `claude -p "/analyze-session"` on a nightly schedule** | **Yes** | **A real department rollout - this is the default choice** |
| 3. Agent SDK service | An always-on background service runs the analysis loop | Yes | Fleet-wide, centrally-managed rollout |

### Method 1 - Manual paste

At the end of the workday, the employee opens Claude Code on their own machine and runs the `/analyze-session` slash command. Claude does the sampling, reads the screenshots, and ingests the results.

This is fine for a *first* pilot with a handful of willing, engaged people (2–5). It's the simplest possible thing and it lets you prove the analysis produces useful output before you invest in automation.

Its weakness is obvious and fatal at scale: **it depends on people remembering to do it.** Miss a day and that day's data is gone or stale. Nobody should run a department this way.

### Method 2 - ⭐ Scheduled headless (RECOMMENDED / PRIMARY)

**This is the recommended approach for a real department rollout, and it should be your default.**

Instead of a person pressing the button, **Windows Task Scheduler** presses it - automatically, every night. You configure a scheduled task that runs `claude -p "/analyze-session"` at a set time (say 6 pm, or overnight when the machine is idle). That's the whole trick: it is *the exact same slash command* everyone already has, but the scheduler runs it on a timer.

A few things worth knowing:

- **It runs headless and non-interactively.** No window pops up, nobody has to be logged in and watching, nothing interrupts the employee's work. The `-p` flag tells Claude Code to run the command and exit.
- **It uses the same versioned `.claude/commands/` command** as everyone else. You maintain one command file; the scheduled task just invokes it. When you improve the analysis, you push the updated command file and every machine picks it up.
- **IT can push the scheduled task to machines centrally.** The task definition can be deployed via Group Policy, an MDM (mobile device management) tool, or even a simple one-time setup script that IT runs on each machine during onboarding. You do not have to touch each computer by hand forever.
- **Each machine's Claude Code must be logged in** so the scheduled, unattended run can authenticate. See the auth section below - this is the one prerequisite to get right.

Why this wins: it needs zero engineering beyond the setup script, it needs zero daily human effort, and it reuses machinery you already have. It is the sweet spot of robust-enough and cheap-enough for a department.

### Method 3 - Agent SDK service

The most robust option is a small, always-on background program built on the **Claude Agent SDK** (that is, Claude Code used as a *library* inside your own program, rather than called as a command-line tool). It runs the same analysis loop as a proper managed Windows service - with its own health checks, retry logic, logging, and central control.

This is the right end-state for a large, centrally-managed fleet where you want IT to monitor every machine's analysis status from one dashboard. The trade-off is that **someone has to build and maintain it** - it is real software engineering, not a scheduled command. Reach for it when you've outgrown the scheduled-CLI approach, not before.

Note the distinction: Method 3 is the **Claude Agent SDK** (Claude Code embedded in your own program). Method 2 is a plain scheduled call to the Claude Code CLI. They look similar from a distance but Method 3 is a program you own and Method 2 is a timer on a command.

## Whose Claude, whose budget? (auth & cost model)

Every nightly analysis run costs some amount of Claude usage. There are two clean ways to pay for it.

- **Each person's own Claude subscription.** The nightly analysis draws on that person's own plan and its session budget - the same budget they'd use chatting with Claude. This is elegant: it mirrors the familiar "you have a session limit, use it" model, and it costs the organization **nothing extra per seat** beyond subscriptions people may already have.
- **Org-provided API keys.** The organization issues API keys, and usage is metered per token and billed centrally. This is predictable and centrally controlled, and it doesn't require every employee to personally hold a subscription.

**Recommendation:** pilot with individual subscriptions plus the scheduled headless command - it's the fastest path with the least procurement. Move to central API keys when you standardize fleet-wide and want one predictable bill and central control.

### A rough cost note (for planning)

If you run on **metered API keys**, here's a back-of-the-envelope figure to plan with. Capturing every 5 seconds across an 8-hour day produces a lot of frames, but heavy deduplication throws away the near-identical ones before they're ever sent to the model. Doing the per-frame vision logging on the cheap model - **Claude Haiku 4.5** (roughly $1 per million input tokens, $5 per million output tokens) - lands somewhere in the range of **$8–$24 per person per work-week.** Where you fall in that range depends almost entirely on how aggressively you skip near-duplicate frames: **more dedupe = cheaper.**

Treat that as a rough planning figure, not a quote. And note that the deeper "how can this person economize their workflow?" synthesis is just a handful of cheap *text* calls at the end - it is not a cost driver; the frame-by-frame vision reading is where the money goes.

If instead you run through **each person's Claude Code subscription**, the marginal cost is effectively **$0** - it's absorbed by the subscription they already have.

## Image retention (do we keep the screenshots after analysis?)

Once a frame has been analyzed and its structured summary is safely in the central database, **the raw image itself is optional.** You've already extracted everything you needed from it.

- **Default policy: a short, configurable local retention window** (default **14 days**), after which the system auto-prunes. Pruning means it deletes the image file, nulls out the stored path, and **keeps the metadata and the summary forever.** You retain the insight; you discard the picture.
- **Maximum-privacy option: delete on analysis.** Deleting each image the moment it's been analyzed is a one-line configuration change - minimum disk usage, minimum privacy exposure.

For an organization-wide rollout, a short retention window or delete-on-analyze is the privacy-friendly default and the one to reach for. And regardless of the setting: **the raw images never leave the machine.**

## Central database reachability

There is one central **PostgreSQL** instance that all department machines can reach - over the corporate network or VPN. Each machine holds the connection string, and its local `fde_audit ingest` CLI is what writes the derived rows into it.

To be explicit about what crosses the wire: **only summaries, observations, and per-app usage rollups go to the central database - never raw images.** The pictures stay on the machine that took them.

## Consent & policy (do not skip)

This is a monitoring tool, so treat the human and legal side as a real requirement, not paperwork to add later. Screen monitoring of employees **almost always requires a written policy and clear notice to staff**, and in some regions it requires explicit consent or works-council / employee-representative approval before you switch anything on (GDPR jurisdictions are a common example).

Build in **a visible on/pause control and a clear indicator that capture is running, from day one.** People should be able to see when it's on and stop it. Doing this up front is far cheaper and far less painful than retrofitting it after a complaint or an audit.

## Recommended rollout path

1. **Pilot.** 2–5 volunteers running `/analyze-session` manually, on their own individual Claude subscriptions. Prove the analysis is useful.
2. **Department.** Move to **scheduled headless** runs: `/analyze-session` fired nightly by Windows Task Scheduler on each machine, using the versioned `.claude/commands/` command, with `fde_audit ingest` writing to the central PostgreSQL database, a 14-day (or shorter) local image retention window, and a written monitoring policy in place before you flip it on.
3. **Fleet-wide / robust.** Graduate to the **Agent SDK service** plus **central API keys** if and when you outgrow the scheduled-CLI approach and want centralized monitoring and billing.
