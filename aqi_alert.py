#!/usr/bin/env python3
"""
aqi_alert.py
Checks PurpleAir AQI for Sunnyvale and sends a Telegram alert if AQI > 100.
No message is sent if air quality is acceptable.
"""

import json, os, subprocess, sys, urllib.request

PURPLE_KEY     = os.environ.get("PURPLE_AIR_API_KEY_READ", "")
PURPLE_SENSOR  = 13987  # Sunnyvale area sensor
TELEGRAM_TARGET = "8438066154"
ALERT_THRESHOLD = 100


def fetch_aqi():
    if not PURPLE_KEY:
        print("ERROR: PURPLE_AIR_API_KEY_READ not set", file=sys.stderr)
        sys.exit(1)
    req = urllib.request.Request(
        f"https://api.purpleair.com/v1/sensors/{PURPLE_SENSOR}?fields=pm2.5",
        headers={"X-API-Key": PURPLE_KEY},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        pm = json.loads(r.read()).get("sensor", {}).get("pm2.5")
    if pm is None:
        return None, None, None
    if pm <= 12:
        aqi, cat = int(pm / 12 * 50), "Good"
    elif pm <= 35.4:
        aqi, cat = int(50 + (pm - 12) / (35.4 - 12) * 50), "Moderate"
    elif pm <= 55.4:
        aqi, cat = int(100 + (pm - 35.4) / (55.4 - 35.4) * 50), "Unhealthy for Sensitive Groups"
    elif pm <= 150.4:
        aqi, cat = int(150 + (pm - 55.4) / (150.4 - 55.4) * 50), "Unhealthy"
    elif pm <= 250.4:
        aqi, cat = int(200 + (pm - 150.4) / (250.4 - 150.4) * 50), "Very Unhealthy"
    else:
        aqi, cat = int(300 + (pm - 250.4) / (350.4 - 250.4) * 100), "Hazardous"
    return pm, aqi, cat


def severity_emoji(aqi):
    if aqi > 250:
        return "🔴"
    elif aqi > 150:
        return "🟠"
    else:
        return "🟡"


def send_telegram(message):
    result = subprocess.run(
        ["openclaw", "message", "send",
         "--channel", "telegram",
         "--target", TELEGRAM_TARGET,
         "--message", message],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Send failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("✅ Alert sent.")


def main():
    pm, aqi, cat = fetch_aqi()
    if aqi is None:
        print("No AQI data available.", file=sys.stderr)
        sys.exit(1)

    print(f"Current AQI: {aqi} ({cat})")

    if aqi <= ALERT_THRESHOLD:
        print("AQI within acceptable range — no alert sent.")
        return

    emoji = severity_emoji(aqi)
    message = (
        f"{emoji} Air Quality Alert — Sunnyvale\n"
        f"AQI: {aqi} ({cat})\n"
        f"PM2.5: {pm} µg/m³\n"
        f"Consider limiting outdoor activity."
    )
    send_telegram(message)


if __name__ == "__main__":
    main()
