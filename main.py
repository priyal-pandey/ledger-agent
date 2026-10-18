import json
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()
IST = ZoneInfo("Asia/Kolkata")

# USD value of one unit of each currency, from your exam API.
RATES = {"USD": 1.0, "EUR": 1.12, "INR": 0.01223}

MONTHS = {
    "january": "01", "february": "02", "march": "03",
    "april": "04", "may": "05", "june": "06",
    "july": "07", "august": "08", "september": "09",
    "october": "10", "november": "11", "december": "12",
}
REGIONS = ["North", "South", "East", "West", "Central"]
PRODUCTS = [
    "Air Fryer", "Rice Cooker", "Juicer", "Toaster",
    "Mixer", "Kettle", "Blender", "Grinder",
]


def parse_time(value):
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt


def load_orders():
    latest = {}

    with open("orders.ndjson", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue

            order = json.loads(line)
            oid = order["id"]
            old = latest.get(oid)

            if old is None or parse_time(order["updated_at"]) > parse_time(old["updated_at"]):
                latest[oid] = order

    rows = list(latest.values())
    df = pd.DataFrame(rows)

    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True)

    # Business dates are based on Asia/Kolkata.
    local_created = df["created_at"].dt.tz_convert("Asia/Kolkata")
    df["created_month"] = local_created.dt.strftime("%Y-%m")
    df["created_year"] = local_created.dt.year

    # Convert all amounts to USD for consistent calculations.
    df["amount_usd"] = (
        df["amount"] * df["currency"].map(RATES)
    )

    if df["amount_usd"].isna().any():
        raise ValueError("An order contains an unknown currency.")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    df.drop(columns=["created_at", "updated_at"]).to_sql(
        "orders", conn, index=False, if_exists="replace"
    )
    return conn, len(df)


db, order_count = load_orders()
print(f"Ledger ready: {order_count} unique orders")


class Question(BaseModel):
    question: str


def parse_question(question):
    q = question.lower()
    conditions = []
    params = []

    # Paid orders count as revenue; refund and void questions use their status.
    if re.search(r"\b(refund|refunded|refunds)\b", q):
        conditions.append("status = ?")
        params.append("refunded")
    elif re.search(r"\b(void|voided|cancel|cancelled|canceled)\b", q):
        conditions.append("status = ?")
        params.append("void")
    else:
        conditions.append("status = ?")
        params.append("paid")

    for region in REGIONS:
        if re.search(rf"\b{region.lower()}\b", q):
            conditions.append("region = ?")
            params.append(region)
            break

    for product in PRODUCTS:
        if re.search(rf"\b{re.escape(product.lower())}\b", q):
            conditions.append("product = ?")
            params.append(product)
            break

    customer_match = re.search(r"\b(c\d{4})\b", q)
    if customer_match:
        conditions.append("customer = ?")
        params.append(customer_match.group(1).upper())

    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    month = None
    for name, number in MONTHS.items():
        if re.search(rf"\b{name}\b", q):
            month = number
            break

    if month:
        # If a month is mentioned without a year, use 2026.
        year = year if year is not None else 2026
        conditions.append("created_month = ?")
        params.append(f"{year}-{month}")
    elif year is not None:
        conditions.append("created_year = ?")
        params.append(year)

    where = " AND ".join(conditions) if conditions else "1=1"

    # Output currency requested by the question; default to USD.
    target_currency = "USD"
    if re.search(r"\b(eur|euro|euros)\b", q):
        target_currency = "EUR"
    elif re.search(r"\b(inr|rupees?|₹)\b", q):
        target_currency = "INR"

    # Questions asking for the best-performing product/customer/region.
    if any(x in q for x in [
        "which product", "what product", "top product",
        "top-selling product", "best-selling product",
        "bestselling product", "product earned the most",
        "most popular product",
    ]):
        metric = "SUM(qty)" if any(
            x in q for x in ["qty", "quantity", "units", "most sold"]
        ) else "SUM(amount_usd)"
        sql = (
            f"SELECT product FROM orders WHERE {where} "
            f"GROUP BY product ORDER BY {metric} DESC, product ASC LIMIT 1"
        )
        return sql, params, "string", target_currency

    if any(x in q for x in [
        "which customer", "what customer", "top customer",
        "customer spent the most",
    ]):
        sql = (
            f"SELECT customer FROM orders WHERE {where} "
            "GROUP BY customer ORDER BY SUM(amount_usd) DESC, customer ASC LIMIT 1"
        )
        return sql, params, "string", target_currency

    if any(x in q for x in [
        "which region", "what region", "top region",
        "region earned the most",
    ]):
        sql = (
            f"SELECT region FROM orders WHERE {where} "
            "GROUP BY region ORDER BY SUM(amount_usd) DESC, region ASC LIMIT 1"
        )
        return sql, params, "string", target_currency

    if any(x in q for x in [
        "distinct customer", "different customer",
        "unique customer", "how many customer",
    ]):
        sql = f"SELECT COUNT(DISTINCT customer) FROM orders WHERE {where}"
        return sql, params, "int", target_currency

    if "average" in q:
        sql = f"SELECT COALESCE(AVG(amount_usd), 0) FROM orders WHERE {where}"
        return sql, params, "money", target_currency

    if any(x in q for x in [
        "how many orders", "how many order", "count the",
        "number of orders", "number of order", "order count",
    ]):
        sql = f"SELECT COUNT(*) FROM orders WHERE {where}"
        return sql, params, "int", target_currency

    if any(x in q for x in [
        "how many units", "total quantity", "total units",
        "quantity sold", "units sold",
    ]):
        sql = f"SELECT COALESCE(SUM(qty), 0) FROM orders WHERE {where}"
        return sql, params, "int", target_currency

    # Default question: total revenue, earnings, or refund amount.
    sql = f"SELECT COALESCE(SUM(amount_usd), 0) FROM orders WHERE {where}"
    return sql, params, "money", target_currency


@app.get("/")
def health():
    return {"status": "ok", "orders_loaded": order_count}


@app.post("/")
@app.post("/answer")
def answer_question(data: Question):
    if not data.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    sql, params, answer_type, target_currency = parse_question(data.question)
    cursor = db.cursor()
    cursor.execute(sql, params)
    row = cursor.fetchone()
    value = row[0] if row and row[0] is not None else 0

    if answer_type == "string":
        result = str(value) if value else "No matching result"
    elif answer_type == "int":
        result = int(value)
    else:
        # Convert from USD into the requested currency.
        result = round(float(value) / RATES[target_currency], 2)

    print(f"Question: {data.question}")
    print(f"SQL: {sql}")
    print(f"Answer: {result}")

    # Return the raw number/string, not a sentence.
    return {"answer": result}
