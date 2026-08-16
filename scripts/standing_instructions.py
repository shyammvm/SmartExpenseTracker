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
from datetime import datetime, date, timezone, timedelta

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))

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


def is_standing_instruction_match(parsed_amount: float, parsed_expense: str | None, parsed_category: str | None, parsed_type: str, candidate: dict) -> bool:
    """Checks if a candidate standing instruction or standing instruction expense matches a transaction."""
    cand_type = candidate.get("expense_type", "debit")
    if (parsed_type or "debit").lower() != (cand_type or "debit").lower():
        return False

    cand_amount = float(candidate.get("amount") or 0.0)
    amount_diff = abs(parsed_amount - cand_amount)
    allowed_diff = max(150.0, cand_amount * 0.20)
    if amount_diff > allowed_diff:
        return False

    parsed_exp_str = (parsed_expense or "").strip().lower()
    cand_exp_str = (candidate.get("expense") or "").strip().lower()
    parsed_cat_str = (parsed_category or "").strip().lower()
    cand_cat_str = (candidate.get("category") or "").strip().lower()

    if cand_exp_str and parsed_exp_str:
        if cand_exp_str in parsed_exp_str or parsed_exp_str in cand_exp_str:
            return True

    parsed_tokens = {w for w in parsed_exp_str.split() if len(w) >= 3}
    cand_tokens = {w for w in cand_exp_str.split() if len(w) >= 3}
    if parsed_tokens & cand_tokens:
        return True

    fixed_categories = {"subscriptions", "emi", "rent/cook", "bills", "rent", "cook"}
    if cand_cat_str and parsed_cat_str and cand_cat_str == parsed_cat_str:
        if cand_cat_str in fixed_categories:
            if amount_diff <= max(50.0, cand_amount * 0.05):
                return True

    return False


def main():
    today = datetime.now(IST).date()
    first_day_of_month = str(today.replace(day=1))

    result = supabase.table("standing_instructions").select("*").eq("is_active", True).execute()
    due_today = [i for i in result.data if is_due_today(i, today)]

    print(f"{today}: {len(due_today)} standing instruction(s) due")

    # Fetch expenses for the current month to check for existing records
    existing_expenses_res = (
        supabase.table("expenses")
        .select("*")
        .gte("expense_date", first_day_of_month)
        .execute()
    )
    existing_expenses = existing_expenses_res.data or []

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

        # Check if an expense matching this standing instruction already exists in expenses for this month
        already_inserted = False
        for exp in existing_expenses:
            if is_standing_instruction_match(
                parsed_amount=float(instruction["amount"]),
                parsed_expense=instruction["expense"],
                parsed_category=instruction["category"],
                parsed_type=instruction["expense_type"],
                candidate=exp,
            ):
                already_inserted = True
                print(f"  Skipped '{instruction['expense']}' -- already recorded in expenses for this month (id: {exp['id']})")
                break

        if already_inserted:
            if end_date and today >= end_date:
                supabase.table("standing_instructions").update({"is_active": False}).eq(
                    "id", instruction["id"]
                ).execute()
                print(f"  '{instruction['expense']}' reached its end date -- deactivated")
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