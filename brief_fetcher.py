#!/usr/bin/env python3
"""
brief_fetcher.py
Fetches morning data and saves to file. Fast, model-free.
Overwrites /data/workspace/brief_data.txt each run.
"""

import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

# Config
DATA_FILE = "/data/workspace/brief_data.txt"
AV_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
BRAVE_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
PURPLE_KEY = os.environ.get("PURPLE_AIR_API_KEY_READ", "")

def fetch_quote(symbol):
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={AV_KEY}"
    with urllib.request.urlopen(url, timeout=10) as r:
        q = json.loads(r.read()).get("Global Quote", {})
    return {
        "price": q.get("05. price", "N/A"),
        "change": q.get("09. change", ""),
        "pct": q.get("10. change percent", ""),
    }

def fetch_news(query, count=2):
    url = f"https://api.search.brave.com/res/v1/news/search?q={urllib.parse.quote(query)}&count={count}&freshness=pd"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_KEY,
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return [item.get("title", "") for item in json.loads(r.read()).get("results", [])]

def fetch_weather():
    req = urllib.request.Request(
        "https://api.weather.gov/gridpoints/MTR/94,85/forecast",
        headers={"User-Agent": "OpenClaw/1.0 morning-brief"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        periods = json.loads(r.read())["properties"]["periods"][:4]
    return [(p["name"], p["temperature"], p["shortForecast"]) for p in periods]

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

def fetch_aqi():
    req = urllib.request.Request(
        "https://api.purpleair.com/v1/sensors/13987?fields=pm2.5",
        headers={"X-API-Key": PURPLE_KEY},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        pm = json.loads(r.read()).get("sensor", {}).get("pm2.5")
    if pm is None:
        return None
    # Simple AQI calc
    if pm <= 12:
        aqi, cat = int(pm/12*50), "Good"
    elif pm <= 35.4:
        aqi, cat = int(50 + (pm-12)/(35.4-12)*50), "Moderate"
    else:
        aqi, cat = 100, "Unhealthy for Sensitive"
    return {"pm25": pm, "aqi": aqi, "category": cat}

def main():
    # Pacific time
    utc = datetime.now(timezone.utc)
    dst = (utc.month > 3 or (utc.month == 3 and utc.day >= 8)) and utc.month < 11
    tz_offset = -7 if dst else -8
    now = utc.astimezone(timezone(timedelta(hours=tz_offset)))
    is_weekday = now.weekday() < 5
    
    lines = []
    lines.append(f"DATE: {now.strftime('%A, %B %-d')} ({'PDT' if dst else 'PST'})")
    lines.append(f"WEEKDAY: {'Yes' if is_weekday else 'Weekend'}")
    lines.append("")
    
    # Weather
    try:
        weather = fetch_weather()
        lines.append("=== WEATHER ===")
        for name, temp, forecast in weather:
            lines.append(f"{name}: {temp}°F — {forecast}")
    except Exception as e:
        lines.append(f"Weather error: {e}")
    lines.append("")
    
    # AQI
    try:
        aqi = fetch_aqi()
        if aqi:
            lines.append("=== AIR QUALITY ===")
            lines.append(f"PM2.5: {aqi['pm25']} µg/m³")
            lines.append(f"AQI: {aqi['aqi']} ({aqi['category']})")
    except Exception as e:
        lines.append(f"AQI error: {e}")
    lines.append("")

    # UV Index
    try:
        uv = fetch_uv()
        lines.append("=== UV INDEX ===")
        lines.append(f"Peak UV Index: {uv['peak_uv']} at {uv['peak_time']}")
    except Exception as e:
        lines.append(f"UV error: {e}")
    lines.append("")
    
    if is_weekday:
        # Stocks
        try:
            spy = fetch_quote("SPY")
            pl = fetch_quote("PL")
            lines.append("=== STOCKS ===")
            lines.append(f"SPY: ${spy['price']}  change: {spy['change']} ({spy['pct']})")
            lines.append(f"PL: ${pl['price']}  change: {pl['change']} ({pl['pct']})")
        except Exception as e:
            lines.append(f"Stock error: {e}")
        lines.append("")
        
        # News
        try:
            market_news = fetch_news("S&P 500 stock market news", 3)
            pl_news = fetch_news("Planet Labs PL stock news", 3)
            lines.append("=== NEWS HEADLINES ===")
            lines.append("Market news:")
            for h in market_news:
                lines.append(f"  • {h}")
            lines.append("Planet Labs news:")
            for h in pl_news:
                lines.append(f"  • {h}")
        except Exception as e:
            lines.append(f"News error: {e}")
        lines.append("")
    
    # Write to file (overwrites previous day)
    with open(DATA_FILE, 'w') as f:
        f.write('\n'.join(lines))
    
    print(f"✅ Brief data saved to {DATA_FILE}")
    print(f"   {len(lines)} lines written")

if __name__ == "__main__":
    main()
