#!/usr/bin/env python3
"""
morning_brief_fetch.py
Fetches stock quotes, news headlines, and weather for the morning brief.
Outputs plain text suitable for an LLM to summarize.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
BRAVE_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
STOCKS = {"SPY": "S&P 500 ETF", "PL": "Planet Labs"}
PURPLE_KEY = os.environ.get("PURPLE_AIR_API_KEY_READ", "")

# Sunnyvale, CA — NWS forecast endpoint (pre-resolved)
NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/MTR/94,85/forecast"
PURPLE_SENSOR_ID = 13987  # Sunnyvale area sensor


def fetch_aqi():
    """Fetch air quality from Purple Air sensor."""
    url = f"https://api.purpleair.com/v1/sensors/{PURPLE_SENSOR_ID}"
    req = urllib.request.Request(url, headers={"X-API-Key": PURPLE_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    sensor = data.get("sensor", {})
    pm25 = sensor.get("pm2.5", "N/A")
    # Convert PM2.5 to AQI (simplified calculation)
    if isinstance(pm25, (int, float)):
        if pm25 <= 12:
            aqi = int((50/12) * pm25)
            category = "Good"
        elif pm25 <= 35.4:
            aqi = int(50 + (100-50)/(35.4-12) * (pm25 - 12))
            category = "Moderate"
        elif pm25 <= 55.4:
            aqi = int(100 + (150-100)/(55.4-35.4) * (pm25 - 35.4))
            category = "Unhealthy for Sensitive"
        elif pm25 <= 150.4:
            aqi = int(150 + (200-150)/(150.4-55.4) * (pm25 - 55.4))
            category = "Unhealthy"
        else:
            aqi = 201
            category = "Very Unhealthy"
    else:
        aqi = "N/A"
        category = "Unknown"
    return {"pm25": pm25, "aqi": aqi, "category": category, "location": sensor.get("name", "Sunnyvale")}


def fetch_quote(symbol):
    url = (
        f"https://www.alphavantage.co/query"
        f"?function=GLOBAL_QUOTE&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    q = data.get("Global Quote", {})
    return {
        "price": q.get("05. price", "N/A"),
        "change": q.get("09. change", "N/A"),
        "change_pct": q.get("10. change percent", "N/A"),
        "prev_close": q.get("08. previous close", "N/A"),
        "volume": q.get("06. volume", "N/A"),
    }


def fetch_news(query, count=3):
    q = urllib.parse.quote(query)
    url = f"https://api.search.brave.com/res/v1/news/search?q={q}&count={count}&freshness=pd"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": BRAVE_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    results = data.get("results", [])
    return [
        {"title": r.get("title", ""), "description": r.get("description", ""), "age": r.get("age", "")}
        for r in results
    ]


def fetch_weather():
    req = urllib.request.Request(
        NWS_FORECAST_URL,
        headers={"User-Agent": "OpenClaw/1.0 morning-brief"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return data["properties"]["periods"][:6]


def fetch_uv():
    """Fetch today's hourly UV index from Open-Meteo and return peak value and time."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=37.3688&longitude=-122.0363"
        "&hourly=uv_index&timezone=America%2FLos_Angeles&forecast_days=1"
    )
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    times = data["hourly"]["time"]
    uv_values = data["hourly"]["uv_index"]
    peak_uv = max(uv_values)
    peak_time_str = times[uv_values.index(peak_uv)]
    peak_hour = datetime.fromisoformat(peak_time_str).strftime("%-I:%M %p")
    return {"peak_uv": peak_uv, "peak_time": peak_hour}


def main():
    lines = []
    # Use Pacific time (UTC-7 PDT / UTC-8 PST); detect DST simply by month
    utc_now = datetime.now(timezone.utc)
    # PDT: 2nd Sun Mar – 1st Sun Nov (UTC-7), PST otherwise (UTC-8)
    pst_offset = -8
    # Rough DST check: March (month 3, after ~8th) through November (before ~1st)
    if (utc_now.month > 3 or (utc_now.month == 3 and utc_now.day >= 8)) and \
       (utc_now.month < 11 or (utc_now.month == 11 and utc_now.day < 1)):
        pst_offset = -7  # PDT
    now = utc_now.astimezone(timezone(timedelta(hours=pst_offset)))
    tz_label = "PDT" if pst_offset == -7 else "PST"
    is_weekday = now.weekday() < 5  # Mon=0 ... Fri=4

    lines.append(f"DATA FETCH TIME: {now.strftime('%Y-%m-%d %H:%M')} {tz_label}")
    lines.append("")

    if is_weekday:
        # Stocks
        lines.append("=== STOCK QUOTES ===")
        if not ALPHA_VANTAGE_KEY:
            lines.append("ERROR: ALPHA_VANTAGE_API_KEY not set")
        else:
            for symbol, name in STOCKS.items():
                try:
                    q = fetch_quote(symbol)
                    price_str = f"${q['price']}" if q["price"] != "N/A" else "N/A (market closed)"
                    lines.append(
                        f"{name} ({symbol}): {price_str}  "
                        f"change {q['change']} ({q['change_pct']})  "
                        f"prev close ${q['prev_close']}  vol {q['volume']}"
                    )
                except Exception as e:
                    lines.append(f"{name} ({symbol}): ERROR — {e}")

        lines.append("")

        # News
        lines.append("=== NEWS HEADLINES ===")
        if not BRAVE_KEY:
            lines.append("ERROR: BRAVE_SEARCH_API_KEY not set")
        else:
            news_queries = [
                ("S&P 500 stock market news", "Market"),
                ("Planet Labs PL stock news", "Planet Labs"),
            ]
            for query, label in news_queries:
                try:
                    articles = fetch_news(query)
                    lines.append(f"--- {label} ---")
                    if articles:
                        for a in articles:
                            lines.append(f"• {a['title']} ({a['age']})")
                            if a["description"]:
                                lines.append(f"  {a['description'][:150]}")
                    else:
                        lines.append("No recent news found.")
                except Exception as e:
                    lines.append(f"{label}: ERROR — {e}")
                lines.append("")
    else:
        lines.append("=== WEEKEND — NO MARKET DATA ===")
        lines.append("")

    # Weather
    lines.append("=== WEATHER FORECAST — Sunnyvale, CA ===")
    try:
        periods = fetch_weather()
        for p in periods:
            lines.append(
                f"{p['name']}: {p['temperature']}°{p['temperatureUnit']} — "
                f"{p['detailedForecast']}"
            )
    except Exception as e:
        lines.append(f"ERROR fetching weather: {e}")

    lines.append("")

    # UV Index
    lines.append("=== UV INDEX — Sunnyvale, CA ===")
    try:
        uv = fetch_uv()
        lines.append(f"Peak UV Index: {uv['peak_uv']} at {uv['peak_time']}")
    except Exception as e:
        lines.append(f"ERROR fetching UV index: {e}")

    lines.append("")

    # Air Quality
    lines.append("=== AIR QUALITY — Sunnyvale, CA ===")
    if not PURPLE_KEY:
        lines.append("ERROR: PURPLE_AIR_API_KEY_READ not set")
    else:
        try:
            aqi_data = fetch_aqi()
            lines.append(f"PM2.5: {aqi_data['pm25']} µg/m³ | AQI: {aqi_data['aqi']} ({aqi_data['category']})")
        except Exception as e:
            lines.append(f"ERROR fetching air quality: {e}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
