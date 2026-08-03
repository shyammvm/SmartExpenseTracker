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
                },
                "required": ["category", "expense_type", "confidence"],
            },
        ),
    )

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail=f"Could not parse AI response: {response.text}")


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
    category_name = cat_result.data[0]["name"] if cat_result.data else None

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
    }

    insert_result = supabase.table("expenses").insert(row).execute()

    return {
        "status": "ok",
        "parsed": parsed,
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
    }

    insert_result = supabase.table("expenses").insert(row).execute()
    return {"status": "ok", "inserted": insert_result.data[0] if insert_result.data else None}


@app.get("/categories")
def list_categories(_=Depends(verify_secret)):
    return {"categories": get_category_names()}


@app.get("/health")
def health():
    return {"status": "ok"}