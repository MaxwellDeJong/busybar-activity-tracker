---
name: backfill-activity-log
description: Add sessions the Busy Bar never recorded (for example, "I forgot to start the timer") to data/activity_log.jsonl. The skill converts times from the user's time zone, checks that entries don't overlap existing ones or fall in the future, and merges them in safely with merge_activity_logs.py while the recorder is stopped. Use it whenever the user wants to log, add, backfill, or fix a missed or forgotten activity in the busybar tracker, even if they only say something like "I exercised Monday 8–10pm but didn't track it". Don't use it to change or delete sessions the bar actually recorded.
---

# Backfill missed sessions into the activity log

The recorder only ever adds lines to the end of `data/activity_log.jsonl`. A backfilled session belongs somewhere in the middle, so never edit the file directly. Build the missing records in a separate file, then fold them in with `backend/merge_activity_logs.py`. That tool:

- sorts the records into place by time,
- saves a read-only copy of the old log to `data/archive/`,
- swaps in the new file all at once, so the dashboard never reads a half-written log.

`data/` is owned by root because the containers write it. Run the merge **inside the recorder image** so you don't need `sudo` on the host.

## 1. Pin down each session

For each session, get the activity, the date, the start and end times, and the time zone. Activity keys come from `config/activity_card_id_map.json`: exercise, chores, rec_reading, tech_reading, work, development, perfect_form. Map what the user says onto a key, and ask if it's ambiguous.

Users often get dates or time zones slightly wrong. That happened in the session this skill was built from:
- "Wednesday" was really Thursday. The Wednesday time overlapped a session the bar had recorded.
- "4:30 PM PT" was really 4:30 PM ET. The PT time was still in the future.

The script below catches both kinds of mistake. When it reports a problem, **tell the user what the conflict is and ask what they meant**. Don't guess a correction. When you ask, suggest the most likely fix (for example, "did you mean ET? That fits a gap"), and wait for their answer.

When the user gives both a weekday and a date, check that they match. The script prints the weekday for each entry.

The default time zone is `America/New_York`, the compose `TZ`. If the user names a different zone, pass it on that entry.

## 2. Build and validate the records

```bash
S=<scratchpad dir>
python3 .claude/skills/backfill-activity-log/scripts/build_backfill.py --out "$S/backfill.jsonl" \
  --entry exercise 2026-09-21T20:00 2026-09-21T21:52 \
  --entry exercise 2026-09-25T16:30 2026-09-25T16:54 America/Los_Angeles
```

The script only reads the live log. It writes the side file only if every entry passes these checks:
- the activity is a known key,
- the end is after the start and isn't in the future,
- the entry doesn't overlap any logged session, the recorder's currently open session, or another entry,
- no existing record has the same start, card, phase, and interval number.

If it prints `REFUSED`, nothing was written. Take the problems back to the user, as described in step 1.

Records follow the recorder's own format exactly. Don't add extra fields such as `"manual": true` unless the user asks for them. Mention that a backfilled record looks identical to one the bar recorded.
- **Single-timer activities** (exercise, chores, reading): `phase: "focus"`, `interval_index: null`.
- **Work/rest interval activities** (work, development, perfect_form): one `phase: "work"` record with `interval_index: 0` and `overtime_s: 0.0`. If the user describes breaks inside the session, ask whether they want separate work and rest records. By default, add one work record.

Before touching the service, show the user a short table of what you'll add: local time, UTC start → end, and duration.

## 3. Merge with the recorder stopped

Stop the recorder so no new line can be added between the merge reading the log and swapping it. While it's stopped, the broker holds the bar's updates and delivers them when it restarts.

First, check `data/recorder_state.json`. If `open` is not null, a session is running. The recorder resumes it only if it's down for less than `RESUME_MAX_GAP_S` (900 s by default). Beyond that, the session is closed and marked truncated. Tell the user, keep the stop short, and don't rely on their memory of whether a timer is running; the state file is the source of truth.

Stopping and starting the service changes a running system, so get the user's go-ahead first unless they've already given it.

Run the whole stop → dry run → merge → restart sequence as one command, so the recorder is only down for a few seconds:

```bash
.claude/skills/backfill-activity-log/scripts/merge_backfill.sh "$S/backfill.jsonl"
```

The script:
- stops the recorder, but only if it was running,
- does a dry run,
- applies the merge only if the dry run says exactly `+N new, 0 already present` (N = records in the side file) and none of the new records appears in a merge warning,
- restarts the recorder on every exit path, including failed checks and errors, so a problem can't leave the recorder down long enough to lose an open session,
- checks that the log grew by N and prints the recorder's last few log lines.

Add `--dry-run-only` to stop after a clean dry run without applying anything. The recorder still stops and restarts.

If the script prints `!!`, nothing was applied, unless the message is about verifying after the merge. Show the user the output. A new record in a warning means it conflicts with something the checker missed, so go back to step 1. Warnings about older, unrelated records don't block the merge; for example, a known pair of chores records on 2026-07-29 overlaps. Report those, but don't fix them here.

If the script can't run, the manual steps are in the README, under merging with `merge_activity_logs.py`. Follow the same order, and restart the recorder right away.

## 4. Verify and report

- Check the script's `== verify` section:
  - log lines went up by N,
  - each new record appears at its line,
  - the recorder log shows either `resuming open … session (down Ns)` or `resumed state … (no session was open)`, followed by the MQTT reconnect.
- Tell the user:
  - what was added,
  - where the backup copy is (`data/archive/...pre-merge.jsonl`),
  - that the recorder is running again, and whether an open session resumed,
  - any older overlap warnings the merge reported.

If the user wants to confirm the recorder is live, have them start a timer, then check `data/recorder_state.json`. `open` should show that activity with a `start_ms` from the last minute or so. A session is added to the log only when it *ends*, so a running timer won't show up in `activity_log.jsonl` yet.
