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
Also search for notable M&A and capital markets deals (acquisitions, mergers,
IPOs, big financings) announced in the last few days, especially Canadian deals
or very large global ones.
Prefer original sources (central banks, statistics agencies, company releases)
and reputable outlets. Write everything in your own words: do not copy
sentences from articles.

Return ONLY a JSON object (no markdown, no code fences, no text before or
after it) with exactly these keys:

{{
  "headline": "The day's biggest story in at most 9 words, like a news headline",
  "mood": "One or two sentences on the overall tone (risk-on / risk-off, what led)",
  "stories": [
    {{"tag": "one of: Stocks, Rates, Energy, Commodities, FX, Macro, Earnings, Canada, Crypto",
      "title": "One-line headline",
      "why_happened": "One or two sentences",
      "why_matters": "One or two sentences"}}
  ],
  "calendar": [
    {{"time": "e.g. 8:30 AM ET, or 'Before open' / 'After close'",
      "event": "What is scheduled",
      "why": "Why it matters, max 15 words"}}
  ],
  "calendar_note": "If nothing major is scheduled, say so here; otherwise empty",
  "deals": [
    {{"type": "Acquisition, Merger, IPO or Financing",
      "buyer": "Acquirer or issuer",
      "target": "Company being bought (empty for IPOs/financings)",
      "value": "Deal size as reported, e.g. US$2.1B, or 'Undisclosed'",
      "why": "One or two sentences: the strategic logic and what it signals"}}
  ],
  "canada": ["2-4 short points on the TSX, Canadian banks, energy, the Bank of Canada or the loonie"],
  "takeaway": "1-2 sentences starting with 'If someone asks you about markets today:'",
  "sources": [{{"title": "Page title", "url": "https://..."}}]
}}

Keep every field concise: one or two sentences each. Don't comment on data
timing or sources inside the text. Plain text only inside the JSON: no citation
tags, no HTML, no markdown. Put links only in "sources".

Rules: up to 3 stories, only ones that moved the numbers above or a major
company or sector. Up to 3 deals, only ones you found in your searches with a
source; use an empty list if there are none. Under 550 words of text in total, plain language, no hype,
no buy/sell recommendations. Do not repeat the numbers table. Valid JSON only:
double quotes, no trailing commas.
"""


def write_with_claude(table, today):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = {
        "model": MODEL,
        "max_tokens": 8000,
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
    for k in ("stories", "calendar", "canada", "sources", "deals"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def looks_like_json(text):
    return bool(text) and "{" in text and ('"headline"' in text or '"stories"' in text)


def brief_markdown(d):
    """Turn the structured brief into the Markdown used by the email and podcast."""
    out = ["## 1. The numbers", d.get("mood", ""), "", "## 2. What moved and why"]
    for st in d["stories"]:
        out += [f"**{st.get('title','')}**", f"Why it happened: {st.get('why_happened','')}",
                f"Why it matters: {st.get('why_matters','')}", ""]
    if d["deals"]:
        out += ["## Deals"]
        for x in d["deals"]:
            who = x.get("buyer", "")
            if x.get("target"):
                who += f" → {x['target']}"
            out += [f"- **{x.get('type', 'Deal')}: {who}** ({x.get('value', 'Undisclosed')}). {x.get('why', '')}"]
        out.append("")
    out += ["## 3. What's coming today"]
    for c in d["calendar"]:
        out.append(f"- {c.get('time','')}: {c.get('event','')}. {c.get('why','')}".strip())
    if d.get("calendar_note"):
        out.append(d["calendar_note"])
    out += ["", "## 4. Canada corner"] + [f"- {x}" for x in d["canada"]]
    out += ["", "## 5. The takeaway", d.get("takeaway", "")]
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
6. On deck today (short): the key scheduled events and why they matter.
7. The interview angle: one thing a listener could say if asked about markets
   today, in a sentence or two.
8. Sign-off: one plain line, then "This is for education only, not investment
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
        json={"model": MODEL, "max_tokens": max_tokens,
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


def make_podcast(brief, out_dir, stem):
    """Returns the path to the MP3, or None if skipped/failed."""
    key = os.getenv("OPENAI_API_KEY")
    if not key or not os.getenv("ANTHROPIC_API_KEY"):
        print("  (podcast skipped: needs OPENAI_API_KEY and ANTHROPIC_API_KEY)")
        return None
    try:
        script = claude_plain(PODCAST_PROMPT.format(brief=brief))
        lines = parse_script(script)
        if not lines:
            raise RuntimeError("script had no ALEX: lines")
        (out_dir / f"{stem}-script.txt").write_text(script, encoding="utf-8")
        audio = b"".join(tts(text, who, key) for who, text in lines)
        print(f"  voices: {TTS_STATS['main']} lines on {TTS_MODEL}, "
              f"{TTS_STATS['fallback']} on the older fallback model")
        mp3 = out_dir / f"{stem}-podcast.mp3"
        mp3.write_bytes(audio)
        speed_up(mp3, PODCAST_SPEED)
        print(f"  podcast saved {mp3} ({len(lines)} lines, {len(audio)//1024} KB)")
        return mp3
    except Exception as e:
        print(f"  ! podcast: {e}", file=sys.stderr)
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
.bar>div{position:absolute;left:0;top:0;bottom:0;border-radius:999px;background:var(--g);width:0}
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
.tile .n{font-size:13px;font-weight:600;color:var(--muted)}
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
.empty{color:var(--muted);font-size:15px;padding:14px 0}
.canada{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:16px 18px}
.canada ul{margin:0;padding:0;list-style:none}
.canada li{position:relative;padding:8px 0 8px 18px;font-size:15px;border-bottom:1px solid var(--line);color:#D6DBE3}
.canada li:last-child{border-bottom:0}
.canada li::before{content:"";position:absolute;left:0;top:17px;width:7px;height:7px;border-radius:50%;background:#E5484D}
.line{background:linear-gradient(180deg,#10291F,#0C1F18);border:1px solid #1D4A36;border-radius:24px;padding:20px}
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
.list a{display:block;padding:16px 18px;background:var(--card);border:1px solid var(--line);border-radius:18px;margin:0 0 10px;text-decoration:none;color:var(--ink);transition:transform .15s,border-color .15s}
.list a:active{transform:scale(.99);border-color:var(--line2)}
.list .d{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.4px;text-transform:uppercase}
.list .h{font-family:Geist,Inter,sans-serif;font-size:17px;font-weight:700;margin:4px 0 6px;letter-spacing:-.2px}
.list .c{font-size:12px;font-weight:700;color:var(--g);background:var(--upbg);padding:3px 8px;border-radius:999px;margin-right:6px}
h1.page{font-family:Geist,Inter,sans-serif;font-size:30px;letter-spacing:-.6px;margin:4px 2px 16px}
footer{color:#6E7684;font-size:12px;margin:26px 2px 0;text-align:center}
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:20;background:rgba(10,13,18,.86);backdrop-filter:saturate(1.4) blur(16px);-webkit-backdrop-filter:saturate(1.4) blur(16px);border-top:1px solid var(--line);padding:8px 0 calc(8px + env(safe-area-inset-bottom))}
.tabbar .in{max-width:640px;margin:0 auto;display:flex;justify-content:space-around}
.tabbar a{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:72px;min-height:48px;justify-content:center;text-decoration:none;color:#7D8594;font-size:11px;font-weight:600}
.tabbar a.on{color:var(--ink)}
.tabbar a.on svg{color:var(--g)}
.reveal{opacity:0;transform:translateY(16px);transition:opacity .6s ease,transform .6s cubic-bezier(.2,.8,.2,1)}
.reveal.in{opacity:1;transform:none}
@keyframes rise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}.reveal{opacity:1;transform:none}}
"""

TAB_ICONS = {
    "today": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 17l6-6 4 4 8-8"/><path d="M14 7h7v7"/></svg>',
    "learn": '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 9l10-5 10 5-10 5z"/><path d="M6 11v5c3 2.5 9 2.5 12 0v-5"/></svg>',
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
  // copy interview line
  var cp=document.getElementById('copyline');
  if(cp){ cp.addEventListener('click', function(){ var t=document.getElementById('theline').innerText;
    (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){cp.textContent='Copied';setTimeout(function(){cp.textContent='Copy'},1600)}).catch(function(){}); }); }
  // audio player
  var a=document.getElementById('aud'); if(!a) return;
  var btn=document.getElementById('pbtn'), fill=document.getElementById('pfill'), seek=document.getElementById('pseek'),
      cur=document.getElementById('pcur'), dur=document.getElementById('pdur'), sp=document.getElementById('pspeed');
  var PLAY='<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>',
      PAUSE='<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>';
  function fmt(s){ if(!isFinite(s)) return '0:00'; s=Math.floor(s); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
  btn.addEventListener('click', function(){ if(a.paused){a.play()}else{a.pause()} });
  a.addEventListener('play', function(){btn.innerHTML=PAUSE;btn.setAttribute('aria-label','Pause episode')});
  a.addEventListener('pause', function(){btn.innerHTML=PLAY;btn.setAttribute('aria-label','Play episode')});
  a.addEventListener('loadedmetadata', function(){dur.textContent=fmt(a.duration)});
  a.addEventListener('timeupdate', function(){ var p=a.duration?a.currentTime/a.duration*100:0; fill.style.width=p+'%'; seek.value=p; cur.textContent=fmt(a.currentTime); });
  seek.addEventListener('input', function(){ if(a.duration) a.currentTime=seek.value/100*a.duration; });
  var speeds=[1,1.25,1.5,2], si=0;
  sp.addEventListener('click', function(){ si=(si+1)%speeds.length; a.playbackRate=speeds[si]; sp.textContent=speeds[si]+'×'; });
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
{{datepill}}</div>
{body}
<footer>For education only. Not investment advice. Some numbers come from ETFs that track an index.</footer>
</div>
<nav class="tabbar" aria-label="Main"><div class="in">{tab("index.html", "Today", "today")}{tab("learn.html", "Learn", "learn")}{tab("archive.html", "Archive", "archive")}</div></nav>
<script>{SITE_JS}</script>
</body></html>"""


def _chg(d):
    if d is None:
        return "n/a", '<span class="chg na">n/a</span>'
    chg = fmt_change(d["value"], d["prev"], d["kind"])
    cls = "up" if chg.startswith("▲") else "down"
    return fmt_value(d["value"], d["kind"]), f'<span class="chg {cls}">{chg}</span>'


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
            tiles.append(f'<div class="tile"><div class="n">{html.escape(lab)}</div>'
                         f'<div class="v">{val}</div>{chip}</div>')
        panels.append(f'<div class="panel{" show" if i == 0 else ""}" id="{pid}" role="tabpanel" '
                      f'aria-labelledby="t{pid}"{"" if i == 0 else " hidden"}>{"".join(tiles)}</div>')
    return (f'<section class="sec reveal" aria-label="Markets"><div class="sech"><h2>Markets</h2><span>At the close</span></div>'
            f'<div class="tabs" role="tablist" aria-label="Market groups">{"".join(tabs)}</div>{"".join(panels)}</section>')


def player_html(rec, root):
    if not rec.get("audio"):
        return '<div class="player"><span class="noaudio">No episode for this day.</span></div>'
    return (f'<div class="player"><button class="pbtn" id="pbtn" type="button" aria-label="Play episode">'
            f'<svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg></button>'
            f'<div class="pmeta"><div class="ptitle">Today\'s episode</div>'
            f'<div class="bar"><div id="pfill"></div><input id="pseek" type="range" min="0" max="100" step="0.1" value="0" aria-label="Seek"></div>'
            f'<div class="ptime"><span id="pcur">0:00</span><span id="pdur">–:––</span></div></div>'
            f'<button class="speed" id="pspeed" type="button" aria-label="Playback speed">1×</button>'
            f'<audio id="aud" preload="metadata" src="{root}audio/{rec["audio"]}"></audio></div>')


def day_body(rec, root):
    e = html.escape
    note = '<p class="note">Sample numbers (demo mode), not real market data.</p>' if rec.get("demo") else ""
    d = rec.get("data")
    if not d:  # older days saved before the redesign
        return (f'{note}<section class="hero"><div class="eyebrow">Morning brief</div><h1>Today\'s markets</h1>'
                f'{player_html(rec, root)}</section>{markets_html(rec["nums"])}'
                f'<section class="sec"><div class="card">{md_to_html(rec["written"])}</div></section>')

    hero = (f'<section class="hero"><div class="eyebrow">Morning brief</div><h1>{e(d.get("headline", ""))}</h1>'
            f'<p class="mood">{e(d.get("mood", ""))}</p>{player_html(rec, root)}</section>')

    stories = d.get("stories", [])
    cards = []
    for i, st in enumerate(stories, 1):
        cards.append(
            f'<article class="story"><div class="top"><span class="tag">{e(st.get("tag", "Markets"))}</span>'
            f'<span class="count">{i} of {len(stories)}</span></div>'
            f'<h3>{e(st.get("title", ""))}</h3>'
            f'<div><div class="lbl">Why it matters</div><p>{e(st.get("why_matters", ""))}</p></div>'
            f'<div class="why" id="why{i}"><div><div class="lbl">Why it happened</div><p>{e(st.get("why_happened", ""))}</p></div></div>'
            f'<button class="more" type="button" aria-expanded="false" aria-controls="why{i}">Why it happened</button></article>')
    dots = "".join(f'<span class="{"on" if i == 0 else ""}"></span>' for i in range(len(stories))) if len(stories) > 1 else ""
    moved = (f'<section class="sec reveal" aria-label="What moved"><div class="sech"><h2>What moved</h2>'
             f'<span>{"Swipe" if len(stories) > 1 else ""}</span></div><div class="cards">{"".join(cards)}</div>'
             f'<div class="dots" aria-hidden="true">{dots}</div></section>') if stories else ""

    dl = []
    for x in d.get("deals", []):
        target = f'<span class="arrow">→</span>{e(x["target"])}' if x.get("target") else ""
        dl.append(f'<article class="deal"><div class="row"><span class="dtype">{e(x.get("type", "Deal"))}</span>'
                  f'<span class="dval">{e(x.get("value", "Undisclosed"))}</span></div>'
                  f'<div class="parties">{e(x.get("buyer", ""))}{target}</div><p>{e(x.get("why", ""))}</p></article>')
    deals = (f'<section class="sec reveal" aria-label="Deals"><div class="sech"><h2>Deals</h2><span>M&amp;A and capital markets</span></div>'
             f'<div class="deals">{"".join(dl)}</div></section>') if dl else ""

    evs = "".join(f'<div class="ev"><div class="t">{e(c.get("time", ""))}</div><div><div class="e">{e(c.get("event", ""))}</div>'
                  f'<div class="w">{e(c.get("why", ""))}</div></div></div>' for c in d.get("calendar", []))
    if not evs:
        evs = f'<div class="empty">{e(d.get("calendar_note") or "Nothing major on the calendar today.")}</div>'
    coming = f'<section class="sec reveal" aria-label="Coming up"><div class="sech"><h2>Coming up today</h2></div><div class="timeline">{evs}</div></section>'

    can = "".join(f"<li>{e(x)}</li>" for x in d.get("canada", []))
    canada = (f'<section class="sec reveal" aria-label="Canada desk"><div class="sech"><h2>Canada desk</h2></div>'
              f'<div class="canada"><ul>{can}</ul></div></section>') if can else ""

    take = d.get("takeaway", "")
    take_clean = re.sub(r"^\s*If someone asks you about markets today:\s*", "", take, flags=re.I)
    take_clean = take_clean[:1].upper() + take_clean[1:]
    line = (f'<section class="sec reveal" aria-label="Your interview line"><div class="line"><div class="eyebrow">Your interview line</div>'
            f'<blockquote id="theline">{e(take_clean)}</blockquote><div class="acts">'
            f'<button class="lbtn a" type="button" id="copyline">Copy</button>'
            f'<a class="lbtn b" href="{root}learn.html">Practice in Learn</a></div></div></section>') if take else ""

    srcs = "".join(f'<li><a href="{e(x.get("url", ""))}" rel="noopener" target="_blank">{e(x.get("title") or "Source")}</a></li>'
                   for x in d.get("sources", []) if str(x.get("url", "")).startswith("http"))
    sources = f'<details class="src"><summary>Sources ({len(d.get("sources", []))})</summary><ol>{srcs}</ol></details>' if srcs else ""

    return note + hero + markets_html(rec["nums"]) + moved + deals + coming + canada + line + sources


def short_date(iso):
    return dt.date.fromisoformat(iso).strftime("%a, %b %-d")


def build_site(site_dir, iso, today, nums, written, mp3, demo, data=None):
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

    rec = {"iso": iso, "today": today, "nums": nums, "written": written,
           "audio": audio_name, "demo": demo, "data": data}
    (days / f"{iso}.json").write_text(json.dumps(rec), encoding="utf-8")

    cutoff = (dt.date.fromisoformat(iso) - dt.timedelta(days=KEEP_AUDIO_DAYS)).isoformat()
    for f in audio.glob("*.mp3"):
        if f.stem < cutoff:
            f.unlink()

    def pill(text):
        return f'<span class="datepill">{html.escape(text)}</span>'

    records = []
    for f in sorted(days.glob("*.json"), reverse=True):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("audio") and not (audio / r["audio"]).exists():
            r["audio"] = None
        records.append(r)
        (days / f"{r['iso']}.html").write_text(
            page(f"{SITE_NAME}: {r['today']}", day_body(r, "../"), "../", "archive")
            .replace("{datepill}", pill(short_date(r["iso"]))), encoding="utf-8")

    latest = records[0]
    (site_dir / "index.html").write_text(
        page(SITE_NAME, day_body(latest, ""), "", "today").replace("{datepill}", pill(short_date(latest["iso"]))),
        encoding="utf-8")

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


BRIEF_VERSION = "2026-11-07d"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="store_true", help="also build the website in demo mode")
    ap.add_argument("--demo", action="store_true", help="use sample numbers")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-podcast", action="store_true")
    args = ap.parse_args()

    today = dt.date.today().strftime("%A, %B %d, %Y")
    print(f"brief.py version {BRIEF_VERSION}")
    print("Fetching numbers...")
    nums = get_numbers(demo=args.demo)
    table = numbers_table(nums)

    print("Writing brief...")
    try:
        written = write_with_claude(table, today)
    except Exception as e:
        print(f"  ! {e}", file=sys.stderr)
        written = None
    data = parse_brief(written)
    if written and not data and os.getenv("ANTHROPIC_API_KEY"):
        print("  brief JSON didn't parse; asking Claude to repair it...")
        try:
            fixed = claude_plain(
                "The text below was meant to be one JSON object for a market brief but it is "
                "invalid or cut off. Return ONLY the corrected, complete JSON object with the same "
                "keys (headline, mood, stories, deals, calendar, calendar_note, canada, takeaway, "
                "sources). Keep all the facts as written. If a field is cut off, finish it briefly "
                "or leave it empty. Do not add new facts.\n\n" + written, max_tokens=6000)
            data = parse_brief(fixed)
        except Exception as e:
            print(f"  ! repair failed: {e}", file=sys.stderr)
    if data:
        written = brief_markdown(data)
    elif written:
        print("  ! brief was not in the expected JSON format; using it as plain text", file=sys.stderr)
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

    mp3 = None
    if not args.no_podcast:
        print("Making podcast...")
        mp3 = make_podcast(brief, out_dir, path.stem)

    if not args.demo or args.site:
        site_dir = Path(os.getenv("SITE_DIR") or Path(__file__).parent / "docs")
        try:
            build_site(site_dir, dt.date.today().isoformat(), today, nums, written, mp3, args.demo, data)
        except Exception as e:
            print(f"  ! website: {e}", file=sys.stderr)

    if not args.no_email:
        send_email(f"Morning Brief: {today}", brief, html_body, mp3)


if __name__ == "__main__":
    main()
