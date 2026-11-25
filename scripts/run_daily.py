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
6. summary + Telegram for apply, stretch and lack_of_info -> summary columns and sent_to_telegram_at.
   Verdicts from earlier runs that never reached Telegram (a crash, a network blip) are sent too
   (db.unsent_verdicts). A message that cannot be sent does not stop the run; it is sent on the next one.
   The 08:00 run starts with a morning message, the 20:00 run ends with the day's summary (bot/telegram.py)
Every run is one row in pipeline_run_logs (its counts, done or failed). If the run crashes, a Telegram message says
so in plain words (unless --no-telegram).
"""

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT)) #adding the ROOT to the sys.path, so python would find it
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
                             PostingSummary, summarize)
from job_fit.llm.schema import Assessment, Requirement                       # noqa: E402
from job_fit.code_nodes.duplicates import KnownJobs                         # noqa: E402
from job_fit.code_nodes.title_filter import is_student_role, title_status                            # noqa: E402
from job_fit.code_nodes.code_verdict import compute_verdict                              # noqa: E402

# the fixed setup, chosen on 20 Nov 2026 (Haiku high beat medium on the 35-posting check)
ASSESSMENT_MODEL = {"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "high"}
PRICE_PER_MILLION = {"claude-haiku-5-5": (0.10, 0.50)}       # dollars per 1M tokens: (input, output)
ASSESSMENT_WORKERS = 4      # Haiku calls at the same time, so a big day still fits in Lambda's 15 minutes
ARBEITSAGENTUR = "arbeitsagentur"
BERLIN = ZoneInfo("Europe/Berlin")      # Lambda runs on UTC; the morning and evening messages go by Berlin time
MORNING_BEFORE_HOUR = 11    # a run starting before 11:00 am sends the morning message (the 08:00 run)
EVENING_FROM_HOUR = 18      # a run starting at 6:00 pm or later sends the day's summary (the 20:00 run)


def new_stats() -> dict:
    """The counts one run collects, saved in pipeline_run_logs.stats and shown in the morning and evening messages."""
    return {"found": {}, "new": {}, "duplicates": 0, "ai_roles": 0, "decisions_errors": 0, "assessed": 0,
            "verdicts": {"apply": 0, "stretch": 0, "lack_of_info": 0, "skip": 0},
            "haiku_errors": {"cut_off": 0, "other": 0}, "sent": 0, "resent": 0, "send_failed": 0,
            "failed_feeds": [], "cost_usd": 0.0}


def add_stats(total: dict, part: dict) -> dict:
    """Adds one run's counts into a day's total: numbers are summed, dicts per key, lists joined."""
    for key, value in part.items():
        if isinstance(value, dict):
            add_stats(total.setdefault(key, {}), value)
        elif isinstance(value, list):
            total[key] = total.get(key, []) + value
        elif isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
    return total


def cost_usd(model: str, usage: dict) -> float:
    price_in, price_out = PRICE_PER_MILLION[model]
    return (usage["input"] * price_in + usage["output"] * price_out) / 1_000_000


def published_on(each_listing: dict) -> date | None:
    """The day the posting was first published ("datumErsteVeroeffentlichung": "2026-11-20" in the search
    result), or None if missing or unreadable."""
    text = each_listing.get("datumErsteVeroeffentlichung") or (each_listing.get("veroeffentlichungszeitraum") or {}).get("von")
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
                                      #provider is for setting the client connection
        result = compute_verdict(assessment, background, posting["location_gap"])
        #location gap - location is too far given: being present daily, weekly, monthly or remote
        result["location_note"] = posting["location_note"]
        return {"result": result, "error": None, "usage": usage, "seconds": time.perf_counter() - start}
    except Exception as e:                                                  # noqa: BLE001
        return {"result": None, "error": f"{type(e).__name__}: {e}"[:500], "usage": None,
                "seconds": time.perf_counter() - start}


def arbeitsagentur_postings(conn, args, known: KnownJobs, stats: dict) -> list[dict]:
    """Steps 1-3 for the Arbeitsagentur: search, drop duplicates and old postings, skip what the database has,
    title filter, then the detail call for the full text. Returns postings ready for the Decisions API."""

    #conn -> open connection to db
    #args -> options
    #known -> memory of jobs we already have, used for the duplicate check.

    found = arbeitsagentur.search(days_back=args.days)
    #we search on arbeitsagentur for already decided terms 
    new, too_old, duplicates = [], 0, 0
    for each_listing in found:
        city = arbeitsagentur.location(each_listing)[0]
        #location returns [city, state]
        key = (each_listing.get("firma") or "", each_listing.get("stellenangebotsTitel") or "", city)
        #key is a tuple of (company, title, city)
        if db.seen_before(conn, ARBEITSAGENTUR, each_listing["referenznummer"], *key):
            continue
            #returns True if Neon has same reference nmuber

        if known.has(*key):
            duplicates += 1                 # the same job under another id, from this search or another source
            continue
        known.add(*key)
        if title_status(each_listing.get("stellenangebotsTitel"), each_listing.get("hauptberuf")) == "drop":
            continue
        published = published_on(each_listing)
        if published and (date.today() - published).days > args.days:
            too_old += 1           # the search's own date filter (veroeffentlichtseit) is ignored, so we check here
            continue
        new.append(each_listing)
    stats["found"][ARBEITSAGENTUR], stats["duplicates"] = len(found), stats["duplicates"] + duplicates
    print(f"Arbeitsagentur: {len(found)} postings, {len(new)} new that pass the title filter"
          + (f" ({too_old} older than {args.days} days skipped)" if too_old else "")
          + (f" ({duplicates} duplicates of known jobs skipped)" if duplicates else ""))

    postings = []
    for each_listing in new[: args.max_new]:
        refnr = each_listing["referenznummer"]
        try:
            full = arbeitsagentur.details(refnr)
            #The full posting, including the description text
        except Exception as e:                                              # noqa: BLE001
            print(f"  {refnr}: detail call failed ({type(e).__name__}), skipped for today")
            continue
        locations = locations_from_api(full if full.get("stellenlokationen") else each_listing)
        #gets the location from the full posting, if it is there, else from the search result
        company = each_listing.get("firma") or full.get("firma") or ""
        city = locations[0]["city"] if locations else None
        postings.append({"source": ARBEITSAGENTUR, "source_id": refnr, "refnr": refnr,
                         "title": each_listing.get("stellenangebotsTitel") or full.get("stellenangebotsTitel", ""),
                         "company": company, "firma": company, "city": city, "ort": city, "locations": locations,
                         "description": full.get("stellenangebotsBeschreibung", "") or "",
                         "published_on": published_on(each_listing), "url": telegram.POSTING_URL.format(refnr=refnr),
                         "location_gap": None, "location_note": None})
    stats["new"][ARBEITSAGENTUR] = len(postings)
    return postings


def arbeitnow_postings(conn, args, known: KnownJobs, stats: dict) -> list[dict]:
    """Steps 1-3 for Arbeitnow: its newest jobs in Germany (it has no search), duplicates and what the database
    has skipped, title filter, then coordinates for the city names (geocode.py). The full text comes with the
    list, so there is no detail call."""
    found = arbeitnow.fetch(args.days)
    postings, duplicates = [], 0
    for each_listing in found:
        posting = arbeitnow.to_posting(each_listing)
        key = (posting["company"], posting["title"], posting["city"] or "")
        if db.seen_before(conn, arbeitnow.SOURCE, posting["source_id"], *key):
            continue
        if known.has(*key):
            duplicates += 1
            continue
        known.add(*key)
        if title_status(posting["title"]) == "drop":
            continue
        postings.append(posting)
    postings = postings[: args.max_new]
    for posting in postings:
        add_coordinates(conn, posting["locations"])
    stats["found"][arbeitnow.SOURCE], stats["new"][arbeitnow.SOURCE] = len(found), len(postings)
    stats["duplicates"] += duplicates
    print(f"Arbeitnow: {len(found)} postings in Germany, {len(postings)} new that pass the title filter"
          + (f" ({duplicates} duplicates of known jobs skipped)" if duplicates else ""))
    return postings


def company_feed_postings(conn, args, known: KnownJobs, stats: dict) -> list[dict]:
    """Steps 1-3 for the company feeds (sources/company_feeds.py): every open job, skip what the database has,
    title filter, then the description for SmartRecruiters (its list has none, so only survivors cost a call)
    and coordinates where the feed gives only a place name. No --days filter: a feed lists only open jobs."""
    found, stats["failed_feeds"] = company_feeds.fetch_all()
    # one group per job: Bosch posts most jobs twice (German and English) under one reference number, and both
    # versions are one job. A group is skipped if ANY version is already known (by id or by duplicates.py).
    groups, by_reference = [], {}
    for source, company, raw in found:
        posting = source.to_posting(raw, company)
        reference = raw.get("refNumber") if source is smartrecruiters else None
        if reference and (company, reference) in by_reference:
            by_reference[(company, reference)][3].append(posting)
            continue
        group = (source, company, raw, [posting])
        groups.append(group)
        if reference:
            by_reference[(company, reference)] = group
    postings, duplicates = [], 0
    for source, company, raw, versions in groups:
        keys = [(p["company"], p["title"], p["city"] or "") for p in versions]
        if any(db.seen_before(conn, p["source"], p["source_id"], *k) for p, k in zip(versions, keys)):
            continue
        is_duplicate = any(known.has(*k) for k in keys)
        for k in keys:
            known.add(*k)
        if is_duplicate:
            duplicates += 1
            continue
        posting = versions[0]
        posting["duplicate_ids"] = [p["source_id"] for p in versions[1:]]
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
    stats["found"]["company_feeds"], stats["new"]["company_feeds"] = len(found), len(postings)
    stats["duplicates"] += duplicates
    print(f"Company feeds: {len(found)} open jobs, {len(postings)} new that pass the title filter"
          + (f" ({duplicates} duplicates of known jobs skipped)" if duplicates else ""))
    return postings


def rebuild_result(row: dict, background: str) -> dict:
    """The verdict of a posting that was assessed earlier but never sent, rebuilt from its saved requirement rows,
    so posting_message can show it as it would have been shown. The saved verdict stays the verdict."""
    assessment = Assessment(role_focus=[], requirements=[Requirement(**r) for r in row["requirements"]],
                            model_verdict_reason=row["verdict_reason"],
                            model_verdict=row["model_verdict"] or row["code_verdict"])
    result = compute_verdict(assessment, background, row["location_gap"])
    result["code_verdict"] = row["code_verdict"]
    result["location_note"] = row["location_note"]
    return result


def run(args) -> None:
    """Opens the database and the pipeline_run_logs row, runs the steps, and marks the row done or failed."""
    background = (ROOT / "data" / "background.txt").read_text(encoding="utf-8")
    conn = db.connect()
    run_id, stats = db.start_run(conn, args.days), new_stats()
    try:
        conn = run_steps(args, conn, background, run_id, stats)
    except Exception as e:
        conn = db.still_open(conn)
        db.save_run(conn, run_id, stats, "failed", f"{type(e).__name__}: {telegram.scrub(e)}"[:500])
        raise
    db.save_run(conn, run_id, stats, "done")


def run_steps(args, conn, background: str, run_id: int | None, stats: dict):
    """Steps 1 to 6. Returns the connection (it may have been reopened while waiting for Haiku)."""
    # 1-3: every source's new postings that pass the title filter, in one shape
    known = KnownJobs(db.recent_jobs(conn))     # duplicates across sources and spellings (code_nodes/duplicates.py)
    candidates = (arbeitsagentur_postings(conn, args, known, stats) + arbeitnow_postings(conn, args, known, stats)
                  + company_feed_postings(conn, args, known, stats))

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
                stats["decisions_errors"] += 1
        posting["id"] = db.save_posting(conn, posting)
        goes_to_haiku = posting.get("decisions", {}).get("is_it_ai_eng") or "decisions_call_error" in posting
        print(f"[{i}] {posting['source']:14} {title[:56]:56} "
              f"{'-> Haiku' if goes_to_haiku and text else 'not an AI engineering role' if text else 'no text'}")
        if goes_to_haiku and text:
            to_assess.append(posting)
    stats["ai_roles"] = len(to_assess)

    # 5: Haiku, ASSESSMENT_WORKERS at a time; each verdict is saved the moment its call ends (the writes stay
    # in this thread), so a crash keeps every verdict already paid for, and the database never sits idle long.
    # Saved postings still without a verdict (a failed call, or a run stopped by --max) come along again.
    waiting = [p for p in db.still_to_assess(conn) if p["id"] not in {q["id"] for q in to_assess}]
    if waiting:
        print(f"{len(waiting)} postings from earlier runs still need a verdict, trying them again")
    to_assess = to_assess + waiting
    if args.students_only:
        others = [p for p in to_assess if not is_student_role(p["title"])]
        for p in others:
            db.skip_assessment(conn, p["id"], f"not a student role, skipped by a --students-only run on {date.today()}")
        to_assess = [p for p in to_assess if is_student_role(p["title"])]
        print(f"--students-only: {len(others)} other roles marked as never to assess, {len(to_assess)} student roles left")
    to_assess = to_assess[: args.max]
    print(f"Assessing {len(to_assess)} postings with {ASSESSMENT_MODEL['model']} ({ASSESSMENT_MODEL['effort']})")
    items, spent = [], 0.0
    with ThreadPoolExecutor(max_workers=ASSESSMENT_WORKERS) as pool:
        futures = {pool.submit(assess, p, background): p for p in to_assess}
        for future in as_completed(futures):
            posting, out = futures[future], future.result()
            conn = db.still_open(conn)
            usage = out["usage"] or {}
            cost = cost_usd(ASSESSMENT_MODEL["model"], usage) if usage else None
            spent += cost or 0
            posting["verdict_id"] = db.save_verdict(
                conn, posting["id"], ASSESSMENT_MODEL["model"], ASSESSMENT_MODEL["effort"], PROMPT_VERSION,
                out["result"], out["error"], usage.get("input"), usage.get("output"), cost, round(out["seconds"], 2))
            verdict = out["result"]["code_verdict"] if out["result"] else "ERROR " + out["error"][:60]
            print(f"  {posting['title'][:60]:60} {verdict}")
            stats["assessed"] += 1
            if out["result"]:
                items.append((posting, out["result"], posting["published_on"]))
                stats["verdicts"][out["result"]["code_verdict"]] += 1
            else:
                kind = "cut_off" if out["error"].startswith("ValueError: Answer cut off") else "other"
                stats["haiku_errors"][kind] += 1
    stats["cost_usd"] = round(spent, 4)
    db.save_run(conn, run_id, stats)

    # 6: summaries and Telegram
    if args.no_telegram:
        print(f"Done without Telegram. Haiku cost ${spent:.3f}")
        return conn
    summary_usage = {}      # posting text -> (usage, seconds) of its successful summary call

    def summarize_and_record(title, text, rows):
        start = time.perf_counter()
        summary, usage = summarize(title, text, rows)
        summary_usage[text] = (usage, time.perf_counter() - start)
        return summary

    def on_sent(posting, result, summary):
        nonlocal spent, conn
        conn = db.still_open(conn)              # summaries can take a while: the database may have gone to sleep
        stats["sent"] += 1
        if posting.get("resend"):
            stats["resent"] += 1
        if posting.get("summary") is not None:             # sent again with the summary saved the first time
            db.mark_sent(conn, posting["verdict_id"])
            return
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

    def on_failed(posting, error):
        stats["send_failed"] += 1       # its sent_to_telegram_at stays empty, so the next run sends it again

    # verdicts from earlier runs that never reached Telegram come along
    conn = db.still_open(conn)
    resend = []
    for row in db.unsent_verdicts(conn, leave_out={p["verdict_id"] for p, _, _ in items}):
        try:
            row["resend"] = True
            row["summary"] = PostingSummary.model_validate(row["stored_summary"]) if row["stored_summary"] else None
            resend.append((row, rebuild_result(row, background), row["published_on"]))
        except Exception as e:                                              # noqa: BLE001
            print(f"  could not rebuild verdict {row['verdict_id']} to send it again: {type(e).__name__}: {e}"[:300])
    if resend:
        print(f"{len(resend)} postings from earlier runs never reached Telegram, sending them again")

    now = datetime.now(BERLIN)
    if now.hour < MORNING_BEFORE_HOUR:
        waiting_now = len(db.still_to_assess(conn))
        send_quietly(telegram.morning_message(now, stats, len(resend), waiting_now))
    sent = telegram.send_alerts(items + resend, summarize=summarize_and_record, on_sent=on_sent, on_failed=on_failed)
    stats["cost_usd"] = round(spent, 4)
    if now.hour >= EVENING_FROM_HOUR:
        conn = db.still_open(conn)
        db.save_run(conn, run_id, stats)
        runs = db.runs_on(conn, now.date())
        total = new_stats()
        for r in runs:
            add_stats(total, r["stats"])
        if not runs:                    # pipeline_run_logs missing or unreadable: show at least this run
            runs, total = [{"hour": now.hour, "status": "running"}], add_stats(new_stats(), stats)
        send_quietly(telegram.evening_message(now, runs, total))
    print(f"Done. {sent} postings sent to Telegram ({stats['resent']} of them again from earlier runs, "
          f"{stats['send_failed']} could not be sent). Spent ${spent:.3f} on Haiku")
    return conn


def send_quietly(text: str) -> None:
    """A morning or evening message. If it cannot be sent, the run still goes on (the postings matter more)."""
    try:
        telegram.send_message(text)
    except Exception as e:                                                  # noqa: BLE001
        print(f"Could not send the morning/evening message: {type(e).__name__}: {telegram.scrub(e)}"[:300])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1,
                        help="only postings first published in the last N days (1 = today and yesterday)")
    parser.add_argument("--max", type=int, default=None, help="at most this many Haiku assessments")
    parser.add_argument("--max-new", type=int, default=None, help="at most this many new postings per source")
    parser.add_argument("--no-telegram", action="store_true", help="skip the summaries and Telegram messages")
    parser.add_argument("--students-only", action="store_true",
                        help="assess only student roles (Werkstudent, Praktikum, internship); every other role found "
                             "or waiting is marked as never to assess. For catch-up runs over old postings")
    args = parser.parse_args()
    try:
        run(args)
    except Exception as e:                                                  # noqa: BLE001
        if not args.no_telegram:
            try:
                telegram.send_message(telegram.crash_alert(e, datetime.now(BERLIN)))
            except Exception:                                               # noqa: BLE001
                pass
        raise       # the full error goes on: a traceback in the terminal, a clear failed run in Lambda


if __name__ == "__main__":
    main()
    