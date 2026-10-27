# nexora-scanner (cloud copy of the Nexora desk scanner)

Runs the desk scanner on GitHub Actions **Mon–Fri 08:00–23:00 Europe/Rome**, so scanner alerts still reach the desk
webhook when the desk machine is asleep. It only reads **public Kraken Futures market data**. It has **no Kraken keys and
places no orders**. Each alert only wakes the desk, which re-checks everything before any trade.

## Secrets to add (exactly 2)

Repository → Settings → Secrets and variables → Actions → **New repository secret**:

| Name | Value |
|---|---|
| `DESK_HEAD_WEBHOOK_URL` | the desk routine webhook URL (must be `https://`) |
| `DESK_HEAD_WEBHOOK_KEY` | the desk routine webhook key |

The workflow reads them only as `${{ secrets.* }}` in the scan step. They are never printed or written to a file.
(GitHub also masks them in logs and does not pass them to workflows triggered from forks.)

Optional repository **variables** (not secrets, not required):
- `DESK_EQUITY`: equity used only for the scanner's size-feasibility check. If unset, the scanner's built-in fallback
  is used. The desk recomputes size anyway.
- `DESK_ENTRIES_SUSPENDED`: set to `1` to stop cloud alerts without disabling the workflow.

## What is inside

| Path | What |
|---|---|
| `desk/scanner.py`, `features.py`, `setup_types.py`, `friday.py`, `whitelist.txt` | byte-identical to the desk copy (FILTER_VER 2026-10-27a) |
| `desk/head_alert.py` | identical except 1 line: trade-log dir points to an empty folder (no trade logs in this repo) |
| `desk/desklib.py` | identical except 2 lines: data dir = this folder, optional User-Agent override (`DESK_HTTP_UA`) |
| `cloud/cloud_scan.py` | one scan: runs the unchanged scanner, cloud gate, cloud sender, equity hidden in logs |
| `cloud/cloud_loop.py` | one block: scans every 5-minute slot (+20 s, same slots as the desk) until the block end |
| `tests/` | the desk's head_alert / filter / Type B stop tests + cloud tests (`python tests/run_all.py`) |
| `.github/workflows/cloud_scanner.yml` | schedule, concurrency, cache, secrets |

`tests/test_cloud.py` checks the bundled files against `tests/UPSTREAM_box_sha256.txt` (hashes of the desk copy), so any
accidental change to the trading logic fails the self-test that runs before every block.

## Schedule (Europe/Rome, the cron entries use `timezone: "Europe/Rome"`, so CET/CEST is automatic)

| Block | Scans | Primary start | Catch-ups |
|---|---|---|---|
| 1 | 08:00–13:00 | 07:55 | 08:20, 09:05 |
| 2 | 13:00–18:00 | 12:55 | 13:20, 14:05 |
| 3 | 18:00–23:00 | 17:55 | 18:20, 19:05 |

GitHub can start scheduled runs late or drop them. `cloud_loop.py` therefore computes the end of its block from the actual
start time in Europe/Rome and never runs more than 340 minutes (the GitHub limit is 360). The `concurrency` group keeps
exactly one job running: a start that arrives during a block waits (a newer waiting start replaces an older one), then
covers the next block, or exits within about a minute when it is past 23:00.

Last scan of the day: 22:55:20 Rome. Weekends: nothing runs.

## What the cloud copy does differently from the desk (alerts only)

- Gate: the desk gate needs the live account (daily −4% / +1.6% stops, suspension, open Type B trades). The cloud does
  not have it, so it checks desk hours only. **The desk re-applies those gates when a `source: "cloud"` alert arrives.**
- Payload: the desk payload plus `"source": "cloud"` and `"filter_ver"`.
- Dedup: at most one alert per SYMBOL + direction per Rome day (ledger kept across blocks in the Actions cache). The
  desk's own 2-hour dedup and every filter still apply unchanged.

## First run

Actions → Cloud scanner → Run workflow with `dry_run = 1` and `max_scans = 2` during desk hours. The log shows one line
per scan (`[DRY-RUN] ... head_alert=... universe=.. hits=.. eligible=..`) and nothing is sent. Then let the schedule run.

## Notes

- Public repo: standard GitHub-hosted runner minutes are free. About 15 h of runtime per weekday.
- GitHub disables scheduled workflows in public repos after **60 days without repository activity**. Push any small
  commit (or re-enable the workflow) before then.
- Logs of a public repo are public: the scripts print no equity, no secrets and no account data.
