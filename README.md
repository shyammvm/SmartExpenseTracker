# 🧾 SmartExpenseTracker (Ledger)

A lightweight, mobile-first PWA & AI-powered expense tracker. It uses **Google Gemini Flash** to extract structured transaction data from raw bank SMS messages or typed text, stores records in **Supabase**, automatically flags ambiguous expenses for review, and sends email alerts via **Resend**.

---

## 🌟 Key Features

- 📱 **Mobile-First PWA & iOS Web App**: Custom receipt-styled UI optimized for iPhone standalone mode with native iOS safe-area notch insets.
- 🤖 **AI-Powered SMS & Text Parsing**: Automated parsing of bank SMS texts or manual dictation using Gemini Flash to extract amount, merchant/payee, payment type (`debit`/`credit`), and category.
- ⚠️ **Conflict Detection & Review Workflow**: Automatically flags obscure UPI merchants (e.g. `SRI SAI DREAM C`), low AI confidence (<80%), or `"Others"` category spend into a dedicated **Conflicts** review queue (`conflicts.html`) with inline **Approve**, **Edit & Approve**, or **Deny** actions.
- 📧 **Instant Email Alerts**: Sends HTML notification emails via **Resend** whenever an expense is queued for review.
- 📲 **Apple Shortcuts Integration**: Trigger seamless hands-free transaction logging directly from iOS Shortcuts or incoming SMS Automations.
- 📊 **Dashboard & Reporting Views**: Features monthly fixed vs. variable spend splits, daily burn rates, and pre-built SQL views for **Google Looker Studio** or Google Sheets.

---

## 🛠️ Tech Stack

- **Backend**: FastAPI (Python 3.11+), Google GenAI (Gemini Flash), Supabase Python Client, HTTPX
- **Frontend**: Vanilla HTML5, CSS3 (Custom Design System with CSS variables), JavaScript (Fetch API)
- **Database**: Supabase PostgreSQL (`schema.sql` with automated triggers & analytical views)
- **Email Notifications**: Resend API

---

## 🚀 Quick Setup

### 1. Environment Variables
Create a `.env` file in the project root:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
GEMINI_API_KEY=your_gemini_api_key
PARSE_ENDPOINT_SECRET=your_shared_secret
RESEND_API_KEY=re_your_resend_key
NOTIFICATION_EMAIL=your_email@domain.com
```

### 2. Database Setup
Run the SQL script in [`schema.sql`](file:///Users/shyammvm/smartexpensetracker/schema.sql) in your **Supabase SQL Editor**. This creates the `expenses` table, `categories` table, indexes, and reporting views (`expenses_flat`, `monthly_category_summary`, `monthly_fixed_vs_variable`).

### 3. Run Locally
```bash
# Create virtual environment & install requirements
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start FastAPI dev server
uvicorn main:app --reload --port 8000
```

---

## 🔗 Main Pages & Endpoints

- `index.html`: Quick entry form & daily tape receipt.
- `dashboard.html`: Category spending breakdown & monthly totals.
- `conflicts.html`: Review queue for flagged items (`status = "pending_review"`).
- `categories.html`: Manage fixed & variable spending categories.
- `POST /parse-expense`: Endpoint for iOS Shortcuts / AI text parsing.
- `POST /add-expense`: Structured manual expense entry.

---

## 📊 Analytics & Looker Studio

Connect **Google Looker Studio** to Supabase using the native PostgreSQL connector and select the **`expenses_flat`** view for instant charts, category breakdowns, and billing cycle summaries.
