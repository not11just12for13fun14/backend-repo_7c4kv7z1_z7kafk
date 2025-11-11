import os
import time
from typing import List, Optional, Literal
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import requests

app = FastAPI(title="Crypto SIP Calculator API", description="Backend API for real-time crypto CAGR and SIP/Lump Sum projections using CoinGecko data.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Simple in-memory cache to reduce API calls
_cache = {"coins": {"data": None, "ts": 0}, "rates": {"data": None, "ts": 0}}
CACHE_TTL = 60 * 10  # 10 minutes


class ProjectionRequest(BaseModel):
    type: Literal["sip", "lump"]
    amount: float = Field(..., gt=0)
    years: float = Field(..., gt=0, le=70)
    cagr: float = Field(..., ge=-0.99, le=10)  # annual rate as decimal
    frequency: Literal["monthly", "yearly"] = "monthly"


class ProjectionPoint(BaseModel):
    year: int
    invested: float
    value: float


class ProjectionResponse(BaseModel):
    total_invested: float
    final_value: float
    profit: float
    cagr: float
    years: float
    series: List[ProjectionPoint]


@app.get("/")
def read_root():
    return {"message": "Crypto SIP Calculator API is running"}


@app.get("/api/coins")
def get_coins(search: Optional[str] = Query(None, description="Search text for coin name or symbol")):
    now = time.time()
    data = _cache["coins"]["data"]
    if not data or now - _cache["coins"]["ts"] > CACHE_TTL:
        # Fetch top market cap coins (first 2 pages = 500 coins)
        all_coins = []
        for page in [1, 2]:
            r = requests.get(
                f"{COINGECKO_BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "sparkline": "false",
                    "price_change_percentage": "24h",
                },
                timeout=15,
            )
            r.raise_for_status()
            all_coins.extend(r.json())
        # Map to simpler structure
        data = [
            {
                "id": c.get("id"),
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "image": c.get("image"),
                "current_price": c.get("current_price"),
                "market_cap_rank": c.get("market_cap_rank"),
            }
            for c in all_coins
        ]
        _cache["coins"] = {"data": data, "ts": now}

    if search:
        s = search.lower()
        data = [c for c in data if s in (c["name"] or "").lower() or s in (c["symbol"] or "").lower()]
    return {"count": len(data), "coins": data}


@app.get("/api/rates")
def get_fx_rates():
    # Using CoinGecko exchange rates endpoint
    now = time.time()
    data = _cache["rates"]["data"]
    if not data or now - _cache["rates"]["ts"] > CACHE_TTL:
        r = requests.get(f"{COINGECKO_BASE}/exchange_rates", timeout=15)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        # Build common fiat mapping vs USD
        usd_per_unit = {k: v.get("value") for k, v in rates.items()}  # value = BTC per unit; but this endpoint is BTC-based
        # Convert to USD-based using USD rate in BTC units
        btc_per_usd = rates.get("usd", {}).get("value")  # BTC per 1 USD
        if not btc_per_usd:
            data = {"USD": 1.0}
        else:
            # For each fiat, value is BTC per 1 unit. So unit_in_usd = (btc_per_unit / btc_per_usd)
            out = {}
            for code, meta in rates.items():
                if meta.get("type") == "fiat":
                    unit_in_usd = meta.get("value", 0) / btc_per_usd
                    out[code.upper()] = unit_in_usd
            data = out
        _cache["rates"] = {"data": data, "ts": now}
    return {"base": "USD", "rates": data}


@app.get("/api/cagr")
def get_cagr(coin_id: str, years: float = Query(5.0, gt=0, le=70), currency: str = Query("usd")):
    days = int(years * 365)
    r = requests.get(
        f"{COINGECKO_BASE}/coins/{coin_id}/market_chart",
        params={"vs_currency": currency, "days": days, "interval": "daily"},
        timeout=30,
    )
    if r.status_code != 200:
        return {"coin_id": coin_id, "years": years, "currency": currency, "cagr": None, "error": r.text}
    prices = r.json().get("prices", [])
    if len(prices) < 2:
        return {"coin_id": coin_id, "years": years, "currency": currency, "cagr": None, "error": "Insufficient data"}

    start_price = prices[0][1]
    end_price = prices[-1][1]
    annual_cagr = (end_price / start_price) ** (1 / years) - 1

    # Build simple year-end series for visualization
    series = []
    # pick approximately every 365th point as year end
    for i in range(0, len(prices), 365):
        idx_year = min(i, len(prices) - 1)
        ts, p = prices[idx_year]
        year_number = int(round((idx_year) / 365))
        series.append({"year": year_number, "price": p})
    series.append({"year": int(years), "price": end_price})

    return {
        "coin_id": coin_id,
        "years": years,
        "currency": currency,
        "cagr": annual_cagr,
        "start_price": start_price,
        "end_price": end_price,
        "series": series,
    }


@app.post("/api/projection", response_model=ProjectionResponse)
def projection(req: ProjectionRequest):
    years = req.years
    rate_annual = req.cagr
    if req.frequency == "monthly":
        r = (1 + rate_annual) ** (1 / 12) - 1
        n = int(round(years * 12))
        step = 1  # month
        label_div = 12
    else:
        r = rate_annual
        n = int(round(years))
        step = 1  # year
        label_div = 1

    invested = 0.0
    value = 0.0
    series: List[ProjectionPoint] = []

    if req.type == "lump":
        invested = req.amount
        value = invested * ((1 + r) ** n if req.frequency == "monthly" else (1 + r) ** n)
        # Build yearly points
        for y in range(1, int(round(years)) + 1):
            periods = y * label_div
            v = invested * ((1 + r) ** periods)
            series.append(ProjectionPoint(year=y, invested=invested, value=v))
    else:
        # SIP monthly deposits at end of period
        amount = req.amount
        invested = 0.0
        value = 0.0
        months_total = n if req.frequency == "monthly" else n * 12
        r_month = r if req.frequency == "monthly" else (1 + rate_annual) ** (1 / 12) - 1
        for m in range(1, months_total + 1):
            invested += amount
            value = (value * (1 + r_month)) + amount  # contribution at end of month
            if m % 12 == 0:
                series.append(ProjectionPoint(year=m // 12, invested=invested, value=value))

    profit = value - invested

    return ProjectionResponse(
        total_invested=round(invested, 2),
        final_value=round(value, 2),
        profit=round(profit, 2),
        cagr=rate_annual,
        years=years,
        series=series,
    )


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": [],
    }

    try:
        from database import db
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = getattr(db, "name", "✅ Connected")
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except ImportError:
        response["database"] = "❌ Database module not found"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
