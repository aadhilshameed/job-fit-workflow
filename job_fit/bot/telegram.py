"""The Telegram alerts: one summary message, then one short message per posting.

Needs two lines in .env:
    TELEGRAM_BOT_TOKEN   the bot's password (from BotFather)
    TELEGRAM_CHAT_ID     the address of my chat with the bot

What gets sent:
- a summary first: how many new postings, how many per verdict, how many skipped
- then one message per posting, so each one can get its own buttons later
- apply, stretch and lack_of_info are sent; skips are only counted in the summary
- order: nearby first (within the weekly radius), then hybrid (office days somewhere reachable),
  then the rest (remote, or office days not mentioned); inside each group apply, then check, then stretch,
  and the closest first
- the place shown is the closest of the posting's locations, with 📍+N for the other places;
  gender tags such as (m/w/d) are dropped from the title
- each posting message is short: verdict, distance, title, company, the job type (🎓 WS Werkstudent,
  🧪 Intern, 📝 Thesis, 🏢 Job) with one line on the role, and the gaps
  as short labels; collapsed boxes you tap to open: "About the job" (2 or 3 lines on the work) under the
  company, and at the end one box with the requirements: REQUIRED (critical) first, then ALSO ASKED FOR
  (standard), each part most important first,
  with a dot (🟢 met, 🟡 partial, 🔴 missing, ⚪ not stated), a short reason for each gap, and
  related items under one heading (for example "Practical experience:")
- a German posting is marked 🇩🇪 German in the first line, since it usually means German every day
- every posting message has an "Open posting" button that opens it on the Arbeitsagentur website
"""

import html
import os
import re
import time
from datetime import date, datetime

import requests

from job_fit.location import HOME, WEEKLY_RADIUS_KM, distance_km

API_URL = "https://api.telegram.org/bot{token}/{method}"
POSTING_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
MAX_LENGTH = 4000          # Telegram allows 4096 characters per message; leave some room

VERDICT_HEADINGS = {"apply": "✅ <b>APPLY</b>", "lack_of_info": "❓ <b>CHECK</b>",
                    "stretch": "🟠 <b>STRETCH</b>", "skip": "⛔ <b>SKIP</b>"}
VERDICT_ORDER = {"apply": 0, "lack_of_info": 1, "stretch": 2, "skip": 3}
SENT_VERDICTS = {"apply", "lack_of_info", "stretch"}
PRESENCE_SHORT = {"not_mentioned": "office days?", "remote": "remote", "occasional": "hybrid",
                  "weekly": "office weekly", "regular_unspecified": "regular office"}
HYBRID = {"occasional", "weekly", "regular_unspecified"}
GAP_PREFIXES = (("hard_gap: ", "hard_gap"), ("partial (language, not counted): ", "language"), ("partial: ", "partial"))
DOTS = {"match": "🟢", "partial": "🟡", "hard_gap": "🔴", "not_stated": "⚪"}
GENDER_TAG = re.compile(r"\s*\((?:all genders|[mwfdxgn*]+|div\.?)(?:\s*[/|,]\s*(?:[mwfdxgn*]+|div\.?))*\)",
                        re.IGNORECASE)    # (m/w/d), (w/m/div.), (all genders), (g*), (m/f/x)
JOB_TYPES = ((re.compile(r"werkstudent|working student|studentische", re.IGNORECASE), "🎓 WS"),
             (re.compile(r"praktik|internship|\bintern\b|trainee", re.IGNORECASE), "🧪 Intern"),
             (re.compile(r"abschlussarbeit|masterarbeit|bachelorarbeit|thesis", re.IGNORECASE), "📝 Thesis"))
MAX_LISTED = 8             # "also asked for" requirements shown (gaps are always shown)
GERMAN_WORDS = {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "von", "zu", "bei", "auf", "oder",
                "eine", "ein", "du", "dich", "dein", "deine", "ihre", "unser", "unsere", "kenntnisse"}
ENGLISH_WORDS = {"and", "the", "with", "for", "we", "you", "is", "of", "to", "at", "on", "or", "an", "your", "our",
                 "skills", "experience"}


# ---------------------------------------------------------------- sending

def send_message(text: str, url_buttons: list[tuple[str, str]] | None = None) -> None:
    """Send one message to my chat. url_buttons: [(label, link), ...], shown in one row under the message."""
    payload = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:MAX_LENGTH],
               "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if url_buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": link} for label, link in url_buttons]]}
    url = API_URL.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method="sendMessage")
    for attempt in range(1, 4):
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code == 429:                       # too many messages too fast: wait as told
            time.sleep(response.json().get("parameters", {}).get("retry_after", 5))
            continue
        if response.ok:
            return
        if attempt == 3:
            raise RuntimeError(f"Telegram refused the message: {response.status_code} {response.text[:200]}")
        time.sleep(3)


# ---------------------------------------------------------------- building the text

def published_ago(published: datetime | date | None, now: datetime | None = None) -> str | None:
    """'5 h ago' within the first day, '1d 5h ago' after that. With only a date: 'today', 'yesterday', '3 days ago'."""
    if published is None:
        return None
    now = now or datetime.now()
    if isinstance(published, datetime):
        hours = int((now - published).total_seconds() // 3600)
        if hours < 1:
            return "less than 1 h ago"
        if hours < 24:
            return f"{hours} h ago"
        return f"{hours // 24}d {hours % 24}h ago"
    days = (now.date() - published).days
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def nearest(locations: list[dict]) -> tuple[float, str] | None:
    """(km, city) of the closest location; a posting can list several (Wegberg, Erlangen, Braunschweig)."""
    places = [(distance_km(HOME, (loc["lat"], loc["lon"])), loc.get("city") or "?") for loc in locations
              if loc.get("lat") is not None and loc.get("lon") is not None]
    return min(places) if places else None


def nearest_km(locations: list[dict]) -> float | None:
    found = nearest(locations)
    return found[0] if found else None


def place_group(posting: dict) -> int:
    """0 nearby, 1 hybrid somewhere reachable, 2 the rest (remote, or office days not mentioned)."""
    km = nearest_km(posting.get("locations") or [])
    if km is not None and km <= WEEKLY_RADIUS_KM:
        return 0
    return 1 if posting.get("office_presence") in HYBRID else 2


def job_type(title: str) -> str:
    """Werkstudent, internship or thesis, read from the title (the first match wins); anything else is a job."""
    for pattern, label in JOB_TYPES:
        if pattern.search(title or ""):
            return label
    return "🏢 Job"


def posting_language(text: str) -> str:
    """"de" or "en", by counting common German and English words. A German posting usually means German
    is used every day at work, whatever the posting says about language levels."""
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    german = sum(w in GERMAN_WORDS for w in words)
    english = sum(w in ENGLISH_WORDS for w in words)
    return "de" if german > english else "en"


def requirement_rows(result: dict) -> list[dict]:
    """The skills and eligibility requirements of one result, in the order summary.py numbers them."""
    return [{"text": r["requirement_text"], "match_result": r["match_result"], "match_reason": r["match_reason"],
             "importance": r["importance"]}
            for r in result.get("requirements") or [] if r["requirement_type"] in ("technical_skill", "eligibility")]


def gap_kinds(result: dict) -> dict[str, str]:
    """requirement text -> hard_gap, partial, language or eligibility, for the gaps that shaped the verdict."""
    kinds = {}
    for gap in result.get("critical_gaps") or []:
        for prefix, kind in GAP_PREFIXES:
            if gap.startswith(prefix):
                kinds[gap[len(prefix):]] = kind
                break
    for text in result.get("missing_eligibility") or []:
        kinds[text] = "eligibility"
    return kinds


def location_lines(result: dict) -> list[str]:
    """The location check and the far-away note, as full sentences."""
    lines = [g.split(": ", 1)[-1] for g in result.get("critical_gaps") or [] if g.startswith("location")]
    return lines + ([result["location_note"]] if result.get("location_note") else [])


def ranked_requirements(result: dict, summary=None) -> list[dict]:
    """Every requirement with a short English label, a short reason and a group heading, most important first,
    from summary.py. Without a summary (the call failed twice), only the gaps, with their full texts, so
    nothing is cut in the middle of a word."""
    rows = requirement_rows(result)
    if not summary:
        gaps = gap_kinds(result)
        return [r | {"label": r["text"], "reason": r["match_reason"], "group": ""} for r in rows if r["text"] in gaps]
    ranked, used = [], set()
    for item in summary.requirements:
        if 1 <= item.number <= len(rows) and item.number not in used:
            used.add(item.number)
            ranked.append(rows[item.number - 1] | {"label": item.label, "reason": item.short_reason,
                                                   "group": item.group.strip()})
    rest = [r for i, r in enumerate(rows, 1) if i not in used]          # anything the summary left out
    rest.sort(key=lambda r: r["importance"] != "critical")
    return ranked + [r | {"label": r["text"], "reason": r["match_reason"], "group": ""} for r in rest]


def grouped(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """Keeps items with the same group heading together, at the place of the group's first (most important) item."""
    out, seen = [], {}
    for item in items:
        group = item["group"]
        if group and group in seen:
            out[seen[group]][1].append(item)
        else:
            if group:
                seen[group] = len(out)
            out.append((group, [item]))
    return out


def posting_message(posting: dict, result: dict, published=None, summary=None) -> str:
    """The text of one posting's message. summary: a PostingSummary from summary.py, or None."""
    esc = html.escape
    locations = posting.get("locations") or []
    closest = nearest(locations)
    city = closest[1] if closest else posting.get("ort")
    n_other = len({loc.get("city") for loc in locations if loc.get("city") and loc.get("city") != city})

    presence = posting.get("office_presence")
    head = [VERDICT_HEADINGS.get(result["code_verdict"], result["code_verdict"])]
    if presence != "remote" and closest:
        head.append(f"{closest[0]:.0f} km")
    head.append(PRESENCE_SHORT.get(presence, "office days?"))
    if posting_language(posting.get("description") or "") == "de":
        head.append("🇩🇪 German")
    place = " · ".join(x for x in (posting.get("firma"), city) if x) + (f" 📍+{n_other}" if n_other else "")
    title = GENDER_TAG.sub("", posting.get("title") or "").strip()
    lines = [" · ".join(head), f"<b>{esc(title)}</b>", esc(place)]
    if summary and summary.about:
        lines.append("<blockquote expandable><b>About the job</b>\n"
                     + "\n".join(f"• {esc(line)}" for line in summary.about) + "</blockquote>")
    lines.append("")

    ago = published_ago(published)
    if ago:
        lines.append(f"🕒 {ago}")
    kind = job_type(posting.get("title") or "")
    lines.append(f"{kind} · {esc(summary.role)}" if summary else kind)

    # the gaps that shaped the verdict, as short labels on one line
    kinds = gap_kinds(result)
    ranked = ranked_requirements(result, summary)
    label_of = {r["text"]: r["label"] for r in ranked}
    short = []
    for kind, dot, extra in (("hard_gap", "🔴", ""), ("partial", "🟡", ""), ("language", "🟡", " (not counted)"),
                             ("eligibility", "⚪", " (not in your background)")):
        short += [f"{dot} {esc(label_of.get(text, text))}{extra}" for text, k in kinds.items() if k == kind]
    if location_lines(result):
        short.append("🚆 check the location")
    lines.append("   ".join(short) if short else "🟢 No gaps")

    # one collapsed box: the required (critical) requirements first, then the ones the posting also asks for
    # (standard), each part most important first
    required = [r for r in ranked if r["importance"] == "critical" or r["text"] in kinds]
    also = [r for r in ranked if r not in required][:MAX_LISTED]
    box = []
    for heading, items, extra in (("REQUIRED", required, location_lines(result)), ("ALSO ASKED FOR", also, [])):
        if not items and not extra:
            continue
        if box:
            box.append("")
        box.append(f"<b>{heading}</b>")
        for group, members in grouped(items):
            if group and len(members) > 1:
                box.append(f"<b>{esc(group)}:</b>")
            indent = "    " if group and len(members) > 1 else ""
            for r in members:
                line = f"{indent}{DOTS.get(r['match_result'], '•')} {esc(r['label'])}"
                if kinds.get(r["text"]) == "language":
                    line += " (not counted)"
                if r["match_result"] in ("partial", "hard_gap") and r.get("reason"):
                    line += f" · <i>{esc(r['reason'])}</i>"
                box.append(line)
        box += [f"🚆 {esc(text)}" for text in extra]
    if box:
        lines.append("<blockquote expandable>" + "\n".join(box) + "</blockquote>")
    return "\n".join(lines)


def summary_message(results: list[dict], day: date | None = None, note: str = "") -> str:
    """The first message: how many new postings and what they got."""
    day = day or date.today()
    counts = {v: sum(1 for r in results if r["code_verdict"] == v) for v in VERDICT_ORDER}
    sent = [f"{counts[v]} {name}" for v, name in (("apply", "apply"), ("stretch", "stretch"),
                                                  ("lack_of_info", "check")) if counts[v]]
    text = f"🗓 <b>{day.strftime('%a %d %b')}: {len(results)} new postings</b>\n"
    text += (" · ".join(sent) or "nothing worth sending") + "\n"
    if counts["skip"]:
        text += f"<i>{counts['skip']} skipped (not sent)</i>"
    return text + (f"\n{html.escape(note)}" if note else "")


def send_alerts(items: list[tuple[dict, dict, object]], note: str = "", limit: int | None = None,
                summarize=None) -> int:
    """items: [(posting, result, published), ...]. Sends the summary, then the postings in order.
    summarize: a function (title, description, requirement rows) -> PostingSummary, called only for postings
    that get sent, with one retry; if both tries fail, the message goes out without the role line and
    lists only the gaps, in the posting's own words.
    Returns how many postings were sent."""
    send_message(summary_message([r for _, r, _ in items], note=note))
    to_send = sorted((i for i in items if i[1]["code_verdict"] in SENT_VERDICTS),
                     key=lambda i: (place_group(i[0]), VERDICT_ORDER[i[1]["code_verdict"]],
                                    nearest_km(i[0].get("locations") or []) or 9999))
    for posting, result, published in to_send[:limit]:
        summary = None
        for attempt in (1, 2) if summarize else ():             # one retry: most failures are passing ones
            try:
                summary = summarize(posting.get("title") or "", posting.get("description") or "",
                                    requirement_rows(result))
                break
            except Exception as e:                                  # noqa: BLE001
                print(f"Summary failed for {posting['refnr']} (try {attempt} of 2): {type(e).__name__}: {e}"[:300])
        send_message(posting_message(posting, result, published, summary),
                     [("Open posting ↗", POSTING_URL.format(refnr=posting["refnr"]))])
        time.sleep(1)                                          # Telegram allows about one message a second per chat
    return len(to_send[:limit])
