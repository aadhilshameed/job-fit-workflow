"""The database (Postgres on Neon): every read and write the daily run makes, in one place.

The tables are in db/schema.sql. The daily run fills them step by step:
1. seen_before()     before anything else: skip a posting we already have
   still_to_assess()  AI engineering roles saved earlier without a verdict yet (the Haiku call failed, or --max
                      stopped the run): they are tried again
2. save_posting()    after the Decisions API screen: one row in screened_postings
3. save_verdict()    after Haiku and code_verdict.py: one row in model_verdicts, plus its rows in verdict_requirements
4. save_summary()    after the summary call: fills in the summary columns of that verdict
5. mark_sent()       after Telegram accepted the message: fills in sent_to_telegram_at

Needs DATABASE_URL in .env (Neon's connection string, with connection pooling on).

Every query passes its values separately (%s), never glued into the SQL text. So a title such as
"Werkstudent (m/w/d) KI'); DROP TABLE ..." is stored as plain text and can never run as SQL.

Try it without leaving anything behind:  python -m job_fit.db
(it writes a fake posting, verdict and requirement, reads them back, then undoes everything)
"""

import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPOST_WINDOW_DAYS = 60   # same company + title + city within this many days counts as the same job
MAX_ATTEMPTS = 3          # a posting whose Haiku call failed is tried again on later runs, this many times in all
RETRY_DAYS = 7            # ...but only while it is at most this many days old


def connect() -> psycopg.Connection:
    """One connection for the whole run. autocommit: every statement is saved the moment it runs,
    so a crash halfway loses nothing that was already written."""
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def still_open(conn) -> psycopg.Connection:
    """The same connection if it still answers, otherwise a new one. Neon's free plan puts the database to
    sleep after 5 minutes without queries and closes open connections (22 Nov 2026: a run crashed after
    10 minutes of Haiku calls), so call this before writing after a long wait."""
    try:
        conn.execute("SELECT 1")
        return conn
    except psycopg.OperationalError:
        print("Database connection was closed (Neon sleeps after 5 idle minutes): reconnected")
        return connect()


def seen_before(conn, source: str, source_id: str, company: str | None, title: str | None,
                city: str | None) -> bool:
    """True if we already have this posting: the same id (or one of its duplicate ids) on the same site,
    or the same company, title and city found within the last REPOST_WINDOW_DAYS days."""
    row = conn.execute(
        """
        SELECT 1 FROM screened_postings
        WHERE (source = %(source)s AND (source_id = %(id)s OR %(id)s = ANY(duplicate_ids)))
           OR (lower(company) = lower(%(company)s)
               AND lower(title) = lower(%(title)s)
               AND lower(coalesce(city, '')) = lower(coalesce(%(city)s, ''))
               AND found_on > now() - make_interval(days => %(days)s))
        LIMIT 1
        """,
        {"source": source, "id": source_id, "company": company or "", "title": title or "",
         "city": city, "days": REPOST_WINDOW_DAYS},
    ).fetchone()
    return row is not None


def recent_jobs(conn) -> list[tuple]:
    """(company, title, city) of every posting found in the last REPOST_WINDOW_DAYS days, for the duplicate
    check across sources and spellings (code_nodes/duplicates.py)."""
    return conn.execute(
        "SELECT company, title, coalesce(city, '') FROM screened_postings "
        "WHERE found_on > now() - make_interval(days => %s)", (REPOST_WINDOW_DAYS,)).fetchall()


def still_to_assess(conn) -> list[dict]:
    """Saved postings that should get a verdict but have none yet: an AI engineering role (or the Decisions API call failed),
    with text, found in the last RETRY_DAYS days, with no successful verdict and fewer than MAX_ATTEMPTS tries.
    Returned in the shape the daily run assesses."""
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT p.*
            FROM screened_postings p
            WHERE (p.is_it_ai_eng OR p.decisions_call_error IS NOT NULL)
              AND p.description <> ''
              AND p.assessment_skipped IS NULL
              AND p.found_on > now() - make_interval(days => %(days)s)
              AND NOT EXISTS (SELECT 1 FROM model_verdicts v
                              WHERE v.screened_posting_id = p.id AND v.model_extraction_error IS NULL)
              AND (SELECT count(*) FROM model_verdicts v WHERE v.screened_posting_id = p.id) < %(max)s
            ORDER BY p.id
            """,
            {"days": RETRY_DAYS, "max": MAX_ATTEMPTS},
        ).fetchall()
    return [{"id": r["id"], "source_id": r["source_id"], "refnr": r["source_id"], "title": r["title"],
             "company": r["company"], "firma": r["company"], "city": r["city"], "ort": r["city"],
             "locations": r["locations"] or [], "description": r["description"],
             "published_on": r["published_on"].date() if r["published_on"] else None,
             "office_presence": r["office_presence"],
             "location_gap": ({"kind": r["location_gap_type"], "reason": r["location_gap_reason"]}
                              if r["location_gap_type"] else None),
             "location_note": r["location_note"], "url": r.get("url")} for r in rows]


def skip_assessment(conn, screened_posting_id: int, reason: str) -> None:
    """Never send this posting to Haiku: still_to_assess() leaves it out from now on. The row itself stays."""
    conn.execute("UPDATE screened_postings SET assessment_skipped = %s WHERE id = %s", (reason, screened_posting_id))


def save_posting(conn, posting: dict) -> int:
    """One row in screened_postings; returns its id.

    posting: source, source_id, title, company, city, locations, description, and optionally duplicate_ids,
    published_on, decisions (the dict ask_decisions_filter() returns), decisions_call_error, location_gap
    ({"kind", "reason"} or None), location_note and url (the posting's page, for the Telegram button).
    """
    decisions = posting.get("decisions") or {}
    gap = posting.get("location_gap") or {}
    row = conn.execute(
        """
        INSERT INTO screened_postings
            (source, source_id, duplicate_ids, title, company, city, locations, description, published_on,
             decisions_call_error, ai_eng_probability, is_it_ai_eng, ai_work_type, work_type_confidence,
             office_presence, presence_confidence, location_note, location_gap_type, location_gap_reason, url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (posting["source"], posting["source_id"], posting.get("duplicate_ids") or [],
         posting["title"], posting["company"], posting.get("city"), Jsonb(posting.get("locations") or []),
         posting.get("description") or "", posting.get("published_on"),
         posting.get("decisions_call_error"), decisions.get("ai_eng_probability"), decisions.get("is_it_ai_eng"),
         decisions.get("ai_work_type"), decisions.get("work_type_confidence"), decisions.get("office_presence"),
         decisions.get("presence_confidence"), posting.get("location_note"), gap.get("kind"), gap.get("reason"),
         posting.get("url")),
    ).fetchone()
    return row[0]


def save_verdict(conn, screened_posting_id: int, model: str, effort: str, prompt_version: str,
                 result: dict | None, error: str | None, input_tokens: int | None, output_tokens: int | None,
                 cost_usd: float | None, time_seconds: float | None) -> int:
    """One row in model_verdicts and one row per requirement in verdict_requirements; returns the verdict id.

    result: what verdict.compute_verdict() returns, or None when the Haiku call failed (then error says why,
    and no requirement rows are written).
    """
    result = result or {}
    with conn.transaction():          # the verdict and its requirements are saved together, or not at all
        verdict_id = conn.execute(
            """
            INSERT INTO model_verdicts
                (screened_posting_id, model, effort, prompt_version, model_extraction_error,
                 model_verdict, verdict_reason, code_verdict, input_tokens, output_tokens, cost_usd, time_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (screened_posting_id, model, effort, prompt_version, error,
             result.get("model_verdict"), result.get("model_verdict_reason"), result.get("code_verdict"),
             input_tokens, output_tokens, cost_usd, time_seconds),
        ).fetchone()[0]
        for position, r in enumerate(result.get("requirements") or [], 1):
            quote = r.get("background_quote")
            conn.execute(
                """
                INSERT INTO verdict_requirements
                    (model_verdict_id, position_in_req_list, requirement_text, found_in, requirement_type,
                     critical_or_standard, background_quote, quote_status, match_reason, match_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (verdict_id, position, r["requirement_text"], r["found_in"], r["requirement_type"],
                 r["importance"], None if (quote or "").strip().lower() in ("", "none") else quote,
                 r["quote_status"], r["match_reason"], r["match_result"]),
            )
    return verdict_id


def save_summary(conn, verdict_id: int, model: str, effort: str, prompt_version: str,
                 summary: dict | None, error: str | None, input_tokens: int | None, output_tokens: int | None,
                 cost_usd: float | None, time_seconds: float | None) -> None:
    """Fills in the summary columns of one verdict. summary: the PostingSummary as a dict, or None on failure."""
    conn.execute(
        """
        UPDATE model_verdicts
        SET summary_model = %s, summary_effort = %s, summary_prompt_version = %s, summary_error = %s,
            summary = %s, summary_input_tokens = %s, summary_output_tokens = %s,
            summary_cost_usd = %s, summary_time_seconds = %s
        WHERE id = %s
        """,
        (model, effort, prompt_version, error, Jsonb(summary) if summary is not None else None,
         input_tokens, output_tokens, cost_usd, time_seconds, verdict_id),
    )


def mark_sent(conn, verdict_id: int) -> None:
    """Telegram accepted this verdict's message: note the moment."""
    conn.execute("UPDATE model_verdicts SET sent_to_telegram_at = now() WHERE id = %s", (verdict_id,))


def _self_test():
    """Writes one fake posting, verdict and requirement, reads them back, then rolls everything back."""
    from dotenv import load_dotenv
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:     # no autocommit: one transaction for the test
        posting = {"source": "selftest", "source_id": "TEST-1", "title": "Werkstudent KI (m/w/d)",
                   "company": "Test GmbH", "city": "Hof", "locations": [{"city": "Hof", "lat": 50.3, "lon": 11.9}],
                   "description": "Wir suchen dich.", "duplicate_ids": ["TEST-1b"],
                   "decisions": {"ai_eng_probability": 0.83, "is_it_ai_eng": True, "ai_work_type": "agentic_ai",
                                 "work_type_confidence": 0.7, "office_presence": "occasional",
                                 "presence_confidence": 0.9}}
        pid = save_posting(conn, posting)
        result = {"code_verdict": "apply", "model_verdict": "apply", "model_verdict_reason": "All critical skills match.",
                  "requirements": [{"requirement_text": "Python", "found_in": "requirements_section",
                                    "requirement_type": "technical_skill", "importance": "critical",
                                    "background_quote": "none", "quote_status": "no_quote",
                                    "match_reason": "Used in several projects.", "match_result": "match"}]}
        vid = save_verdict(conn, pid, "claude-haiku-5-5", "high", "v4", result, None, 3000, 9000, 0.0048, 41.2)
        save_summary(conn, vid, "claude-haiku-5-5", "high", "s2", {"role": "AI agents → testing"}, None,
                     2500, 400, 0.0005, 6.1)
        mark_sent(conn, vid)
        seen = [seen_before(conn, "selftest", "TEST-1", None, None, None),
                seen_before(conn, "selftest", "TEST-1b", None, None, None),
                seen_before(conn, "other_site", "X", "test gmbh", "werkstudent ki (m/w/d)", "HOF"),
                seen_before(conn, "selftest", "TEST-2", "Other AG", "Werkstudent KI (m/w/d)", "Hof")]
        row = conn.execute(
            """
            SELECT p.title, v.code_verdict, v.summary->>'role', v.sent_to_telegram_at IS NOT NULL,
                   r.requirement_text, r.background_quote IS NULL
            FROM screened_postings p
            JOIN model_verdicts v ON v.screened_posting_id = p.id
            JOIN verdict_requirements r ON r.model_verdict_id = v.id
            WHERE p.id = %s
            """, (pid,)).fetchone()
        conn.rollback()                                               # undo everything: nothing stays in the database
    print("seen before (same id, duplicate id, same job on another site, different company):", seen)
    print("read back:", row)
    ok = seen == [True, True, True, False] and row[1] == "apply" and row[3] and row[5]
    print("Database OK, nothing was kept." if ok else "Something is off, see above.")


if __name__ == "__main__":
    _self_test()
