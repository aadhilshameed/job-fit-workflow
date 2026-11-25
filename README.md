# job-fit-workflow

Three times a day, this pipeline reads new job postings in Germany from free job APIs and company job boards,
checks each one against my background and sends the ones worth applying to to my phone on Telegram. Each
message says **apply**, **stretch** or **lack of info**, lists the gaps and links to the posting. A **skip** is
not sent, but it is saved in the database with its reasons.

**Why a workflow and not an agent.** I looked at both. The job is the same every time (collect, filter, check,
decide), so there is nothing for an agent to plan. A fixed workflow always runs the same steps in the same
order, costs about the same each run and is easy to debug. The final verdict is decided by plain code, not by
the model, so the same labels always give the same verdict.

![How a posting flows through the pipeline](docs/pipeline.svg)

## How it works

1. **Collect.** New postings come from the Arbeitsagentur API, Arbeitnow and the job boards of a list of companies.
2. **Skip what is known.** A posting already in the database, or the same job from another source, is dropped.
3. **Title filter.** A free keyword check drops titles that are clearly not AI work (sales, marketing and so on).
4. **Decisions API check.** One call to OpenAI's Decisions API (a small model that answers fixed questions with a choice, not with free text) answers three questions:
   - Is this hands-on AI engineering?
   - What is the role mostly about (for example agents, classic ML, data)?
   - How many days a week is the office required?
5. **Location rule.** Code decides whether the office is close enough for that many days. Too far means skip, and unsure means "check it yourself".
6. **Requirement check.** Claude Haiku goes through the posting requirement by requirement and compares each one with my background. Every "I have this" must come with a quote from the background, and code checks that the quote is really there.
7. **Verdict.** Code counts the gaps and decides (see the rules below).
8. **Telegram.** Only apply, stretch and lack of info are sent. Each run's messages start with a short divider, and a morning and an evening message report on the runs themselves.

Everything is saved in a Postgres database on [Neon](https://neon.tech) (postings, verdicts, every requirement,
costs and a log of each run). So if a message fails, it is sent again on the next run.

**Why the Decisions API step.** At first Haiku did everything in one go, and it was not consistent. So the work
was split. The Decisions API drops postings that are not AI engineering before they reach Haiku (fewer paid
Haiku calls), and it answers the office question with a fixed choice. Then plain code checks the distance. So
Haiku is left with one job only: matching the requirements against my background.

## The verdict rules

Only the **must-have** requirements decide the verdict. The nice-to-haves are listed in the message but never
change it. A language that is only partly met (for example German B1 where B2 is asked for) is also left out of
the count (my own choice: I would rather apply and let the company decide on my language level).

"Missing" means my background has nothing for it. "Partly met" means I have some of it, but less than asked.
The rules are checked from top to bottom, and the first one that fits decides.

| Verdict | Any one of these |
| --- | --- |
| **skip** | the office is too far · a must-have eligibility is missing · 2 or more skills missing · 3 or more skills partly met · 1 skill missing **+** 2 skills partly met |
| **lack of info** | the office distance needs a manual check · the posting asks for an eligibility my background does not mention |
| **stretch** | 1 skill missing (**+** at most 1 partly met) · 2 skills partly met · an eligibility only partly met (for example the start date) |
| **apply** | everything else: all skills met, or only 1 skill partly met |

## Run it yourself

You need Python 3.14, a Postgres database (I use [Neon](https://neon.tech), the free plan is enough) and these keys:

| Key | For |
| --- | --- |
| `ANTHROPIC_API_KEY` | the requirement check and the summaries (Claude Haiku) |
| `OPENAI_API_KEY` | the decisions API check |
| `DATABASE_URL` | your Postgres connection string |
| `GEOAPIFY_API_KEY` | turning city names into coordinates ([Geoapify](https://www.geoapify.com), free plan) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | your bot from [@BotFather](https://t.me/BotFather) and your chat with it |

```
python -m venv .venv
.venv\Scripts\activate            (on Mac or Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env             (then fill in your keys)
```

Then:

1. **Create the tables.** Run `db/schema.sql` once in your database.
2. **Write your background.** Copy `data/background.example.txt` to `data/background.txt` and describe yourself in the same sections. This file stays out of Git.
3. **Set your home.** Change `HOME` and the state lists in `job_fit/code_nodes/location.py` (they are set for Münchberg in Bavaria).
4. **Choose what to search.** `SEARCH_TERMS` in `job_fit/sources/arbeitsagentur.py`, and the companies in `WATCH_LIST` in `job_fit/sources/company_feeds.py`.
5. **Run it once.**

```
python -m scripts.run_daily --days 1 --max 3      # at most 3 Haiku checks, to test cheaply
python -m scripts.run_daily --no-telegram         # everything except the messages
```

### Run it every day on AWS Lambda

```
python -m scripts.build_lambda       # builds lambda.zip for Lambda's Linux
```

1. Create a Lambda function with the Python 3.14 runtime, 512 MB memory and a 15 minute timeout (a run takes about 5 minutes).
2. Upload `lambda.zip` (Code > Upload from > .zip file) and add the keys above as environment variables.
3. Add an EventBridge schedule for each run time, with the event `{"days": 1}`.

## Cost

In my use it costs a few cents to about 35 cents a day, almost all of it Claude Haiku. The exact cost of every
call is saved in the `model_verdicts` table.

## Project layout

| Path | What it is |
| --- | --- |
| `scripts/run_daily.py` | the whole daily run, step by step |
| `lambda_function.py` | the entry point AWS Lambda calls |
| `job_fit/sources/` | one file per job source |
| `job_fit/code_nodes/` | the free rules: duplicates, title filter, location, verdict |
| `job_fit/llm/` | the model calls, the prompt and the answer schema |
| `job_fit/bot/telegram.py` | the messages and the error alerts |
| `job_fit/db.py` | every database query |
| `db/schema.sql` | the tables |
| `scripts/compare_models.py` | how the model was chosen ([history](docs/model-comparison.md)) |

## Credits

Job data from the [Arbeitsagentur Jobsuche API](https://jobsuche.api.bund.dev) and [Arbeitnow](https://www.arbeitnow.com).
Geocoding powered by [Geoapify](https://www.geoapify.com), © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.

## License

[MIT](LICENSE)
