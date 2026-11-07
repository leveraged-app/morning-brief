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
# 4b. THE PODCAST (needs OPENAI_API_KEY; skipped if it's not set)
# ---------------------------------------------------------------------------
HOSTS = {  # host name -> OpenAI voice. Both male, picked to sound clearly different.
    "ALEX": os.getenv("VOICE_ALEX", "ash"),
    "SAM": os.getenv("VOICE_SAM", "verse"),
}
FALLBACK_VOICES = {"ALEX": "onyx", "SAM": "echo"}  # used only if the newer model fails
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
# Speeds up the finished episode without changing pitch (1.0 = normal, 1.2 = 20% faster)
PODCAST_SPEED = float(os.getenv("PODCAST_SPEED", "1.18"))

# Acting directions for each host (how they should sound, not what they say)
HOST_STYLE = {
    "ALEX": ("You are ALEX, a male host of a daily markets podcast. You sound like someone "
             "who works on a trading floor and is walking a friend through the morning: "
             "confident, relaxed, engaged. Natural, conversational delivery with a brisk pace "
             "and no long pauses. Emphasize the numbers that matter. Don't oversell or sound "
             "like an announcer, and never sound flat or like you're reading."),
    "SAM": ("You are SAM, a male co-host of a daily markets podcast. Thoughtful and curious, "
            "with a calm, warm voice. You talk like a sharp analyst having a real "
            "conversation: natural rhythm, brisk pace, no long pauses. Only sound surprised "
            "when something is actually surprising. Don't oversell, and never sound flat or "
            "like you're reading."),
}

PODCAST_PROMPT = """Write the script for today's episode of "The Morning Brief", a daily
markets podcast with two male hosts, ALEX and SAM. Audience: finance students
and young professionals in Canada recruiting for IB, consulting and equity
research.

The feel: two people who follow markets for a living, giving an honest morning
rundown. Think of a good trading-desk morning call, or two analysts talking at
the coffee machine. It should sound authentic, not like an entertainment show.

How they talk:
- ALEX leads the rundown. SAM adds the "why it matters", asks the follow-up a
  smart listener would ask, or pushes back when a point needs nuance. Both
  know their stuff; neither is just there to react.
- Conversation comes from the content: a question, a connection between two
  stories, a "the part I'm watching is...". No banter for its own sake.
- Keep openings and transitions short and plain, e.g. "Morning, it's Friday,
  November 7th. Big story today is oil." or "Okay, Canada." Then get into it.
- Do NOT use: scripted jokes, fake laughter, "wild", "crazy", "buckle up",
  "let's dive in", "you heard that right", "great question", "wait, really?",
  "folks", over-the-top excitement, or one host repeating what the other just
  said. No more than one light aside in the whole episode, and only if it fits.
- Use contractions and everyday words. Mix short and longer lines. Most turns
  are one to four sentences. Say numbers the way people speak ("down about
  three percent", "the ten-year at four-eighteen").

What to cover, in order:
1. A one-line open with the date and the headline story.
2. The numbers that moved most, and the overall mood.
3. What moved and why, story by story.
4. What's coming today.
5. Canada: the TSX, banks, energy, the Bank of Canada, the loonie.
6. "The interview angle": one thing a listener could say if asked about
   markets today, explained briefly.
7. A short, plain sign-off and a one-line note that this is for education,
   not investment advice.

Rules:
- About 1,000-1,100 words.
- Use only facts and numbers from the brief. Do not add new facts.
- No sound effects, stage directions, music cues or markdown.
- Every line must start with "ALEX:" or "SAM:". Nothing else in the output.

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
        m = re.match(r"^\s*\**\s*(ALEX|SAM)\s*\**\s*:\s*(.+)$", raw.strip(), re.I)
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
            raise RuntimeError("script had no ALEX:/SAM: lines")
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

SITE_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#F6F5F1;color:#12161C;font-family:Inter,system-ui,-apple-system,Segoe UI,Arial,sans-serif;font-size:16px;line-height:1.5}
a{color:#0F6B4F}
.wrap{max-width:680px;margin:0 auto;padding:20px 16px 48px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:6px}
.brand{font-family:'Space Grotesk',Inter,sans-serif;font-weight:700;font-size:15px;letter-spacing:.3px}
nav a{margin-left:14px;font-size:14px;font-weight:600;text-decoration:none;color:#5B6270;display:inline-block;padding:10px 0}
nav a.on{color:#0F6B4F}
h1{font-family:'Space Grotesk',Inter,sans-serif;font-size:30px;line-height:1.15;margin:8px 0 4px;letter-spacing:-.5px}
.date{color:#5B6270;font-size:14px;font-weight:500;margin:0 0 18px}
.note{background:#FFF4E5;color:#8A3B06;padding:8px 12px;border-radius:10px;font-size:13px;margin:0 0 14px}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0 0 16px}
.tile{background:#fff;border:1px solid #E3E1DA;border-radius:14px;padding:10px 12px}
.tile .n{font-size:12px;font-weight:600;color:#5B6270}
.tile .v{font-size:17px;font-weight:700}
.tile .c{font-size:13px;font-weight:700}
.up{color:#0B6E4F}.down{color:#B54708}.na{color:#5B6270}
.player{background:#12161C;color:#fff;border-radius:18px;padding:16px;margin:0 0 20px}
.player .k{font-size:12px;color:#B9BEC7;font-weight:500}
.player .t{font-size:17px;font-weight:600;margin:2px 0 10px}
.player audio{width:100%}
.card{background:#fff;border:1px solid #E3E1DA;border-radius:18px;padding:4px 18px 14px}
.list a{display:flex;justify-content:space-between;gap:12px;padding:14px 16px;background:#fff;border:1px solid #E3E1DA;border-radius:14px;margin:0 0 8px;text-decoration:none;color:#12161C;font-weight:600}
.list span{color:#5B6270;font-weight:500;font-size:14px}
footer{color:#5B6270;font-size:12px;margin-top:24px}
@media (max-width:420px){.tile .v{font-size:15px}h1{font-size:26px}}
"""


def page(title, body, root, active):
    def link(href, label, key):
        cls = ' class="on" aria-current="page"' if key == active else ""
        return f'<a href="{root}{href}"{cls}>{label}</a>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&amp;family=Space+Grotesk:wght@600;700&amp;display=swap" rel="stylesheet">
<style>{SITE_CSS}</style></head>
<body><div class="wrap">
<header><div class="brand">{SITE_NAME}</div>
<nav aria-label="Main">{link("index.html", "Today", "today")}{link("learn.html", "Learn", "learn")}{link("archive.html", "Archive", "archive")}</nav></header>
{body}
<footer>For education only. Not investment advice. Some numbers may come from ETFs that track an index.</footer>
</div></body></html>"""


def tiles_html(nums):
    out = []
    for name in ORDER:
        d = nums.get(name)
        if d is None:
            out.append(f'<div class="tile"><div class="n">{html.escape(name)}</div>'
                       f'<div class="v">n/a</div><div class="c na">n/a</div></div>')
            continue
        chg = fmt_change(d["value"], d["prev"], d["kind"])
        cls = "up" if chg.startswith("▲") else "down"
        out.append(f'<div class="tile"><div class="n">{html.escape(d.get("label", name))}</div>'
                   f'<div class="v">{fmt_value(d["value"], d["kind"])}</div>'
                   f'<div class="c {cls}">{chg}</div></div>')
    return '<div class="tiles">' + "".join(out) + "</div>"


def day_body(rec, root):
    note = '<p class="note">Sample numbers (demo mode), not real market data.</p>' if rec.get("demo") else ""
    player = ""
    if rec.get("audio"):
        player = (f'<div class="player"><div class="k">Today\'s episode</div>'
                  f'<div class="t">The Morning Brief podcast</div>'
                  f'<audio controls preload="none" src="{root}audio/{rec["audio"]}"></audio></div>')
    return (f'<h1>Today\'s brief</h1><p class="date">{html.escape(rec["today"])}</p>{note}'
            f'{tiles_html(rec["nums"])}{player}<div class="card">{md_to_html(rec["written"])}</div>')


def build_site(site_dir, iso, today, nums, written, mp3, demo):
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
           "audio": audio_name, "demo": demo}
    (days / f"{iso}.json").write_text(json.dumps(rec), encoding="utf-8")

    # Delete old audio so the project stays small
    cutoff = (dt.date.fromisoformat(iso) - dt.timedelta(days=KEEP_AUDIO_DAYS)).isoformat()
    for f in audio.glob("*.mp3"):
        if f.stem < cutoff:
            f.unlink()

    # Rebuild every day page, the home page and the archive
    records = []
    for f in sorted(days.glob("*.json"), reverse=True):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("audio") and not (audio / r["audio"]).exists():
            r["audio"] = None
        records.append(r)
        (days / f"{r['iso']}.html").write_text(
            page(f"{SITE_NAME}: {r['today']}", day_body(r, "../"), "../", "day"), encoding="utf-8")

    latest = records[0]
    (site_dir / "index.html").write_text(
        page(SITE_NAME, day_body(latest, ""), "", "today"), encoding="utf-8")

    items = "".join(
        f'<a href="days/{r["iso"]}.html">{html.escape(r["today"])}'
        f'<span>{"Brief + podcast" if r.get("audio") else "Brief"}</span></a>' for r in records)
    (site_dir / "archive.html").write_text(
        page(f"{SITE_NAME}: Archive",
             f'<h1>Archive</h1><p class="date">Every past brief</p><div class="list">{items}</div>',
             "", "archive"), encoding="utf-8")
    print(f"  website updated ({len(records)} days)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="store_true", help="also build the website in demo mode")
    ap.add_argument("--demo", action="store_true", help="use sample numbers")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--no-podcast", action="store_true")
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

    mp3 = None
    if not args.no_podcast:
        print("Making podcast...")
        mp3 = make_podcast(brief, out_dir, path.stem)

    if not args.demo or args.site:
        site_dir = Path(os.getenv("SITE_DIR") or Path(__file__).parent / "docs")
        try:
            build_site(site_dir, dt.date.today().isoformat(), today, nums, written, mp3, args.demo)
        except Exception as e:
            print(f"  ! website: {e}", file=sys.stderr)

    if not args.no_email:
        send_email(f"Morning Brief: {today}", brief, html_body, mp3)


if __name__ == "__main__":
    main()
