"""AWS Lambda entry point: runs the daily pipeline once. EventBridge Scheduler calls it every morning.

The event can change the search window, for example {"days": 2} after a day the run was missed.
Secrets (API keys, DATABASE_URL, the Telegram token and chat id) come from the Lambda's environment
variables, not from a .env file. data/background.txt is packed into the zip by scripts/build_lambda.py.
"""

import sys

from scripts import run_daily


def lambda_handler(event, context):
    days = (event or {}).get("days", 1)
    sys.argv = ["run_daily", "--days", str(days)]
    run_daily.main()      # on a crash it sends the Telegram alert and exits with an error, so Lambda marks the run as failed
    return {"status": "done", "days": days}
