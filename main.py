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
def get_recent_expenses(limit: int = 30, _=Depends(verify_secret)):
    """Fetch the most recent N (default 30) expenses for editing."""
    result = (
        supabase.table("expenses_flat")
        .select("*")
        .order("expense_date", desc=True)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return {"expenses": result.data}


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
    today_total = sum(r["amount"] for r in today_rows.data)

    month_rows = (
        supabase.table("expenses_flat")
        .select("amount, category_type, expense_date")
        .gte("expense_date", str(month_start))
        .lte("expense_date", str(today))
        .neq("status", "pending_review")
        .execute()
    )
    month_total = sum(r["amount"] for r in month_rows.data)
    month_variable = sum(r["amount"] for r in month_rows.data if r["category_type"] == "variable")
    month_fixed = sum(r["amount"] for r in month_rows.data if r["category_type"] == "fixed")

    days_elapsed = today.day  # 1st of month = day 1, so this is correct as a divisor
    avg_daily_variable = round(month_variable / days_elapsed, 2) if days_elapsed else 0

    # Aggregate daily variable spend breakdown for widget chart (zero extra DB queries)
    daily_spend_map = {}
    for r in month_rows.data:
        d_str = r.get("expense_date")
        if d_str and r.get("category_type") == "variable":
            daily_spend_map[d_str] = round(daily_spend_map.get(d_str, 0.0) + r["amount"], 2)

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

    return {
        "today_total": round(today_total, 2),
        "today_variable_total": round(daily_spend_map.get(str(today), 0.0), 2),
        "month_total": round(month_total, 2),
        "month_fixed_total": round(month_fixed, 2),
        "month_variable_total": round(month_variable, 2),
        "avg_daily_variable_spend": avg_daily_variable,
        "daily_history": daily_history,
    }


@app.get("/summary/dashboard")
def summary_dashboard(_=Depends(verify_secret)):
    """Category-wise breakdown with credit/debit split, last month MTD comparison,
    and current credit card billing cycle total."""
    today = get_ist_today()
    month_start = today.replace(day=1)

    # 1. Last month till date calculation
    last_month_end_prev = month_start - timedelta(days=1)
    last_month_start = last_month_end_prev.replace(day=1)
    max_days_last_month = calendar.monthrange(last_month_start.year, last_month_start.month)[1]
    target_day = min(today.day, max_days_last_month)
    last_month_till_date_end = date(last_month_start.year, last_month_start.month, target_day)

    # 2. Credit card billing cycle calculation (16th to 15th)
    if today.day <= 15:
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
        .lte("expense_date", str(today))
        .neq("status", "pending_review")
        .execute()
    )

    # Fetch last month till date rows
    last_month_rows = (
        supabase.table("expenses_flat")
        .select("amount")
        .gte("expense_date", str(last_month_start))
        .lte("expense_date", str(last_month_till_date_end))
        .neq("status", "pending_review")
        .execute()
    )
    last_month_mtd_total = sum(r["amount"] for r in last_month_rows.data)

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
    credit_cycle_total = sum(r["amount"] for r in credit_cycle_rows.data)

    by_category: dict[str, dict] = {}
    for r in this_month_rows.data:
        cat = r["category"]
        etype = r.get("expense_type", "debit")
        entry = by_category.setdefault(cat, {
            "category": cat,
            "type": r["category_type"],
            "total": 0.0,
            "debit_total": 0.0,
            "credit_total": 0.0,
            "count": 0
        })
        entry["total"] += r["amount"]
        if etype == "credit":
            entry["credit_total"] += r["amount"]
        else:
            entry["debit_total"] += r["amount"]
        entry["count"] += 1

    categories = sorted(by_category.values(), key=lambda c: c["total"], reverse=True)
    for c in categories:
        c["total"] = round(c["total"], 2)
        c["debit_total"] = round(c["debit_total"], 2)
        c["credit_total"] = round(c["credit_total"], 2)

    month_total = round(sum(c["total"] for c in categories), 2)

    return {
        "month": month_start.strftime("%B %Y"),
        "month_total": month_total,
        "last_month_mtd_total": round(last_month_mtd_total, 2),
        "last_month_mtd_range": f"{last_month_start.strftime('%b 1')} – {last_month_till_date_end.strftime('%b %d')}",
        "credit_cycle_total": round(credit_cycle_total, 2),
        "credit_cycle_range": f"{cycle_start.strftime('%b 16')} – {cycle_end.strftime('%b 15')}",
        "categories": categories,
    }



@app.get("/health")
def health():
    return {"status": "ok"}
