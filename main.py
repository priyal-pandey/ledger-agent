import json
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
IST = ZoneInfo("Asia/Kolkata")

RATES = {"USD": 1.0, "EUR": 1.12, "INR": 0.01223}
ORDERS = []


def parse_time(value):
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


# Keep only the latest version of each order.
latest = {}
with open("orders.ndjson", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        order = json.loads(line)
        oid = order["id"]
        if oid not in latest or parse_time(order["updated_at"]) > parse_time(latest[oid]["updated_at"]):
            latest[oid] = order

ORDERS = list(latest.values())


class Question(BaseModel):
    question: str


def money(order, target):
    source = order.get("currency", "USD").upper()
    amount = float(order.get("amount", 0))
    return amount * RATES.get(source, 1.0) / RATES[target]


def answer_question(question):
    q = question.lower()
    target = next((c for c in RATES if re.search(r"\b" + c.lower() + r"\b", q)), "USD")

    # Filter by region, product, and customer when mentioned.
    rows = ORDERS[:]

    regions = sorted({o["region"] for o in ORDERS}, key=len, reverse=True)
    for region in regions:
        if re.search(r"\b" + re.escape(region.lower()) + r"\b", q):
            rows = [o for o in rows if o["region"].lower() == region.lower()]
            break

    products = sorted({o["product"] for o in ORDERS}, key=len, reverse=True)
    for product in products:
        if product.lower() in q:
            rows = [o for o in rows if o["product"].lower() == product.lower()]
            break

    customers = sorted({o["customer"] for o in ORDERS}, key=len, reverse=True)
    for customer in customers:
        if customer.lower() in q:
            rows = [o for o in rows if o["customer"].lower() == customer.lower()]
            break

    # Filter by a named month and optional year.
    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    for name, number in months.items():
        if re.search(r"\b" + name + r"\b", q):
            year_match = re.search(r"\b(20\d{2})\b", q)
            year = int(year_match.group(1)) if year_match else None

            def in_month(o):
                dt = parse_time(o["created_at"])
                return dt.month == number and (year is None or dt.year == year)

            rows = [o for o in rows if in_month(o)]
            break

    # Handle an explicit year when no month was named.
    if not any(re.search(r"\b" + m + r"\b", q) for m in months):
        year_match = re.search(r"\b(20\d{2})\b", q)
        if year_match:
            year = int(year_match.group(1))
            rows = [o for o in rows if parse_time(o["created_at"]).year == year]

    # Recognize common business questions.
    if any(w in q for w in ("refund", "refunded", "refunds")):
        selected = [o for o in rows if o["status"].lower() == "refunded"]
        total = sum(money(o, target) for o in selected)
        return f"Total refunds: {total:.2f} {target} across {len(selected)} refunded orders."

    paid = [o for o in rows if o["status"].lower() == "paid"]

    if any(w in q for w in ("how many customer", "number of customer", "unique customer", "customer count")):
        return f"Unique customers: {len({o['customer'] for o in rows})}."

    if any(w in q for w in ("how many order", "number of order", "order count", "count of order")):
        return f"Orders: {len(rows)}."

    if any(w in q for w in ("units sold", "quantity sold", "how many units", "total quantity")):
        qty = sum(int(o.get("qty", 0)) for o in paid)
        return f"Total units sold: {qty}."

    if any(w in q for w in ("average order value", "average sale", "average revenue")):
        total = sum(money(o, target) for o in paid)
        avg = total / len(paid) if paid else 0
        return f"Average order value: {avg:.2f} {target}."

    if any(w in q for w in ("top product", "best-selling product", "bestselling product", "most popular product", "most sold product")):
        totals = defaultdict(float)
        by_revenue = "revenue" in q or "sales" in q
        for o in paid:
            totals[o["product"]] += money(o, target) if by_revenue else int(o.get("qty", 0))
        if not totals:
            return "No matching paid orders were found."
        product, value = max(totals.items(), key=lambda x: x[1])
        unit = target if by_revenue else "units"
        return f"Top product: {product}, with {value:.2f} {unit}."

    if any(w in q for w in ("revenue", "sales", "turnover", "earn", "earnings", "income", "total amount")):
        total = sum(money(o, target) for o in paid)
        return f"Total revenue: {total:.2f} {target} across {len(paid)} paid orders."

    # Useful default for a general ledger question.
    total = sum(money(o, target) for o in paid)
    return (
        f"Ledger summary: revenue {total:.2f} {target}; "
        f"{len(paid)} paid orders; "
        f"{sum(1 for o in rows if o['status'].lower() == 'refunded')} refunded orders; "
        f"{len({o['customer'] for o in rows})} unique customers."
    )


@app.get("/")
def health():
    return {"status": "ok", "orders_loaded": len(ORDERS)}


@app.post("/")
@app.post("/answer")
def answer(data: Question):
    try:
        return {"answer": answer_question(data.question)}
    except Exception:
        return {"answer": "Unable to calculate an answer for that question."}
