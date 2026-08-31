# PayLess Mobile — Bitly Metrics Report

Pulls Bitly link metrics from the Bitly REST API v4 — group-level rollups plus
per-link drill-downs for a configured set of Bitlinks — generates embedded PNG
charts and summary cards, and sends an HTML email with an interactive Chart.js
attachment via Amazon SES.

---

## Files

| File | Purpose |
|---|---|
| `bitly_report.py` | Main script — fetches data, builds charts, sends email |
| `bitlink_map.json` | Maps specific Bitlinks → friendly names; only links listed here get their own drill-down section |
| `.env` | Configuration (tokens, recipients, lookback window) — not committed |
| `.env.example` | Template for `.env` |
| `requirements.txt` | Python dependencies |

---

## Setup

### 1. Install dependencies

```powershell
.venv\Scripts\pip.exe install -r requirements.txt
```

### 2. Get a Bitly access token

Generate a generic access token at [app.bitly.com/settings/api](https://app.bitly.com/settings/api/).
This works for a single account without OAuth.

### 3. Configure AWS credentials (for SES)

Add your IAM credentials to `C:\Users\James\.aws\credentials`:

```ini
[default]
aws_access_key_id     = YOUR_KEY
aws_secret_access_key = YOUR_SECRET
region                = us-east-1
```

Or set `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` directly in `.env`.

### 4. Configure `.env`

```powershell
copy .env.example .env
```

Fill in `BITLY_ACCESS_TOKEN`. `BITLY_GROUP_GUID` can be left blank if the
account has only one group — the script will detect and use it automatically
(and print the guid so you can pin it going forward). `TO_ADDRESSES` is
comma-separated; `TIMEZONE` accepts any IANA timezone name.

### 5. Configure per-link drill-downs (optional)

Edit `bitlink_map.json` to list the Bitlinks that should get their own
countries/devices/clicks breakdown section, e.g.:

```json
{
  "bit.ly/3abcXYZ": "Homepage Campaign",
  "bit.ly/4defUVW": "Spring Promo SMS Link"
}
```

Bitlinks not listed here still appear in the "Top Links by Clicks" table but
don't get a dedicated section. The file is optional — if missing or empty,
drill-down sections are simply omitted.

---

## Required Bitly Scopes

The access token needs read access to the group's metrics endpoints (clicks,
countries, devices, referrers, referring networks, shorten counts, QR scans,
top links). Country/city/device breakdowns are gated to paid Bitly plans —
if your plan doesn't include one of these, the script logs a warning and
skips that section rather than failing the whole report.

---

## Running Manually

```powershell
.venv\Scripts\python.exe bitly_report.py
```

Useful flags:

```powershell
# Save the report locally instead of emailing it (good for testing)
.venv\Scripts\python.exe bitly_report.py --preview

# Print raw Bitly API responses (handy if a section looks off — response
# field names aren't fully pinned down in Bitly's public docs)
.venv\Scripts\python.exe bitly_report.py --preview --debug

# Override the lookback window for this run
.venv\Scripts\python.exe bitly_report.py --days 7

# Override recipients for this run
.venv\Scripts\python.exe bitly_report.py --to ops@example.com ceo@example.com
```

If `FROM_ADDRESS` or `TO_ADDRESSES` are blank in `.env`, the script
automatically falls back to saving the interactive report and the email body
locally instead of sending — no flag needed.

---

## Scheduling (Windows Task Scheduler)

To run the report automatically, e.g. every Monday at 7:00 AM:

1. Open **Task Scheduler** → **Create Basic Task**
2. Set the trigger to **Weekly**, Monday, **7:00 AM**
3. Set the action to **Start a Program**:
   - **Program:** `D:\projects\reporting\Bitly\.venv\Scripts\python.exe`
   - **Arguments:** `bitly_report.py`
   - **Start in:** `D:\projects\reporting\Bitly`
4. Under **General**, check **Run whether user is logged on or not**

On a Linux server with a venv at `/opt/bitlyenv`:

```cron
0 7 * * 1 /opt/bitlyenv/bin/python /opt/reporting/bitly_report.py >> /var/log/bitly_report.log 2>&1
```

---

## Email Output

**HTML email body**
- Gradient header with report title and date range
- Summary cards: Total Clicks, New Links Created, Bitlinks in Group, Top Country, Top Referrer, QR Code Scans (if any)
- Line charts: Clicks Over Time, New Links Created, QR Code Scans
- Donut/bar charts: Top Countries, Devices, Referrers, Referring Networks
- Top Links by Clicks table
- One drill-down section per Bitlink configured in `bitlink_map.json` — mini clicks chart plus top country/device
- All charts and dates are rendered in the configured `TIMEZONE`

**Interactive HTML attachment** (`PayLess_Bitly_YYYYMMDD.html`)
- Self-contained Chart.js file — open locally in any browser, no internet required after download except the one CDN `<script>` tag
- Hover tooltips on every chart
- Same summary cards and metrics as the email, plus a full drill-down section per configured Bitlink

---

## Notes

- **Graceful degradation:** every Bitly API call is wrapped so a 403 (plan-tier gating), missing scope, or transient error just skips that section of the report instead of crashing the run — the same pattern used for AWS Cost Explorer in the SES report.
- **Response shape assumptions:** Bitly's public docs don't fully pin down every metrics endpoint's JSON field names. The parsing helpers (`parse_time_series`, `parse_facets`, `parse_top_links`) try several common key names and degrade to "no data" rather than crashing if a shape doesn't match. If a section is unexpectedly empty, run with `--preview --debug` and check the raw response printed for that endpoint.
- **Outlook preview pane:** embedded chart images may be clipped in the Outlook desktop preview pane; they display correctly when the email is opened in its own window.
