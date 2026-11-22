"""The company watch list: companies whose own career feeds are read on every run, whatever they post.

A company is on the list because it hires for AI (agents, LLMs, generative AI), not because of one posting:
a company feed only lists jobs that are open right now, so the list stays useful when single postings close.
Found on 22 Nov 2026 by searching for German companies with AI postings on each system; add or remove
companies here. The name is the one in the feed's address (appliedai.jobs.personio.de -> "appliedai").

A feed lists only jobs the company has not closed, but some stay listed for years (a Lever job from 2022), so
company feeds use their own age limit (MAX_AGE_DAYS) instead of --days, and "new" means "not in the database
yet" (db.seen_before). Each job is screened once, so the first run is the only big one.
"""

from datetime import date, timedelta

from job_fit.sources import ashby, greenhouse, lever, personio, smartrecruiters

MAX_AGE_DAYS = 180      # older postings are left out: a job still listed after half a year is likely filled or
                        # a standing "always hiring" ad. 180 keeps appliedAI's working student role (June 2026).

WATCH_LIST = {
    personio: ["appliedai", "statworx", "fact-finder", "acto", "deeploi", "nfon", "cinemo", "feld-m",
               "certivity", "hypatos-gmbh", "merantix-momentum", "celus"],          # last four added 22 Nov 2026
    # removed 22 Nov 2026, no Personio feed on .de or .com (moved to another system?): deepset, justaddai,
    # agile-robots-se (Agile Robots is on Greenhouse EU); xayn (Noxtua)
    lever: ["xentral"],
    smartrecruiters: ["BoschGroup", "Continental", "RolandBerger"],
    # added 22 Nov 2026; every board name answered on the live API that day
    ashby: ["DeepL", "AlephAlpha", "atira", "peec", "lio", "langdock", "parloa", "n8n", "black-forest-labs"],
    greenhouse: ["celonis"],        # US-hosted Greenhouse boards only (boards on job-boards.eu.greenhouse.io have no API)
}


def fetch_all() -> list[tuple]:
    """(source module, company, raw job) for every job on every watched feed. A feed that fails (a wrong name,
    a site that is down) is reported and skipped, so one company never stops the run."""
    found, failed, too_old = [], [], 0
    cutoff = date.today() - timedelta(days=MAX_AGE_DAYS)
    for source, companies in WATCH_LIST.items():
        for company in companies:
            try:
                for raw in source.fetch(company):
                    published = source.published_on(raw)
                    if published and published < cutoff:
                        too_old += 1
                        continue
                    found.append((source, company, raw))
            except Exception as e:                                          # noqa: BLE001
                failed.append(f"{source.SOURCE}/{company} ({type(e).__name__})")
    if too_old:
        print(f"Company feeds: {too_old} jobs older than {MAX_AGE_DAYS} days left out")
    if failed:
        print(f"Company feeds that failed this run: {', '.join(failed)}")
    return found
