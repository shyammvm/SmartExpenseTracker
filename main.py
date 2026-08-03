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
from datetime import date

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from supabase import create_client, Client
from google import genai
from google.genai import types

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]  # backend only, bypasses RLS
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]  # get a free key at aistudio.google.com

# Optional shared secret so randos can't POST to your public endpoint.
# Set this in your hosting env vars and in the Shortcut's request headers.
PARSE_ENDPOINT_SECRET = os.environ.get("PARSE_ENDPOINT_SECRET")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
gemini = genai.Client(api_key=GEMINI_API_KEY)

# "gemini-flash-latest" is a Google-maintained alias that always points to
# their current Flash model, so this stays current without code changes.
GEMINI_MODEL = "gemini-flash-latest"

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


def verify_secret(x_endpoint_secret: str | None = Header(default=None, alias="x-endpoint-secret")):
    if PARSE_ENDPOINT_SECRET and x_endpoint_secret != PARSE_ENDPOINT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_category_names() -> list[str]:
    result = supabase.table("categories").select("name").eq("is_active", True).execute()
    return [row["name"] for row in result.data]


def parse_with_gemini(text: str, category_names: list[str]) -> dict:
    """Ask Gemini Flash to extract structured expense fields from raw text."""

    system_prompt = f"""You extract structured expense data from raw text, which is
either a message the user typed themselves, or a bank transaction SMS.

Available categories (pick the closest match, exactly as spelled): {category_names}

expense_type is the PAYMENT METHOD, not direction of money: "credit" if paid
using a credit card, "debit" if paid via debit card, UPI, cash, or directly
from a bank account. Look for cues like "credit card", "Card ending", or the
card/account type mentioned in bank SMS. Default to "debit" if unclear --
most day-to-day spending is not on a credit card.

This table only tracks money going OUT. If the text describes money coming
IN instead (salary credited, a refund, cashback received, someone paying you
back), that is NOT an expense -- set amount to null.

"expense" is a short description of what this was for -- a merchant/payee name
if there is one (e.g. "Swiggy", "Uber"), otherwise a brief description of the
spend (e.g. "cash withdrawal", "friend's birthday gift").

needs_review: Set to true if:
1. The merchant or payee name in the bank SMS is raw, cryptic, or an individual's UPI name (e.g. "SRI SAI DREAM C", "RAMESH KUMAR", raw VPA code), where the actual nature of the expense cannot be determined from text alone.
2. The closest category picked is "Others" or "Other", or your category choice is an educated guess.
3. The text is ambiguous or lacks clear merchant/expense details.

review_reason: A brief string explaining why needs_review was set to true (e.g. "Categorized as Others", "Cryptic merchant name", "Ambiguous expense details").

If the text does not appear to describe an outgoing expense, set amount to null."""

    # response_schema forces Gemini to return well-formed JSON matching this
    # shape, so there's no markdown-fence stripping or malformed-JSON risk.
    response = gemini.models.generate_content(
        model=GEMINI_MODEL,
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


class ExpenseReviewInput(BaseModel):
    category: str | None = None
    expense: str | None = None
    amount: float | None = None
    expense_type: str | None = None
    status: str = "approved"


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


@app.post("/parse-expense")
def parse_expense(payload: ExpenseInput, _=Depends(verify_secret)):
    category_names = get_category_names()
    parsed = parse_with_gemini(payload.text, category_names)

    if parsed.get("amount") is None:
        raise HTTPException(status_code=422, detail="Text did not appear to describe an expense")

    # confirm the category exists (case-insensitive) and use its canonical
    # spelling -- no separate id lookup needed now that category is the key
    cat_result = (
        supabase.table("categories")
        .select("name")
        .ilike("name", parsed["category"])
        .limit(1)
        .execute()
    )
    category_name = cat_result.data[0]["name"] if cat_result.data else parsed["category"]

    category_lower = (category_name or "").lower()
    is_others = category_lower in ("others", "other")
    needs_review = parsed.get("needs_review", False) or is_others or (parsed.get("confidence", 1.0) < 0.8)

    review_reason = parsed.get("review_reason")
    if is_others and not review_reason:
        review_reason = "Categorized as Others"
    elif needs_review and not review_reason:
        review_reason = "Obscure merchant or low AI confidence"

    status = "pending_review" if needs_review else "approved"

    row = {
        "amount": parsed["amount"],
        "expense": parsed.get("expense"),
        "category": category_name,
        "note": parsed.get("note"),
        "source": payload.source,
        "raw_text": payload.text,
        "expense_type": parsed.get("expense_type", "debit"),
        "ai_confidence": parsed.get("confidence"),
        "expense_date": str(date.today()),
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

    return {
        "status": "ok",
        "parsed": parsed,
        "needs_review": needs_review,
        "inserted": insert_result.data[0] if insert_result.data else None,
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

    row = {
        "amount": payload.amount,
        "expense": payload.expense,
        "category": category_name,
        "note": payload.note,
        "source": "manual",
        "expense_type": payload.expense_type,
        "expense_date": payload.expense_date or str(date.today()),
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


@app.get("/summary/entry-page")
def summary_entry_page(_=Depends(verify_secret)):
    """Quick stats for the entry page: today's total, this month's total,
    fixed vs variable split, and average daily variable spend so far this
    month (a rough day-to-day burn-rate indicator)."""
    today = date.today()
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
        .select("amount, category_type")
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

    return {
        "today_total": round(today_total, 2),
        "month_total": round(month_total, 2),
        "month_fixed_total": round(month_fixed, 2),
        "month_variable_total": round(month_variable, 2),
        "avg_daily_variable_spend": avg_daily_variable,
    }


@app.get("/summary/dashboard")
def summary_dashboard(_=Depends(verify_secret)):
    """Category-wise breakdown for the current month, for the dashboard page."""
    today = date.today()
    month_start = today.replace(day=1)

    rows = (
        supabase.table("expenses_flat")
        .select("amount, category, category_type")
        .gte("expense_date", str(month_start))
        .lte("expense_date", str(today))
        .neq("status", "pending_review")
        .execute()
    )

    by_category: dict[str, dict] = {}
    for r in rows.data:
        cat = r["category"]
        entry = by_category.setdefault(cat, {"category": cat, "type": r["category_type"], "total": 0, "count": 0})
        entry["total"] += r["amount"]
        entry["count"] += 1

    categories = sorted(by_category.values(), key=lambda c: c["total"], reverse=True)
    for c in categories:
        c["total"] = round(c["total"], 2)

    month_total = round(sum(c["total"] for c in categories), 2)

    return {
        "month": month_start.strftime("%Y-%m"),
        "month_total": month_total,
        "categories": categories,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
