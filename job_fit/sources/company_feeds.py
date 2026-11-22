"""The company watch list: companies whose own career feeds are read on every run, whatever they post.

A company is on the list because it hires for AI (agents, LLMs, generative AI), not because of one posting:
a company feed only lists jobs that are open right now, so the list stays useful when single postings close.
Found on 22 Nov 2026 by searching for German companies with AI postings on each system; add or remove
companies here. The name is the one in the feed's address (appliedai.jobs.personio.de -> "appliedai").

Every job a feed lists is open, however old its date (a live Lever job can date from 2022), so company feeds
skip the --days filter: "new" means "not in the database yet" (db.seen_before).
"""

from job_fit.sources import lever, personio, smartrecruiters

WATCH_LIST = {
    personio: ["appliedai", "deepset", "statworx", "justaddai", "fact-finder", "acto", "deeploi", "nfon",
               "agile-robots-se", "cinemo", "feld-m"],
    lever: ["xentral"],
    smartrecruiters: ["BoschGroup", "Continental", "RolandBerger"],
}


def fetch_all() -> list[tuple]:
    """(source module, company, raw job) for every job on every watched feed. A feed that fails (a wrong name,
    a site that is down) is reported and skipped, so one company never stops the run."""
    found, failed = [], []
    for source, companies in WATCH_LIST.items():
        for company in companies:
            try:
                found += [(source, company, raw) for raw in source.fetch(company)]
            except Exception as e:                                          # noqa: BLE001
                failed.append(f"{source.SOURCE}/{company} ({type(e).__name__})")
    if failed:
        print(f"Company feeds that failed this run: {', '.join(failed)}")
    return found
