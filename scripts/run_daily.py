"""The daily run: fresh postings from the job sites, screened, assessed, saved and sent to Telegram.

    python -m scripts.run_daily                      # postings first published today or yesterday
    python -m scripts.run_daily --days 1 --max 5     # a small first run: at most 5 Haiku assessments
    python -m scripts.run_daily --no-telegram        # everything except the Telegram messages

The steps, in order (each writes to the database as it goes, so a crash loses nothing already done):
1. every source's new postings: the Arbeitsagentur (search terms in sources/arbeitsagentur.py, every page)
   and Arbeitnow (its newest jobs in Germany), and the company feeds on the watch list (sources/company_feeds.py).
   Duplicates within a source and postings first published more than --days ago are dropped (except on the
   company feeds, which list only open jobs)
2. skip what the database already has (db.seen_before)
3. title filter: postings it drops are not fetched or stored (filtering again tomorrow costs nothing).
   Then the Arbeitsagentur detail call for the full text, and coordinates for Arbeitnow's city names (geocode.py)
4. Decisions API screen + location rule -> one row in screened_postings
5. Haiku assessment + code_verdict.py, for AI engineering roles (and for postings whose Decisions API call failed: fail-open),
   ASSESSMENT_WORKERS at a time -> model_verdicts and verdict_requirements. Postings from earlier runs that
   still have no verdict (a failed call, or a run stopped by --max) are tried again (db.still_to_assess)
6. summary + Telegram for apply, stretch and lack_of_info -> summary columns and sent_to_telegram_at
If the run crashes, a Telegram message says so (unless --no-telegram).
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import html
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT))
load_dotenv(ROOT / ".env")

from job_fit import db                                                      # noqa: E402
from job_fit.sources import arbeitnow, arbeitsagentur, company_feeds, smartrecruiters   # noqa: E402
from job_fit.geocode import add_coordinates                                 # noqa: E402
from job_fit.bot import telegram                                         # noqa: E402
from job_fit.llm.model_calls import ask_model                                        # noqa: E402
from job_fit.code_nodes.location import location_gap, location_note, locations_from_api   # noqa: E402
from job_fit.llm.prompt import PROMPT_VERSION, build_user_message            # noqa: E402
from job_fit.llm.decisions_api_filter import ask_decisions_filter                            # noqa: E402
from job_fit.llm.summary import (SUMMARY_EFFORT, SUMMARY_MODEL, SUMMARY_PROMPT_VERSION,   # noqa: E402
                             summarize)
from job_fit.code_nodes.title_filter import title_status                            # noqa: E402
from job_fit.code_nodes.code_verdict import compute_verdict                              # noqa: E402

# the fixed setup, chosen on 20 Nov 2026 (Haiku high beat medium on the 35-posting check)
ASSESSMENT_MODEL = {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "high"}
PRICE_PER_MILLION = {"claude-haiku-5-5": (0.10, 0.50)}       # dollars per 1M tokens: (input, output)
ASSESSMENT_WORKERS = 4      # Haiku calls at the same time, so a big day still fits in Lambda's 15 minutes
ARBEITSAGENTUR = "arbeitsagentur"


def cost_usd(model: str, usage: dict) -> float:
    price_in, price_out = PRICE_PER_MILLION[model]
    return (usage["input"] * price_in + usage["output"] * price_out) / 1_000_000


def published_on(job: dict) -> date | None:
    """The day the posting was first published ("datumErsteVeroeffentlichung": "2026-11-20" in the search
    result), or None if missing or unreadable."""
    text = job.get("datumErsteVeroeffentlichung") or (job.get("veroeffentlichungszeitraum") or {}).get("von")
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def assess(posting: dict, background: str) -> dict:
    """One Haiku call + code_verdict.py for one posting. Never raises: a failure comes back as an error."""
    cfg = ASSESSMENT_MODEL
    start = time.perf_counter()
    try:
        assessment, usage = ask_model(build_user_message(posting["description"], background),
                                      cfg["provider"], cfg["model"], cfg["effort"])
        result = compute_verdict(assessment, background, posting["location_gap"])
        result["location_note"] = posting["location_note"]
        return {"result": result, "error": None, "usage": usage, "seconds": time.perf_counter() - start}
    except Exception as e:                                                  # noqa: BLE001
        return {"result": None, "error": f"{type(e).__name__}: {e}"[:500], "usage": None,
                "seconds": time.perf_counter() - start}


def arbeitsagentur_postings(conn, args) -> list[dict]:
    """Steps 1-3 for the Arbeitsagentur: search, drop duplicates and old postings, skip what the database has,
    title filter, then the detail call for the full text. Returns postings ready for the Decisions API."""
    found = arbeitsagentur.search(days_back=args.days)
    keys, new, too_old = set(), [], 0
    for job in found:
        city = arbeitsagentur.location(job)[0]
        key = (job.get("firma") or "", job.get("stellenangebotsTitel") or "", city)
        if key in keys:
            continue                                                        # the same job twice in one search
        keys.add(key)
        if db.seen_before(conn, ARBEITSAGENTUR, job["referenznummer"], *key):
            continue
        if title_status(job.get("stellenangebotsTitel"), job.get("hauptberuf")) == "drop":
            continue
        published = published_on(job)
        if published and (date.today() - published).days > args.days:
            too_old += 1           # the search's own date filter (veroeffentlichtseit) is ignored, so we check here
            continue
        new.append(job)
    print(f"Arbeitsagentur: {len(found)} postings, {len(new)} new that pass the title filter"
          + (f" ({too_old} older than {args.days} days skipped)" if too_old else ""))

    postings = []
    for job in new[: args.max_new]:
        refnr = job["referenznummer"]
        try:
            full = arbeitsagentur.details(refnr)
        except Exception as e:                                              # noqa: BLE001
            print(f"  {refnr}: detail call failed ({type(e).__name__}), skipped for today")
            continue
        locations = locations_from_api(full if full.get("stellenlokationen") else job)
        company = job.get("firma") or full.get("firma") or ""
        city = locations[0]["city"] if locations else None
        postings.append({"source": ARBEITSAGENTUR, "source_id": refnr, "refnr": refnr,
                         "title": job.get("stellenangebotsTitel") or full.get("stellenangebotsTitel", ""),
                         "company": company, "firma": company, "city": city, "ort": city, "locations": locations,
                         "description": full.get("stellenangebotsBeschreibung", "") or "",
                         "published_on": published_on(job), "url": telegram.POSTING_URL.format(refnr=refnr),
                         "location_gap": None, "location_note": None})
    return postings


def arbeitnow_postings(conn, args) -> list[dict]:
    """Steps 1-3 for Arbeitnow: its newest jobs in Germany (it has no search), duplicates and what the database
    has skipped, title filter, then coordinates for the city names (geocode.py). The full text comes with the
    list, so there is no detail call."""
    found = arbeitnow.fetch(args.days)
    keys, postings = set(), []
    for job in found:
        posting = arbeitnow.to_posting(job)
        key = (posting["company"], posting["title"], posting["city"] or "")
        if key in keys:
            continue
        keys.add(key)
        if db.seen_before(conn, arbeitnow.SOURCE, posting["source_id"], *key):
            continue
        if title_status(posting["title"]) == "drop":
            continue
        postings.append(posting)
    postings = postings[: args.max_new]
    for posting in postings:
        add_coordinates(conn, posting["locations"])
    print(f"Arbeitnow: {len(found)} postings in Germany, {len(postings)} new that pass the title filter")
    return postings


def company_feed_postings(conn, args) -> list[dict]:
    """Steps 1-3 for the company feeds (sources/company_feeds.py): every open job, skip what the database has,
    title filter, then the description for SmartRecruiters (its list has none, so only survivors cost a call)
    and coordinates where the feed gives only a place name. No --days filter: a feed lists only open jobs."""
    found = company_feeds.fetch_all()
    postings = []
    for source, company, raw in found:
        posting = source.to_posting(raw, company)
        if db.seen_before(conn, posting["source"], posting["source_id"],
                          posting["company"], posting["title"], posting["city"] or ""):
            continue
        if title_status(posting["title"]) == "drop":
            continue
        if source is smartrecruiters:
            try:
                posting["description"], better_url = smartrecruiters.fetch_description(company, raw["id"])
                posting["url"] = better_url or posting["url"]
            except Exception as e:                                          # noqa: BLE001
                print(f"  {posting['source_id']}: description call failed ({type(e).__name__}), skipped for today")
                continue
        postings.append(posting)
    postings = postings[: args.max_new]
    for posting in postings:
        add_coordinates(conn, posting["locations"])
    print(f"Company feeds: {len(found)} open jobs, {len(postings)} new that pass the title filter")
    return postings


def run(args) -> None:
    background = (ROOT / "data" / "background.txt").read_text(encoding="utf-8")
    conn = db.connect()

    # 1-3: every source's new postings that pass the title filter, in one shape
    candidates = (arbeitsagentur_postings(conn, args) + arbeitnow_postings(conn, args)
                  + company_feed_postings(conn, args))

    # 4: Decisions API screen, location rule, save
    to_assess = []
    for i, posting in enumerate(candidates, 1):
        title, text, locations = posting["title"], posting["description"], posting["locations"]
        if text:
            try:
                decisions = ask_decisions_filter(title, text)
                decisions.pop("input_tokens", None)
                posting["decisions"] = decisions
                posting["office_presence"] = decisions["office_presence"]
                posting["location_gap"] = location_gap(decisions["office_presence"], locations, decisions["presence_confidence"])
                posting["location_note"] = location_note(decisions["office_presence"], locations)
            except Exception as e:                                          # noqa: BLE001
                posting["decisions_call_error"] = f"{type(e).__name__}: {e}"[:300]
        posting["id"] = db.save_posting(conn, posting)
        goes_to_haiku = posting.get("decisions", {}).get("is_it_ai_eng") or "decisions_call_error" in posting
        print(f"[{i}] {posting['source']:14} {title[:56]:56} "
              f"{'-> Haiku' if goes_to_haiku and text else 'not an AI engineering role' if text else 'no text'}")
        if goes_to_haiku and text:
            to_assess.append(posting)

    # 5: Haiku, several at a time; the database writes stay in this thread.
    # Saved postings still without a verdict (a failed call, or a run stopped by --max) come along again.
    waiting = [p for p in db.still_to_assess(conn) if p["id"] not in {q["id"] for q in to_assess}]
    if waiting:
        print(f"{len(waiting)} postings from earlier runs still need a verdict, trying them again")
    to_assess = (to_assess + waiting)[: args.max]
    print(f"Assessing {len(to_assess)} postings with {ASSESSMENT_MODEL['model']} ({ASSESSMENT_MODEL['effort']})")
    with ThreadPoolExecutor(max_workers=ASSESSMENT_WORKERS) as pool:
        outcomes = list(pool.map(lambda p: assess(p, background), to_assess))
    items, spent = [], 0.0
    for posting, out in zip(to_assess, outcomes):
        usage = out["usage"] or {}
        cost = cost_usd(ASSESSMENT_MODEL["model"], usage) if usage else None
        spent += cost or 0
        posting["verdict_id"] = db.save_verdict(
            conn, posting["id"], ASSESSMENT_MODEL["model"], ASSESSMENT_MODEL["effort"], PROMPT_VERSION,
            out["result"], out["error"], usage.get("input"), usage.get("output"), cost, round(out["seconds"], 2))
        verdict = out["result"]["code_verdict"] if out["result"] else "ERROR " + out["error"][:60]
        print(f"  {posting['title'][:60]:60} {verdict}")
        if out["result"]:
            items.append((posting, out["result"], posting["published_on"]))

    # 6: summaries and Telegram
    if args.no_telegram:
        print(f"Done without Telegram. Haiku cost ${spent:.3f}")
        return
    summary_usage = {}      # posting text -> (usage, seconds) of its successful summary call

    def summarize_and_record(title, text, rows):
        start = time.perf_counter()
        summary, usage = summarize(title, text, rows)
        summary_usage[text] = (usage, time.perf_counter() - start)
        return summary

    def on_sent(posting, result, summary):
        nonlocal spent
        usage, seconds = summary_usage.get(posting["description"], (None, None))
        if summary is not None and usage:
            db.save_summary(conn, posting["verdict_id"], SUMMARY_MODEL, SUMMARY_EFFORT, SUMMARY_PROMPT_VERSION,
                            summary.model_dump(), None, usage["input"], usage["output"],
                            cost_usd(SUMMARY_MODEL, usage), round(seconds, 2))
            spent += cost_usd(SUMMARY_MODEL, usage)
        else:
            db.save_summary(conn, posting["verdict_id"], SUMMARY_MODEL, SUMMARY_EFFORT, SUMMARY_PROMPT_VERSION,
                            None, "summary failed twice (see the run log)", None, None, None, None)
        db.mark_sent(conn, posting["verdict_id"])

    errors = sum(1 for o in outcomes if o["error"])
    note = f"{errors} Haiku calls failed, see the run log" if errors else ""
    sent = telegram.send_alerts(items, note=note, summarize=summarize_and_record, on_sent=on_sent)
    print(f"Done. {sent} postings sent to Telegram. Spent ${spent:.3f} on Haiku")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="only postings first published in the last N days (1 = today and yesterday)")
    parser.add_argument("--max", type=int, default=None, help="at most this many Haiku assessments")
    parser.add_argument("--max-new", type=int, default=None, help="at most this many new postings per source")
    parser.add_argument("--no-telegram", action="store_true", help="skip the summaries and Telegram messages")
    args = parser.parse_args()
    try:
        run(args)
    except Exception as e:                                                  # noqa: BLE001
        if not args.no_telegram:
            try:
                telegram.send_message(f"⚠️ <b>Job Scout run failed</b>\n{html.escape(type(e).__name__)}: "
                                      f"{html.escape(str(e)[:300])}")
            except Exception:                                               # noqa: BLE001
                pass
        raise       # the full error goes on: a traceback in the terminal, a clear failed run in Lambda


if __name__ == "__main__":
    main()
    