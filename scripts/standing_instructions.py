"""
Processes standing instructions once a day: for any active instruction whose
day_of_month matches today, inserts a matching row into `expenses`. If an
instruction has an end_date and today is on or past it, deactivates it after
this final charge so it stops recurring.

Run via GitHub Actions on a daily schedule (see
.github/workflows/standing-instructions.yml) -- this is a scheduled batch
job, not something that needs to be always-on, so it doesn't belong on the
same Railway/Cloud Run service as the FastAPI app.
"""

import os
import calendar
from datetime import date

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def is_due_today(instruction: dict, today: date) -> bool:
    """True if this instruction's day_of_month matches today -- with a
    month-end fallback: an instruction set for day 31 should still fire on
    Feb 28/29 or Apr/Jun/Sep/Nov 30, since those months never reach day 31."""
    day_of_month = instruction["day_of_month"]
    last_day_of_this_month = calendar.monthrange(today.year, today.month)[1]

    if day_of_month == today.day:
        return True
    if day_of_month > last_day_of_this_month and today.day == last_day_of_this_month:
        return True
    return False


def main():
    today = date.today()

    result = supabase.table("standing_instructions").select("*").eq("is_active", True).execute()
    due_today = [i for i in result.data if is_due_today(i, today)]

    print(f"{today}: {len(due_today)} standing instruction(s) due")

    for instruction in due_today:
        end_date = date.fromisoformat(instruction["end_date"]) if instruction["end_date"] else None

        if end_date and today > end_date:
            # safeguard -- shouldn't normally reach here since it should
            # have been deactivated on its last charge, but stops it firing
            # again if it somehow stayed active past its end date
            supabase.table("standing_instructions").update({"is_active": False}).eq(
                "id", instruction["id"]
            ).execute()
            print(f"  Skipped '{instruction['expense']}' -- past end date, deactivated")
            continue

        row = {
            "amount": instruction["amount"],
            "expense": instruction["expense"],
            "category": instruction["category"],
            "expense_type": instruction["expense_type"],
            "source": "standing_instruction",
            "expense_date": str(today),
        }
        supabase.table("expenses").insert(row).execute()
        print(f"  Added '{instruction['expense']}' -- {instruction['amount']}")

        if end_date and today >= end_date:
            supabase.table("standing_instructions").update({"is_active": False}).eq(
                "id", instruction["id"]
            ).execute()
            print(f"  '{instruction['expense']}' reached its end date -- deactivated")


if __name__ == "__main__":
    main()