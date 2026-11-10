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
MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5-5")          # research + writing (uses web search)
FAST_MODEL = os.getenv("BRIEF_FAST_MODEL", "claude-haiku-5-5")  # formatting, level rewrites, podcast scripts (cheaper)
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


# Live quotes tried at run time (first one that works wins). ETFs are backups when
# the free plan doesn't include an index; the % change tracks the index closely.
LIVE_SYMBOLS = {
    "S&P 500": [("^GSPC", "S&P 500"), ("SPY", "S&P 500 (via SPY)")],
    "Nasdaq": [("^IXIC", "Nasdaq"), ("QQQ", "Nasdaq 100 (via QQQ)")],
    "VIX": [("^VIX", "VIX")],
    "WTI oil ($/bbl)": [("CLUSD", "WTI oil ($/bbl)"), ("USO", "Oil (via USO)")],
    "Natural gas ($/MMBtu)": [("NGUSD", "Natural gas ($/MMBtu)")],
    "USD/CAD": [("USDCAD", "USD/CAD")],
}
LIVE_KIND = {"USD/CAD": "fx", "VIX": "index", "S&P 500": "index", "Nasdaq": "index"}


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

    # Live quotes (as of the run time) replace the daily closes where the FMP plan allows.
    if fmp_key:
        for name, choices in LIVE_SYMBOLS.items():
            for sym, label in choices:
                try:
                    v, p, d = fmp_quote(sym, fmp_key)
                    if v <= 0 or p <= 0:
                        raise RuntimeError("empty quote")
                    kind = (out.get(name) or {}).get("kind") or LIVE_KIND.get(name, "price")
                    out[name] = {"value": v, "prev": p, "kind": kind, "asof": d, "label": label, "live": True}
                    break
                except Exception as e:
                    print(f"  (live {name} via {sym} unavailable: {str(e)[:80]})", file=sys.stderr)
    # The TSX, gold and Bitcoin quotes from FMP above are live too
    for name in FMP_SYMBOLS:
        if out.get(name):
            out[name]["live"] = True
    live = [n for n, d in out.items() if d and d.get("live")]
    print(f"  live quotes: {', '.join(live) if live else 'none'}")

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

Here are the latest market numbers, mostly live quotes as of this morning (about 11 AM ET)
compared with the previous close; yields are the last close. Use them exactly;
never invent or change a number:

{table}

Use web search to find out WHY the biggest moves happened and WHAT is scheduled
for today (economic data, Fed / Bank of Canada events, major earnings).
Also search for notable M&A and capital markets deals (acquisitions, mergers,
IPOs, big financings) announced in the last few days, especially Canadian deals
or very large global ones.
Prefer original sources (central banks, statistics agencies, company releases)
and reputable outlets. Write everything in your own words: do not copy
sentences from articles.

Write the brief in plain Markdown with exactly these sections:

## Headline
The day's biggest story in at most 9 words.

## Mood
One or two sentences on the overall tone (risk-on / risk-off, what led).

## What moved and why
Up to 3 stories. For each: a bold one-line title, a tag in brackets (one of
Stocks, Rates, Energy, Commodities, FX, Macro, Earnings, Canada, Crypto), then
"Why it happened:" and "Why it matters:" lines, one or two sentences each, then
a "Full story:" line followed by 3-4 short paragraphs (about 150-200 words in
total) for readers who want every detail: the background, what exactly happened
with the key numbers, who wins and loses, and what to watch next.

## Deals
Up to 3 notable M&A or capital markets deals from the last few days. For each,
a bullet starting with a bold "Type: buyer → target" (type is Acquisition,
Merger, IPO or Financing), the deal size, and one or two sentences on the logic.
Then a "Full story:" line followed by 2-3 short paragraphs (about 120-160 words)
covering the terms, the strategic rationale, valuation if reported, and what it
signals. Write "None" if you found none.

## Already out today
Economic data and scheduled events that already happened today before you are
writing (for example 8:30 AM ET releases like CPI or jobs, earnings released
before the open, or a central bank decision). One bullet each: time (ET), the
event, the actual result versus what was expected, then one short sentence on
what it means. Format: "8:30 AM: US October CPI. 2.9% vs 3.0% expected. Cooler
inflation keeps a December rate cut in play." Write "None" if nothing came out.

## What's coming today
Only events still AHEAD of the time you are writing (later today, ET). If
nothing else is scheduled today, list the most important events for the next
few days and put the day in the time (e.g. "Thu 8:30 AM"). Bullets: time, the
event, and why it matters in a few words. Never list something that already
happened; that goes in "Already out today".

## Canada corner
2-4 short bullets. Always include at least 2: the TSX, Bank of Canada, the
loonie, Canadian data or a notable Canadian company.

## The takeaway
Three short interview lines, one for each type of candidate, each 1-2 sentences:
- IB & Markets: start with "If someone asks you about markets today:" and give the market view.
- Consulting: the strategic or business angle on today's biggest story, e.g. what a
  company should do or what it means for an industry.
- Accounting: the accounting or audit angle, e.g. how a deal, write-down or
  earnings item would be accounted for, or what an auditor would focus on.

## Sources
A numbered list of the pages you used, as Markdown links.

Rules: keep everything except the full stories under 550 words, plain language,
no hype, no buy/sell recommendations. Do not repeat the numbers table. Don't comment on data timing.
"""


def write_with_claude(table, today):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = {
        "model": MODEL,
        "max_tokens": 8000,
        "tools": [{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": int(os.getenv("BRIEF_MAX_SEARCHES", "5"))}],
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
    res = r.json()
    if res.get("stop_reason") == "max_tokens":
        print("  ! the brief hit the length limit and may be cut off", file=sys.stderr)
    blocks = res.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


CITE_RE = re.compile(r"</?cite[^>]*>", re.I)


def clean_text(v):
    """Remove citation tags and stray markup the model sometimes adds."""
    if isinstance(v, str):
        return re.sub(r"\s{2,}", " ", CITE_RE.sub("", v)).strip()
    if isinstance(v, list):
        return [clean_text(x) for x in v]
    if isinstance(v, dict):
        return {k: clean_text(x) for k, x in v.items()}
    return v


def parse_brief(text):
    """Pull the JSON object out of Claude's reply. Returns a dict, or None."""
    import json
    if not text:
        return None
    t = re.sub(r"```(?:json)?", "", text)
    t = CITE_RE.sub("", t)
    dec = json.JSONDecoder(strict=False)
    d = None
    for m in re.finditer(r"\{", t):  # try each '{' until one parses as a full object
        try:
            obj, _ = dec.raw_decode(t[m.start():])
        except Exception:
            continue
        if isinstance(obj, dict) and ("stories" in obj or "mood" in obj or "headline" in obj):
            d = obj
            break
    if d is None:
        return None
    d = clean_text(d)
    d.setdefault("headline", "Today's markets")
    for k in ("stories", "calendar", "canada", "sources", "deals", "results"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def looks_like_json(text):
    return bool(text) and "{" in text and ('"headline"' in text or '"stories"' in text)


LEVELS = ["intern", "analyst", "partner"]
LEVEL_NAMES = {"intern": "Intern", "analyst": "Analyst", "partner": "Partner"}
LEVEL_REWRITE = {
    "intern": ("Rewrite this market brief for complete beginners (the 'Intern' level): students new to "
               "markets. Use plain, friendly language and short sentences. The first time a finance term "
               "appears, explain it in a few words in brackets, e.g. 'yields (the interest rate on bonds)'. "
               "Focus on what happened and why it matters to everyday people and new investors. Keep the "
               "same stories, deals, numbers and facts; don't add new facts. Also fill 'glossary' with 4-6 "
               "key terms from today's brief and a one-sentence plain-English meaning for each."),
    "analyst": ("Copy this market brief into the publish_brief tool. Keep the wording and facts; don't add "
                "anything."),
    "partner": ("Rewrite this market brief for experienced finance professionals (the 'Partner' level). Be "
                "concise and technical; don't explain basic terms. In each 'why it matters', add the "
                "second-order effects: implications for valuation multiples, rates and credit spreads, "
                "sector positioning, and deal or financing activity. Keep the same stories, deals, numbers "
                "and facts; don't invent new data. Leave glossary empty."),
}


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "mood": {"type": "string"},
        "stories": {"type": "array", "items": {"type": "object", "properties": {
            "tag": {"type": "string"}, "title": {"type": "string"},
            "why_happened": {"type": "string"}, "why_matters": {"type": "string"},
            "full": {"type": "string", "description": "The full story: 3-4 paragraphs separated by blank lines"}},
            "required": ["title", "why_matters"]}},
        "deals": {"type": "array", "items": {"type": "object", "properties": {
            "type": {"type": "string"}, "buyer": {"type": "string"}, "target": {"type": "string"},
            "value": {"type": "string"}, "why": {"type": "string"},
            "full": {"type": "string", "description": "The full story: 2-3 paragraphs separated by blank lines"}},
            "required": ["buyer"]}},
        "calendar": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "string"}, "event": {"type": "string"}, "why": {"type": "string"}},
            "required": ["event"]}},
        "calendar_note": {"type": "string"},
        "results": {"type": "array", "description": "Data and events already released today, with actual vs expected",
            "items": {"type": "object", "properties": {
            "time": {"type": "string"}, "event": {"type": "string"},
            "actual": {"type": "string", "description": "The actual figure, short, e.g. '2.9% y/y'"},
            "expected": {"type": "string", "description": "The forecast, short, e.g. '3.0%'; empty if none"},
            "why": {"type": "string", "description": "One short sentence on what it means"}},
            "required": ["event"]}},
        "canada": {"type": "array", "items": {"type": "string"}},
        "glossary": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["term", "meaning"]}},
        "takeaway": {"type": "string", "description": "The IB & Markets interview line"},
        "takeaway_consulting": {"type": "string", "description": "The Consulting interview line"},
        "takeaway_accounting": {"type": "string", "description": "The Accounting interview line"},
        "sources": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}}}},
    },
    "required": ["headline", "mood", "stories", "takeaway"],
}


def structure_brief(md, level="analyst"):
    """Second step: turn the written brief into the app's format, rewritten for one level.
    A forced tool call means the reply always comes back as a valid object."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key or not md:
        return None
    body = {
        "model": FAST_MODEL, "max_tokens": 6000,
        "tools": [{"name": "publish_brief",
                   "description": "Publish the morning brief to the app.",
                   "input_schema": BRIEF_SCHEMA}],
        "tool_choice": {"type": "tool", "name": "publish_brief"},
        "messages": [{"role": "user", "content":
            LEVEL_REWRITE.get(level, LEVEL_REWRITE["analyst"]) + " "
            "Put each story's and deal's 'Full story' text in its full field (rewritten for the same "
            "audience), keeping the paragraph breaks. Use plain text only (no citation tags, HTML or markdown). "
            "Leave deals empty if the brief says none. If nothing is scheduled today, leave "
            "calendar empty and say so in calendar_note. Put the 'Already out today' items in results "
            "(leave it empty if none) and only future events in calendar.\n\n" + CITE_RE.sub("", md)}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json=body, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:300]}")
    for blk in r.json().get("content", []):
        if blk.get("type") == "tool_use":
            d = clean_text(blk.get("input") or {})
            d.setdefault("headline", "Today's markets")
            for k in ("stories", "calendar", "canada", "sources", "deals", "glossary", "results"):
                if not isinstance(d.get(k), list):
                    d[k] = []
            return d
    return None


def parse_markdown_brief(md):
    """Backup that needs no AI: split the written brief by its fixed headings."""
    if not md or "## " not in md:
        return None
    md = CITE_RE.sub("", md)
    secs, cur = {}, None
    for raw in md.splitlines():
        line = raw.strip()
        h = re.match(r"^#{1,3}\s*(?:\d+\.\s*)?(.+?)\s*#*$", line)
        if h:
            cur = h.group(1).strip().lower()
            secs[cur] = []
        elif cur is not None and line:
            secs[cur].append(line)

    def get(*keys):
        for k, v in secs.items():
            if any(x in k for x in keys):
                return v
        return []

    def plain(t):
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", t)
        return re.sub(r"\*\*|__|^[-*•]\s+|^#+\s*", "", t).strip()

    d = {"headline": plain((get("headline") or ["Today's markets"])[0]),
         "mood": " ".join(plain(x) for x in get("mood")),
         "stories": [], "deals": [], "calendar": [], "calendar_note": "", "canada": [], "results": [],
         "takeaway": "", "sources": []}

    st = None
    infull = False
    for line in get("what moved"):
        low = plain(line).lower()
        if low.startswith("full story"):
            infull = True
            rest = plain(line).split(":", 1)[-1].strip()
            if st is not None and rest:
                st["full"] = (st.get("full", "") + "\n\n" + rest).strip()
            continue
        if infull and st is not None and not line.startswith("**"):
            st["full"] = (st.get("full", "") + "\n\n" + plain(line)).strip()
            continue
        infull = False
        if low.startswith("why it happened"):
            if st: st["why_happened"] = plain(line).split(":", 1)[-1].strip()
        elif low.startswith("why it matters"):
            if st: st["why_matters"] = plain(line).split(":", 1)[-1].strip()
        elif line.startswith("**") or line[:1] in "-*•" or not st:
            tag = re.search(r"\[([A-Za-z &]+)\]", line)
            title = plain(re.sub(r"\[[A-Za-z &]+\]", "", line)).rstrip(" -–—:")
            st = {"tag": tag.group(1).strip() if tag else "Markets", "title": title,
                  "why_happened": "", "why_matters": ""}
            d["stories"].append(st)
    d["stories"] = [x for x in d["stories"] if x["title"]][:3]

    infull = False
    for line in get("deals"):
        t = plain(line)
        if not t or t.lower().startswith("none"):
            continue
        if t.lower().startswith("full story"):
            infull = True
            rest = t.split(":", 1)[-1].strip()
            if d["deals"] and rest:
                d["deals"][-1]["full"] = (d["deals"][-1].get("full", "") + "\n\n" + rest).strip()
            continue
        if infull and d["deals"] and not re.match(r"^[-*•]?\s*\*\*", line):
            d["deals"][-1]["full"] = (d["deals"][-1].get("full", "") + "\n\n" + t).strip()
            continue
        infull = False
        bold = re.match(r"^[-*•]?\s*\*\*(.+?)\*\*[\s:.\-–—]*(.*)$", line)
        head, rest = (bold.group(1), bold.group(2)) if bold else (t, "")
        typ = "Deal"
        if ":" in head and len(head.split(":", 1)[0]) < 20:
            typ, head = [x.strip() for x in head.split(":", 1)]
        val = re.search(r"((?:US|C|CA)?\$\s?[\d.,]+\s?(?:billion|million|B|M|bn|mm)?)", t)
        buyer, target = plain(head), ""
        if "→" in buyer:
            buyer, target = [x.strip() for x in buyer.split("→", 1)]
        d["deals"].append({"type": typ, "buyer": buyer, "target": target,
                           "value": val.group(1) if val else "Undisclosed", "why": plain(rest)})
    d["deals"] = d["deals"][:3]

    for line in get("coming", "calendar", "on deck"):
        t = plain(line)
        if not t:
            continue
        m = re.match(r"^(.{1,28}?(?:AM|PM|ET|open|close|day|Today|Mon|Tue|Wed|Thu|Fri)[^:]*):\s*(.+)$", t, re.I)
        when, rest = (m.group(1), m.group(2)) if m else ("", t)
        parts = re.split(r"\s+[—–-]\s+|\.\s+", rest, maxsplit=1)
        d["calendar"].append({"time": when.strip(), "event": parts[0].strip().rstrip("."),
                              "why": parts[1].strip() if len(parts) > 1 else ""})
    if len(d["calendar"]) == 1 and not d["calendar"][0]["time"] and "nothing" in d["calendar"][0]["event"].lower():
        d["calendar_note"], d["calendar"] = d["calendar"][0]["event"], []

    d["canada"] = [plain(x) for x in get("canada") if plain(x)][:4]

    for line in get("already out", "out today", "results"):
        t = plain(line)
        if not t or t.lower().rstrip(".") == "none":
            continue
        m = re.match(r"^(.{1,20}?(?:AM|PM|ET))[^:]*:\s*(.+)$", t, re.I)
        when, rest = (m.group(1), m.group(2)) if m else ("", t)
        parts = [x.strip() for x in re.split(r"\.\s+", rest, maxsplit=2)]
        d["results"].append({"time": when.strip(), "event": parts[0].rstrip("."),
                             "actual": parts[1].rstrip(".") if len(parts) > 1 else "", "expected": "",
                             "why": parts[2] if len(parts) > 2 else ""})
    d["results"] = d["results"][:5]

    for line in get("takeaway", "interview"):
        t = plain(line)
        low = t.lower()
        if low.startswith("consulting"):
            d["takeaway_consulting"] = t.split(":", 1)[-1].strip()
        elif low.startswith("accounting"):
            d["takeaway_accounting"] = t.split(":", 1)[-1].strip()
        elif low.startswith("ib") or "markets today" in low or not d["takeaway"]:
            d["takeaway"] = re.sub(r"^ib\s*(?:&|and)\s*markets\s*:\s*", "", t, flags=re.I).strip()

    for line in get("sources"):
        for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", line):
            d["sources"].append({"title": title, "url": url})

    return d if (d["stories"] or d["mood"]) else None


def brief_markdown(d, full=False):
    """Turn the structured brief into Markdown (full=True adds the long-form stories, for the podcast)."""
    out = ["## 1. The numbers", d.get("mood", ""), "", "## 2. What moved and why"]
    for st in d["stories"]:
        out += [f"**{st.get('title','')}**", f"Why it happened: {st.get('why_happened','')}",
                f"Why it matters: {st.get('why_matters','')}"]
        if full and st.get("full"):
            out += [f"Full story: {st['full']}"]
        out.append("")
    if d["deals"]:
        out += ["## Deals"]
        for x in d["deals"]:
            who = x.get("buyer", "")
            if x.get("target"):
                who += f" → {x['target']}"
            out += [f"- **{x.get('type', 'Deal')}: {who}** ({x.get('value', 'Undisclosed')}). {x.get('why', '')}"]
            if full and x.get("full"):
                out += [f"  Full story: {x['full']}"]
        out.append("")
    if d.get("results"):
        out += ["## Already out today"]
        for r_ in d["results"]:
            exp = f" vs {r_['expected']} expected" if r_.get("expected") else ""
            out.append(f"- {r_.get('time', '')}: {r_.get('event', '')}. {r_.get('actual', '')}{exp}. {r_.get('why', '')}".strip())
        out.append("")
    out += ["## 3. What's coming today"]
    for c in d["calendar"]:
        out.append(f"- {c.get('time','')}: {c.get('event','')}. {c.get('why','')}".strip())
    if d.get("calendar_note"):
        out.append(d["calendar_note"])
    out += ["", "## 4. Canada corner"] + [f"- {x}" for x in d["canada"]]
    out += ["", "## 5. The takeaway", d.get("takeaway", "")]
    if d.get("takeaway_consulting"):
        out.append(f"Consulting angle: {d['takeaway_consulting']}")
    if d.get("takeaway_accounting"):
        out.append(f"Accounting angle: {d['takeaway_accounting']}")
    if d["sources"]:
        out += ["", "## Sources"] + [f"{i}. [{x.get('title','Source')}]({x.get('url','')})"
                                      for i, x in enumerate(d["sources"], 1)]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 4b. THE PODCAST (needs OPENAI_API_KEY; skipped if it's not set)
# ---------------------------------------------------------------------------
HOSTS = {  # one host. "ash" was the deeper of the two voices.
    "ALEX": os.getenv("VOICE_ALEX", "ash"),
}
FALLBACK_VOICES = {"ALEX": "onyx"}  # used only if the newer model fails
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
# Speeds up the finished episode without changing pitch (1.0 = normal, 1.2 = 20% faster)
PODCAST_SPEED = float(os.getenv("PODCAST_SPEED", "1.18"))

# Acting direction for the host (how he should sound, not what he says)
HOST_STYLE = {
    "ALEX": ("You are ALEX, the solo host of a daily markets podcast. Deep, calm, confident "
             "voice with quiet authority, like a senior analyst delivering the morning call to "
             "the trading floor. Natural and conversational, never like an announcer or a "
             "radio ad. Brisk pace with no long pauses; pick up speed in the quick market "
             "rundown, slow down slightly for the big stories. Emphasize key numbers and "
             "names. Never sound flat or like you're reading."),
}

PODCAST_PROMPT = """Write the script for today's episode of "The Morning Brief", a daily
markets podcast with one host, ALEX. Audience: finance students and young
professionals in Canada recruiting for IB, consulting and equity research.

The feel: a sharp analyst giving the morning call. Authentic, direct and
informed. It's about what's going on in markets and why, not a list of prices.

Structure, in this order:
1. Open (2 sentences): "Morning, it's [day and date]." Then the headline story
   in one line.
2. The tape (rapid fire, about 20% of the episode): go through the markets
   that matter quickly, one short line each: the move plus a few words on why.
   e.g. "Oil, down about three percent, as supply worries fade." Group them
   naturally (stocks, then rates, then commodities and currencies). Skip
   anything that barely moved. Keep it fast.
3. The big stories (the heart of the episode, about 45%): the 2-3 stories
   actually driving markets. For each: what happened, why it happened, who
   wins and loses, and what it means next. Connect the dots between stories
   where they're linked.
4. Deals (about 15%): the M&A and capital markets deals in the brief. Who's
   buying whom, for how much, the strategic logic, and what it signals about
   the deal market. If there are no deals in the brief, skip this section
   without mentioning it.
5. Canada (short): what's moving the TSX, banks, energy, the Bank of Canada
   or the loonie, if not already covered.
6. Data and what's next (short): any data already out today, with the actual
   number versus what was expected, then the key events still ahead and why
   they matter.
7. The interview angle: from the brief's takeaway, one quick line each for
   IB and markets candidates, consulting candidates and accounting candidates.
8. Sign-off: one plain line, then "This show is written and voiced by AI, so
   double-check anything important. It's for education only, not investment
   advice."

How ALEX talks:
- Like a person, not a script: contractions, everyday words, a mix of short and
  longer sentences. Use smooth spoken transitions ("Now, the bigger story...",
  "On the deal front...", "Closer to home...").
- Say numbers the way people speak ("down about three percent", "the ten-year
  at four-eighteen", "a two-point-one billion dollar deal").
- Do NOT use: jokes, "wild", "crazy", "buckle up", "let's dive in", "folks",
  "you heard that right", "without further ado", rhetorical questions to the
  listener, or over-the-top excitement.

Rules:
- About 900-1,000 words.
- Use only facts and numbers from the brief. Do not add new facts.
- No sound effects, stage directions, music cues, headings or markdown.
- Write it as paragraphs. Every paragraph must start with "ALEX:". Nothing else
  in the output.

THE BRIEF:
{brief}
"""


def claude_plain(prompt, max_tokens=3000):
    key = os.getenv("ANTHROPIC_API_KEY")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": FAST_MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=180,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:500]}")
    return "".join(b.get("text", "") for b in r.json().get("content", [])
                   if b.get("type") == "text").strip()


def parse_script(script):
    lines = []
    for raw in script.splitlines():
        m = re.match(r"^\s*\**\s*(ALEX)\s*\**\s*:\s*(.+)$", raw.strip(), re.I)
        if m:
            lines.append((m.group(1).upper(), m.group(2).strip()))
    return lines


TTS_STATS = {"main": 0, "fallback": 0}


def tts(text, who, key):
    headers = {"Authorization": f"Bearer {key}"}
    body = {"model": TTS_MODEL, "voice": HOSTS[who], "input": text[:4000],
            "response_format": "mp3", "instructions": HOST_STYLE[who]}
    r = requests.post("https://api.openai.com/v1/audio/speech", headers=headers,
                      json=body, timeout=120)
    if r.status_code == 200:
        TTS_STATS["main"] += 1
        return r.content
    # Fallback: older, flatter model with classic voices
    print(f"  ! {TTS_MODEL} failed ({r.status_code}): {r.text[:200]}", file=sys.stderr)
    body = {"model": "tts-1", "voice": FALLBACK_VOICES[who], "input": text[:4000],
            "response_format": "mp3", "speed": 1.08}
    r = requests.post("https://api.openai.com/v1/audio/speech", headers=headers,
                      json=body, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI TTS error {r.status_code}: {r.text[:300]}")
    TTS_STATS["fallback"] += 1
    return r.content


def find_ffmpeg():
    import shutil
    import subprocess
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "imageio-ffmpeg"],
                       check=False)
        try:
            import imageio_ffmpeg
        except ImportError:
            return None
    return imageio_ffmpeg.get_ffmpeg_exe()


def speed_up(mp3_path, speed):
    """Re-encode the joined MP3 at a faster tempo (pitch stays the same)."""
    import subprocess
    if abs(speed - 1.0) < 0.01:
        return
    exe = find_ffmpeg()
    if not exe:
        print("  (speed-up skipped: ffmpeg not available)")
        return
    tmp = mp3_path.with_name(mp3_path.stem + "-fast.mp3")
    res = subprocess.run([exe, "-y", "-loglevel", "error", "-i", str(mp3_path),
                          "-filter:a", f"atempo={speed}", "-b:a", "128k", str(tmp)],
                         capture_output=True, text=True)
    if res.returncode == 0 and tmp.exists():
        tmp.replace(mp3_path)
        print(f"  sped up to {speed}x")
    else:
        print(f"  ! speed-up failed: {res.stderr[:200]}", file=sys.stderr)


LEVEL_PODCAST = {
    "intern": ("\n\nAUDIENCE: this is the 'Intern' edition for complete beginners. Explain every finance "
               "term in simple words the first time it comes up, use everyday comparisons, go a little "
               "slower, and focus on what happened and why it matters. Skip technical detail like "
               "valuation multiples. About 800-900 words."),
    "analyst": "",
    "partner": ("\n\nAUDIENCE: this is the 'Partner' edition for experienced finance professionals. Don't "
                "explain basic terms. Be dense and analytical: spend more time on second-order effects for "
                "valuations, rates, credit spreads, sector positioning and deal activity, and on what to "
                "watch. About 1,000-1,100 words."),
}


def make_podcast(brief, out_dir, stem, level="analyst"):
    """Returns the path to the MP3, or None if skipped/failed."""
    key = os.getenv("OPENAI_API_KEY")
    if not key or not os.getenv("ANTHROPIC_API_KEY"):
        print("  (podcast skipped: needs OPENAI_API_KEY and ANTHROPIC_API_KEY)")
        return None
    try:
        script = claude_plain(PODCAST_PROMPT.format(brief=brief) + LEVEL_PODCAST.get(level, ""))
        lines = parse_script(script)
        if not lines:
            raise RuntimeError("script had no ALEX: lines")
        (out_dir / f"{stem}-{level}-script.txt").write_text(script, encoding="utf-8")
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:  # record several lines at once, keep order
            audio = b"".join(pool.map(lambda wt: tts(wt[1], wt[0], key), lines))
        print(f"  voices: {TTS_STATS['main']} lines on {TTS_MODEL}, "
              f"{TTS_STATS['fallback']} on the older fallback model")
        mp3 = out_dir / f"{stem}-{level}-podcast.mp3"
        mp3.write_bytes(audio)
        speed_up(mp3, PODCAST_SPEED)
        print(f"  {level} podcast saved {mp3.name} ({len(lines)} lines, {len(audio)//1024} KB)")
        return mp3
    except Exception as e:
        print(f"  ! {level} podcast: {e}", file=sys.stderr)
        return None


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


def send_email(subject, text, html_body=None, attachment=None):
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
    if attachment:
        msg.add_attachment(Path(attachment).read_bytes(), maintype="audio",
                           subtype="mpeg", filename=Path(attachment).name)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"  emailed to {to}")


# ---------------------------------------------------------------------------
# 6. THE WEBSITE (written to docs/, which GitHub Pages serves)
# ---------------------------------------------------------------------------
SITE_NAME = "Leveraged"
KEEP_AUDIO_DAYS = 30  # older MP3s are deleted so the project doesn't get too big

# Which numbers appear under each tab on the Today page
MARKET_TABS = [
    ("Stocks", ["S&P 500", "Nasdaq", "TSX", "VIX"]),
    ("Rates", ["US 10Y yield", "Canada 10Y yield"]),
    ("Commodities", ["WTI oil ($/bbl)", "Gold ($/oz)", "Natural gas ($/MMBtu)"]),
    ("FX & crypto", ["USD/CAD", "US dollar index", "Bitcoin"]),
]

SITE_CSS = """
:root{--bg:#0A0D12;--card:#131820;--card2:#1A202B;--ink:#F3F5F8;--muted:#98A1B0;--line:#232B37;--line2:#2F3846;--g:#3DDC97;--gd:#1A8A5B;--up:#3DDC97;--down:#FF9A5C;--upbg:rgba(61,220,151,.12);--downbg:rgba(255,154,92,.13);--gold:#E3B341}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{scroll-behavior:smooth;color-scheme:dark}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Geist,Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;font-feature-settings:'ss01';font-size:16px;line-height:1.5;-webkit-font-smoothing:antialiased}
a{color:var(--g)}
button{font:inherit;color:inherit}
.wrap{max-width:640px;margin:0 auto;padding:14px 16px calc(110px + env(safe-area-inset-bottom))}
.appbar{display:flex;justify-content:space-between;align-items:center;padding:6px 2px 14px}
.brand{display:flex;align-items:center;gap:8px;font-family:Geist,Inter,sans-serif;font-weight:700;font-size:17px;letter-spacing:-.2px}
.logo{width:26px;height:26px;border-radius:8px;background:var(--card2);border:1px solid var(--line2);display:flex;align-items:center;justify-content:center}
.barr{display:flex;align-items:center;gap:8px}
.gear{width:40px;height:40px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--muted);display:flex;align-items:center;justify-content:center}
.datepill{font-size:13px;font-weight:600;color:var(--muted);background:var(--card);border:1px solid var(--line);padding:6px 12px;border-radius:999px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:26px;padding:22px 20px 18px;position:relative;overflow:hidden}
.hero::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:var(--g);opacity:.8}
.hero::after{content:"";position:absolute;right:-70px;top:-70px;width:200px;height:200px;border-radius:50%;border:1px solid rgba(255,255,255,.05)}
.eyebrow{font-size:11px;font-weight:700;letter-spacing:1.4px;text-transform:uppercase;color:var(--g)}
.hero h1{font-family:Geist,Inter,sans-serif;font-size:29px;line-height:1.12;letter-spacing:-1px;margin:8px 0 10px;font-weight:700}
.hero .mood{color:#B9C0CC;font-size:15px;margin:0 0 18px}
.player{background:var(--bg);border:1px solid var(--line);border-radius:18px;padding:12px;display:flex;align-items:center;gap:12px}
.pbtn{width:52px;height:52px;border-radius:50%;border:0;background:var(--g);color:#06140E;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;transition:transform .15s;box-shadow:0 0 0 6px rgba(61,220,151,.12)}
.pbtn:active{transform:scale(.93)}
.pmeta{flex:1;min-width:0}
.ptitle{font-size:14px;font-weight:600;margin:0 0 6px}
.bar{position:relative;height:6px;border-radius:999px;background:var(--card2);cursor:pointer}
.bar>.pfill{position:absolute;left:0;top:0;bottom:0;border-radius:999px;background:var(--g);width:0}
.bar input{position:absolute;inset:-10px 0;width:100%;opacity:0;cursor:pointer;margin:0}
.ptime{font-family:'Geist Mono',Geist,monospace;display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:6px;font-variant-numeric:tabular-nums}
.speed{min-width:52px;height:36px;border-radius:10px;border:1px solid var(--line2);background:transparent;color:var(--ink);font-size:13px;font-weight:700;cursor:pointer}
.noaudio{font-size:13px;color:var(--muted)}
.sec{margin:28px 0 0}
.sech{display:flex;justify-content:space-between;align-items:baseline;margin:0 2px 12px}
.sech h2{font-family:Geist,Inter,sans-serif;font-size:19px;letter-spacing:-.3px;margin:0}
.sech span{font-size:13px;color:var(--muted)}
.tabs{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:2px;margin:0 0 12px}
.tabs::-webkit-scrollbar{display:none}
.tab{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:999px;padding:8px 14px;font-size:14px;font-weight:600;white-space:nowrap;cursor:pointer;min-height:40px;transition:all .2s}
.tab[aria-selected="true"]{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.panel{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.panel[hidden]{display:none}
.panel.show .tile{animation:rise .4s cubic-bezier(.2,.8,.2,1) backwards}
.panel.show .tile:nth-child(2){animation-delay:.05s}.panel.show .tile:nth-child(3){animation-delay:.1s}.panel.show .tile:nth-child(4){animation-delay:.15s}
.tile{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px}
.tile .n{font-size:13px;font-weight:600;color:var(--muted);display:flex;justify-content:space-between;gap:6px;align-items:center}
.tile .live,.tile .close{font-family:'Geist Mono',Geist,monospace;font-size:9px;font-weight:700;letter-spacing:.8px;padding:2px 5px;border-radius:5px;flex-shrink:0}
.tile .live{color:var(--g);background:var(--upbg)}.tile .close{color:var(--muted);background:var(--card2)}
.tile .v{font-family:'Geist Mono',Geist,monospace;letter-spacing:-.8px;font-size:24px;font-weight:700;letter-spacing:-.5px;margin:4px 0 8px;font-variant-numeric:tabular-nums}
.chg{font-family:'Geist Mono',Geist,monospace;display:inline-flex;align-items:center;gap:4px;font-size:13px;font-weight:700;padding:3px 8px;border-radius:999px;font-variant-numeric:tabular-nums}
.chg.up{color:var(--up);background:var(--upbg)}.chg.down{color:var(--down);background:var(--downbg)}.chg.na{color:var(--muted);background:var(--card2)}
.cards{display:flex;gap:12px;overflow-x:auto;scroll-snap-type:x mandatory;scrollbar-width:none;margin:0 -16px;padding:2px 16px 6px}
.cards::-webkit-scrollbar{display:none}
.story{flex:0 0 86%;scroll-snap-align:start;background:var(--card);border:1px solid var(--line);border-radius:22px;padding:18px;display:flex;flex-direction:column;gap:10px;min-height:230px}
.story .top{display:flex;justify-content:space-between;align-items:center}
.tag{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--g);background:var(--upbg);padding:4px 9px;border-radius:999px}
.count{font-size:12px;color:var(--muted);font-weight:600}
.story h3{font-family:Geist,Inter,sans-serif;font-size:20px;line-height:1.25;letter-spacing:-.3px;margin:2px 0 0}
.story .lbl,.deal .lbl{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);margin:0 0 2px}
.story p{margin:0;font-size:15px;color:#D6DBE3}
.more{margin-top:auto;align-self:flex-start;border:1px solid var(--line2);background:var(--card2);border-radius:999px;padding:8px 14px;font-size:14px;font-weight:600;cursor:pointer;min-height:40px}
.why{display:grid;grid-template-rows:0fr;transition:grid-template-rows .35s ease}
.why>div{overflow:hidden}
.why.open{grid-template-rows:1fr}
.dots{display:flex;justify-content:center;gap:6px;margin-top:10px}
.dots span{width:7px;height:7px;border-radius:999px;background:var(--line2);transition:all .3s}
.dots span.on{width:22px;background:var(--ink)}
.deals{display:flex;flex-direction:column;gap:10px}
.deal{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px 18px}
.deal .row{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 8px}
.dtype{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--gold);background:rgba(227,179,65,.12);padding:4px 9px;border-radius:999px}
.dval{font-family:'Geist Mono',Geist,monospace;font-weight:700;font-size:17px;font-variant-numeric:tabular-nums}
.parties{font-family:Geist,Inter,sans-serif;font-size:18px;font-weight:700;letter-spacing:-.2px;line-height:1.3;margin:0 0 6px}
.parties .arrow{color:var(--muted);font-weight:500;margin:0 6px}
.deal p{margin:0;font-size:14px;color:#C6CCD6}
.timeline{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:6px 18px}
.ev{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid var(--line)}
.ev:last-child{border-bottom:0}
.ev .t{font-family:'Geist Mono',Geist,monospace;flex:0 0 86px;font-size:13px;font-weight:700;color:var(--g);font-variant-numeric:tabular-nums;padding-top:2px}
.ev .e{font-weight:600;font-size:15px}.ev .w{font-size:13px;color:var(--muted)}
.ae{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px;margin:3px 0 2px}
.ae .act{font-family:'Geist Mono',Geist,monospace;font-weight:700;font-size:15px;color:var(--ink)}
.ae .exp{font-size:13px;color:var(--muted)}
.ev.past{opacity:.45}.ev.past .t::after{content:" · Done";color:var(--muted);font-weight:600}
.empty{color:var(--muted);font-size:15px;padding:14px 0}
.canada{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:16px 18px}
.canada ul{margin:0;padding:0;list-style:none}
.canada li{position:relative;padding:8px 0 8px 18px;font-size:15px;border-bottom:1px solid var(--line);color:#D6DBE3}
.canada li:last-child{border-bottom:0}
.canada li::before{content:"";position:absolute;left:0;top:17px;width:7px;height:7px;border-radius:50%;background:#E5484D}
.line{background:linear-gradient(180deg,#10291F,#0C1F18);border:1px solid #1D4A36;border-radius:24px;padding:20px}
.lsegs{display:flex;gap:6px;margin:12px 0 2px;flex-wrap:wrap}
.lseg{border:1px solid rgba(255,255,255,.14);background:transparent;color:#BFE8D6;border-radius:999px;padding:6px 12px;font-size:13px;font-weight:700;cursor:pointer;min-height:36px}
.lseg[aria-pressed="true"]{background:var(--g);border-color:var(--g);color:#06140E}
.line blockquote{margin:10px 0 16px;font-family:Geist,Inter,sans-serif;font-size:19px;line-height:1.35;letter-spacing:-.2px}
.line .acts{display:flex;gap:10px}
.lbtn{flex:1;min-height:46px;border-radius:14px;border:0;font-weight:700;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;text-decoration:none}
.lbtn.a{background:var(--g);color:#06140E}.lbtn.b{background:rgba(255,255,255,.07);color:var(--ink);border:1px solid rgba(255,255,255,.12)}
details.src{margin-top:18px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:4px 18px}
details.src summary{cursor:pointer;font-weight:600;font-size:14px;padding:12px 0;list-style:none;display:flex;justify-content:space-between}
details.src summary::-webkit-details-marker{display:none}
details.src summary::after{content:"+";font-size:18px;color:var(--muted)}
details.src[open] summary::after{content:"−"}
details.src ol{margin:0 0 12px;padding-left:20px;font-size:14px}
details.src li{margin:0 0 6px}
.note{background:var(--downbg);color:#FFB98A;padding:8px 12px;border-radius:12px;font-size:13px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:6px 18px 14px}
.netlist{display:flex;flex-direction:column;gap:10px}
.net{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px}
.net .ni{width:36px;height:36px;border-radius:12px;background:rgba(61,220,151,.12);color:var(--g);display:flex;align-items:center;justify-content:center;font-family:'Geist Mono',Geist,monospace;font-weight:700;flex-shrink:0}
.net h3{margin:2px 0 4px;font-size:16px;color:var(--ink)}
.net p{margin:0;font-size:14px;color:#C6CCD6}
.list a{display:block;padding:16px 18px;background:var(--card);border:1px solid var(--line);border-radius:18px;margin:0 0 10px;text-decoration:none;color:var(--ink);transition:transform .15s,border-color .15s}
.list a:active{transform:scale(.99);border-color:var(--line2)}
.list .d{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.4px;text-transform:uppercase}
.list .h{font-family:Geist,Inter,sans-serif;font-size:17px;font-weight:700;margin:4px 0 6px;letter-spacing:-.2px}
.list .c{font-size:12px;font-weight:700;color:var(--g);background:var(--upbg);padding:3px 8px;border-radius:999px;margin-right:6px}
h1.page{font-family:Geist,Inter,sans-serif;font-size:30px;letter-spacing:-.6px;margin:4px 2px 16px}
footer{color:#6E7684;font-size:12px;margin:26px 2px 0;text-align:center;line-height:1.6}
footer a{color:#98A1B0}
.aitag{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;border:1px solid var(--line2);font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--muted);vertical-align:2px}
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:20;background:rgba(10,13,18,.86);backdrop-filter:saturate(1.4) blur(16px);-webkit-backdrop-filter:saturate(1.4) blur(16px);border-top:1px solid var(--line);padding:8px 0 calc(8px + env(safe-area-inset-bottom))}
.tabbar .in{max-width:640px;margin:0 auto;display:flex;justify-content:space-around}
.tabbar a{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:64px;min-height:48px;justify-content:center;text-decoration:none;color:#7D8594;font-size:11px;font-weight:600}
.tabbar a.on{color:var(--ink)}
.tabbar a.on svg{color:var(--g)}
.reveal{opacity:0;transform:translateY(16px);transition:opacity .6s ease,transform .6s cubic-bezier(.2,.8,.2,1)}
.reveal.in{opacity:1;transform:none}
.lvlbar{display:flex;align-items:center;gap:6px;margin:0 0 12px;padding:4px;background:var(--card);border:1px solid var(--line);border-radius:14px}
.lvl-l{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);padding:0 8px 0 6px;white-space:nowrap}
.lvlbtn{flex:1;min-height:36px;border:0;border-radius:10px;background:transparent;color:var(--muted);font-size:14px;font-weight:700;cursor:pointer}
.lvlbtn[aria-pressed="true"]{background:var(--g);color:#06140E}
.lvlv[hidden]{display:none}
.edlink{margin-left:6px;color:var(--muted);font-weight:600;text-transform:none;letter-spacing:0;text-decoration:underline;text-underline-offset:2px}
.gloss{display:flex;flex-direction:column;gap:10px}
.gterm{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px 16px}
.gterm h3{margin:0 0 4px;font-size:17px;color:var(--g)}
.gterm p{margin:0;font-size:15px;color:#D6DBE3}
.sums{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.sum{display:flex;flex-direction:column;gap:8px;text-align:left;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:14px;cursor:pointer;min-height:132px;color:var(--ink);transition:transform .12s,border-color .15s,background .15s}
.sum:active{transform:scale(.98)}
.sum:hover{border-color:var(--line2)}
.sum.wide{grid-column:1/-1;min-height:0}
.sum-h{display:flex;justify-content:space-between;align-items:center}
.sum-l{font-size:12px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--muted)}
.sum .go{color:var(--muted)}
.sum-t{margin:0;font-size:16px;font-weight:600;line-height:1.3;color:var(--ink);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.sum-t.sm{font-size:14px;font-weight:500;color:#D6DBE3}
.sum-t[hidden],.lq[hidden]{display:none}
.sum-m{margin-top:auto;font-size:12px;color:var(--muted);font-weight:600}
.sum .tag,.sum .dtype{align-self:flex-start}
.when{font-family:'Geist Mono',Geist,monospace;font-size:13px;font-weight:700;color:var(--g)}
.minis{display:flex;gap:8px;flex-wrap:wrap}
.mini{display:flex;flex-direction:column;gap:4px;background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:8px 10px;flex:1;min-width:92px}
.mini b{font-size:13px;color:#D6DBE3;font-weight:600}
.srcbtn{margin-top:12px;background:none;border:0;color:var(--muted);font-size:13px;font-weight:600;cursor:pointer;padding:8px 2px;text-decoration:underline;text-underline-offset:3px}
.sheet{position:fixed;inset:0;z-index:50;background:var(--bg);display:flex;flex-direction:column;animation:sheetin .32s cubic-bezier(.2,.8,.2,1)}
.sheet[hidden]{display:none}
.sheet-bar{display:grid;grid-template-columns:44px 1fr 44px;align-items:center;gap:8px;padding:calc(10px + env(safe-area-inset-top)) 12px 10px;border-bottom:1px solid var(--line);background:rgba(10,13,18,.92);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.sheet-bar h2{margin:0;text-align:center;font-size:17px;font-weight:700;letter-spacing:-.2px;color:var(--ink)}
.sheet-x{width:44px;height:44px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--ink);display:flex;align-items:center;justify-content:center;cursor:pointer}
.sheet-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:18px 16px calc(40px + env(safe-area-inset-bottom));max-width:640px;width:100%;margin:0 auto}
.sheet-body .sec{margin:0}
.sheet-body .sech{display:none}
.story-full{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:18px;margin:0 0 12px}
.story-full .top{display:flex;justify-content:space-between;align-items:center;margin:0 0 8px}
.story-full h3{font-size:21px;line-height:1.25;letter-spacing:-.4px;margin:0 0 12px;color:var(--ink)}
.story-full .lbl{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;color:var(--muted);margin:12px 0 4px}
.story-full p{margin:0;font-size:15px;color:#D6DBE3}
.readfull{margin-top:14px;display:inline-flex;align-items:center;gap:6px;background:rgba(61,220,151,.1);color:var(--g);border:1px solid rgba(61,220,151,.3);border-radius:999px;padding:9px 14px;font-size:14px;font-weight:700;cursor:pointer;min-height:42px}
.readfull .go{color:var(--g)}
.longread{padding:4px 2px}
.longread h3{font-size:25px;line-height:1.2;letter-spacing:-.6px;margin:12px 0 16px;color:var(--ink)}
.longread .lr-body p{font-size:16.5px;line-height:1.65;color:#D6DBE3;margin:0 0 16px}
.longread .lr-body p:first-child{font-size:18px;color:var(--ink)}
.srclist{margin:0;padding-left:20px;font-size:15px}.srclist li{margin:0 0 10px}
.card.md h2,.card.md h3,.card.md b,.card.md strong,.card.md p,.card.md li{color:var(--ink)!important}
.card.md h2{border-color:var(--line)!important}
.card.md a{color:var(--g)!important}
body.locked{overflow:hidden}
@keyframes sheetin{from{transform:translateY(28px);opacity:0}to{transform:none;opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}.reveal{opacity:1;transform:none}}
"""

TAB_ICONS = {
    "today": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>',
    "learn": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 9l10-5 10 5-10 5z"/><path d="M6 11v5c3 2.5 9 2.5 12 0v-5"/></svg>',
    "network": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17.5" cy="9" r="2.5"/><path d="M16 14.2c2.9.3 5 2.6 5 5.8"/></svg>',
    "archive": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/></svg>',
}

SITE_JS = r"""
(function(){
  var rm = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  // reveal on scroll
  var els = document.querySelectorAll('.reveal');
  if (rm || !('IntersectionObserver' in window)) { els.forEach(function(e){e.classList.add('in')}); }
  else { var io = new IntersectionObserver(function(es){es.forEach(function(x){if(x.isIntersecting){x.target.classList.add('in');io.unobserve(x.target)}})},{threshold:.12});
         els.forEach(function(e){io.observe(e)}); }
  // market tabs
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function(t){ t.addEventListener('click', function(){
    tabs.forEach(function(o){ o.setAttribute('aria-selected','false'); var p=document.getElementById(o.getAttribute('aria-controls')); p.hidden=true; p.classList.remove('show'); });
    t.setAttribute('aria-selected','true'); var p=document.getElementById(t.getAttribute('aria-controls')); p.hidden=false; void p.offsetWidth; p.classList.add('show');
  }); });
  // story cards: expand + dots
  document.querySelectorAll('.more').forEach(function(b){ b.addEventListener('click', function(){
    var w=document.getElementById(b.getAttribute('aria-controls')); var open=!w.classList.contains('open');
    w.classList.toggle('open',open); b.setAttribute('aria-expanded',open?'true':'false'); b.textContent=open?'Show less':'Why it happened';
  }); });
  var cards=document.querySelector('.cards'), dots=document.querySelectorAll('.dots span');
  if(cards && dots.length){ cards.addEventListener('scroll', function(){
    var w=cards.firstElementChild.getBoundingClientRect().width+12; var i=Math.round(cards.scrollLeft/w);
    dots.forEach(function(d,k){d.classList.toggle('on',k===i)});
  }, {passive:true}); }
  // section sheets: tap a tile to open, back button or arrow to close
  function openSheet(id, push){ var sh=document.getElementById(id); if(!sh) return;
    document.querySelectorAll('.sheet').forEach(function(x){x.hidden=true});
    sh.hidden=false; document.body.classList.add('locked'); sh.querySelector('.sheet-body').scrollTop=0;
    if(push) history.pushState({sheet:id},'','#'+id); var x=sh.querySelector('.sheet-x'); if(x) x.focus(); }
  function closeSheets(){ document.querySelectorAll('.sheet').forEach(function(x){x.hidden=true}); document.body.classList.remove('locked'); }
  document.querySelectorAll('[data-open]').forEach(function(b){ b.addEventListener('click', function(){ openSheet(b.getAttribute('data-open'), true); }); });
  document.querySelectorAll('.sheet-x').forEach(function(b){ b.addEventListener('click', function(){
    if(history.state && history.state.sheet) history.back(); else { closeSheets(); history.replaceState(null,'',location.pathname); } }); });
  window.addEventListener('popstate', function(){ var id=location.hash.slice(1); if(id && document.getElementById(id)) openSheet(id,false); else closeSheets(); });
  document.addEventListener('keydown', function(ev){ if(ev.key==='Escape' && document.querySelector('.sheet:not([hidden])')){ var x=document.querySelector('.sheet:not([hidden]) .sheet-x'); if(x) x.click(); } });
  if(location.hash && document.getElementById(location.hash.slice(1)) && document.getElementById(location.hash.slice(1)).classList.contains('sheet')) openSheet(location.hash.slice(1), false);
  // interview line: one per track, defaults to the track picked in Learn
  var segs=document.querySelectorAll('.lseg'), qs=document.querySelectorAll('.lq');
  function showLine(k){ var found=false; qs.forEach(function(q){ if(q.getAttribute('data-k')===k) found=true; });
    if(!found) return; qs.forEach(function(q){ q.hidden=q.getAttribute('data-k')!==k; });
    document.querySelectorAll('.lprev').forEach(function(q){ q.hidden=q.getAttribute('data-k')!==k; });
    segs.forEach(function(b){ b.setAttribute('aria-pressed', b.getAttribute('data-k')===k ? 'true' : 'false'); }); }
  segs.forEach(function(b){ b.addEventListener('click', function(){ showLine(b.getAttribute('data-k')); }); });
  try { var st=JSON.parse(localStorage.getItem('leveraged-learn-v1')||'{}'); if(st.track) showLine(st.track); } catch(e) {}
  // copy interview line (one button per level version)
  document.querySelectorAll('.copyline').forEach(function(cp){ cp.addEventListener('click', function(){
    var box=cp.closest('.line'); var vis=box?box.querySelector('.lq:not([hidden])'):null; var t=vis?vis.innerText:'';
    (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){cp.textContent='Copied';setTimeout(function(){cp.textContent='Copy'},1600)}).catch(function(){}); }); });
  // Coming up: events whose time has passed (Toronto time) are marked Done
  (function(){
    function torNow(){ var p={}; try{ new Intl.DateTimeFormat('en-CA',{timeZone:'America/Toronto',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date()).forEach(function(x){p[x.type]=x.value;}); }catch(e){ return null; }
      return {iso:p.year+'-'+p.month+'-'+p.day, min:(+p.hour)*60+(+p.minute)}; }
    function run(){
      var now=torNow(); if(!now) return;
      document.querySelectorAll('.cal-list').forEach(function(list){
        var iso=list.getAttribute('data-iso'); var evs=[].slice.call(list.querySelectorAll('.ev'));
        var past=function(ev){ var m=ev.getAttribute('data-min'); if(m===''||m===null) return false; return iso<now.iso || (iso===now.iso && +m<=now.min); };
        evs.forEach(function(ev){ ev.classList.toggle('past', past(ev)); });
        var v=list.closest('.lvlv')||document; var tile=v.querySelector('.cal-tile'); if(!tile) return;
        var left=evs.filter(function(ev){ return !past(ev); });
        var w=tile.querySelector('.cal-w'), t=tile.querySelector('.cal-e'), m=tile.querySelector('.cal-m');
        if(!w||!t||!m||!evs.length) return;
        if(left.length){ w.textContent=left[0].querySelector('.t').textContent; t.textContent=left[0].querySelector('.e').textContent;
          m.textContent=left.length>1?('+'+(left.length-1)+' more'):(iso===now.iso?'Last one today':'Today'); }
        else { w.textContent='All done'; t.textContent="That's everything scheduled. Results land in tomorrow's brief."; m.textContent=evs.length+' events'; }
      });
    }
    run(); setInterval(run, 60000);
  })();
  // level versions: Intern / Analyst / Partner (shared with Learn)
  var KEY='leveraged-learn-v1';
  function getState(){ try{ return JSON.parse(localStorage.getItem(KEY)||'{}'); }catch(e){ return {}; } }
  function setLevel(l, save){
    var vs=document.querySelectorAll('.lvlv'); if(!vs.length) return;
    var ok=false; vs.forEach(function(v){ if(v.getAttribute('data-level')===l) ok=true; }); if(!ok) l='analyst';
    vs.forEach(function(v){ v.hidden=v.getAttribute('data-level')!==l; });
    document.querySelectorAll('.lvlbtn').forEach(function(b){ b.setAttribute('aria-pressed', b.getAttribute('data-lvl')===l?'true':'false'); });
    document.querySelectorAll('.player audio').forEach(function(x){ if(!x.closest('.lvlv:not([hidden])')) x.pause(); });
    if(save){ try{ var st=getState(); st.level=l; localStorage.setItem(KEY, JSON.stringify(st)); }catch(e){} }
  }
  document.querySelectorAll('.lvlbtn').forEach(function(b){ b.addEventListener('click', function(){ setLevel(b.getAttribute('data-lvl'), true); }); });
  setLevel(getState().level || 'analyst', false);
  if(location.hash){ var t=document.getElementById(location.hash.slice(1)); if(t && t.closest('.lvlv')) setLevel(t.closest('.lvlv').getAttribute('data-level'), false); }
  // audio players
  var PLAY='<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
      PAUSE='<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  function fmt(s){ if(!isFinite(s)) return '0:00'; s=Math.floor(s); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
  document.querySelectorAll('.player').forEach(function(pl){
    var a=pl.querySelector('audio'); if(!a) return;
    var btn=pl.querySelector('.pbtn'), fill=pl.querySelector('.pfill'), seek=pl.querySelector('.pseek'),
        cur=pl.querySelector('.pcur'), dur=pl.querySelector('.pdur'), sp=pl.querySelector('.speed');
    btn.addEventListener('click', function(){ if(a.paused){a.play()}else{a.pause()} });
    a.addEventListener('play', function(){btn.innerHTML=PAUSE;btn.setAttribute('aria-label','Pause episode')});
    a.addEventListener('pause', function(){btn.innerHTML=PLAY;btn.setAttribute('aria-label','Play episode')});
    a.addEventListener('loadedmetadata', function(){dur.textContent=fmt(a.duration)});
    a.addEventListener('timeupdate', function(){ var p=a.duration?a.currentTime/a.duration*100:0; fill.style.width=p+'%'; seek.value=p; cur.textContent=fmt(a.currentTime); });
    seek.addEventListener('input', function(){ if(a.duration) a.currentTime=seek.value/100*a.duration; });
    var speeds=[1,1.25,1.5,2], si=0;
    sp.addEventListener('click', function(){ si=(si+1)%speeds.length; a.playbackRate=speeds[si]; sp.textContent=speeds[si]+'×'; });
  });
})();
"""


def page(title, body, root, active):
    def tab(href, label, key):
        cls = ' class="on" aria-current="page"' if key == active else ""
        return f'<a href="{root}{href}"{cls}>{TAB_ICONS[key]}{label}</a>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0A0D12">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700;800&amp;family=Geist+Mono:wght@500;600;700&amp;display=swap" rel="stylesheet">
<style>{SITE_CSS}</style></head>
<body><div class="wrap">
<div class="appbar"><div class="brand"><span class="logo" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#3DDC97" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/></svg></span>{SITE_NAME}</div>
<div class="barr">{{datepill}}<a class="gear" href="{root}settings.html" aria-label="Settings"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></a></div></div>
{body}
<footer>Written and voiced by AI, so it can contain errors. For education only, not investment advice.<br>Market data: Financial Modeling Prep, FRED (Federal Reserve Bank of St. Louis) and the Bank of Canada. Some numbers come from ETFs that track an index and may be delayed.<br><a href="{root}terms.html">Terms</a> · <a href="{root}privacy.html">Privacy</a></footer>
</div>
<nav class="tabbar" aria-label="Main"><div class="in">{tab("index.html", "Today", "today")}{tab("learn.html", "Learn", "learn")}{tab("network.html", "Network", "network")}{tab("archive.html", "Archive", "archive")}</div></nav>
<script>{SITE_JS}</script>
</body></html>"""


def _chg(d):
    if d is None:
        return "n/a", '<span class="chg na">n/a</span>'
    chg = fmt_change(d["value"], d["prev"], d["kind"])
    cls = "up" if chg.startswith("▲") else "down"
    return fmt_value(d["value"], d["kind"]), f'<span class="chg {cls}">{chg}</span>'


def as_of_label(nums):
    live = any(d and d.get("live") for d in nums.values())
    return "Live this morning · change vs. last close" if live else "At the last close"


def markets_html(nums):
    tabs, panels = [], []
    for i, (label, names) in enumerate(MARKET_TABS):
        pid = f"mk{i}"
        tabs.append(f'<button class="tab" type="button" role="tab" id="t{pid}" aria-controls="{pid}" '
                    f'aria-selected="{"true" if i == 0 else "false"}">{label}</button>')
        tiles = []
        for name in names:
            d = nums.get(name)
            val, chip = _chg(d)
            lab = (d or {}).get("label", name)
            tag = '<span class="live">LIVE</span>' if d and d.get("live") else '<span class="close">CLOSE</span>' if d else ""
            tiles.append(f'<div class="tile"><div class="n"><span>{html.escape(lab)}</span>{tag}</div>'
                         f'<div class="v">{val}</div>{chip}</div>')
        panels.append(f'<div class="panel{" show" if i == 0 else ""}" id="{pid}" role="tabpanel" '
                      f'aria-labelledby="t{pid}"{"" if i == 0 else " hidden"}>{"".join(tiles)}</div>')
    return (f'<section class="sec reveal" aria-label="Markets"><div class="sech"><h2>Markets</h2><span>{as_of_label(nums)}</span></div>'
            f'<div class="tabs" role="tablist" aria-label="Market groups">{"".join(tabs)}</div>{"".join(panels)}</section>')


def player_html(rec, root):
    if not rec.get("audio"):
        return '<div class="player"><span class="noaudio">No episode for this day.</span></div>'
    return (f'<div class="player"><button class="pbtn" type="button" aria-label="Play episode">'
            f'<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg></button>'
            f'<div class="pmeta"><div class="ptitle">Today\'s episode <span class="aitag">AI voice</span></div>'
            f'<div class="bar"><div class="pfill"></div><input class="pseek" type="range" min="0" max="100" step="0.1" value="0" aria-label="Seek"></div>'
            f'<div class="ptime"><span class="pcur">0:00</span><span class="pdur">–:––</span></div></div>'
            f'<button class="speed" type="button" aria-label="Playback speed">1×</button>'
            f'<audio preload="metadata" src="{root}audio/{rec["audio"]}"></audio></div>')


def _first_sentence(t, n=140):
    t = (t or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


ARROW = '<svg class="go" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>'


def sheet(sid, title, inner):
    return (f'<section class="sheet" id="{sid}" role="dialog" aria-modal="true" aria-labelledby="{sid}-t" hidden>'
            f'<div class="sheet-bar"><button class="sheet-x" type="button" aria-label="Close">'
            f'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6"/></svg></button>'
            f'<h2 id="{sid}-t">{title}</h2><span></span></div><div class="sheet-body">{inner}</div></section>')


def full_html(kicker, title, text, has_sources):
    e = html.escape
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    body = "".join(f"<p>{e(p)}</p>" for p in paras)
    src = '<button class="srcbtn" type="button" data-open="sources">See sources</button>' if has_sources else ""
    return (f'<article class="longread"><span class="tag">{e(kicker)}</span><h3>{e(title)}</h3>'
            f'<div class="lr-body">{body}</div>{src}</article>')


def _even_tiles(tiles):
    """Two small tiles per row; if a run of small tiles is odd, the last one goes full width."""
    out, run = [], []
    def flush():
        if len(run) % 2:
            run[-1] = run[-1].replace('<button class="sum', '<button class="sum wide', 1)
        out.extend(run); run.clear()
    for t in tiles:
        if t.startswith('<button class="sum wide'):
            flush(); out.append(t)
        else:
            run.append(t)
    flush()
    return out


def tile_btn(sid, label, meta, body, wide=False):
    return (f'<button class="sum{" wide" if wide else ""}" type="button" data-open="{sid}">'
            f'<div class="sum-h"><span class="sum-l">{label}</span>{ARROW}</div>{body}'
            f'<div class="sum-m">{meta}</div></button>')


def _day_variant(rec, root):
    e = html.escape
    note = '<p class="note">Sample numbers (demo mode), not real market data.</p>' if rec.get("demo") else ""
    d = rec.get("data") or parse_brief(rec.get("written", "")) or parse_markdown_brief(rec.get("written", ""))
    if not d and looks_like_json(rec.get("written", "")):
        rec = dict(rec, written="_This day's written brief couldn't be formatted._")
    if not d:  # older days saved before the redesign
        return (f'{note}<section class="hero"><div class="eyebrow">Morning brief</div><h1>Today\'s markets</h1>'
                f'{player_html(rec, root)}</section>{markets_html(rec["nums"])}'
                f'<section class="sec"><div class="card md">{md_to_html(rec["written"])}</div></section>')

    nums = rec["nums"]
    hero = (f'<section class="hero"><div class="eyebrow">Morning brief</div><h1>{e(d.get("headline", ""))}</h1>'
            f'<p class="mood">{e(d.get("mood", ""))}</p>{player_html(rec, root)}</section>')

    tiles, sheets = [], []
    srcs_list = [x for x in d.get("sources", []) if str(x.get("url", "")).startswith("http")]

    # Markets: three headline numbers on the tile, every number in the sheet
    chips = []
    for name in ["S&P 500", "TSX", "WTI oil ($/bbl)", "US 10Y yield"]:
        dd = nums.get(name)
        if dd is None or len(chips) == 3:
            continue
        _, chip = _chg(dd)
        short = {"WTI oil ($/bbl)": "Oil", "US 10Y yield": "US 10Y"}.get(name, dd.get("label", name).split(" (")[0])
        chips.append(f'<span class="mini"><b>{e(short)}</b>{chip}</span>')
    tiles.append(tile_btn("markets", "Markets", ("Live · " if any(x and x.get("live") for x in nums.values()) else "") + "12 markets · tap for all", f'<div class="minis">{"".join(chips)}</div>', wide=True))
    sheets.append(sheet("markets", "Markets", markets_html(nums).replace(' reveal"', '"')))

    # What moved
    stories = d.get("stories", [])
    if stories:
        top = stories[0]
        tiles.append(tile_btn("moved", "What moved",
                              f'{len(stories)} {"story" if len(stories) == 1 else "stories"}',
                              f'<span class="tag">{e(top.get("tag", "Markets"))}</span><p class="sum-t">{e(top.get("title", ""))}</p>'))
        blocks = []
        for i, st in enumerate(stories, 1):
            more = ""
            if st.get("full"):
                fid = f"story{i}"
                more = f'<button class="readfull" type="button" data-open="{fid}">Read the full story {ARROW}</button>'
                sheets.append(sheet(fid, "Full story", full_html(st.get("tag", "Markets"), st.get("title", ""), st["full"], bool(srcs_list))))
            blocks.append(f'<article class="story-full"><div class="top"><span class="tag">{e(st.get("tag", "Markets"))}</span>'
                          f'<span class="count">{i} of {len(stories)}</span></div><h3>{e(st.get("title", ""))}</h3>'
                          f'<div class="lbl">Why it happened</div><p>{e(st.get("why_happened", ""))}</p>'
                          f'<div class="lbl">Why it matters</div><p>{e(st.get("why_matters", ""))}</p>{more}</article>')
        sheets.insert(0, sheet("moved", "What moved", "".join(blocks)))

    # Deals
    deals = d.get("deals", [])
    if deals:
        x = deals[0]
        who = e(x.get("buyer", "")) + (f' <span class="arrow">→</span> {e(x["target"])}' if x.get("target") else "")
        tiles.append(tile_btn("deals", "Deals", f'{len(deals)} {"deal" if len(deals) == 1 else "deals"}',
                              f'<span class="dtype">{e(x.get("type", "Deal"))} · {e(x.get("value", ""))}</span><p class="sum-t">{who}</p>'))
        dl = []
        for j, x in enumerate(deals, 1):
            target = f'<span class="arrow">→</span>{e(x["target"])}' if x.get("target") else ""
            more = ""
            if x.get("full"):
                fid = f"deal{j}"
                head = x.get("buyer", "") + (f" → {x['target']}" if x.get("target") else "")
                more = f'<button class="readfull" type="button" data-open="{fid}">Read the full story {ARROW}</button>'
                sheets.append(sheet(fid, "Full story", full_html(f'{x.get("type", "Deal")} · {x.get("value", "")}', head, x["full"], bool(srcs_list))))
            dl.append(f'<article class="deal"><div class="row"><span class="dtype">{e(x.get("type", "Deal"))}</span>'
                      f'<span class="dval">{e(x.get("value", "Undisclosed"))}</span></div>'
                      f'<div class="parties">{e(x.get("buyer", ""))}{target}</div><p>{e(x.get("why", ""))}</p>{more}</article>')
        sheets.insert(0, sheet("deals", "Deals", f'<div class="deals">{"".join(dl)}</div>'))

    # Out today: data that already came out, actual vs expected
    res = [r_ for r_ in d.get("results", []) if r_.get("event")]
    if res:
        r0 = res[0]
        def _ae(r_):
            a, x = r_.get("actual", ""), r_.get("expected", "")
            return (a + (f" vs {x} est." if x else "")).strip()
        tiles.append(tile_btn("out", "Out today", f'{len(res)} release{"s" if len(res) != 1 else ""}',
                              f'<span class="when">{e(_ae(r0) or r0.get("time", ""))}</span><p class="sum-t">{e(r0.get("event", ""))}</p>'))
        rows = "".join(
            f'<div class="ev"><div class="t">{e(r_.get("time", ""))}</div><div><div class="e">{e(r_.get("event", ""))}</div>'
            + (f'<div class="ae"><span class="act">{e(r_.get("actual", ""))}</span>'
               + (f'<span class="exp">vs {e(r_.get("expected", ""))} expected</span>' if r_.get("expected") else "") + '</div>'
               if r_.get("actual") else "")
            + f'<div class="w">{e(r_.get("why", ""))}</div></div></div>' for r_ in res)
        sheets.append(sheet("out", "Out today", f'<div class="timeline">{rows}</div>'))

    # Coming up (the page greys out events once their time passes)
    cal = d.get("calendar", [])
    iso = e(rec.get("iso", ""))
    def _mins(t):
        m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M", t or "", re.I)
        if not m:
            return ""
        h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "P" else 0)
        return str(h * 60 + int(m.group(2) or 0))
    if cal:
        c0 = cal[0]
        body = f'<span class="when cal-w">{e(c0.get("time", ""))}</span><p class="sum-t cal-e">{e(c0.get("event", ""))}</p>'
        meta = f'+{len(cal) - 1} more' if len(cal) > 1 else "Today"
    else:
        body = f'<p class="sum-t">{e(d.get("calendar_note") or "Nothing major on the calendar today.")}</p>'
        meta = "Today"
    tiles.append(tile_btn("coming", "Coming up", f'<span class="cal-m">{meta}</span>', body).replace(
        'class="sum"', f'class="sum cal-tile" data-iso="{iso}"', 1))
    evs = "".join(f'<div class="ev" data-min="{_mins(c.get("time", ""))}"><div class="t">{e(c.get("time", ""))}</div><div><div class="e">{e(c.get("event", ""))}</div>'
                  f'<div class="w">{e(c.get("why", ""))}</div></div></div>' for c in cal) or \
        f'<div class="empty">{e(d.get("calendar_note") or "Nothing major on the calendar today.")}</div>'
    sheets.append(sheet("coming", "Coming up", f'<div class="timeline cal-list" data-iso="{iso}">{evs}</div>'))

    # Canada desk
    can = d.get("canada", [])
    if can:
        tiles.append(tile_btn("canada", "Canada desk", f'{len(can)} points', f'<p class="sum-t sm">{e(_first_sentence(can[0], 110))}</p>'))
        sheets.append(sheet("canada", "Canada desk", '<div class="canada"><ul>' + "".join(f"<li>{e(x)}</li>" for x in can) + "</ul></div>"))

    # Interview line (one per track)
    take = re.sub(r"^\s*If someone asks you about markets today:\s*", "", d.get("takeaway", ""), flags=re.I)
    take = take[:1].upper() + take[1:]
    lines = [x for x in [("ib", "IB & Markets", take), ("consulting", "Consulting", d.get("takeaway_consulting", "")),
                         ("accounting", "Accounting", d.get("takeaway_accounting", ""))] if x[2]]
    if lines:
        prev = "".join(f'<p class="sum-t sm lprev" data-k="{k}"{"" if i == 0 else " hidden"}>{e(_first_sentence(t, 120))}</p>'
                       for i, (k, _, t) in enumerate(lines))
        tiles.append(tile_btn("line", "Interview line", "Copy or practice", prev, wide=True))
        seg = "".join(f'<button type="button" class="lseg" data-k="{k}" aria-pressed="{"true" if i == 0 else "false"}">{lab}</button>'
                      for i, (k, lab, _) in enumerate(lines)) if len(lines) > 1 else ""
        quotes = "".join(f'<blockquote class="lq" data-k="{k}"{"" if i == 0 else " hidden"}>{e(t)}</blockquote>'
                         for i, (k, _, t) in enumerate(lines))
        sheets.append(sheet("line", "Your interview line",
                            f'<div class="line">{f"<div class=lsegs role=group aria-label=Track>{seg}</div>" if seg else ""}{quotes}'
                            f'<div class="acts"><button class="lbtn a copyline" type="button">Copy</button>'
                            f'<a class="lbtn b" href="{root}learn.html">Practice in Learn</a></div></div>'))

    # Words to know (Intern version)
    gl = [g for g in d.get("glossary", []) if g.get("term")]
    if gl:
        tiles.append(tile_btn("words", "Words to know", f'{len(gl)} terms from today',
                              f'<p class="sum-t sm">{" · ".join(e(g["term"]) for g in gl[:4])}</p>', wide=True))
        sheets.append(sheet("words", "Words to know", '<div class="gloss">' + "".join(
            f'<div class="gterm"><h3>{e(g["term"])}</h3><p>{e(g.get("meaning", ""))}</p></div>' for g in gl) + "</div>"))

    # Sources
    srcs = "".join(f'<li><a href="{e(x.get("url", ""))}" rel="noopener" target="_blank">{e(x.get("title") or "Source")}</a></li>'
                   for x in d.get("sources", []) if str(x.get("url", "")).startswith("http"))
    src_link = ""
    if srcs:
        src_link = f'<button class="srcbtn" type="button" data-open="sources">Sources ({srcs.count("<li>")})</button>'
        sheets.append(sheet("sources", "Sources", f'<ol class="srclist">{srcs}</ol>'))

    grid = (f'<section class="sec reveal" aria-label="Today\'s sections"><div class="sech"><h2>Today\'s brief</h2>'
            f'<span>Tap a section to read more</span></div><div class="sums">{"".join(_even_tiles(tiles))}</div>{src_link}</section>')
    return note + hero + grid + "".join(sheets)


LEVEL_BLURB = {"intern": "New to markets: everything explained simply",
               "analyst": "Knows the basics: interview-level detail",
               "partner": "Deep and technical: second-order effects"}


def _prefix_ids(markup, pre):
    return re.sub(r'\b(id|aria-controls|aria-labelledby|data-open|for)="([^"]+)"',
                  lambda m: f'{m.group(1)}="{pre}{m.group(2)}"', markup)


def day_body(rec, root):
    levels = rec.get("levels") or {}
    if not levels:
        return _day_variant(rec, root)
    audios = rec.get("audios") or {}
    switch = ""
    parts = []
    for l in LEVELS:
        d = levels.get(l) or levels.get("analyst")
        r2 = dict(rec, data=d, audio=audios.get(l) or rec.get("audio"))
        inner = _prefix_ids(_day_variant(r2, root), f"{l}-").replace(
            '<div class="eyebrow">Morning brief</div>',
            f'<div class="eyebrow">Morning brief · {LEVEL_NAMES[l]} edition <a class="edlink" href="{root}settings.html">Change</a></div>', 1)
        parts.append(f'<div class="lvlv" data-level="{l}"{"" if l == "analyst" else " hidden"}>{inner}</div>')
    return switch + "".join(parts)


def short_date(iso):
    return dt.date.fromisoformat(iso).strftime("%a, %b %-d")


NETWORK_BODY = """
<section class="hero"><div class="eyebrow">Network</div><h1>Compete, compare and keep each other going</h1>
<p class="mood">Leagues, school and club rankings, friend streaks and a daily market call. Arriving with accounts.</p></section>
<section class="sec reveal"><div class="sech"><h2>What's coming</h2></div>
<div class="netlist">
<div class="net"><span class="ni">1</span><div><h3>Weekly leagues</h3><p>Earn XP from lessons and climb a weekly leaderboard. Top finishers move up a division.</p></div></div>
<div class="net"><span class="ni">2</span><div><h3>School and club rankings</h3><p>See how your school and your club stack up, from Laurier vs. Ivey to your case team vs. the accounting association.</p></div></div>
<div class="net"><span class="ni">3</span><div><h3>Daily market call</h3><p>Ten seconds a day: will the S&amp;P or oil close up or down? Build your accuracy score.</p></div></div>
<div class="net"><span class="ni">4</span><div><h3>Friend streaks</h3><p>Keep a learning streak going with friends and see who's falling behind.</p></div></div>
<div class="net"><span class="ni">5</span><div><h3>Clubs</h3><p>Club leaders can create a group, run challenges and track members' progress.</p></div></div>
</div></section>
"""

def build_site(site_dir, iso, today, nums, written, mp3, demo, data=None, levels=None, mp3s=None):
    import json
    import shutil
    days = site_dir / "days"
    audio = site_dir / "audio"
    days.mkdir(parents=True, exist_ok=True)
    audio.mkdir(parents=True, exist_ok=True)
    (site_dir / ".nojekyll").write_text("")

    audio_name = None
    if mp3 and Path(mp3).exists():
        audio_name = f"{iso}.mp3"
        shutil.copyfile(mp3, audio / audio_name)

    audios = {}
    for lvl, m in (mp3s or {}).items():
        if m and Path(m).exists():
            audios[lvl] = f"{iso}-{lvl}.mp3"
            shutil.copyfile(m, audio / audios[lvl])
    if audios.get("analyst"):
        audio_name = audios["analyst"]
    prev_path = days / f"{iso}.json"
    if not audios and not audio_name and prev_path.exists():
        # a run without podcasts (e.g. a test) keeps the episodes already made today
        try:
            prev = json.loads(prev_path.read_text(encoding="utf-8"))
            audios = {k: v for k, v in (prev.get("audios") or {}).items() if (audio / v).exists()}
            audio_name = prev.get("audio") if prev.get("audio") and (audio / prev["audio"]).exists() else audios.get("analyst")
        except Exception:
            pass
    rec = {"iso": iso, "today": today, "nums": nums, "written": written,
           "audio": audio_name, "demo": demo, "data": data, "levels": levels, "audios": audios}
    (days / f"{iso}.json").write_text(json.dumps(rec), encoding="utf-8")

    cutoff = (dt.date.fromisoformat(iso) - dt.timedelta(days=KEEP_AUDIO_DAYS)).isoformat()
    for f in audio.glob("*.mp3"):
        if f.stem[:10] < cutoff:
            f.unlink()
    render_site(site_dir)


def render_site(site_dir):
    """Rebuild every page from the saved days. Costs nothing: no AI or data calls."""
    import json
    days = site_dir / "days"
    audio = site_dir / "audio"
    (site_dir / ".nojekyll").write_text("")

    def pill(text):
        return f'<span class="datepill">{html.escape(text)}</span>'

    records = []
    for f in sorted(days.glob("*.json"), reverse=True):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("audio") and not (audio / r["audio"]).exists():
            r["audio"] = None
        r["audios"] = {k: v for k, v in (r.get("audios") or {}).items() if (audio / v).exists()}
        records.append(r)
        (days / f"{r['iso']}.html").write_text(
            page(f"{SITE_NAME}: {r['today']}", day_body(r, "../"), "../", "archive")
            .replace("{datepill}", pill(short_date(r["iso"]))), encoding="utf-8")

    latest = records[0]
    (site_dir / "index.html").write_text(
        page(SITE_NAME, day_body(latest, ""), "", "today").replace("{datepill}", pill(short_date(latest["iso"]))),
        encoding="utf-8")

    # network.html is its own file (uploaded to docs/), so it isn't rebuilt here

    items = []
    for r in records:
        head = (r.get("data") or {}).get("headline") or "Morning brief"
        chips = '<span class="c">Podcast</span>' if r.get("audio") else ""
        items.append(f'<a href="days/{r["iso"]}.html"><div class="d">{html.escape(short_date(r["iso"]))}</div>'
                     f'<div class="h">{html.escape(head)}</div>{chips}</a>')
    (site_dir / "archive.html").write_text(
        page(f"{SITE_NAME}: Archive", f'<h1 class="page">Archive</h1><div class="list">{"".join(items)}</div>',
             "", "archive").replace("{datepill}", pill(f"{len(records)} day{'' if len(records) == 1 else 's'}")), encoding="utf-8")
    print(f"  website updated ({len(records)} days)")


def resolve_market_calls(nums):
    """Score the daily market calls for the latest S&P 500 close. Needs SUPABASE_URL and SUPABASE_SERVICE_KEY."""
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    spx = None
    if os.getenv("FRED_API_KEY"):
        try:
            v, p, d = fred_latest_two("SP500", os.getenv("FRED_API_KEY"))
            spx = {"value": v, "prev": p, "asof": d}
        except Exception as e:
            print(f"  ! S&P 500 close for market calls: {e}", file=sys.stderr)
    if not (url and key):
        print("  (market calls not scored: SUPABASE_URL / SUPABASE_SERVICE_KEY not set)")
        return
    if not spx or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(spx.get("asof", ""))):
        print("  ! market calls not scored: no dated S&P 500 close", file=sys.stderr)
        return
    headers = {"apikey": key, "Content-Type": "application/json"}
    if key.startswith("eyJ"):  # older-style keys also need this header
        headers["Authorization"] = f"Bearer {key}"
    went_up = spx["value"] > spx["prev"]
    try:
        r = requests.post(f"{url.rstrip('/')}/rest/v1/rpc/resolve_market_calls", headers=headers,
                          json={"p_date": spx["asof"], "p_up": went_up}, timeout=TIMEOUT)
        if r.status_code == 200:
            print(f"  market calls for {spx['asof']} scored ({'up' if went_up else 'down'} day): {r.text} calls")
        else:
            print(f"  ! market call scoring failed {r.status_code}: {r.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"  ! market call scoring failed: {e}", file=sys.stderr)


BRIEF_VERSION = "2026-11-10b"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="store_true", help="also build the website in demo mode")
    ap.add_argument("--demo", action="store_true", help="use sample numbers")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-podcast", action="store_true")
    ap.add_argument("--rebuild-site", action="store_true",
                    help="only rebuild the website from saved days (no AI, data or email; costs nothing)")
    args = ap.parse_args()

    if args.rebuild_site:
        print(f"brief.py version {BRIEF_VERSION}")
        site_dir = Path(os.getenv("SITE_DIR") or Path(__file__).parent / "docs")
        render_site(site_dir)
        print("  site-only rebuild done (no AI, no email)")
        return

    today = dt.date.today().strftime("%A, %B %d, %Y")
    print(f"brief.py version {BRIEF_VERSION}")
    print("Fetching numbers...")
    nums = get_numbers(demo=args.demo)
    table = numbers_table(nums)
    if not args.demo:
        resolve_market_calls(nums)

    print("Writing brief...")
    try:
        written = write_with_claude(table, today)
    except Exception as e:
        print(f"  ! {e}", file=sys.stderr)
        written = None
    data = None
    research = None
    if written:
        written = CITE_RE.sub("", written)
        research = written
        print("Formatting brief for the app...")
        try:
            data = structure_brief(written, "analyst")
        except Exception as e:
            print(f"  ! formatting step failed: {str(e)[:400]}", file=sys.stderr)
        if not data:
            data = parse_brief(written)  # in case the reply was already structured
        if not data:
            data = parse_markdown_brief(written)
            if data:
                print("  used the backup formatter (no AI needed)")
    levels = {}
    if data:
        levels["analyst"] = data
        for lvl in ("intern", "partner"):
            try:
                d_l = structure_brief(research, lvl)
                if d_l and (d_l.get("stories") or d_l.get("mood")):
                    levels[lvl] = d_l
                    print(f"  {lvl} version OK")
                else:
                    raise RuntimeError("empty")
            except Exception as e:
                print(f"  ! {lvl} version failed ({str(e)[:200]}); using the Analyst version", file=sys.stderr)
                levels[lvl] = data
    if data:
        written = brief_markdown(data)
        print(f"  formatted OK: {len(data['stories'])} stories, {len(data['deals'])} deals")
    elif written:
        print("  ! couldn't format the brief; the site will show it as plain text", file=sys.stderr)
        if looks_like_json(written):
            written = "_Today's written brief couldn't be formatted. The numbers and podcast above are still current._"
    else:
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

    mp3s = {}
    if not args.no_podcast:
        print("Making podcasts...")
        for lvl in (LEVELS if levels else ["analyst"]):
            d_l = levels.get(lvl)
            pod_input = f"# Morning Brief: {today}\n\n{table}\n\n{brief_markdown(d_l, full=True)}" if d_l else brief
            m = make_podcast(pod_input, out_dir, path.stem, lvl)
            if m:
                mp3s[lvl] = m
    mp3 = mp3s.get("analyst")

    if not args.demo or args.site:
        site_dir = Path(os.getenv("SITE_DIR") or Path(__file__).parent / "docs")
        try:
            build_site(site_dir, dt.date.today().isoformat(), today, nums, written, mp3, args.demo, data,
                       levels=levels or None, mp3s=mp3s)
        except Exception as e:
            print(f"  ! website: {e}", file=sys.stderr)

    if not args.no_email:
        send_email(f"Morning Brief: {today}", brief, html_body, mp3)


if __name__ == "__main__":
    main()
