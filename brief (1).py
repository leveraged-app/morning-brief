"""
Morning Brief v1
Pulls the day's market numbers, asks Claude to write the 5-part brief
(numbers / what moved and why / what's coming / Canada corner / takeaway),
saves it to briefs/YYYY-MM-DD.md and optionally emails it.

Run:
    python brief.py            # real data (needs keys in environment)
    python brief.py --demo     # sample numbers, no data keys needed
    python brief.py --no-email # skip the email step
"""

import argparse
import datetime as dt
import html
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# 1. WHAT GOES IN THE NUMBERS SECTION (edit these lists to change the brief)
# ---------------------------------------------------------------------------
# kind: "index"/"price"/"fx" -> show % change; "rate" -> show change in bps

# FRED (free, one key): https://fred.stlouisfed.org/docs/api/api_key.html
FRED_SERIES = {
    "S&P 500": ("SP500", "index"),
    "Nasdaq": ("NASDAQCOM", "index"),
    "US 10Y yield": ("DGS10", "rate"),
    "WTI oil ($/bbl)": ("DCOILWTICO", "price"),
    "Natural gas ($/MMBtu)": ("DHHNGSP", "price"),
    "VIX": ("VIXCLS", "index"),
    "US dollar index": ("DTWEXBGS", "index"),
}

# Bank of Canada Valet API (free, no key)
BOC_SERIES = {
    "Canada 10Y yield": ("BD.CDN.10YR.DQ.YLD", "rate"),
    "USD/CAD": ("FXUSDCAD", "fx"),
}

# Financial Modeling Prep (free tier key, optional): https://site.financialmodelingprep.com
# Each entry lists symbols to try in order. If the first is not on your plan,
# the next one is used and the row is labelled with it.
FMP_SYMBOLS = {
    "TSX": ([("^GSPTSE", "TSX"), ("XIU.TO", "TSX (via XIU ETF)"), ("EWC", "Canada (via EWC ETF)")], "index"),
    "Gold ($/oz)": ([("GCUSD", "Gold ($/oz)")], "price"),
    "Bitcoin": ([("BTCUSD", "Bitcoin")], "price"),
}

# Display order in the brief
ORDER = [
    "S&P 500", "Nasdaq", "TSX",
    "US 10Y yield", "Canada 10Y yield",
    "WTI oil ($/bbl)", "Gold ($/oz)", "Natural gas ($/MMBtu)",
    "USD/CAD", "US dollar index",
    "VIX", "Bitcoin",
]

# Sample data for --demo (made up, for testing only)
SAMPLE = {
    "S&P 500": (6812.4, 6771.8, "index"),
    "Nasdaq": (23050.1, 22844.6, "index"),
    "TSX": (28140.0, 28028.3, "index"),
    "US 10Y yield": (4.18, 4.13, "rate"),
    "Canada 10Y yield": (3.21, 3.18, "rate"),
    "WTI oil ($/bbl)": (61.20, 63.03, "price"),
    "Gold ($/oz)": (3980.5, 3961.2, "price"),
    "Natural gas ($/MMBtu)": (3.42, 3.38, "price"),
    "USD/CAD": (1.392, 1.395, "fx"),
    "US dollar index": (121.4, 121.6, "index"),
    "VIX": (16.8, 17.9, "index"),
    "Bitcoin": (98500.0, 97200.0, "price"),
}

# Claude settings
MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5-5")
WEB_SEARCH_TOOL = os.getenv("BRIEF_SEARCH_TOOL", "web_search_20250305")

TIMEOUT = 20


# ---------------------------------------------------------------------------
# 2. FETCHING THE NUMBERS
# ---------------------------------------------------------------------------
def fred_latest_two(series_id, key):
    r = requests.get(
        "https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": series_id, "api_key": key, "file_type": "json",
                "sort_order": "desc", "limit": 10},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o["value"] not in (".", "")]
    return float(obs[0]["value"]), float(obs[1]["value"]), obs[0]["date"]


def boc_latest_two(series_id):
    r = requests.get(
        f"https://www.bankofcanada.ca/valet/observations/{series_id}/json",
        params={"recent": 5}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o.get(series_id, {}).get("v")]
    obs.sort(key=lambda o: o["d"], reverse=True)
    return float(obs[0][series_id]["v"]), float(obs[1][series_id]["v"]), obs[0]["d"]


def fmp_quote(symbol, key):
    r = requests.get(
        "https://financialmodelingprep.com/stable/quote",
        params={"symbol": symbol, "apikey": key}, timeout=TIMEOUT,
    )
    r.raise_for_status()
    q = r.json()[0]
    price = float(q["price"])
    prev = float(q.get("previousClose") or price - float(q.get("change", 0)))
    return price, prev, str(q.get("timestamp", ""))


def get_numbers(demo=False):
    """Returns {name: {"value", "prev", "kind", "asof"} or None if it failed}."""
    if demo:
        return {n: {"value": v, "prev": p, "kind": k, "asof": "sample"}
                for n, (v, p, k) in SAMPLE.items()}

    out = {}
    fred_key = os.getenv("FRED_API_KEY")
    fmp_key = os.getenv("FMP_API_KEY")

    for name, (sid, kind) in FRED_SERIES.items():
        try:
            if not fred_key:
                raise RuntimeError("no FRED_API_KEY")
            v, p, d = fred_latest_two(sid, fred_key)
            out[name] = {"value": v, "prev": p, "kind": kind, "asof": d}
        except Exception as e:
            print(f"  ! {name}: {e}", file=sys.stderr)
            out[name] = None

    for name, (sid, kind) in BOC_SERIES.items():
        try:
            v, p, d = boc_latest_two(sid)
            out[name] = {"value": v, "prev": p, "kind": kind, "asof": d}
        except Exception as e:
            print(f"  ! {name}: {e}", file=sys.stderr)
            out[name] = None

    for name, (choices, kind) in FMP_SYMBOLS.items():
        out[name] = None
        if not fmp_key:
            print(f"  ! {name}: no FMP_API_KEY", file=sys.stderr)
            continue
        for sym, label in choices:
            try:
                v, p, d = fmp_quote(sym, fmp_key)
                out[name] = {"value": v, "prev": p, "kind": kind, "asof": d, "label": label}
                break
            except Exception as e:
                print(f"  ! {name} ({sym}): {e}", file=sys.stderr)

    return out


# ---------------------------------------------------------------------------
# 3. FORMATTING THE NUMBERS TABLE
# ---------------------------------------------------------------------------
def fmt_value(v, kind):
    if kind == "rate":
        return f"{v:.2f}%"
    if kind == "fx":
        return f"{v:.4f}"
    return f"{v:,.2f}"


def fmt_change(v, p, kind):
    if kind == "rate":
        bps = (v - p) * 100
        return f"{'▲' if bps >= 0 else '▼'} {abs(bps):.0f} bps"
    pct = (v - p) / p * 100 if p else 0
    return f"{'▲' if pct >= 0 else '▼'} {abs(pct):.2f}%"


def numbers_table(nums):
    lines = ["| Market | Level | Change |", "|---|---|---|"]
    for name in ORDER:
        d = nums.get(name)
        if d is None:
            lines.append(f"| {name} | n/a | n/a |")
        else:
            lines.append(f"| {d.get('label', name)} | {fmt_value(d['value'], d['kind'])} | "
                         f"{fmt_change(d['value'], d['prev'], d['kind'])} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. ASKING CLAUDE TO WRITE THE BRIEF
# ---------------------------------------------------------------------------
PROMPT = """You are writing a daily morning market brief for finance students and
young professionals in Canada who are recruiting for investment banking,
consulting and equity research. Today is {today}.

Here are the latest closing numbers (use them exactly; never invent or change a number):

{table}

Use web search to find out WHY the biggest moves happened and WHAT is scheduled
for today (economic data, Fed / Bank of Canada events, major earnings).
Prefer original sources (central banks, statistics agencies, company releases)
and reputable outlets. Write everything in your own words: do not copy
sentences from articles.

Write the brief in Markdown with exactly these sections:

## 1. The numbers
One or two sentences summarizing the overall tone (risk-on / risk-off, what led).
Do not repeat the table; it is added separately.

## 2. What moved and why
Up to 3 stories. For each: a bold one-line headline, then one line "Why it
happened:" and one line "Why it matters:". Only stories that moved the numbers
above or a major company or sector.

## 3. What's coming today
Bullet list of scheduled data releases, central bank events and major earnings
for today, with times in ET where known. If nothing major, say so.

## 4. Canada corner
2-4 bullets on what is moving the TSX, Canadian banks, energy, the Bank of
Canada or the loonie.

## 5. The takeaway
1-2 sentences starting with "If someone asks you about markets today:" that a
student could say in an interview.

Rules: under 550 words total, plain language, no hype, no buy/sell
recommendations. Finish with a short "Sources" list of the pages you used.
"""


def write_with_claude(table, today):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = {
        "model": MODEL,
        "max_tokens": 3000,
        "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 8}],
        "messages": [{"role": "user", "content": PROMPT.format(today=today, table=table)}],
    }
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=body, timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:500]}")
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


# ---------------------------------------------------------------------------
# 5. SAVING AND EMAILING
# ---------------------------------------------------------------------------
UP, DOWN, INK, GREY, LINE = "#0B6E4F", "#B54708", "#12161C", "#5B6270", "#E3E1DA"


def _inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               rf'<a href="\2" style="color:{UP};">\1</a>', s)
    return s


def md_to_html(md):
    """Small Markdown converter for the brief's headings, bullets, bold and links."""
    out, para, list_tag = [], [], None

    def flush_para():
        if para:
            out.append(f'<p style="margin:0 0 12px;line-height:1.5;">{"<br>".join(_inline(p) for p in para)}</p>')
            para.clear()

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    for raw in md.splitlines():
        line = raw.strip()
        bullet = re.match(r"^[-*•]\s+(.*)", line)
        number = re.match(r"^\d+[.)]\s+(.*)", line)
        if not line:
            flush_para(); close_list()
        elif line.startswith("#"):
            flush_para(); close_list()
            text = line.lstrip("#").strip()
            out.append(f'<h2 style="margin:24px 0 8px;font-size:17px;color:{INK};'
                       f'border-bottom:1px solid {LINE};padding-bottom:6px;">{_inline(text)}</h2>')
        elif line in ("---", "***"):
            flush_para(); close_list()
            out.append(f'<hr style="border:0;border-top:1px solid {LINE};margin:20px 0;">')
        elif bullet or number:
            flush_para()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                out.append(f'<{tag} style="margin:0 0 12px;padding-left:20px;">')
                list_tag = tag
            out.append(f'<li style="margin:0 0 6px;line-height:1.5;">{_inline((bullet or number).group(1))}</li>')
        else:
            close_list()
            para.append(line)
    flush_para(); close_list()
    return "\n".join(out)


def numbers_html(nums):
    rows = []
    for i, name in enumerate(ORDER):
        d = nums.get(name)
        bg = "#FFFFFF" if i % 2 == 0 else "#F6F5F1"
        if d is None:
            level, chg, color = "n/a", "n/a", GREY
        else:
            level = fmt_value(d["value"], d["kind"])
            chg = fmt_change(d["value"], d["prev"], d["kind"])
            color = UP if chg.startswith("▲") else DOWN
        rows.append(
            f'<tr style="background:{bg};">'
            f'<td style="padding:7px 10px;font-weight:600;">{html.escape((d or {}).get("label", name))}</td>'
            f'<td style="padding:7px 10px;text-align:right;">{level}</td>'
            f'<td style="padding:7px 10px;text-align:right;font-weight:700;color:{color};">{chg}</td></tr>'
        )
    return (f'<table role="presentation" cellspacing="0" cellpadding="0" '
            f'style="width:100%;border-collapse:collapse;border:1px solid {LINE};font-size:14px;">'
            + "".join(rows) + "</table>")


def build_html(today, nums, written, demo):
    demo_note = (f'<p style="margin:0 0 12px;padding:8px 10px;background:#FFF4E5;'
                 f'color:{DOWN};font-size:13px;">Sample numbers (demo mode), not real market data.</p>'
                 if demo else "")
    return f"""<!doctype html><html><body style="margin:0;background:#F6F5F1;">
<div style="max-width:640px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;color:{INK};font-size:15px;">
<div style="background:{INK};color:#FFFFFF;padding:20px 24px;">
<div style="font-size:12px;letter-spacing:1px;color:#B9BEC7;">MORNING BRIEF</div>
<div style="font-size:22px;font-weight:700;margin-top:4px;">{html.escape(today)}</div>
</div>
<div style="background:#FFFFFF;padding:20px 24px;">
{demo_note}
<h2 style="margin:0 0 10px;font-size:17px;">Markets at the close</h2>
{numbers_html(nums)}
{md_to_html(written)}
<p style="margin:24px 0 0;font-size:12px;color:{GREY};">For education only. Not investment advice.</p>
</div></div></body></html>"""


def send_email(subject, text, html_body=None):
    user = os.getenv("GMAIL_ADDRESS")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    to = os.getenv("BRIEF_TO") or user
    if not (user and pw):
        print("  (email skipped: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set)")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"  emailed to {to}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="use sample numbers")
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args()

    today = dt.date.today().strftime("%A, %B %d, %Y")
    print("Fetching numbers...")
    nums = get_numbers(demo=args.demo)
    table = numbers_table(nums)

    print("Writing brief...")
    try:
        written = write_with_claude(table, today)
    except Exception as e:
        print(f"  ! {e}", file=sys.stderr)
        written = None
    if written is None:
        written = ("_AI sections not generated. Set ANTHROPIC_API_KEY to have Claude "
                   "write sections 1-5._")

    demo_note = "\n\n> Sample numbers (demo mode), not real market data.\n" if args.demo else ""
    brief = (f"# Morning Brief: {today}{demo_note}\n\n{table}\n\n{written}\n\n"
             "---\n_For education only. Not investment advice._\n")

    out_dir = Path(__file__).parent / "briefs"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{dt.date.today().isoformat()}{'-demo' if args.demo else ''}.md"
    path.write_text(brief, encoding="utf-8")
    print(f"  saved {path}")

    html_body = build_html(today, nums, written, args.demo)
    (out_dir / path.name.replace(".md", ".html")).write_text(html_body, encoding="utf-8")

    if not args.no_email:
        send_email(f"Morning Brief: {today}", brief, html_body)


if __name__ == "__main__":
    main()
