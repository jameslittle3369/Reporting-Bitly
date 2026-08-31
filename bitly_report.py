#!/usr/bin/env python3
"""
bitly_report.py
----------------
Pulls Bitly link metrics (group-level rollups + per-link drill-downs for the
links configured in bitlink_map.json) from the Bitly REST API v4, builds an
HTML email with embedded charts and summary cards, attaches a self-contained
interactive Chart.js HTML report, and sends it via Amazon SES.

Setup:
  1. copy .env.example .env   and fill in BITLY_ACCESS_TOKEN / BITLY_GROUP_GUID
     and the AWS SES / recipient settings.
  2. .venv\\Scripts\\pip.exe install -r requirements.txt
  3. Optionally list Bitlinks you want a drill-down section for in
     bitlink_map.json, e.g. {"bit.ly/3abcXYZ": "Homepage Campaign"}.
  4. .venv\\Scripts\\python.exe bitly_report.py --preview --debug

Usage:
  python bitly_report.py                  # build and send the report
  python bitly_report.py --preview        # save locally, don't send
  python bitly_report.py --days 7         # override the lookback window
  python bitly_report.py --to a@b.com     # override recipients for this run
  python bitly_report.py --debug          # print raw Bitly API responses
"""

import argparse
import base64
import io
import json
import os
import re
import struct
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import email.encoders
import email.mime.base
import email.mime.multipart
import email.mime.text

import requests
import boto3
from dotenv import load_dotenv

load_dotenv()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patches as mpatches

# -- CONFIG (loaded from .env) ------------------------------------------------
BITLY_ACCESS_TOKEN = os.getenv("BITLY_ACCESS_TOKEN", "")
BITLY_GROUP_GUID    = os.getenv("BITLY_GROUP_GUID", "")

AWS_REGION            = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")

FROM_ADDRESS = os.getenv("FROM_ADDRESS", "")
TO_ADDRESSES = [a.strip() for a in os.getenv("TO_ADDRESSES", "").split(",") if a.strip()]

REPORT_DAYS = int(os.getenv("REPORT_DAYS", "30"))
TIMEZONE    = ZoneInfo(os.getenv("TIMEZONE", "America/New_York"))

BITLY_API = "https://api-ssl.bitly.com/v4"
DEBUG = False
# -----------------------------------------------------------------------------

ses_kwargs = {"region_name": AWS_REGION}
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
    ses_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
    ses_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
ses = boto3.client("sesv2", **ses_kwargs)

# -- Bitlink drill-down map ----------------------------------------------------
# Optional file: bitlink_map.json  {"bit.ly/3abcXYZ": "Homepage Campaign", ...}
# Only links listed here get their own drill-down section in the report.
_BITLINK_MAP_FILE = os.path.join(os.path.dirname(__file__), "bitlink_map.json")
try:
    with open(_BITLINK_MAP_FILE, encoding="utf-8") as _f:
        BITLINK_MAP: dict = json.load(_f)
    print(f"Loaded {len(BITLINK_MAP)} bitlink(s) from bitlink_map.json")
except FileNotFoundError:
    BITLINK_MAP = {}
except Exception as _e:
    print(f"Warning: could not load bitlink_map.json - {_e}")
    BITLINK_MAP = {}


def bitlink_label(bitlink: str) -> str:
    name = BITLINK_MAP.get(bitlink, "")
    return f"{bitlink} — {name}" if name else bitlink


def safe_id(s: str) -> str:
    """Turn a bitlink/string into a safe CSS/JS identifier (must not start with a digit)."""
    clean = re.sub(r"[^a-zA-Z0-9]", "_", s)
    if clean and clean[0].isdigit():
        clean = "n" + clean
    return clean


def fmt_int(v) -> str:
    try:
        return f"{int(round(v)):,}"
    except (TypeError, ValueError):
        return "0"


# ---------------------------------------------------------------------------
# Bitly API client
# ---------------------------------------------------------------------------

def bitly_get(path, **params):
    """GET a Bitly v4 endpoint. Returns the parsed JSON dict, or None on failure
    (missing scope, plan-tier gating, network error) so callers can skip that
    section of the report gracefully instead of crashing the whole run."""
    url = f"{BITLY_API}{path}"
    headers = {"Authorization": f"Bearer {BITLY_ACCESS_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if DEBUG:
            print(f"  GET {resp.url} -> {resp.status_code}")
            print("  " + resp.text[:1500])
        if resp.status_code == 403:
            print(f"  Bitly: {path} not available (403 - plan tier or missing scope) - skipping")
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  Bitly API error on {path}: {e}")
        return None


def list_groups():
    data = bitly_get("/groups")
    return (data or {}).get("groups", [])


def unit_reference_str(dt) -> str:
    """Bitly wants an ISO-8601 timestamp with a numeric UTC offset, e.g. 2026-08-31T00:00:00-0400."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_time_series(data, series_key_candidates=(), value_key_candidates=("clicks", "count", "value")):
    """Parse a Bitly {"<series_key>": [{"date": iso, "<value_key>": n}, ...]} response
    into full-window (timestamps, values) lists, gap-filled with 0 and aligned to TIMEZONE."""
    if not data:
        return [], []
    series = None
    for k in series_key_candidates:
        if k in data and isinstance(data[k], list):
            series = data[k]
            break
    if series is None:
        for v in data.values():
            if isinstance(v, list):
                series = v
                break
    if not series:
        return [], []

    daily = {}
    for point in series:
        date_str = point.get("date") or point.get("dt") or point.get("timestamp") or point.get("ts") or point.get("key")
        if not date_str:
            continue
        try:
            ts = datetime.fromisoformat(date_str).astimezone(TIMEZONE)
        except ValueError:
            continue
        val = 0
        for vk in value_key_candidates:
            if vk in point:
                val = point[vk]
                break
        daily[ts.date()] = daily.get(ts.date(), 0) + (val or 0)

    if not daily:
        return [], []
    cursor, end = min(daily), max(daily)
    timestamps, values = [], []
    while cursor <= end:
        timestamps.append(datetime(cursor.year, cursor.month, cursor.day, tzinfo=TIMEZONE))
        values.append(daily.get(cursor, 0))
        cursor += timedelta(days=1)
    return timestamps, values


def parse_facets(data, top_n=8):
    """Parse a Bitly {"metrics": [{"clicks": n, "<facet>": label}, ...]} response into
    a list of (label, clicks) sorted descending, capped to top_n with an 'Other' bucket."""
    if not data:
        return []
    rows = []
    for m in data.get("metrics", []):
        clicks = m.get("clicks", 0) or 0
        label = next((v for k, v in m.items() if k != "clicks"), None)
        rows.append((str(label) if label is not None else "Unknown", clicks))
    rows.sort(key=lambda r: r[1], reverse=True)
    if len(rows) > top_n:
        other = sum(c for _, c in rows[top_n:])
        rows = rows[:top_n] + [("Other", other)]
    return rows


def parse_top_links(data, top_n=10):
    if not data:
        return []
    rows = None
    for v in data.values():
        if isinstance(v, list):
            rows = v
            break
    if not rows:
        return []
    parsed = []
    for r in rows:
        link = r.get("link") or r.get("id") or r.get("bitlink") or "unknown"
        clicks = r.get("clicks", 0) or 0
        parsed.append((link, clicks))
    parsed.sort(key=lambda r: r[1], reverse=True)
    return parsed[:top_n]


# -- Group-level fetches --------------------------------------------------------

def fetch_group_clicks(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/clicks", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_time_series(data, ("link_clicks",))


def fetch_group_countries(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/countries", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data)


def fetch_group_devices(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/links/clicks/devices", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data)


def fetch_group_referrers(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/referrers", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data)


def fetch_group_referring_networks(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/referring_networks", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data)


def fetch_shorten_counts(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/shorten_counts", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_time_series(data, ("shorten_counts", "metrics", "link_clicks"), value_key_candidates=("value", "count", "clicks"))


def fetch_qr_scans(group_guid, end_dt, days):
    data = bitly_get(f"/groups/{group_guid}/codes/scans/over_time", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_time_series(data, ("scans", "qr_scans", "link_clicks"))


def fetch_top_links(group_guid, end_dt, days, top_n=10):
    data = bitly_get(f"/groups/{group_guid}/links/clicks/top", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_top_links(data, top_n)


def fetch_group_bitlink_count(group_guid):
    """Bitly's /groups/{guid}/bitlinks uses cursor-based (search_after) pagination
    with no 'total' field, so the only way to get a count is to page through
    everything and tally up the links returned per page."""
    data = bitly_get(f"/groups/{group_guid}/bitlinks", size=100)
    if not data:
        return None
    total = len(data.get("links", []))
    next_url = (data.get("pagination") or {}).get("next")
    while next_url:
        page = bitly_get(next_url.replace(BITLY_API, ""))
        if not page:
            break
        total += len(page.get("links", []))
        next_url = (page.get("pagination") or {}).get("next")
    return total


# -- Per-bitlink fetches ---------------------------------------------------------

def fetch_bitlink_clicks(bitlink, end_dt, days):
    data = bitly_get(f"/bitlinks/{bitlink}/clicks", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_time_series(data, ("link_clicks",))


def fetch_bitlink_countries(bitlink, end_dt, days):
    data = bitly_get(f"/bitlinks/{bitlink}/countries", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data, top_n=6)


def fetch_bitlink_devices(bitlink, end_dt, days):
    data = bitly_get(f"/bitlinks/{bitlink}/devices", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data, top_n=6)


def fetch_bitlink_referrers(bitlink, end_dt, days):
    data = bitly_get(f"/bitlinks/{bitlink}/referrers", unit="day", units=days,
                     unit_reference=unit_reference_str(end_dt))
    return parse_facets(data, top_n=6)


# ---------------------------------------------------------------------------
# Chart generation (matplotlib, Agg backend, base64 PNG embeds)
# ---------------------------------------------------------------------------

PALETTE = ["#4A90D9", "#E8704A", "#50C878", "#9B59B6", "#F1C40F", "#1ABC9C", "#F39C12", "#EC7063"]


def chart_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    buf.seek(0)
    data = buf.read()
    plt.close(fig)
    native_w = struct.unpack(">I", data[16:20])[0]
    native_h = struct.unpack(">I", data[20:24])[0]
    return base64.b64encode(data).decode(), native_w, native_h


def make_line_chart(title, series, days, y_fmt=None):
    """series: list of (label, timestamps, values) tuples."""
    fig, ax = plt.subplots(figsize=(7, 3))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")

    has_data = False
    for i, (label, ts, vals) in enumerate(series):
        if ts:
            ax.plot(ts, vals, marker="o", markersize=3,
                    label=label, color=PALETTE[i % len(PALETTE)], linewidth=2)
            has_data = True

    if not has_data:
        ax.text(0.5, 0.5, "No data available", ha="center", va="center",
                transform=ax.transAxes, color="#888")

    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=TIMEZONE))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, days // 8), tz=TIMEZONE))
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    _yfmt = y_fmt if y_fmt else (lambda x, _: f"{int(x):,}")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(_yfmt))
    ax.grid(axis="y", linestyle="--", alpha=0.4)
    ax.spines[["top", "right"]].set_visible(False)
    if has_data:
        ax.legend(fontsize=8, framealpha=0.5)
    fig.tight_layout()
    return chart_to_b64(fig)


def make_donut_chart(title, rows):
    """rows: list of (label, value) tuples."""
    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]
    total = sum(values) or 1
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(labels))]

    fig, ax = plt.subplots(figsize=(5, 4))
    fig.patch.set_facecolor("#F8F9FA")
    _, _, autotexts = ax.pie(
        values, labels=None, colors=colors, autopct="%1.1f%%",
        startangle=90, pctdistance=0.78,
        wedgeprops={"width": 0.52, "edgecolor": "#F8F9FA", "linewidth": 2},
    )
    for t in autotexts:
        t.set_fontsize(8)
        t.set_color("white")
        t.set_fontweight("bold")
    ax.legend(
        handles=[mpatches.Patch(color=c, label=f"{l} ({v:,})") for l, c, v in zip(labels, colors, values)],
        loc="lower center", bbox_to_anchor=(0.5, -0.15),
        ncol=min(3, len(labels)), fontsize=8, frameon=False,
    )
    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.text(0, 0, f"{total:,}\nTotal", ha="center", va="center",
            fontsize=11, fontweight="bold", color="#333")
    fig.tight_layout()
    return chart_to_b64(fig)


def make_bar_chart(title, rows):
    """rows: list of (label, value) tuples, rendered as a horizontal bar chart."""
    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]
    maxv = max(values) if values else 1

    fig, ax = plt.subplots(figsize=(7, max(2.5, len(labels) * 0.4 + 1)))
    fig.patch.set_facecolor("#F8F9FA")
    ax.set_facecolor("#F8F9FA")
    bars = ax.barh(labels, values, color=PALETTE[0], height=0.6)
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + maxv * 0.015, bar.get_y() + bar.get_height() / 2,
                f"{v:,}", va="center", fontsize=8, color="#333")
    ax.invert_yaxis()
    ax.set_title(title, fontsize=13, fontweight="bold", pad=10)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.tick_params(axis="y", labelsize=8)
    ax.tick_params(axis="x", labelsize=8)
    fig.tight_layout()
    return chart_to_b64(fig)


def img_tag(b64, native_w, native_h, alt="chart", display_w=600):
    """Outlook-safe image tag wrapped in a 1x1 presentation table (see ses_daily_report.py
    for why explicit width/height attributes, not CSS, are required)."""
    display_h = round(native_h * display_w / native_w)
    dw, dh = str(display_w), str(display_h)
    return (
        '<table width="' + dw + '" cellpadding="0" cellspacing="0" border="0" role="presentation" '
        'style="border:0;mso-table-lspace:0;mso-table-rspace:0;">'
        '<tr><td>'
        '<img src="data:image/png;base64,' + b64 + '" alt="' + alt + '" '
        'width="' + dw + '" height="' + dh + '" '
        'style="display:block;border-radius:6px;">'
        '</td></tr></table>'
    )


def stat_box(label, value, color="#4A90D9"):
    return (
        '<div style="flex:1;min-width:140px;background:#fff;border-radius:8px;'
        'padding:16px 12px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,.08);">'
        '<div style="font-size:26px;font-weight:700;color:' + color + ';">' + str(value) + '</div>'
        '<div style="font-size:11px;color:#666;margin-top:4px;">' + label + '</div>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Email body (light theme, self-contained base64 PNG charts)
# ---------------------------------------------------------------------------

def build_email_html(report_date, days, charts, summary, top_links, bitlink_sections, attach_name):
    boxes = (
        stat_box("Total Clicks", fmt_int(summary["total_clicks"]), "#4A90D9")
        + stat_box("New Links Created", fmt_int(summary["new_links"]), "#E8704A")
        + stat_box("Bitlinks in Group", summary["total_bitlinks"] or "—", "#50C878")
        + stat_box("Top Country", summary["top_country"] or "—", "#9B59B6")
        + stat_box("Top Referrer", summary["top_referrer"] or "—", "#F1C40F")
    )
    if summary["total_qr_scans"]:
        boxes += stat_box("QR Code Scans", fmt_int(summary["total_qr_scans"]), "#1ABC9C")

    def chart_section(title, key, note=None):
        if not charts.get(key):
            return ""
        b64, w, h = charts[key]
        note_html = f'<p style="font-size:11px;color:#aaa;margin:0 0 6px;">{note}</p>' if note else ""
        return (
            f'<h2 style="margin:24px 0 8px;font-size:15px;color:#333;">{title}</h2>'
            + note_html + img_tag(b64, w, h, title)
        )

    top_links_rows = "".join(
        f'<tr><td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;">{bitlink_label(link)}</td>'
        f'<td style="padding:6px 10px;border-bottom:1px solid #eee;font-size:12px;text-align:right;">{clicks:,}</td></tr>'
        for link, clicks in top_links
    ) or '<tr><td style="padding:6px 10px;font-size:12px;color:#888;">No link click data available</td></tr>'

    top_links_html = (
        '<h2 style="margin:24px 0 8px;font-size:15px;color:#333;">Top Links by Clicks</h2>'
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">'
        f'<tr><th style="text-align:left;padding:6px 10px;font-size:11px;color:#888;border-bottom:2px solid #eee;">Link</th>'
        f'<th style="text-align:right;padding:6px 10px;font-size:11px;color:#888;border-bottom:2px solid #eee;">Clicks</th></tr>'
        f'{top_links_rows}</table>'
    )

    ts_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")

    lines = [
        "<!DOCTYPE html>",
        '<html xmlns:v="urn:schemas-microsoft-com:vml">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>PayLess Mobile Bitly Report | " + report_date + "</title>",
        "</head>",
        '<body style="margin:0;padding:0;background:#F0F2F5;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Arial,sans-serif;">',
        '<div style="max-width:700px;margin:32px auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1);">',
        '<!--[if gte mso 9]><v:rect xmlns:v="urn:schemas-microsoft-com:vml" fill="true" stroke="false" style="mso-width-percent:1000;"><v:fill type="gradient" color="#EE6123" color2="#333333" angle="135"/><v:textbox style="mso-fit-shape-to-text:true" inset="0,0,0,0"><![endif]-->',
        '<div style="background-color:#EE6123;background:linear-gradient(135deg,#EE6123 0%,#333333 100%);padding:28px 32px;">',
        '<div style="color:#fff;font-size:20px;font-weight:700;">PayLess Mobile Bitly Report | ' + report_date + '</div>',
        "</div>",
        '<!--[if gte mso 9]></v:textbox></v:rect><![endif]-->',
        '<div style="padding:24px 32px;">',
        '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:24px;">',
        boxes,
        "</div>",
        chart_section("Clicks Over Time", "clicks"),
        chart_section("New Links Created", "shorten"),
        chart_section("QR Code Scans", "qr"),
        chart_section("Top Countries", "countries"),
        chart_section("Devices", "devices"),
        chart_section("Referrers", "referrers"),
        chart_section("Referring Networks", "referring_networks"),
        top_links_html,
        bitlink_sections,
        '<p style="margin:32px 0 0;font-size:11px;color:#aaa;text-align:center;">',
        f"Generated {ts_str} &bull; PayLess Mobile Bitly Report &bull; Last {days} days &bull; "
        f"See attached {attach_name} for interactive version",
        "</p>",
        "</div>",
        "</div>",
        "</body>",
        "</html>",
    ]
    return "\n".join(l for l in lines if l)


def build_bitlink_email_section(bitlink, days, clicks_chart, countries, devices):
    """Table-based (Outlook-safe) drill-down section for one configured Bitlink."""
    total = sum(clicks_chart[1]) if clicks_chart else 0
    chart_html = ""
    if clicks_chart and clicks_chart[0]:
        b64, w, h = clicks_chart[0]
        chart_html = img_tag(b64, w, h, "clicks", display_w=560)

    top_country = countries[0][0] if countries else "—"
    top_device = devices[0][0] if devices else "—"

    return (
        '<div style="margin:28px 0 0;padding-top:20px;border-top:2px solid #f0f0f0;">'
        f'<h2 style="margin:0 0 10px;font-size:15px;color:#333;">{bitlink_label(bitlink)}</h2>'
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td width="33%" style="padding:0 5px 12px 0;"><div style="background:#F8F9FA;border-radius:8px;'
        f'border-left:4px solid #4A90D9;padding:14px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;color:#4A90D9;">{fmt_int(total)}</div>'
        f'<div style="font-size:11px;color:#666;margin-top:3px;">Clicks ({days}d)</div></div></td>'
        f'<td width="33%" style="padding:0 5px 12px;"><div style="background:#F8F9FA;border-radius:8px;'
        f'border-left:4px solid #9B59B6;padding:14px;text-align:center;">'
        f'<div style="font-size:16px;font-weight:700;color:#9B59B6;">{top_country}</div>'
        f'<div style="font-size:11px;color:#666;margin-top:3px;">Top Country</div></div></td>'
        f'<td width="33%" style="padding:0 0 12px 5px;"><div style="background:#F8F9FA;border-radius:8px;'
        f'border-left:4px solid #1ABC9C;padding:14px;text-align:center;">'
        f'<div style="font-size:16px;font-weight:700;color:#1ABC9C;">{top_device}</div>'
        f'<div style="font-size:11px;color:#666;margin-top:3px;">Top Device</div></div></td>'
        '</tr></table>'
        + chart_html
        + "</div>"
    )


# ---------------------------------------------------------------------------
# Interactive HTML attachment (Chart.js with hover tooltips)
# ---------------------------------------------------------------------------

def jlist(x):
    return json.dumps(x)


def build_interactive_html(report_date, days, series_data, facet_data, top_links, bitlink_data):
    """series_data: dict of key -> (labels, values) for line charts.
    facet_data: dict of key -> list[(label, value)] for donut/bar charts.
    bitlink_data: dict of bitlink -> {"clicks": (labels, values), "countries": [...], "devices": [...]}
    """

    def line_cfg(canvas_id, title, timestamps, values, color_idx=0):
        labels = [t.strftime("%b %d") for t in timestamps] if timestamps else []
        color = PALETTE[color_idx % len(PALETTE)]
        return (
            f'<div class="chart-card"><h3>{title}</h3>'
            f'<div class="chart-wrap"><canvas id="{canvas_id}"></canvas></div></div>\n'
            f'<script>mk("{canvas_id}","line",{jlist(labels)},'
            f'[{{label:"{title}",data:{jlist(values)},borderColor:"{color}",'
            f'backgroundColor:"{color}33",pointRadius:3,pointHoverRadius:6,tension:.3,fill:true}}],'
            f'{{plugins:{{legend:{{display:false}}}}}});</script>'
        )

    def donut_cfg(canvas_id, title, rows):
        if not rows:
            return f'<div class="chart-card"><h3>{title}</h3><p class="muted">No data available</p></div>'
        labels = [r[0] for r in rows]
        values = [r[1] for r in rows]
        colors = [PALETTE[i % len(PALETTE)] for i in range(len(labels))]
        return (
            f'<div class="chart-card"><h3>{title}</h3>'
            f'<div class="chart-wrap"><canvas id="{canvas_id}"></canvas></div></div>\n'
            f'<script>mk("{canvas_id}","doughnut",{jlist(labels)},'
            f'[{{data:{jlist(values)},backgroundColor:{jlist(colors)},borderWidth:2,borderColor:"#fff"}}],'
            f'{{cutout:"55%",plugins:{{legend:{{display:true,position:"bottom",'
            f'labels:{{font:{{size:11}},boxWidth:10}}}}}}}});</script>'
        )

    def bar_cfg(canvas_id, title, rows):
        if not rows:
            return f'<div class="chart-card"><h3>{title}</h3><p class="muted">No data available</p></div>'
        labels = [r[0] for r in rows]
        values = [r[1] for r in rows]
        return (
            f'<div class="chart-card"><h3>{title}</h3>'
            f'<div class="chart-wrap"><canvas id="{canvas_id}"></canvas></div></div>\n'
            f'<script>mk("{canvas_id}","bar",{jlist(labels)},'
            f'[{{label:"Clicks",data:{jlist(values)},backgroundColor:"{PALETTE[0]}",borderRadius:4}}],'
            f'{{indexAxis:"y",plugins:{{legend:{{display:false}}}}}});</script>'
        )

    group_blocks = []
    if series_data.get("clicks")[0]:
        group_blocks.append(line_cfg("clicksChart", "Clicks Over Time", *series_data["clicks"], 0))
    if series_data.get("shorten")[0]:
        group_blocks.append(line_cfg("shortenChart", "New Links Created", *series_data["shorten"], 1))
    if series_data.get("qr")[0]:
        group_blocks.append(line_cfg("qrChart", "QR Code Scans", *series_data["qr"], 2))
    group_blocks.append(donut_cfg("countriesChart", "Top Countries", facet_data.get("countries", [])))
    group_blocks.append(donut_cfg("devicesChart", "Devices", facet_data.get("devices", [])))
    group_blocks.append(donut_cfg("referrersChart", "Referrers", facet_data.get("referrers", [])))
    group_blocks.append(bar_cfg("networksChart", "Referring Networks", facet_data.get("referring_networks", [])))
    group_blocks.append(bar_cfg("topLinksChart", "Top Links by Clicks",
                                [(bitlink_label(l), c) for l, c in top_links]))

    bitlink_sections = []
    for bitlink, d in bitlink_data.items():
        sfx = safe_id(bitlink)
        labels, values = d["clicks"]
        section = [f'<div class="link-section"><h2>{bitlink_label(bitlink)}</h2><div class="charts-grid">']
        if labels:
            section.append(line_cfg(f"clicks_{sfx}", "Clicks", labels, values, 0))
        else:
            section.append('<div class="chart-card"><h3>Clicks</h3><p class="muted">No data available</p></div>')
        section.append(donut_cfg(f"countries_{sfx}", "Countries", d["countries"]))
        section.append(donut_cfg(f"devices_{sfx}", "Devices", d["devices"]))
        section.append("</div></div>")
        bitlink_sections.append("\n".join(section))

    stat_html = (
        '<div class="cards">'
        + f'<div class="card c1"><label>Total Clicks</label><div class="val">{fmt_int(sum(series_data["clicks"][1]))}</div></div>'
        + f'<div class="card c2"><label>New Links Created</label><div class="val">{fmt_int(sum(series_data["shorten"][1]))}</div></div>'
        + f'<div class="card c3"><label>Top Country</label><div class="val">{facet_data.get("countries", [("—", 0)])[0][0]}</div></div>'
        + f'<div class="card c4"><label>Top Referrer</label><div class="val">{facet_data.get("referrers", [("—", 0)])[0][0]}</div></div>'
        + '</div>'
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PayLess Mobile Bitly Report | {report_date}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#F0F2F5;color:#1a1a1a;padding:24px 16px}}
  .wrap{{max-width:1100px;margin:0 auto}}
  header{{background:linear-gradient(135deg,#EE6123 0%,#333333 100%);color:#fff;border-radius:10px 10px 0 0;padding:28px 32px}}
  header h1{{font-size:22px}}
  header p{{margin-top:4px;color:#ffe0cc;font-size:13px}}
  .body{{background:#fff;border-radius:0 0 10px 10px;padding:24px 32px;margin-bottom:24px;box-shadow:0 1px 6px rgba(0,0,0,.08)}}
  .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
  .card{{background:#fff;border:1px solid #e5e7eb;border-left:3px solid;border-radius:8px;padding:14px 16px}}
  .card.c1{{border-left-color:#4A90D9}} .card.c2{{border-left-color:#E8704A}}
  .card.c3{{border-left-color:#9B59B6}} .card.c4{{border-left-color:#F1C40F}}
  .card label{{font-size:.68rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#9ca3af}}
  .card .val{{font-size:1.4rem;font-weight:700;margin-top:4px;color:#111827}}
  .charts-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:8px}}
  .chart-card{{background:#F8F9FA;border-radius:10px;padding:16px 18px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
  .chart-card h3{{font-size:13px;color:#333;margin-bottom:10px;font-weight:600}}
  .chart-wrap{{position:relative;height:260px}}
  .muted{{color:#888;font-size:12px}}
  .link-section{{margin-top:28px;padding-top:20px;border-top:2px solid #eee}}
  .link-section h2{{font-size:16px;color:#333;margin-bottom:12px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>PayLess Mobile Bitly Report | {report_date}</h1>
    <p>Last {days} days &bull; Hover over any chart for exact values</p>
  </header>
  <div class="body">
    {stat_html}
    <div class="charts-grid">
      {''.join(group_blocks)}
    </div>
    {''.join(bitlink_sections)}
  </div>
</div>
<script>
const BASE_OPTS = {{
  responsive: true, maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{ legend: {{ display: false }},
    tooltip: {{ backgroundColor: '#111827', padding: 10, cornerRadius: 6,
               titleFont: {{size:12}}, bodyFont: {{size:12}} }} }},
  scales: {{
    x: {{ grid: {{ display: false }}, ticks: {{ color: '#6b7280', font: {{size:11}}, maxRotation: 45, autoSkip: true, maxTicksLimit: 10 }} }},
    y: {{ grid: {{ color: '#e5e7eb' }}, ticks: {{ color: '#6b7280', font: {{size:11}} }} }}
  }}
}};
function mk(id, type, labels, datasets, extra) {{
  extra = extra || {{}};
  const el = document.getElementById(id);
  if (!el) return;
  const opts = JSON.parse(JSON.stringify(BASE_OPTS));
  Object.assign(opts, extra);
  if (extra.scales) Object.assign(opts.scales, extra.scales);
  if (extra.plugins) Object.assign(opts.plugins, extra.plugins);
  if (type === 'doughnut' || type === 'pie') {{ delete opts.scales; }}
  new Chart(el, {{ type: type, data: {{ labels: labels, datasets: datasets }}, options: opts }});
}}
</script>
</body>
</html>"""
    return page


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(subject, html_body, to_addresses, attachment_html=None, attachment_filename=None):
    if attachment_html:
        msg = email.mime.multipart.MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = "PayLess Mobile Reports <" + FROM_ADDRESS + ">"
        msg["To"] = ", ".join(to_addresses)

        body_part = email.mime.multipart.MIMEMultipart("alternative")
        body_part.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
        msg.attach(body_part)

        att = email.mime.base.MIMEBase("text", "html")
        att.set_payload(attachment_html.encode("utf-8"))
        email.encoders.encode_base64(att)
        att.add_header("Content-Disposition", "attachment",
                       filename=attachment_filename or "bitly_interactive_report.html")
        msg.attach(att)

        ses.send_email(
            FromEmailAddress="PayLess Mobile Reports <" + FROM_ADDRESS + ">",
            Destination={"ToAddresses": to_addresses},
            Content={"Raw": {"Data": msg.as_bytes()}},
        )
    else:
        ses.send_email(
            FromEmailAddress="PayLess Mobile Reports <" + FROM_ADDRESS + ">",
            Destination={"ToAddresses": to_addresses},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}},
                }
            },
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Bitly metrics report emailed via AWS SES")
    p.add_argument("--days", type=int, default=None, help="Override REPORT_DAYS for this run")
    p.add_argument("--preview", action="store_true", help="Save the interactive HTML locally instead of emailing")
    p.add_argument("--to", nargs="+", default=None, help="Override recipients for this run")
    p.add_argument("--debug", action="store_true", help="Print raw Bitly API responses")
    return p.parse_args()


def resolve_group_guid():
    if BITLY_GROUP_GUID:
        return BITLY_GROUP_GUID
    groups = list_groups()
    if len(groups) == 1:
        guid = groups[0]["guid"]
        print(f"BITLY_GROUP_GUID not set - using the only group on this account: {guid}")
        return guid
    guids = ", ".join(g.get("guid", "?") for g in groups) or "none found"
    raise ValueError(f"BITLY_GROUP_GUID must be set in .env. Groups on this account: {guids}")


def main():
    global DEBUG
    args = parse_args()
    DEBUG = args.debug

    if not BITLY_ACCESS_TOKEN:
        raise ValueError("BITLY_ACCESS_TOKEN must be set in .env")

    days = args.days or REPORT_DAYS
    to_addresses = args.to or TO_ADDRESSES

    group_guid = resolve_group_guid()
    end_dt = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=days)
    report_date = start_dt.strftime("%b %d") + " - " + end_dt.strftime("%b %d, %Y")

    print(f"Fetching Bitly group metrics ({days}-day window) ...")
    ts_clicks, v_clicks = fetch_group_clicks(group_guid, end_dt, days)
    ts_shorten, v_shorten = fetch_shorten_counts(group_guid, end_dt, days)
    ts_qr, v_qr = fetch_qr_scans(group_guid, end_dt, days)
    countries = fetch_group_countries(group_guid, end_dt, days)
    devices = fetch_group_devices(group_guid, end_dt, days)
    referrers = fetch_group_referrers(group_guid, end_dt, days)
    referring_networks = fetch_group_referring_networks(group_guid, end_dt, days)
    top_links = fetch_top_links(group_guid, end_dt, days)
    total_bitlinks = fetch_group_bitlink_count(group_guid)

    print("Fetching per-bitlink drill-down data ...")
    bitlink_data = {}
    for bitlink in BITLINK_MAP:
        print(f"  {bitlink} ...")
        bl_ts, bl_vals = fetch_bitlink_clicks(bitlink, end_dt, days)
        bitlink_data[bitlink] = {
            "clicks": (bl_ts, bl_vals),
            "countries": fetch_bitlink_countries(bitlink, end_dt, days),
            "devices": fetch_bitlink_devices(bitlink, end_dt, days),
            "referrers": fetch_bitlink_referrers(bitlink, end_dt, days),
        }

    print("Building charts ...")
    charts = {}
    if ts_clicks:
        charts["clicks"] = make_line_chart("Clicks Over Time", [("Clicks", ts_clicks, v_clicks)], days)
    if ts_shorten:
        charts["shorten"] = make_line_chart("New Links Created", [("New Links", ts_shorten, v_shorten)], days)
    if ts_qr and any(v_qr):
        charts["qr"] = make_line_chart("QR Code Scans", [("Scans", ts_qr, v_qr)], days)
    if countries:
        charts["countries"] = make_donut_chart("Top Countries", countries)
    if devices:
        charts["devices"] = make_donut_chart("Devices", devices)
    if referrers:
        charts["referrers"] = make_donut_chart("Referrers", referrers)
    if referring_networks:
        charts["referring_networks"] = make_bar_chart("Referring Networks", referring_networks)

    bitlink_email_sections = ""
    for bitlink, d in bitlink_data.items():
        chart = None
        if d["clicks"][0]:
            chart = make_line_chart(bitlink_label(bitlink), [("Clicks", *d["clicks"])], days)
        bitlink_email_sections += build_bitlink_email_section(
            bitlink, days, (chart, d["clicks"][1]) if chart else None, d["countries"], d["devices"]
        )

    summary = {
        "total_clicks": sum(v_clicks) if v_clicks else 0,
        "new_links": sum(v_shorten) if v_shorten else 0,
        "total_bitlinks": fmt_int(total_bitlinks) if total_bitlinks is not None else None,
        "top_country": countries[0][0] if countries else None,
        "top_referrer": referrers[0][0] if referrers else None,
        "total_qr_scans": sum(v_qr) if v_qr else 0,
    }

    attach_name = "PayLess_Bitly_" + end_dt.strftime("%Y%m%d") + ".html"

    print("Composing email ...")
    html_body = build_email_html(report_date, days, charts, summary, top_links, bitlink_email_sections, attach_name)

    interactive_html = build_interactive_html(
        report_date, days,
        series_data={"clicks": (ts_clicks, v_clicks), "shorten": (ts_shorten, v_shorten), "qr": (ts_qr, v_qr)},
        facet_data={"countries": countries, "devices": devices, "referrers": referrers,
                    "referring_networks": referring_networks},
        top_links=top_links,
        bitlink_data=bitlink_data,
    )

    subject = "PayLess Mobile Bitly Report | " + report_date

    if args.preview or not FROM_ADDRESS or not to_addresses:
        if not (FROM_ADDRESS and to_addresses) and not args.preview:
            print("FROM_ADDRESS or TO_ADDRESSES not set in .env - saving locally instead of sending.")
        with open(attach_name, "w", encoding="utf-8") as f:
            f.write(interactive_html)
        with open(attach_name.replace(".html", "_email.html"), "w", encoding="utf-8") as f:
            f.write(html_body)
        print(f"Saved interactive report to {attach_name}")
        print(f"Saved email body preview to {attach_name.replace('.html', '_email.html')}")
    else:
        print(f"Sending to {', '.join(to_addresses)} ...")
        send_email(subject, html_body, to_addresses,
                  attachment_html=interactive_html, attachment_filename=attach_name)
        print("Done! Report sent successfully.")


if __name__ == "__main__":
    main()
