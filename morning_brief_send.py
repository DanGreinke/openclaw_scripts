#!/usr/bin/env python3
"""
morning_brief_send.py
Fetches data, formats a morning brief, and sends it via Telegram.
Runs standalone — no LLM needed.
"""

import json, os, subprocess, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
BRAVE_KEY         = os.environ.get("BRAVE_SEARCH_API_KEY", "")
PURPLE_KEY        = os.environ.get("PURPLE_AIR_API_KEY_READ", "")
TELEGRAM_TARGET   = "8438066154"


def fetch_quote(symbol):
    url = (f"https://www.alphavantage.co/query"
           f"?function=GLOBAL_QUOTE&symbol={symbol}&apikey={ALPHA_VANTAGE_KEY}")
    with urllib.request.urlopen(url, timeout=10) as r:
        q = json.loads(r.read()).get("Global Quote", {})
    price = q.get("05. price")
    pct   = q.get("10. change percent", "").replace("%", "").strip()
    return price, pct


def fetch_news(query, count=2):
    url = (f"https://api.search.brave.com/res/v1/news/search"
           f"?q={urllib.parse.quote(query)}&count={count}&freshness=pd")
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
    return [
        (
            p["name"],
            p["temperature"],
            p["shortForecast"],
            p.get("probabilityOfPrecipitation", {}).get("value", 0) or 0
        )
        for p in periods
    ]


# EPA UV Index API endpoint
# Docs: https://www.epa.gov/enviro/facts/services
# Endpoint format: https://enviro.epa.gov/enviro/efservice/getEnvirofactsUVHOURLY/ZIP/{zip}/JSON
# Sunnyvale ZIP: 94086
EPA_UV_API_URL = "https://enviro.epa.gov/enviro/efservice/getEnvirofactsUVHOURLY/ZIP/94086/JSON"

def fetch_uv_index_epa():
    """
    Fetch current UV index from EPA Envirofacts API.
    Uses Sunnyvale ZIP code 94086.
    
    Returns:
        int: UV index value (0-11+) or None if unavailable/error
    """
    try:
        req = urllib.request.Request(
            EPA_UV_API_URL,
            headers={"User-Agent": "OpenClaw/1.0 morning-brief"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            
        # EPA returns a list of records with UV data
        # Each record has: UV_VALUE, DATE_TIME, etc.
        if not data or not isinstance(data, list) or len(data) == 0:
            return None
            
        # Get the most recent UV reading (first in list is usually current hour)
        # Data comes sorted by time, newest first
        for record in data:
            uv_value = record.get("UV_VALUE")
            if uv_value is not None:
                try:
                    return int(float(uv_value))
                except (ValueError, TypeError):
                    continue
                    
        return None
    except urllib.error.URLError as e:
        print(f"EPA UV API network error: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"EPA UV API JSON parse error: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"EPA UV API error: {e}", file=sys.stderr)
        return None


def fetch_uv_index():
    """
    DEPRECATED: Fetch UV index from weather.gov API.
    Note: UV data is not currently available via the standard forecast endpoints.
    Use fetch_uv_index_epa() instead for live UV data.
    
    Kept for backward compatibility.
    """
    return None


def get_uv_category(uv_index):
    """Return UV index category based on EPA guidelines."""
    if uv_index is None:
        return None
    if uv_index <= 2:
        return "Low"
    elif uv_index <= 5:
        return "Moderate"
    elif uv_index <= 7:
        return "High"
    elif uv_index <= 10:
        return "Very High"
    else:
        return "Extreme"


def fetch_aqi():
    req = urllib.request.Request(
        "https://api.purpleair.com/v1/sensors/13987?fields=pm2.5",
        headers={"X-API-Key": PURPLE_KEY},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        pm = json.loads(r.read()).get("sensor", {}).get("pm2.5")
    if pm is None:
        return None
    aqi = int(pm / 12 * 50) if pm <= 12 else int(50 + (pm - 12) / (35.4 - 12) * 50) if pm <= 35.4 else 100
    cat = "Good" if pm <= 12 else "Moderate" if pm <= 35.4 else "Unhealthy for Sensitive"
    return pm, aqi, cat


def send_telegram(message):
    result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "telegram",
         "--target", TELEGRAM_TARGET,
         "--message", message],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Send failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("✅ Sent!")


def main():
    # Pacific time
    utc = datetime.now(timezone.utc)
    dst = (utc.month > 3 or (utc.month == 3 and utc.day >= 8)) and utc.month < 11
    now = utc.astimezone(timezone(timedelta(hours=-7 if dst else -8)))
    is_weekday = now.weekday() < 5
    day_name = now.strftime("%A")

    lines = [f"Good {day_name} morning, Dan! ☀️\n"]

    # Weather
    try:
        weather = fetch_weather()
        today = weather[0]
        tomorrow = next((p for p in weather if "night" not in p[0].lower() and p != today), None)
        
        # Format weather line with precipitation chance if > 10%
        today_name, today_temp, today_forecast, today_precip = today
        weather_line = f"🌤 Weather: {today_temp}°F and {today_forecast.lower()} today in Sunnyvale."
        
        # Add precipitation chance if > 10%
        if today_precip and today_precip > 10:
            weather_line += f" ({today_precip}% chance of rain)"
        
        # Add tomorrow forecast
        if tomorrow:
            tomorrow_name, tomorrow_temp, tomorrow_forecast, tomorrow_precip = tomorrow
            tomorrow_line = f" {tomorrow_name}: {tomorrow_temp}°F, {tomorrow_forecast.lower()}."
            # Add precipitation for tomorrow if > 10%
            if tomorrow_precip and tomorrow_precip > 10:
                tomorrow_line = f" {tomorrow_name}: {tomorrow_temp}°F, {tomorrow_forecast.lower()} ({tomorrow_precip}% rain)."
            weather_line += tomorrow_line
            
        lines.append(weather_line)
        
        # UV Index from EPA API
        uv_index = fetch_uv_index_epa()
        if uv_index is not None:
            uv_cat = get_uv_category(uv_index)
            uv_emoji = {"Low": "🟢", "Moderate": "🟡", "High": "🟠", "Very High": "🔴", "Extreme": "⚫"}.get(uv_cat, "")
            lines.append(f"☀️ UV Index: {uv_index} ({uv_cat}) {uv_emoji}")
    except Exception as e:
        lines.append(f"🌤 Weather: unavailable")

    # AQI
    try:
        aqi_data = fetch_aqi()
        if aqi_data:
            pm, aqi, cat = aqi_data
            lines.append(f"💨 Air quality: AQI {aqi} ({cat}) — PM2.5 {pm} µg/m³")
    except:
        pass

    if is_weekday:
        # Stocks
        try:
            spy_price, spy_pct = fetch_quote("SPY")
            pl_price, pl_pct   = fetch_quote("PL")
            spy_str = f"SPY ${spy_price} ({spy_pct}%)" if spy_price else "SPY: closed"
            pl_str  = f"PL ${pl_price} ({pl_pct}%)"  if pl_price  else "PL: closed"
            lines.append(f"📈 Markets: {spy_str}  |  {pl_str}")
        except Exception as e:
            lines.append("📈 Markets: data unavailable")

        # News
        try:
            headlines = (fetch_news("S&P 500 stock market news", 2)
                       + fetch_news("Planet Labs PL stock news", 2))
            if headlines:
                lines.append("\n📰 Headlines:")
                for h in headlines[:4]:
                    lines.append(f"• {h}")
        except:
            pass
    else:
        lines.append("📅 Weekend — markets closed.")

    lines.append("\n🦀")
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()

