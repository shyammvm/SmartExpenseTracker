"""
/parse-expense endpoint.

Accepts raw text (typed manually via the PWA, or a forwarded bank SMS),
uses the Gemini API to extract structured expense data, matches it to
an existing category, and writes the row to Supabase.

Run locally:
    uvicorn main:app --reload --port 8000

Deploy: push this folder to Railway or Fly.io as-is; both auto-detect
the Procfile / requirements.txt.
"""

import os
import json
import time
import calendar
import re
import warnings
from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

# Filter third-party Pydantic UserWarning from SDK imports on startup
warnings.filterwarnings("ignore", category=UserWarning, message=".*is not a Python type.*")

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types, errors

IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_today() -> date:
    return datetime.now(IST).date()


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # backend only, bypasses RLS
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]  # get a free key at aistudio.google.com

# Optional shared secret so randos can't POST to your public endpoint.
# Set this in your hosting env vars and in the Shortcut's request headers.
PARSE_ENDPOINT_SECRET = os.environ.get("PARSE_ENDPOINT_SECRET")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
gemini = genai.Client(api_key=GEMINI_API_KEY)

# Use gemini-flash-lite-latest for high speed (sub-second) and generous 1500 RPM free limits
GEMINI_MODEL = "gemini-flash-lite-latest"

app = FastAPI()

# Allows the web form (hosted on a different domain, e.g. Vercel) to call
# this API directly from the browser. Tighten allow_origins to your actual
# form's domain once it's deployed, instead of leaving it wide open.
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExpenseInput(BaseModel):
    text: str
    source: str = "manual"  # "manual" | "bank_sms"


class ManualExpenseInput(BaseModel):
    amount: float
    category: str
    expense: str | None = None
    note: str | None = None
    expense_type: str = "debit"  # "debit" | "credit"
    expense_date: str | None = None  # "YYYY-MM-DD", defaults to today if omitted


class CategoryInput(BaseModel):
    name: str
    type: str  # "fixed" | "variable"


class CategoryUpdateInput(BaseModel):
    type: str | None = None
    is_active: bool | None = None


class StandingInstructionInput(BaseModel):
    expense: str
    amount: float
    category: str | None = None
    expense_type: str = "debit"  # "debit" | "credit"
    day_of_month: int  # 1 to 31
    end_date: str | None = None  # "YYYY-MM-DD" or None
    is_active: bool = True


class StandingInstructionUpdateInput(BaseModel):
    expense: str | None = None
    amount: float | None = None
    category: str | None = None
    expense_type: str | None = None
    day_of_month: int | None = None
    end_date: str | None = None
    is_active: bool | None = None


class BudgetUpdateInput(BaseModel):
    proposed_budget: float | None = None
    avg_spending: float | None = None


class BudgetBatchUpdateInput(BaseModel):
    budgets: dict[str, float]


class BudgetSettingsInput(BaseModel):
    monthly_income: float
    month: str | None = None


def get_monthly_salaries() -> dict[str, float]:
    """Fetch all month-specific salaries stored in budget_settings table."""
    salaries = {}
    try:
        res = supabase.table("budget_settings").select("month, monthly_income").execute()
        if res.data:
            for r in res.data:
                m = r.get("month")
                inc = float(r.get("monthly_income") or 0.0)
                if m and m != "default":
                    salaries[m] = inc
    except Exception:
        pass
    return salaries


def get_salary_for_month(month: str, default_salary: float = 0.0) -> float:
    salaries = get_monthly_salaries()
    if month in salaries:
        return float(salaries[month])

    # Automatically inherit from the most recent past month
    past_months = [m for m in salaries.keys() if m < month]
    inherited_salary = float(salaries[max(past_months)]) if past_months else default_salary

    # If this is the current active month and it doesn't have an explicit entry yet,
    # auto-record it into budget_settings using the inherited salary so history builds automatically
    today_month = get_ist_today().strftime("%Y-%m")
    if month == today_month and inherited_salary > 0:
        save_monthly_salary(month, inherited_salary, update_default=False)

    return inherited_salary


def save_monthly_salary(month: str, salary: float, update_default: bool = True) -> float:
    """Save month-specific salary directly into budget_settings table."""
    salary = max(0.0, float(salary))

    # 1. Save to budget_settings by month
    try:
        supabase.table("budget_settings").upsert({
            "month": month,
            "monthly_income": salary,
            "updated_at": "now()"
        }, on_conflict="month").execute()
    except Exception as e:
        print(f"Notice saving monthly salary to budget_settings: {e}")

    # 2. If update_default is True, also update baseline id=1 in budget_settings
    if update_default:
        try:
            supabase.table("budget_settings").upsert({
                "id": 1,
                "monthly_income": salary,
                "updated_at": "now()"
            }).execute()
        except Exception as e:
            print(f"Notice updating default budget_settings: {e}")

    return salary


def verify_secret(x_endpoint_secret: str | None = Header(default=None, alias="x-endpoint-secret")):
    if PARSE_ENDPOINT_SECRET and x_endpoint_secret != PARSE_ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid secret")


def get_category_names() -> list[str]:
    result = supabase.table("categories").select("name").eq("is_active", True).execute()
    return [row["name"] for row in result.data]


def parse_with_gemini(text: str, category_names: list[str]) -> dict:
    """Ask Gemini Flash Lite to extract structured expense fields from raw text.
    Includes retries with exponential backoff and fallback models if Gemini is overloaded (503 / 429).
    """

    system_prompt = f"""You extract structured expense data from raw text.
The input text can be:
1. A bank transaction SMS (e.g. "Debited Rs 450.00 at Swiggy via HDFC Bank card xx1234").
2. A quick, informal spend note from Apple Shortcuts, voice input, or manual typing (e.g. "banana 80", "80 for bananas", "chai 20", "cab to office 250", "spent 500 on petrol", "dinner 1500 with friends").

Available categories (pick the closest match, exactly as spelled): {category_names}

expense_type is the PAYMENT METHOD:
- "credit" ONLY if the text explicitly specifies a credit card (e.g. "credit card", "HDFC Credit Card", "paid via credit card").
- "debit" for EVERYTHING ELSE (including UPI, debit card, cash, netbanking, or when payment method is NOT mentioned or omitted).
- CRITICAL DEFAULT RULE: If the expense type or payment method is not explicitly mentioned (which is standard for informal notes like "banana 80" or "coffee 150"), ALWAYS set expense_type to "debit".

This table only tracks money going OUT. If the text describes money coming
IN instead (salary credited, a refund, cashback received, someone paying you
back), that is NOT an expense -- set amount to null.

"expense" is a short description of what this was for -- a merchant/payee name
if there is one (e.g. "Swiggy", "Uber"), otherwise a brief description of the
spend (e.g. "banana", "chai", "cash withdrawal", "friend's birthday gift").

needs_review: Set to true ONLY if:
1. The merchant or payee name in a bank SMS is raw, cryptic, or an individual's UPI name (e.g. "SRI SAI DREAM C", "RAMESH KUMAR", raw VPA code), where the actual nature of the expense cannot be determined from text alone.
2. The closest category picked is "Others" or "Other", or your category choice is an educated guess.
3. The text is completely ambiguous or lacks clear merchant/expense details.

For simple informal notes like "banana 80", "chai 20", "cab to office 250", the expense item and category ARE clear, so needs_review should be FALSE and confidence should be high (>= 0.9).

review_reason: A brief string explaining why needs_review was set to true (e.g. "Categorized as Others", "Cryptic merchant name", "Ambiguous expense details").

If the text does not appear to describe an outgoing expense, set amount to null."""

    # Models to attempt in sequence if primary model is unavailable or rate-limited
    models_to_try = [GEMINI_MODEL]
    for alt in ["gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-flash-latest"]:
        if alt not in models_to_try:
            models_to_try.append(alt)

    last_error = None

    for model_name in models_to_try:
        # Retry up to 2 times per model
        for attempt in range(2):
            try:
                response = gemini.models.generate_content(
                    model=model_name,
                    contents=text,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        response_mime_type="application/json",
                        response_schema={
                            "type": "object",
                            "properties": {
                                "amount": {"type": "number", "nullable": True},
                                "expense": {"type": "string", "nullable": True},
                                "category": {"type": "string"},
                                "note": {"type": "string", "nullable": True},
                                "expense_type": {"type": "string", "enum": ["debit", "credit"]},
                                "confidence": {"type": "number"},
                                "needs_review": {"type": "boolean"},
                                "review_reason": {"type": "string", "nullable": True},
                            },
                            "required": ["category", "expense_type", "confidence", "needs_review"],
                        },
                    ),
                )
                try:
                    return json.loads(response.text)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=422, detail=f"Could not parse AI response: {response.text}")

            except errors.ClientError as e:
                last_error = e
                # Don't retry non-existent or 404/400 models; skip to next valid model instantly
                if "NOT_FOUND" in str(e) or getattr(e, "code", None) in (400, 404):
                    break
                time.sleep(1)
            except (errors.APIError, errors.ServerError) as e:
                last_error = e
                time.sleep(1 * (attempt + 1))
            except HTTPException:
                raise
            except Exception as e:
                last_error = e
                break

    # If all models and retries fail due to 503 / high demand:
    error_detail = "Gemini service is currently experiencing high demand. Please try again in a few seconds."
    if last_error:
        error_detail += f" ({last_error})"
    raise HTTPException(status_code=503, detail=error_detail)


class ExpenseReviewInput(BaseModel):
    category: str | None = None
    expense: str | None = None
    amount: float | None = None
    expense_type: str | None = None
    status: str = "approved"


class ExpenseUpdateInput(BaseModel):
    amount: float | None = None
    expense: str | None = None
    category: str | None = None
    expense_type: str | None = None
    expense_date: str | None = None
    note: str | None = None



RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
NOTIFICATION_EMAIL = os.environ.get("NOTIFICATION_EMAIL")


def send_conflict_email(amount: float, expense: str | None, category: str | None, reason: str | None, raw_text: str | None):
    key = os.environ.get("RESEND_API_KEY")
    to_email = os.environ.get("NOTIFICATION_EMAIL")
    if not key or not to_email:
        return

    subject = f"⚠️ Expense Review Needed: ₹{amount:.2f} ({expense or category or 'Expense'})"
    
    html_content = f"""
    <div style="font-family: monospace, sans-serif; max-width: 520px; padding: 24px; border: 2px solid #1F5C4F; background: #EDE7D3; color: #23241F; border-radius: 8px;">
      <h2 style="color: #9c3b2e; margin-top: 0; font-size: 20px;">⚠️ Expense Conflict Review Required</h2>
      <p style="font-size: 14px;">An incoming transaction was flagged for review:</p>
      <table style="width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 14px;">
        <tr><td style="padding: 6px 0; color: #8A8570;"><strong>Amount:</strong></td><td style="font-weight: bold; font-size: 18px;">₹{amount:.2f}</td></tr>
        <tr><td style="padding: 6px 0; color: #8A8570;"><strong>Payee / Merchant:</strong></td><td>{expense or 'N/A'}</td></tr>
        <tr><td style="padding: 6px 0; color: #8A8570;"><strong>Category:</strong></td><td>{category or 'Unassigned'}</td></tr>
        <tr><td style="padding: 6px 0; color: #8A8570;"><strong>Flag Reason:</strong></td><td style="color: #9c3b2e; font-weight: bold;">{reason or 'Needs review'}</td></tr>
      </table>
      {"<div style='background: #e4dec8; padding: 12px; border-left: 4px solid #9c3b2e; font-size: 12px; margin-bottom: 20px;'><strong>Raw text:</strong> " + raw_text + "</div>" if raw_text else ""}
      <p style="margin-top: 24px;">
        <a href="https://shyammvm.github.io/SmartExpenseTracker/conflicts.html" 
           style="background: #1F5C4F; color: #EDE7D3; padding: 12px 20px; text-decoration: none; font-weight: bold; display: inline-block; border-radius: 4px; text-transform: uppercase; font-size: 13px;">
          Review & Approve Item
        </a>
      </p>
    </div>
    """

    try:
        httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Ledger Tracker <onboarding@resend.dev>",
                "to": [to_email],
                "subject": subject,
                "html": html_content,
            },
            timeout=5.0,
        )
    except Exception as e:
        print(f"Error sending Resend conflict notification email: {e}")


EMPTY_PARSED_RESPONSE = {
    "amount": None,
    "expense": None,
    "category": None,
    "note": None,
    "expense_type": "debit",
    "confidence": 0.0,
    "needs_review": False,
    "review_reason": None,
}


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


def find_existing_standing_instruction_expense(parsed_amount: float, parsed_expense: str | None, category_name: str | None, parsed_type: str) -> dict | None:
    today = get_ist_today()
    first_day_of_month = str(today.replace(day=1))

    res = (
        supabase.table("expenses")
        .select("*")
        .eq("source", "standing_instruction")
        .gte("expense_date", first_day_of_month)
        .execute()
    )
    if not res.data:
        return None

    for row in res.data:
        if is_standing_instruction_match(parsed_amount, parsed_expense, category_name, parsed_type, row):
            return row
    return None


def find_matching_standing_instruction(parsed_amount: float, parsed_expense: str | None, category_name: str | None, parsed_type: str) -> dict | None:
    res = (
        supabase.table("standing_instructions")
        .select("*")
        .eq("is_active", True)
        .execute()
    )
    if not res.data:
        return None

    for inst in res.data:
        if is_standing_instruction_match(parsed_amount, parsed_expense, category_name, parsed_type, inst):
            return inst
    return None


def fallback_shortcut_parser(text: str, category_names: list[str]) -> dict | None:
    """Fallback parser for quick informal spend inputs when Gemini is unreachable or returns amount=None.
    Examples: 'banana 80', '80 banana', 'chai 20', '20 for chai', 'auto 150 rs', 'spent 500 on petrol'.
    """
    text_clean = text.strip()
    if not text_clean:
        return None

    # Check for explicit credit indicator
    is_credit = bool(re.search(r'\b(credit|credit card)\b', text_clean, re.IGNORECASE))
    expense_type = "credit" if is_credit else "debit"

    # Pattern 1: "banana 80", "coffee 150.50", "auto 150 rs", "cab 200 bucks"
    m1 = re.match(r'^(?P<expense>[A-Za-z\s]+?)\s+(?:rs\.?|inr|₹)?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:rs\.?|inr|bucks|rupees)?(?:\s+(?:via|paid|by|on)\s+.*)?$', text_clean, re.IGNORECASE)
    
    # Pattern 2: "80 banana", "150 for coffee", "200 rs cab"
    m2 = re.match(r'^(?:rs\.?|inr|₹)?\s*(?P<amount>\d+(?:\.\d+)?)\s*(?:rs\.?|inr|bucks|rupees)?\s+(?:for\s+)?(?P<expense>[A-Za-z\s]+?)$', text_clean, re.IGNORECASE)

    # Pattern 3: "spent 500 on petrol", "paid 200 for haircut"
    m3 = re.match(r'^(?:spent|paid)\s+(?:rs\.?|inr|₹)?\s*(?P<amount>\d+(?:\.\d+)?)\s+(?:on|for)\s+(?P<expense>[A-Za-z\s]+?)$', text_clean, re.IGNORECASE)

    match = m1 or m2 or m3
    if not match:
        return None

    try:
        amount = float(match.group("amount"))
    except ValueError:
        return None

    expense_desc = match.group("expense").strip()
    if not expense_desc or amount <= 0:
        return None

    expense_lower = expense_desc.lower()
    best_cat = "Others"
    cat_map = {
        "Grocery": ["banana", "apple", "fruit", "grocery", "groceries", "milk", "vegetable", "veggies", "bread", "egg", "supermarket"],
        "Food": ["chai", "tea", "coffee", "food", "lunch", "dinner", "breakfast", "snack", "dosa", "biryani", "restaurant", "swiggy", "zomato", "cafe"],
        "Travel": ["cab", "auto", "uber", "ola", "rapido", "taxi", "bus", "train", "flight", "metro", "fare", "toll"],
        "Petrol": ["petrol", "fuel", "diesel", "gasoline", "gas"],
        "Bills": ["electricity", "water", "wifi", "internet", "recharge", "mobile bill", "bill"],
        "Health": ["haircut", "doctor", "medicine", "pharmacy", "clinic", "hospital", "gym", "spa"],
        "Entertainment": ["movie", "cinema", "netflix", "spotify", "game", "concert"],
        "Shopping": ["clothes", "shoes", "amazon", "flipkart", "mall", "dress"],
    }
    
    for cat_name in category_names:
        if cat_name in cat_map:
            if any(kw in expense_lower for kw in cat_map[cat_name]):
                best_cat = cat_name
                break
        elif cat_name.lower() in expense_lower:
            best_cat = cat_name
            break

    return {
        "amount": amount,
        "expense": expense_desc.title(),
        "category": best_cat,
        "note": None,
        "expense_type": expense_type,
        "confidence": 0.85,
        "needs_review": False,
        "review_reason": None,
    }


@app.post("/parse-expense")
def parse_expense(payload: ExpenseInput, _=Depends(verify_secret)):
    try:
        category_names = get_category_names()
        parsed = None
        try:
            parsed = parse_with_gemini(payload.text, category_names)
        except Exception as ge:
            print(f"Gemini parsing failed or unavailable: {ge}")

        if not isinstance(parsed, dict) or parsed.get("amount") is None:
            fallback = fallback_shortcut_parser(payload.text, category_names)
            if fallback and fallback.get("amount") is not None:
                parsed = fallback

        if not isinstance(parsed, dict) or parsed.get("amount") is None:
            return {
                "status": "error",
                "error": "Text did not appear to describe an outgoing expense",
                "parsed": {**EMPTY_PARSED_RESPONSE, **(parsed if isinstance(parsed, dict) else {})},
                "needs_review": False,
                "inserted": None,
            }

        # Ensure expense_type defaults to "debit" if not explicitly "credit"
        parsed_type = (parsed.get("expense_type") or "debit").lower()
        if parsed_type not in ("debit", "credit"):
            parsed_type = "debit"
        parsed["expense_type"] = parsed_type

        # confirm the category exists (case-insensitive) and use its canonical spelling
        cat_result = (
            supabase.table("categories")
            .select("name")
            .ilike("name", parsed.get("category", ""))
            .limit(1)
            .execute()
        )
        category_name = cat_result.data[0]["name"] if cat_result.data else parsed.get("category", "Others")

        category_lower = (category_name or "").lower()
        is_others = category_lower in ("others", "other")
        needs_review = parsed.get("needs_review", False) or is_others or (parsed.get("confidence", 1.0) < 0.8)
        review_reason = parsed.get("review_reason")

        # History Lookup BEFORE inserting into DB:
        merchant_name = (parsed.get("expense") or "").strip()
        if needs_review and merchant_name:
            history_query = (
                supabase.table("expenses")
                .select("category")
                .ilike("expense", f"%{merchant_name}%")
                .eq("status", "approved")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if history_query.data:
                learned_category = history_query.data[0]["category"]
                if learned_category:
                    category_name = learned_category
                    parsed["category"] = learned_category
                    needs_review = False
                    review_reason = None
                    is_others = False

        if is_others and needs_review and not review_reason:
            review_reason = "Categorized as Others"
        elif needs_review and not review_reason:
            review_reason = "Obscure merchant or low AI confidence"

        status = "pending_review" if needs_review else "approved"

        parsed_amount = parsed["amount"]
        parsed_expense_name = parsed.get("expense")

        # 1. Check if a standing instruction expense was already generated for this month
        existing_si_expense = find_existing_standing_instruction_expense(
            parsed_amount=parsed_amount,
            parsed_expense=parsed_expense_name,
            category_name=category_name,
            parsed_type=parsed_type,
        )

        if existing_si_expense:
            # Update existing standing instruction expense with SMS details & exact billed amount
            updates = {
                "amount": parsed_amount,
                "raw_text": payload.text,
                "ai_confidence": parsed.get("confidence"),
                "status": "approved",
                "needs_review": False,
                "review_reason": None,
            }
            if payload.source and payload.source != "manual":
                updates["source"] = payload.source
            if parsed_expense_name and len(parsed_expense_name) > len(existing_si_expense.get("expense") or ""):
                updates["expense"] = parsed_expense_name
            if parsed.get("note"):
                updates["note"] = parsed.get("note")

            update_res = supabase.table("expenses").update(updates).eq("id", existing_si_expense["id"]).execute()
            updated_row = update_res.data[0] if update_res.data else {**existing_si_expense, **updates}

            full_parsed = {**EMPTY_PARSED_RESPONSE, **parsed}
            return {
                "status": "ok",
                "error": None,
                "parsed": full_parsed,
                "needs_review": False,
                "inserted": updated_row,
                "updated_standing_instruction": True,
            }

        # 2. Check if transaction matches an active standing instruction definition
        matched_si = find_matching_standing_instruction(
            parsed_amount=parsed_amount,
            parsed_expense=parsed_expense_name,
            category_name=category_name,
            parsed_type=parsed_type,
        )

        if matched_si:
            needs_review = False
            review_reason = None
            status = "approved"
            override_source = "standing_instruction"
        else:
            override_source = payload.source

        row = {
            "amount": parsed["amount"],
            "expense": parsed.get("expense"),
            "category": category_name,
            "note": parsed.get("note"),
            "source": override_source,
            "raw_text": payload.text,
            "expense_type": parsed_type,
            "ai_confidence": parsed.get("confidence"),
            "expense_date": str(get_ist_today()),
            "status": status,
            "needs_review": needs_review,
            "review_reason": review_reason,
        }

        insert_result = supabase.table("expenses").insert(row).execute()

        if needs_review:
            send_conflict_email(
                amount=parsed["amount"],
                expense=parsed.get("expense"),
                category=category_name,
                reason=review_reason,
                raw_text=payload.text,
            )

        full_parsed = {**EMPTY_PARSED_RESPONSE, **parsed}

        return {
            "status": "ok",
            "error": None,
            "parsed": full_parsed,
            "needs_review": needs_review,
            "inserted": insert_result.data[0] if insert_result.data else None,
        }

    except HTTPException as he:
        error_msg = he.detail if isinstance(he.detail, str) else str(he.detail)
        return {
            "status": "error",
            "error": error_msg,
            "parsed": EMPTY_PARSED_RESPONSE,
            "needs_review": False,
            "inserted": None,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "parsed": EMPTY_PARSED_RESPONSE,
            "needs_review": False,
            "inserted": None,
        }


@app.get("/expenses/check-duplicate")
def check_duplicate_expense(
    amount: float,
    expense_date: str | None = None,
    expense: str | None = None,
    _=Depends(verify_secret),
):
    """Checks if an expense with the same amount was logged on the given date (or today)."""
    check_date = (expense_date or str(get_ist_today())).strip()
    query = (
        supabase.table("expenses_flat")
        .select("id, amount, expense, category, expense_date, created_at, source")
        .eq("expense_date", check_date)
        .eq("amount", round(amount, 2))
        .neq("status", "pending_review")
        .order("created_at", desc=True)
        .limit(5)
    )
    result = query.execute()
    existing = result.data or []

    similar = []
    if expense and expense.strip():
        exp_clean = expense.strip().lower()
        for item in existing:
            item_exp = (item.get("expense") or "").strip().lower()
            if item_exp and (exp_clean in item_exp or item_exp in exp_clean):
                similar.append(item)

    return {
        "has_duplicate": len(existing) > 0,
        "matches": similar if similar else existing,
        "count": len(existing),
    }


@app.post("/add-expense")
def add_expense(payload: ManualExpenseInput, _=Depends(verify_secret)):
    """Direct insert for structured manual entry from the web form -- no AI
    call, since the user already picked the exact amount and category."""

    cat_result = (
        supabase.table("categories")
        .select("name")
        .ilike("name", payload.category)
        .limit(1)
        .execute()
    )
    if not cat_result.data:
        raise HTTPException(status_code=422, detail=f"Unknown category: {payload.category}")
    category_name = cat_result.data[0]["name"]

    exp_type = (payload.expense_type or "debit").lower()
    if exp_type not in ("debit", "credit"):
        exp_type = "debit"

    row = {
        "amount": payload.amount,
        "expense": payload.expense,
        "category": category_name,
        "note": payload.note,
        "source": "manual",
        "expense_type": exp_type,
        "expense_date": payload.expense_date or str(get_ist_today()),
        "status": "approved",
        "needs_review": False,
    }

    insert_result = supabase.table("expenses").insert(row).execute()
    return {"status": "ok", "inserted": insert_result.data[0] if insert_result.data else None}


@app.get("/expenses/pending")
def get_pending_expenses(_=Depends(verify_secret)):
    """Fetch all expenses flagged as pending review."""
    result = (
        supabase.table("expenses")
        .select("*")
        .eq("status", "pending_review")
        .execute()
    )
    return {"expenses": result.data}


@app.get("/conflicts/count")
def get_conflicts_count(_=Depends(verify_secret)):
    """Fetch count of pending conflicts."""
    result = (
        supabase.table("expenses")
        .select("id", count="exact")
        .eq("status", "pending_review")
        .execute()
    )
    count = result.count if result.count is not None else len(result.data)
    return {"count": count}


@app.patch("/expenses/{expense_id}/review")
def review_expense(expense_id: str, payload: ExpenseReviewInput, _=Depends(verify_secret)):
    """Approve or edit & approve a pending expense."""
    updates = {"status": payload.status, "needs_review": False}
    if payload.category:
        updates["category"] = payload.category
    if payload.expense is not None:
        updates["expense"] = payload.expense
    if payload.amount is not None:
        updates["amount"] = payload.amount
    if payload.expense_type:
        updates["expense_type"] = payload.expense_type

    result = supabase.table("expenses").update(updates).eq("id", expense_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Expense not found")
    return {"status": "ok", "updated": result.data[0]}


@app.delete("/expenses/{expense_id}")
def delete_expense(expense_id: str, _=Depends(verify_secret)):
    """Deny / delete an expense record."""
    result = supabase.table("expenses").delete().eq("id", expense_id).execute()
    return {"status": "ok"}


@app.get("/expenses/recent")
def get_recent_expenses(
    limit: int = 30,
    page: int = 1,
    start_date: str | None = None,
    end_date: str | None = None,
    q: str | None = None,
    _=Depends(verify_secret),
):
    """Fetch paginated expenses with optional date range and search filtering."""
    limit = min(max(1, limit), 5000)
    page = max(1, page)
    offset = (page - 1) * limit

    query = supabase.table("expenses_flat").select("*", count="exact")

    if start_date and start_date.strip():
        query = query.gte("expense_date", start_date.strip())
    if end_date and end_date.strip():
        query = query.lte("expense_date", end_date.strip())
    if q and q.strip():
        clean_q = re.sub(r"[,%()]", " ", q).strip()
        if clean_q:
            query = query.or_(f"expense.ilike.%{clean_q}%,category.ilike.%{clean_q}%")

    query = (
        query.order("expense_date", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    result = query.execute()
    count = result.count if result.count is not None else len(result.data or [])
    total_pages = (count + limit - 1) // limit if count > 0 else 1

    return {
        "expenses": result.data or [],
        "total": count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
    }


@app.patch("/expenses/{expense_id}")
def update_expense(expense_id: str, payload: ExpenseUpdateInput, _=Depends(verify_secret)):
    """Update fields on an existing expense record."""
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    if "category" in updates and updates["category"]:
        cat_res = (
            supabase.table("categories")
            .select("name")
            .ilike("name", updates["category"])
            .limit(1)
            .execute()
        )
        if cat_res.data:
            updates["category"] = cat_res.data[0]["name"]

    if "expense" in updates and updates["expense"]:
        updates["expense"] = updates["expense"].strip()

    result = supabase.table("expenses").update(updates).eq("id", expense_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Expense not found")
    return {"status": "ok", "updated": result.data[0]}



@app.get("/categories")
def list_categories(_=Depends(verify_secret)):
    """Returns full category records -- used by the categories management
    page. The entry page filters this client-side to active ones only."""
    result = supabase.table("categories").select("name, type, is_active").order("name").execute()
    return {"categories": result.data}


@app.post("/categories")
def create_category(payload: CategoryInput, _=Depends(verify_secret)):
    if payload.type not in ("fixed", "variable"):
        raise HTTPException(status_code=422, detail="type must be 'fixed' or 'variable'")
    try:
        result = supabase.table("categories").insert({
            "name": payload.name,
            "type": payload.type,
        }).execute()
    except Exception as e:
        # most likely a duplicate name (case-insensitive unique index)
        raise HTTPException(status_code=409, detail=f"Could not create category: {e}")
    return {"status": "ok", "category": result.data[0] if result.data else None}


@app.patch("/categories/{name}")
def update_category(name: str, payload: CategoryUpdateInput, _=Depends(verify_secret)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "type" in updates and updates["type"] not in ("fixed", "variable"):
        raise HTTPException(status_code=422, detail="type must be 'fixed' or 'variable'")

    result = supabase.table("categories").update(updates).ilike("name", name).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail=f"Category not found: {name}")
    return {"status": "ok", "category": result.data[0]}


@app.get("/standing-instructions/upcoming")
def get_upcoming_standing_instructions(days_ahead: int = 14, _=Depends(verify_secret)):
    """Return active standing instructions due in the next N days (default 14)."""
    today = get_ist_today()
    res = supabase.table("standing_instructions").select("*").eq("is_active", True).execute()
    instructions = res.data or []

    upcoming = []
    total_due = 0.0
    debit_due = 0.0
    credit_due = 0.0

    for inst in instructions:
        dom = inst["day_of_month"]
        last_day_this_month = calendar.monthrange(today.year, today.month)[1]
        target_day_this_month = min(dom, last_day_this_month)

        due_date = date(today.year, today.month, target_day_this_month)
        if due_date < today:
            if today.month == 12:
                next_y = today.year + 1
                next_m = 1
            else:
                next_y = today.year
                next_m = today.month + 1
            last_day_next_month = calendar.monthrange(next_y, next_m)[1]
            target_day_next_month = min(dom, last_day_next_month)
            due_date = date(next_y, next_m, target_day_next_month)

        if inst.get("end_date"):
            try:
                end_d = date.fromisoformat(inst["end_date"])
                if due_date > end_d:
                    continue
            except Exception:
                pass

        days_away = (due_date - today).days
        if 0 <= days_away <= days_ahead:
            amt = float(inst.get("amount") or 0.0)
            etype = inst.get("expense_type", "debit")
            total_due += amt
            if etype == "debit":
                debit_due += amt
            else:
                credit_due += amt

            upcoming.append({
                "id": inst["id"],
                "expense": inst["expense"],
                "amount": amt,
                "category": inst.get("category"),
                "expense_type": etype,
                "day_of_month": dom,
                "due_date": str(due_date),
                "due_date_formatted": due_date.strftime("%b %d"),
                "days_away": days_away,
            })

    upcoming.sort(key=lambda x: (x["days_away"], -x["amount"]))

    return {
        "upcoming": upcoming,
        "count": len(upcoming),
        "total_due": round(total_due, 2),
        "debit_due": round(debit_due, 2),
        "credit_due": round(credit_due, 2),
        "days_window": days_ahead,
    }


@app.get("/standing-instructions")
def list_standing_instructions(expense_type: str | None = None, _=Depends(verify_secret)):
    query = supabase.table("standing_instructions").select("*").order("day_of_month")
    if expense_type:
        query = query.eq("expense_type", expense_type)
    result = query.execute()
    return {"instructions": result.data}


@app.post("/standing-instructions")
def create_standing_instruction(payload: StandingInstructionInput, _=Depends(verify_secret)):
    if payload.day_of_month < 1 or payload.day_of_month > 31:
        raise HTTPException(status_code=422, detail="day_of_month must be between 1 and 31")
    if payload.expense_type not in ("debit", "credit"):
        raise HTTPException(status_code=422, detail="expense_type must be 'debit' or 'credit'")

    category_name = None
    if payload.category:
        cat_res = (
            supabase.table("categories")
            .select("name")
            .ilike("name", payload.category)
            .limit(1)
            .execute()
        )
        if cat_res.data:
            category_name = cat_res.data[0]["name"]
        else:
            category_name = payload.category

    end_date_val = payload.end_date.strip() if payload.end_date and payload.end_date.strip() else None

    row = {
        "expense": payload.expense.strip(),
        "amount": payload.amount,
        "category": category_name,
        "expense_type": payload.expense_type,
        "day_of_month": payload.day_of_month,
        "end_date": end_date_val,
        "is_active": payload.is_active,
    }
    result = supabase.table("standing_instructions").insert(row).execute()
    return {"status": "ok", "instruction": result.data[0] if result.data else None}


@app.patch("/standing-instructions/{instruction_id}")
def update_standing_instruction(instruction_id: str, payload: StandingInstructionUpdateInput, _=Depends(verify_secret)):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    if "day_of_month" in updates and (updates["day_of_month"] < 1 or updates["day_of_month"] > 31):
        raise HTTPException(status_code=422, detail="day_of_month must be between 1 and 31")
    if "expense_type" in updates and updates["expense_type"] not in ("debit", "credit"):
        raise HTTPException(status_code=422, detail="expense_type must be 'debit' or 'credit'")

    if "category" in updates and updates["category"]:
        cat_res = (
            supabase.table("categories")
            .select("name")
            .ilike("name", updates["category"])
            .limit(1)
            .execute()
        )
        if cat_res.data:
            updates["category"] = cat_res.data[0]["name"]

    if "end_date" in updates:
        end_date_str = updates["end_date"]
        updates["end_date"] = end_date_str.strip() if end_date_str and isinstance(end_date_str, str) and end_date_str.strip() else None

    if "expense" in updates and updates["expense"]:
        updates["expense"] = updates["expense"].strip()

    result = supabase.table("standing_instructions").update(updates).eq("id", instruction_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Standing instruction not found")
    return {"status": "ok", "instruction": result.data[0]}


@app.delete("/standing-instructions/{instruction_id}")
def delete_standing_instruction(instruction_id: str, _=Depends(verify_secret)):
    result = supabase.table("standing_instructions").delete().eq("id", instruction_id).execute()
    return {"status": "ok"}



@app.get("/summary/entry-page")
def summary_entry_page(_=Depends(verify_secret)):
    """Quick stats for the entry page: today's total, this month's total,
    fixed vs variable split, and average daily variable spend so far this
    month (a rough day-to-day burn-rate indicator)."""
    today = get_ist_today()
    month_start = today.replace(day=1)

    today_rows = (
        supabase.table("expenses")
        .select("amount")
        .eq("expense_date", str(today))
        .neq("status", "pending_review")
        .execute()
    )
    today_total = sum(float(r["amount"] or 0.0) for r in today_rows.data)

    month_rows = (
        supabase.table("expenses_flat")
        .select("amount, category, category_type, expense_type, expense_date")
        .gte("expense_date", str(month_start))
        .lte("expense_date", str(today))
        .neq("status", "pending_review")
        .execute()
    )

    month_variable = 0.0
    month_fixed = 0.0
    cc_bill_payment = 0.0
    debit_spend = 0.0
    credit_spend = 0.0
    daily_spend_map = {}
    category_spend_map = {}

    for r in (month_rows.data or []):
        amt = float(r.get("amount") or 0.0)
        cat = r.get("category")
        ctype = r.get("category_type")
        etype = r.get("expense_type") or "debit"
        d_str = r.get("expense_date")

        # Exclude CC bill payment from true expenses to prevent double counting
        if cat == "Credit Card" and etype == "debit":
            cc_bill_payment += amt
        else:
            if cat:
                category_spend_map[cat] = round(category_spend_map.get(cat, 0.0) + amt, 2)
            if ctype == "variable":
                month_variable += amt
                if d_str:
                    daily_spend_map[d_str] = round(daily_spend_map.get(d_str, 0.0) + amt, 2)
            elif ctype == "fixed":
                month_fixed += amt

            if etype == "credit":
                credit_spend += amt
            else:
                debit_spend += amt

    month_total = round(month_variable + month_fixed, 2)
    bank_cash_outflow = round(debit_spend + cc_bill_payment, 2)

    days_elapsed = today.day  # 1st of month = day 1, so this is correct as a divisor
    avg_daily_variable = round(month_variable / days_elapsed, 2) if days_elapsed else 0

    days_to_show = min(14, max(7, today.day))
    daily_history = []
    for i in range(days_to_show - 1, -1, -1):
        d = today - timedelta(days=i)
        d_str = str(d)
        daily_history.append({
            "date": d_str,
            "day": d.day,
            "amount": daily_spend_map.get(d_str, 0.0)
        })

    # Fetch budget totals & key categories for widget indicators
    monthly_income = 0.0
    total_proposed_variable_budget = 0.0
    fixed_obligations = month_fixed
    total_budget = month_total
    current_savings = 0.0
    key_categories = {
        "food": {"name": "Food", "spent": category_spend_map.get("Food", 0.0), "budget": 0.0, "is_over": False, "remaining": 0.0},
        "grocery": {"name": "Grocery", "spent": category_spend_map.get("Grocery", 0.0), "budget": 0.0, "is_over": False, "remaining": 0.0},
        "shopping": {"name": "Shopping", "spent": category_spend_map.get("Shopping", 0.0), "budget": 0.0, "is_over": False, "remaining": 0.0},
    }

    try:
        settings_res = supabase.table("budget_settings").select("monthly_income").eq("id", 1).limit(1).execute()
        if settings_res.data:
            monthly_income = float(settings_res.data[0].get("monthly_income") or 0.0)
        cur_month_str = today.strftime("%Y-%m")
        monthly_income = get_salary_for_month(cur_month_str, monthly_income)

        budgets_res = supabase.table("budgets").select("category, proposed_budget").execute()
        budgets_data = budgets_res.data or []
        budget_map = {b["category"].lower(): float(b.get("proposed_budget") or 0.0) for b in budgets_data}
        total_proposed_variable_budget = round(sum(float(b.get("proposed_budget") or 0.0) for b in budgets_data), 2)

        standing_res = supabase.table("standing_instructions").select("amount, expense_type").eq("is_active", True).execute()
        standing_data = standing_res.data or []
        fixed_bank_debits = round(sum(float(r.get("amount") or 0.0) for r in standing_data if r.get("expense_type") != "credit"), 2)
        fixed_obligations = fixed_bank_debits if fixed_bank_debits > 0 else round(month_fixed, 2)
        total_budget = round(fixed_obligations + total_proposed_variable_budget, 2)
        current_savings = round(monthly_income - bank_cash_outflow, 2) if monthly_income > 0 else 0.0

        for key, cat_name in [("food", "Food"), ("grocery", "Grocery"), ("shopping", "Shopping")]:
            sp = category_spend_map.get(cat_name, 0.0)
            bg = budget_map.get(cat_name.lower(), 0.0)
            key_categories[key] = {
                "name": cat_name,
                "spent": round(sp, 2),
                "budget": round(bg, 2),
                "is_over": (sp > bg) if bg > 0 else False,
                "remaining": round(bg - sp, 2)
            }
    except Exception as e:
        print(f"Non-fatal error fetching budget summary in entry-page: {e}")

    return {
        "today_total": round(today_total, 2),
        "today_variable_total": round(daily_spend_map.get(str(today), 0.0), 2),
        "month_total": month_total,
        "month_fixed_total": round(month_fixed, 2),
        "month_variable_total": round(month_variable, 2),
        "bank_cash_outflow": bank_cash_outflow,
        "credit_card_bill_payments": round(cc_bill_payment, 2),
        "avg_daily_variable_spend": avg_daily_variable,
        "daily_history": daily_history,
        "monthly_income": monthly_income,
        "total_budget": total_budget,
        "total_proposed_variable_budget": total_proposed_variable_budget,
        "fixed_obligations": fixed_obligations,
        "current_savings": current_savings,
        "key_categories": key_categories,
    }


@app.get("/summary/dashboard")
def summary_dashboard(month: str | None = None, _=Depends(verify_secret)):
    """Category-wise breakdown with credit/debit split, last month MTD comparison,
    and current credit card billing cycle total. Optionally filter by month (YYYY-MM)."""
    today = get_ist_today()
    if month and re.match(r"^\d{4}-\d{2}$", month.strip()):
        y, m = map(int, month.strip().split("-"))
        month_start = date(y, m, 1)
        max_days = calendar.monthrange(y, m)[1]
        is_current_month = (y == today.year and m == today.month)
        month_end = date(y, m, min(today.day, max_days)) if is_current_month else date(y, m, max_days)
    else:
        month_start = today.replace(day=1)
        is_current_month = True
        month_end = today

    # 1. Last month till date calculation (relative to selected month)
    last_month_end_prev = month_start - timedelta(days=1)
    last_month_start = last_month_end_prev.replace(day=1)
    max_days_last_month = calendar.monthrange(last_month_start.year, last_month_start.month)[1]
    target_day = min(month_end.day, max_days_last_month)
    last_month_till_date_end = date(last_month_start.year, last_month_start.month, target_day)

    # 2. Credit card billing cycle calculation (16th to 15th)
    if month_end.day <= 15:
        cycle_start = (month_start - timedelta(days=1)).replace(day=16)
        cycle_end = month_start.replace(day=15)
    else:
        cycle_start = month_start.replace(day=16)
        if month_start.month == 12:
            next_month_start = date(month_start.year + 1, 1, 1)
        else:
            next_month_start = date(month_start.year, month_start.month + 1, 1)
        cycle_end = next_month_start.replace(day=15)

    # Fetch this month's rows
    this_month_rows = (
        supabase.table("expenses_flat")
        .select("amount, category, category_type, expense_type")
        .gte("expense_date", str(month_start))
        .lte("expense_date", str(month_end))
        .neq("status", "pending_review")
        .execute()
    )

    # Fetch last month till date rows (excluding last month's CC bill payment)
    last_month_rows = (
        supabase.table("expenses_flat")
        .select("amount, category, expense_type")
        .gte("expense_date", str(last_month_start))
        .lte("expense_date", str(last_month_till_date_end))
        .neq("status", "pending_review")
        .execute()
    )
    last_month_mtd_total = sum(
        float(r["amount"] or 0.0)
        for r in (last_month_rows.data or [])
        if not (r.get("category") == "Credit Card" and r.get("expense_type") == "debit")
    )

    # Fetch current credit cycle rows
    credit_cycle_rows = (
        supabase.table("expenses_flat")
        .select("amount")
        .eq("expense_type", "credit")
        .gte("expense_date", str(cycle_start))
        .lte("expense_date", str(cycle_end))
        .neq("status", "pending_review")
        .execute()
    )
    credit_cycle_total = sum(float(r["amount"] or 0.0) for r in (credit_cycle_rows.data or []))

    by_category: dict[str, dict] = {}
    cc_bill_payment = 0.0
    debit_spend_net = 0.0
    credit_spend_total = 0.0

    for r in (this_month_rows.data or []):
        cat = r["category"]
        etype = r.get("expense_type", "debit")
        amt = float(r.get("amount") or 0.0)

        # Exclude CC bill payment debit from category breakdown to eliminate double counting
        if cat == "Credit Card" and etype == "debit":
            cc_bill_payment += amt
        else:
            entry = by_category.setdefault(cat, {
                "category": cat,
                "type": r["category_type"],
                "total": 0.0,
                "debit_total": 0.0,
                "credit_total": 0.0,
                "count": 0
            })
            entry["total"] += amt
            if etype == "credit":
                entry["credit_total"] += amt
                credit_spend_total += amt
            else:
                entry["debit_total"] += amt
                debit_spend_net += amt
            entry["count"] += 1

    categories = sorted(by_category.values(), key=lambda c: c["total"], reverse=True)
    for c in categories:
        c["total"] = round(c["total"], 2)
        c["debit_total"] = round(c["debit_total"], 2)
        c["credit_total"] = round(c["credit_total"], 2)

    month_total = round(sum(c["total"] for c in categories), 2)
    bank_cash_outflow = round(debit_spend_net + cc_bill_payment, 2)

    return {
        "month": month_start.strftime("%B %Y"),
        "selected_month": month_start.strftime("%Y-%m"),
        "is_current_month": is_current_month,
        "month_start": str(month_start),
        "month_end": str(month_end),
        "month_total": month_total,
        "bank_cash_outflow": bank_cash_outflow,
        "credit_card_bill_payment": round(cc_bill_payment, 2),
        "debit_spend_net": round(debit_spend_net, 2),
        "credit_spend_total": round(credit_spend_total, 2),
        "last_month_mtd_total": round(last_month_mtd_total, 2),
        "last_month_mtd_range": f"{last_month_start.strftime('%b 1')} – {last_month_till_date_end.strftime('%b %d')}",
        "credit_cycle_total": round(credit_cycle_total, 2),
        "credit_cycle_range": f"{cycle_start.strftime('%b 16')} – {cycle_end.strftime('%b 15')}",
        "categories": categories,
    }


def compute_variable_category_averages() -> dict[str, float]:
    """Calculate historical monthly average spend for each variable category from monthly_category_summary."""
    try:
        cats_res = supabase.table("categories").select("name").eq("type", "variable").eq("is_active", True).execute()
        var_cats = {c["name"] for c in (cats_res.data or [])}
        rows = (supabase.table("monthly_category_summary").select("*").execute()).data or []
        from collections import defaultdict
        cat_amounts = defaultdict(list)
        for r in rows:
            if r.get("category") in var_cats:
                cat_amounts[r["category"]].append(float(r.get("total_amount") or 0.0))
        
        avg_map = {}
        for c in var_cats:
            amts = cat_amounts.get(c, [])
            avg_map[c] = round(sum(amts) / len(amts), 2) if amts else 0.0
        return avg_map
    except Exception as e:
        print(f"Error computing variable category averages: {e}")
        return {}


@app.get("/budgets")
def get_budgets(month: str | None = None, _=Depends(verify_secret)):
    """Fetch all variable category budgets, historical averages, current month spending,
    credit card bill impact, and savings analysis based on income and spending.
    Optionally filter by month (YYYY-MM)."""
    today = get_ist_today()
    if month and re.match(r"^\d{4}-\d{2}$", month.strip()):
        y, m = map(int, month.strip().split("-"))
        selected_month = f"{y:04d}-{m:02d}"
        month_start = date(y, m, 1)
        max_days = calendar.monthrange(y, m)[1]
        is_current_month = (y == today.year and m == today.month)
        month_end = date(y, m, min(today.day, max_days)) if is_current_month else date(y, m, max_days)
        days_in_month = max_days
        days_elapsed = month_end.day
    else:
        selected_month = today.strftime("%Y-%m")
        month_start = today.replace(day=1)
        is_current_month = True
        month_end = today
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        days_elapsed = today.day

    # 1. Fetch baseline default monthly income from budget_settings
    default_monthly_income = 0.0
    schema_ready = True
    try:
        bs_res = supabase.table("budget_settings").select("monthly_income").eq("id", 1).execute()
        if bs_res.data:
            default_monthly_income = float(bs_res.data[0].get("monthly_income") or 0.0)
    except Exception as e:
        schema_ready = False
        print(f"budget_settings table notice: {e}")

    # Resolve month-specific salary (falls back to default baseline income)
    monthly_income = get_salary_for_month(selected_month, default_monthly_income)

    # 2. Fetch variable categories
    cat_res = supabase.table("categories").select("name, type").eq("type", "variable").eq("is_active", True).order("name").execute()
    var_categories = cat_res.data or []

    # 3. Check for dynamic database view budgets_view first
    view_map = {}
    try:
        bv_res = supabase.table("budgets_view").select("*").execute()
        if bv_res.data:
            for row in bv_res.data:
                view_map[row["category"]] = row
    except Exception as e:
        pass

    # 4. Fetch budgets table
    budget_map = {}
    try:
        b_res = supabase.table("budgets").select("*").execute()
        for b in (b_res.data or []):
            budget_map[b["category"]] = b
    except Exception as e:
        schema_ready = False
        print(f"budgets table notice: {e}")

    # 5. Computed averages for fallback or missing rows
    computed_avgs = compute_variable_category_averages()

    # 6. Fetch this month's spending by category from expenses_flat
    month_rows = (
        supabase.table("expenses_flat")
        .select("amount, category, category_type, expense_type, expense_date, expense")
        .gte("expense_date", str(month_start))
        .lte("expense_date", str(month_end))
        .neq("status", "pending_review")
        .execute()
    ).data or []

    from collections import defaultdict
    month_spent_by_cat = defaultdict(float)
    current_month_var_spend = 0.0
    current_month_fixed_spend_net = 0.0
    credit_card_bill_payments = 0.0
    debit_spend_net = 0.0
    credit_spend_total = 0.0

    for r in month_rows:
        amt = float(r.get("amount") or 0.0)
        cat = r.get("category")
        etype = r.get("expense_type") or "debit"
        cat_type = r.get("category_type")

        month_spent_by_cat[cat] += amt

        # Credit card bill payment is an internal debt transfer, not an expense
        if cat == "Credit Card" and etype == "debit":
            credit_card_bill_payments += amt
        else:
            if cat_type == "variable":
                current_month_var_spend += amt
            elif cat_type == "fixed":
                current_month_fixed_spend_net += amt

            if etype == "credit":
                credit_spend_total += amt
            else:
                debit_spend_net += amt

    # True spend (Accrual): real consumption across debit and credit cards, excluding CC bill debt transfer
    current_month_true_spend = round(current_month_var_spend + current_month_fixed_spend_net, 2)
    # Bank cash outflow (Cash Basis): total cash that left the bank account this month
    current_month_bank_outflow = round(debit_spend_net + credit_card_bill_payments, 2)

    # 7. Credit card billing cycle calculation
    # ACTIVE cycle: the billing period currently accumulating charges (not yet billed)
    # PREVIOUS closed cycle: the period whose bill is actually DUE this month (paid from this month's salary)
    #
    # With a 16th–15th cycle and ~20 day grace, the bill due this month is always the *previous* closed cycle:
    #   e.g. Today = Sep 30  →  Active: Sep 16–Oct 15 (due Nov)  |  Due this month: Aug 16–Sep 15
    #   e.g. Today = Oct 20  →  Active: Oct 16–Nov 15 (due Dec)  |  Due this month: Sep 16–Oct 15

    if today.day <= 15:
        # Active cycle started on the 16th of last month
        if today.month == 1:
            cycle_start = date(today.year - 1, 12, 16)
        else:
            cycle_start = date(today.year, today.month - 1, 16)
        cycle_end = date(today.year, today.month, 15)

        # Previous closed cycle: one month earlier
        if today.month <= 2:
            prev_cycle_start = date(today.year - 1 if today.month == 1 else today.year, 11 if today.month == 1 else 12, 16)
            prev_cycle_end   = date(today.year - 1 if today.month == 1 else today.year, 12 if today.month == 1 else 1, 15) if today.month == 1 else date(today.year, today.month - 1, 15)
        else:
            prev_cycle_start = date(today.year, today.month - 2, 16)
            prev_cycle_end   = date(today.year, today.month - 1, 15)
    else:
        # Active cycle started on the 16th of this month
        cycle_start = date(today.year, today.month, 16)
        if today.month == 12:
            cycle_end = date(today.year + 1, 1, 15)
        else:
            cycle_end = date(today.year, today.month + 1, 15)

        # Previous closed cycle: started 16th of last month, ended 15th of this month
        if today.month == 1:
            prev_cycle_start = date(today.year - 1, 12, 16)
        else:
            prev_cycle_start = date(today.year, today.month - 1, 16)
        prev_cycle_end = date(today.year, today.month, 15)

    # Query current ACTIVE cycle (for banner — shows unbilled accumulation)
    credit_cycle_rows = (
        supabase.table("expenses_flat")
        .select("amount")
        .eq("expense_type", "credit")
        .gte("expense_date", str(cycle_start))
        .lte("expense_date", str(cycle_end))
        .neq("status", "pending_review")
        .execute()
    )
    current_cycle_credit_total = round(sum(float(r["amount"] or 0.0) for r in (credit_cycle_rows.data or [])), 2)

    # Query PREVIOUS closed cycle (for bill estimate — the bill actually due this month)
    prev_cycle_rows = (
        supabase.table("expenses_flat")
        .select("amount")
        .eq("expense_type", "credit")
        .gte("expense_date", str(prev_cycle_start))
        .lte("expense_date", str(prev_cycle_end))
        .neq("status", "pending_review")
        .execute()
    )
    prev_cycle_credit_total = round(sum(float(r["amount"] or 0.0) for r in (prev_cycle_rows.data or [])), 2)

    # 8. Fixed obligations: split into direct Bank Debits (Rent, cook, loans) vs Credit-based SIs
    si_res = supabase.table("standing_instructions").select("amount, expense_type, expense").eq("is_active", True).execute()
    si_data = si_res.data or []
    fixed_bank_debits = round(sum(float(s["amount"] or 0.0) for s in si_data if s.get("expense_type") != "credit"), 2)
    fixed_credit_obligations = round(sum(float(s["amount"] or 0.0) for s in si_data if s.get("expense_type") == "credit"), 2)
    fixed_obligations_total = round(fixed_bank_debits + fixed_credit_obligations, 2)
    if fixed_bank_debits == 0:
        fixed_bank_debits = round(current_month_fixed_spend_net, 2)

    # 9. Credit Card Bill Analysis (Exactly ONE bill per month, settled with salary)
    # Historical average CC bill (from past CC bill payments)
    cc_hist = (
        supabase.table("expenses_flat")
        .select("amount")
        .eq("category", "Credit Card")
        .eq("expense_type", "debit")
        .neq("status", "pending_review")
        .order("expense_date", desc=True)
        .limit(12)
        .execute()
    ).data or []
    historical_avg_cc_bill = round(sum(float(r["amount"] or 0.0) for r in cc_hist) / len(cc_hist), 2) if cc_hist else 0.0

    # Effective CC bill for the month (only ONE bill per month):
    # Priority:
    # 1. Actual CC bill payment recorded this month → use exact amount paid
    # 2. Previous closed cycle total → best estimate of bill due this month
    #    (the cycle that closed before this month, whose bill is now payable)
    # 3. Historical average → last resort fallback
    # NOTE: The currently ACTIVE cycle is intentionally NOT used here — that cycle's
    #       bill won't be due until next month. It is shown separately in the CC banner.
    if credit_card_bill_payments > 0:
        cc_bill_budget = round(credit_card_bill_payments, 2)
        cc_bill_source = "paid_this_month"
    elif prev_cycle_credit_total > 0:
        cc_bill_budget = prev_cycle_credit_total
        cc_bill_source = "prev_cycle_total"  # bill due this month (from previous closed cycle)
    else:
        cc_bill_budget = historical_avg_cc_bill
        cc_bill_source = "historical_avg"

    # Salary commitments: Fixed Bank Debits + exactly ONE Credit Card Bill for the month
    salary_commitments = round(fixed_bank_debits + cc_bill_budget, 2)
    pool_after_commitments = round(monthly_income - salary_commitments, 2) if monthly_income > 0 else 0.0

    # 10. Assemble category budget list
    categories_data = []
    total_proposed_var_budget = 0.0
    total_var_avg_spend = 0.0

    for c in var_categories:
        cname = c["name"]
        
        if cname in budget_map:
            db_budget = budget_map[cname]
            avg_val = float(db_budget.get("avg_spending") or computed_avgs.get(cname, 0.0))
            proposed_val = float(db_budget.get("proposed_budget") or 0.0)
        elif cname in view_map:
            v_row = view_map[cname]
            avg_val = float(v_row.get("avg_spending") or computed_avgs.get(cname, 0.0))
            proposed_val = float(v_row.get("proposed_budget") or 0.0)
        else:
            avg_val = computed_avgs.get(cname, 0.0)
            proposed_val = avg_val

        # Always calculate spending and remaining from month_spent_by_cat for the selected month
        spent_val = round(month_spent_by_cat.get(cname, 0.0), 2)
        remaining_val = round(proposed_val - spent_val, 2)
        pct_used = round((spent_val / proposed_val * 100), 1) if proposed_val > 0 else 0.0

        if spent_val > proposed_val and proposed_val > 0:
            cat_status = "over"
        elif pct_used >= 80:
            cat_status = "warning"
        else:
            cat_status = "ok"

        total_proposed_var_budget += proposed_val
        total_var_avg_spend += avg_val

        categories_data.append({
            "category": cname,
            "category_type": "variable",
            "avg_spending": round(avg_val, 2),
            "proposed_budget": round(proposed_val, 2),
            "this_month_spent": spent_val,
            "remaining": remaining_val,
            "percent_used": pct_used,
            "status": cat_status,
        })

    # Sort: highest proposed budget first
    categories_data.sort(key=lambda x: x["proposed_budget"], reverse=True)

    total_proposed_var_budget = round(total_proposed_var_budget, 2)
    total_var_avg_spend = round(total_var_avg_spend, 2)
    
    # Total Budgeted Outflow incorporates Fixed Obligations + Variable Budgets
    # Under standard financial planning (Accrual/Lifestyle), category budgets cover ALL spending (both debit & credit card).
    # Fixed obligations (Rent, EMIs, Cook, subscriptions) + Variable Budgets (Food, Groceries, Shopping, Travel) = Total Planned Outflow.
    total_budgeted_outflow = round(fixed_obligations_total + total_proposed_var_budget, 2)

    # 11. Savings calculations
    # 11A. Based on Proposed Budgets (Lifestyle Planning / Net Worth Target):
    savings_from_budget = round(monthly_income - total_budgeted_outflow, 2) if monthly_income > 0 else 0.0
    savings_rate_budget = round((savings_from_budget / monthly_income * 100), 1) if monthly_income > 0 else 0.0

    # 11B. Based on True Spending so far (Accrual Basis - Real Consumption across debit + credit):
    # Standard Accounting: Real Savings = Monthly Income - Real Spend (Credit card bill is an internal transfer, not double counted)
    real_outflow_so_far = round(current_month_true_spend, 2)
    savings_from_actual = round(monthly_income - current_month_true_spend, 2) if monthly_income > 0 else 0.0
    savings_rate_actual = round((savings_from_actual / monthly_income * 100), 1) if monthly_income > 0 else 0.0

    # Projected month-end spend & savings based on burn rate:
    projected_month_spend = round((current_month_true_spend / days_elapsed) * days_in_month, 2) if days_elapsed > 0 else 0.0
    projected_savings = round(monthly_income - projected_month_spend, 2) if monthly_income > 0 else 0.0
    projected_savings_rate = round((projected_savings / monthly_income * 100), 1) if monthly_income > 0 else 0.0

    # 11C. Based on Bank Cash Outflow (Cash Flow Basis - Bank Account Liquidity):
    # Cash Flow Accounting: Cash Remaining = Monthly Income - (Direct Debits + CC Bill Paid)
    cash_savings_actual = round(monthly_income - current_month_bank_outflow, 2) if monthly_income > 0 else 0.0
    cash_savings_rate = round((cash_savings_actual / monthly_income * 100), 1) if monthly_income > 0 else 0.0
    projected_bank_outflow = round((current_month_bank_outflow / days_elapsed) * days_in_month, 2) if days_elapsed > 0 else 0.0
    projected_cash_savings = round(monthly_income - projected_bank_outflow, 2) if monthly_income > 0 else 0.0
    projected_cash_savings_rate = round((projected_cash_savings / monthly_income * 100), 1) if monthly_income > 0 else 0.0

    return {
        "monthly_income": monthly_income,
        "categories": categories_data,
        "budgets": categories_data,
        "totals": {
            "total_proposed_variable_budget": total_proposed_var_budget,
            "total_variable_avg_spending": total_var_avg_spend,
            "current_month_variable_spent": round(current_month_var_spend, 2),
            "fixed_bank_debits": fixed_bank_debits,
            "fixed_credit_obligations": fixed_credit_obligations,
            "fixed_obligations": fixed_bank_debits,
            "fixed_obligations_total": fixed_obligations_total,
            "credit_card_bill_budget": cc_bill_budget,
            "credit_card_bill_source": cc_bill_source,
            "credit_card_bill_paid": round(credit_card_bill_payments, 2),
            "historical_avg_cc_bill": historical_avg_cc_bill,
            "salary_commitments": salary_commitments,
            "pool_after_commitments": pool_after_commitments,
            "current_month_fixed_spent": round(current_month_fixed_spend_net, 2),
            "current_month_total_spent": current_month_true_spend,
            "credit_card_bill_payments": round(credit_card_bill_payments, 2),
            "debit_spend_net": round(debit_spend_net, 2),
            "credit_spend_total": round(credit_spend_total, 2),
            "bank_cash_outflow": current_month_bank_outflow,
            "bank_cash_remaining": cash_savings_actual,
            "total_budgeted_outflow": total_budgeted_outflow,
            "credit_card_cycle": {
                "cycle_start": str(cycle_start),
                "cycle_end": str(cycle_end),
                "cycle_range": f"{cycle_start.strftime('%b %d')} – {cycle_end.strftime('%b %d')}",
                "cycle_total": current_cycle_credit_total,
                "prev_cycle_start": str(prev_cycle_start),
                "prev_cycle_end": str(prev_cycle_end),
                "prev_cycle_range": f"{prev_cycle_start.strftime('%b %d')} – {prev_cycle_end.strftime('%b %d')}",
                "prev_cycle_total": prev_cycle_credit_total,
            }
        },
        "savings": {
            "monthly_income": monthly_income,
            "based_on_budget": {
                "budgeted_outflow": total_budgeted_outflow,
                "projected_savings": savings_from_budget,
                "savings_rate": savings_rate_budget,
                "fixed_bank_debits": fixed_bank_debits,
                "credit_card_bill": cc_bill_budget,
                "salary_commitments": salary_commitments,
                "available_pool": pool_after_commitments,
            },
            "based_on_spending": {
                "current_spent": current_month_true_spend,
                "current_savings": savings_from_actual,
                "current_savings_rate": savings_rate_actual,
                "projected_month_end_spend": projected_month_spend,
                "projected_month_end_savings": projected_savings,
                "projected_savings_rate": projected_savings_rate,
            },
            "based_on_cash_flow": {
                "current_spent": current_month_bank_outflow,
                "current_savings": cash_savings_actual,
                "current_savings_rate": cash_savings_rate,
                "projected_month_end_spend": projected_bank_outflow,
                "projected_month_end_savings": projected_cash_savings,
                "projected_savings_rate": projected_cash_savings_rate,
            }
        },
        "meta": {
            "month": month_start.strftime("%B %Y"),
            "selected_month": selected_month,
            "is_current_month": is_current_month,
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "schema_ready": schema_ready,
            "default_monthly_income": default_monthly_income,
            "has_month_salary_override": (selected_month in get_monthly_salaries()),
        }
    }


@app.patch("/budgets/{category}")
def update_budget(category: str, payload: BudgetUpdateInput, _=Depends(verify_secret)):
    """Update proposed budget and/or avg spending for a specific category."""
    cat_res = (
        supabase.table("categories")
        .select("name")
        .ilike("name", category)
        .limit(1)
        .execute()
    )
    if not cat_res.data:
        raise HTTPException(status_code=404, detail=f"Category not found: {category}")
    canonical_name = cat_res.data[0]["name"]

    try:
        existing = (
            supabase.table("budgets")
            .select("*")
            .ilike("category", canonical_name)
            .limit(1)
            .execute()
        )

        updates = {}
        if payload.proposed_budget is not None:
            updates["proposed_budget"] = max(0.0, float(payload.proposed_budget))

        if not updates:
            raise HTTPException(status_code=422, detail="No fields to update")

        if existing.data:
            match_col = "id" if "id" in existing.data[0] else "category"
            match_val = existing.data[0]["id"] if match_col == "id" else canonical_name
            res = supabase.table("budgets").update(updates).eq(match_col, match_val).execute()
            return {"status": "ok", "budget": res.data[0] if res.data else None}
        else:
            new_row = {
                "category": canonical_name,
                "proposed_budget": updates.get("proposed_budget", 0.0),
            }
            res = supabase.table("budgets").insert(new_row).execute()
            return {"status": "ok", "budget": res.data[0] if res.data else None}
    except Exception as e:
        err_msg = str(e)
        if "PGRST205" in err_msg or "budgets" in err_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="The 'budgets' table does not exist in the database yet. Please run the SQL migration in Supabase SQL Editor."
            )
        raise HTTPException(status_code=500, detail=f"Could not update budget: {err_msg}")


@app.post("/budgets/batch")
def update_budgets_batch(payload: BudgetBatchUpdateInput, _=Depends(verify_secret)):
    """Update proposed budgets for multiple categories in a single call."""
    if not payload.budgets:
        raise HTTPException(status_code=422, detail="No budgets provided")

    results = {}
    errors = {}

    for cat_name, amount in payload.budgets.items():
        try:
            val = max(0.0, float(amount))
            existing = (
                supabase.table("budgets")
                .select("category")
                .ilike("category", cat_name)
                .limit(1)
                .execute()
            )
            if existing.data:
                canonical_name = existing.data[0]["category"]
                supabase.table("budgets").update({"proposed_budget": val}).eq("category", canonical_name).execute()
                results[canonical_name] = val
            else:
                cat_res = supabase.table("categories").select("name").ilike("name", cat_name).limit(1).execute()
                canonical_name = cat_res.data[0]["name"] if cat_res.data else cat_name
                supabase.table("budgets").insert({"category": canonical_name, "proposed_budget": val}).execute()
                results[canonical_name] = val
        except Exception as e:
            errors[cat_name] = str(e)

    if errors and not results:
        raise HTTPException(status_code=500, detail=f"Failed to update budgets: {errors}")

    return {"status": "ok", "updated": results, "errors": errors}


@app.post("/budgets/sync-averages")
def sync_budget_averages(_=Depends(verify_secret)):
    """Recalculate historical average spend per month for all variable categories and sync to budgets table."""
    try:
        computed_avgs = compute_variable_category_averages()
        cat_res = supabase.table("categories").select("name").eq("type", "variable").eq("is_active", True).execute()
        active_cats = [c["name"] for c in (cat_res.data or [])]

        updated_count = 0
        for cname in active_cats:
            avg_val = computed_avgs.get(cname, 0.0)
            existing = supabase.table("budgets").select("category, proposed_budget").ilike("category", cname).limit(1).execute()
            if existing.data:
                row = existing.data[0]
                upd = {}
                if float(row.get("proposed_budget") or 0.0) == 0.0:
                    upd["proposed_budget"] = avg_val
                if upd:
                    supabase.table("budgets").update(upd).eq("category", row["category"]).execute()
            else:
                supabase.table("budgets").insert({
                    "category": cname,
                    "proposed_budget": avg_val,
                }).execute()
            updated_count += 1

        return {"status": "ok", "synced_categories": updated_count, "averages": computed_avgs}
    except Exception as e:
        err_msg = str(e)
        if "PGRST205" in err_msg or "budgets" in err_msg.lower():
            raise HTTPException(
                status_code=400,
                detail="The 'budgets' table does not exist in the database yet. Please run the SQL migration in Supabase SQL Editor."
            )
        raise HTTPException(status_code=500, detail=f"Could not sync averages: {err_msg}")


@app.get("/budget-settings")
def get_budget_settings(month: str | None = None, _=Depends(verify_secret)):
    """Get current monthly income and budget settings, optionally for a specific month."""
    today = get_ist_today()
    target_month = month.strip() if month and re.match(r"^\d{4}-\d{2}$", month.strip()) else today.strftime("%Y-%m")
    default_income = 0.0
    try:
        res = supabase.table("budget_settings").select("*").eq("id", 1).execute()
        if res.data:
            default_income = float(res.data[0].get("monthly_income") or 0.0)
    except Exception:
        pass

    salary = get_salary_for_month(target_month, default_income)
    return {
        "status": "ok",
        "month": target_month,
        "monthly_income": salary,
        "default_income": default_income,
        "has_override": (target_month in get_monthly_salaries()),
        "settings": {"monthly_income": salary}
    }


@app.post("/budget-settings")
@app.patch("/budget-settings")
@app.patch("/budgets/salary")
def update_budget_settings(payload: BudgetSettingsInput, month: str | None = None, _=Depends(verify_secret)):
    """Update user's monthly income. If month is provided, sets salary for that specific month.
    If month is current or future (or omitted), also updates the baseline default income."""
    income = max(0.0, float(payload.monthly_income))
    today = get_ist_today()
    cur_month_str = today.strftime("%Y-%m")

    target_month = payload.month or month
    if not target_month or not re.match(r"^\d{4}-\d{2}$", target_month.strip()):
        target_month = cur_month_str
    target_month = target_month.strip()

    is_current_or_future = (target_month >= cur_month_str)

    # Save to monthly_salaries store
    saved_salary = save_monthly_salary(target_month, income, update_default=is_current_or_future)

    return {
        "status": "ok",
        "month": target_month,
        "monthly_income": saved_salary,
        "updated_default": is_current_or_future,
        "settings": {"monthly_income": saved_salary}
    }


@app.get("/budgets/history")
def get_budgets_history(_=Depends(verify_secret)):
    """Fetch monthly historical breakdown of salary, true spend, CC bill paid,
    bank cash outflow, cash saved, real saved, and budget target performance."""
    today = get_ist_today()
    cur_month_str = today.strftime("%Y-%m")

    # 1. Baseline income & salaries map
    default_income = 0.0
    try:
        bs_res = supabase.table("budget_settings").select("monthly_income").eq("id", 1).execute()
        if bs_res.data:
            default_income = float(bs_res.data[0].get("monthly_income") or 0.0)
    except Exception:
        pass
    salaries_map = get_monthly_salaries()

    # 2. Fixed bank debits from active standing instructions
    fixed_bank_debits = 0.0
    try:
        si_res = supabase.table("standing_instructions").select("amount, expense_type").eq("is_active", True).execute()
        fixed_bank_debits = round(sum(float(s["amount"] or 0.0) for s in (si_res.data or []) if s.get("expense_type") != "credit"), 2)
    except Exception:
        pass

    # 3. Variable budget total from budgets table
    var_budget_total = 0.0
    try:
        b_res = supabase.table("budgets").select("proposed_budget").execute()
        var_budget_total = round(sum(float(b.get("proposed_budget") or 0.0) for b in (b_res.data or [])), 2)
    except Exception:
        pass

    # 4. Fetch monthly_summary view for historical spend
    ms_map = {}
    try:
        ms_res = supabase.table("monthly_summary").select("*").order("month", desc=True).limit(24).execute()
        for r in (ms_res.data or []):
            ms_map[r["month"][:7]] = float(r.get("total_amount") or 0.0)
    except Exception:
        pass

    # 5. Fetch CC bill payments by month
    from collections import defaultdict
    cc_by_month = defaultdict(float)
    try:
        cc_res = (
            supabase.table("expenses_flat")
            .select("amount, expense_date")
            .eq("category", "Credit Card")
            .eq("expense_type", "debit")
            .neq("status", "pending_review")
            .execute()
        )
        for r in (cc_res.data or []):
            m = r["expense_date"][:7]
            cc_by_month[m] += float(r.get("amount") or 0.0)
    except Exception:
        pass

    # 6. Fetch credit card spend by month
    credit_by_month = defaultdict(float)
    try:
        credit_res = (
            supabase.table("expenses_flat")
            .select("amount, expense_date")
            .eq("expense_type", "credit")
            .neq("status", "pending_review")
            .order("expense_date", desc=True)
            .limit(5000)
            .execute()
        )
        for r in (credit_res.data or []):
            m = r["expense_date"][:7]
            credit_by_month[m] += float(r.get("amount") or 0.0)
    except Exception:
        pass

    # 7. Check if upcoming month has expenses or CC bills
    upcoming_month_str = (date(today.year + (1 if today.month == 12 else 0), 1 if today.month == 12 else today.month + 1, 1)).strftime("%Y-%m")
    all_months = set(ms_map.keys()) | set(cc_by_month.keys()) | set(salaries_map.keys())
    all_months.add(cur_month_str)
    all_months.add(upcoming_month_str)

    history_items = []
    total_cash_saved_all = 0.0
    total_salary_all = 0.0
    completed_months_count = 0

    for m in sorted(all_months, reverse=True):
        if m < "2025-05":
            continue

        y, mo = map(int, m.split("-"))
        m_date = date(y, mo, 1)
        m_name = m_date.strftime("%B %Y")
        is_cur = (m == cur_month_str)
        is_future = (m > cur_month_str)

        salary = salaries_map.get(m, default_income)

        real_spend = round(ms_map.get(m, 0.0), 2)
        credit_spend = round(credit_by_month.get(m, 0.0), 2)
        debit_spend = max(0.0, round(real_spend - credit_spend, 2))

        cc_bill_paid = round(cc_by_month.get(m, 0.0), 2)
        bank_outflow = round(debit_spend + cc_bill_paid, 2)

        cash_saved = round(salary - bank_outflow, 2) if salary > 0 else 0.0
        cash_savings_rate = round((cash_saved / salary * 100), 1) if salary > 0 else 0.0

        real_saved = round(salary - real_spend, 2) if salary > 0 else 0.0
        real_savings_rate = round((real_saved / salary * 100), 1) if salary > 0 else 0.0

        target_outflow = round(fixed_bank_debits + var_budget_total, 2)
        target_savings = round(salary - target_outflow, 2) if salary > 0 else 0.0
        target_savings_rate = round((target_savings / salary * 100), 1) if salary > 0 else 0.0

        # Include completed past months in totals
        if not is_future and not is_cur and bank_outflow > 0:
            total_cash_saved_all += cash_saved
            total_salary_all += salary
            completed_months_count += 1

        history_items.append({
            "month": m,
            "month_name": m_name,
            "salary": salary,
            "has_custom_salary": (m in salaries_map),
            "is_current": is_cur,
            "is_future": is_future,
            "real_spend": real_spend,
            "credit_spend": credit_spend,
            "debit_spend": debit_spend,
            "credit_card_bill_paid": cc_bill_paid,
            "bank_cash_outflow": bank_outflow,
            "bank_cash_saved": cash_saved,
            "bank_cash_savings_rate": cash_savings_rate,
            "real_saved": real_saved,
            "real_savings_rate": real_savings_rate,
            "fixed_bank_debits": fixed_bank_debits,
            "variable_budget": var_budget_total,
            "target_outflow": target_outflow,
            "target_savings": target_savings,
            "target_savings_rate": target_savings_rate,
        })

    avg_monthly_savings = round(total_cash_saved_all / completed_months_count, 2) if completed_months_count > 0 else 0.0
    avg_savings_rate = round((total_cash_saved_all / total_salary_all * 100), 1) if total_salary_all > 0 else 0.0

    return {
        "status": "ok",
        "history": history_items,
        "summary": {
            "total_saved": round(total_cash_saved_all, 2),
            "avg_monthly_savings": avg_monthly_savings,
            "avg_savings_rate": avg_savings_rate,
            "completed_months_count": completed_months_count,
            "default_income": default_income,
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}
