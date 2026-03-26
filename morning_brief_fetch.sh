#!/usr/bin/env bash
# morning_brief_fetch.sh — pre-formatted brief, Haiku just sends it

AV_KEY="${ALPHA_VANTAGE_API_KEY:-}"
BRAVE_KEY="${BRAVE_SEARCH_API_KEY:-}"
PURPLE_KEY="${PURPLE_AIR_API_KEY_READ:-}"

# Pacific time
MONTH=$(date -u +%-m); DAY=$(date -u +%-d)
if { [ "$MONTH" -gt 3 ] || { [ "$MONTH" -eq 3 ] && [ "$DAY" -ge 8 ]; }; } && [ "$MONTH" -lt 11 ]; then
  TZ_LABEL="PDT"; OFFSET=-7
else
  TZ_LABEL="PST"; OFFSET=-8
fi
PT_HOUR=$(( ($(date -u +%-H) + OFFSET + 24) % 24 ))
WEEKDAY=$(date -u +%u)  # 1=Mon … 7=Sun

# Start building brief
BRIEF="Good morning, Dan! ☀️

"

# Fetch and format weather
WEATHER_RAW=$(curl -s "https://api.weather.gov/gridpoints/MTR/94,85/forecast" \
  -H "User-Agent: OpenClaw/1.0" \
  | python3 -c "
import sys, json
periods = json.load(sys.stdin)['properties']['periods'][:3]
for p in periods:
    print(p['name'] + '|' + str(p['temperature']) + '|' + p['shortForecast'])
" 2>/dev/null)

TODAY=$(echo "$WEATHER_RAW" | head -1)
TODAY_NAME=$(echo "$TODAY" | cut -d'|' -f1)
TODAY_TEMP=$(echo "$TODAY" | cut -d'|' -f2)
TODAY_COND=$(echo "$TODAY" | cut -d'|' -f3)

BRIEF+="🌤 Weather: ${TODAY_TEMP}°F and ${TODAY_COND,,} in Sunnyvale."

# Look for tomorrow's daytime forecast
TOMORROW=$(echo "$WEATHER_RAW" | grep -v "Night" | tail -1)
if [ -n "$TOMORROW" ] && [ "$TOMORROW" != "$TODAY" ]; then
  TMRW_NAME=$(echo "$TOMORROW" | cut -d'|' -f1)
  TMRW_TEMP=$(echo "$TOMORROW" | cut -d'|' -f2)
  BRIEF+=" ${TMRW_NAME}: ${TMRW_TEMP}°F."
fi

BRIEF+="
"

# Air quality (simplified — just show raw PM2.5 and rough category)
AQI_RAW=$(curl -s "https://api.purpleair.com/v1/sensors/13987?fields=pm2.5" \
  -H "X-API-Key: ${PURPLE_KEY}" 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('sensor',{}).get('pm2.5',''))" 2>/dev/null)

if [ -n "$AQI_RAW" ] && [ "$AQI_RAW" != "None" ]; then
  PM25_INT="${AQI_RAW%.*}"
  # Very rough AQI approximation without bc
  if [ "$PM25_INT" -le 5 ]; then
    AQI_TXT="Excellent (~20)"
  elif [ "$PM25_INT" -le 12 ]; then
    AQI_TXT="Good (~35)"
  elif [ "$PM25_INT" -le 35 ]; then
    AQI_TXT="Moderate (~60)"
  else
    AQI_TXT="Unhealthy"
  fi
  BRIEF+="💨 Air quality: ${AQI_TXT} — PM2.5 at ${AQI_RAW} µg/m³
"
fi

if [ "$WEEKDAY" -le 5 ]; then
  # Stocks
  BRIEF+="📈 Markets: "
  
  SPY_DATA=$(curl -s "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=SPY&apikey=${AV_KEY}" 2>/dev/null \
    | python3 -c "
import sys, json
q = json.load(sys.stdin).get('Global Quote', {})
price = q.get('05. price', '')
if price:
    print(f\"SPY \${price} ({q.get('10. change percent','')})\")
else:
    print('SPY: closed')
" 2>/dev/null)
  
  PL_DATA=$(curl -s "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=PL&apikey=${AV_KEY}" 2>/dev/null \
    | python3 -c "
import sys, json
q = json.load(sys.stdin).get('Global Quote', {})
price = q.get('05. price', '')
if price:
    print(f\"PL \${price} ({q.get('10. change percent','')})\")
else:
    print('PL: closed')
" 2>/dev/null)
  
  BRIEF+="${SPY_DATA}  |  ${PL_DATA}"
  BRIEF+="
"
  
  # News
  BRIEF+="📰 Recent headlines:
"
  
  MARKET_NEWS=$(curl -s "https://api.search.brave.com/res/v1/news/search?q=S%26P+500+stock+market+news&count=2&freshness=pd" \
    -H "Accept: application/json" -H "X-Subscription-Token: ${BRAVE_KEY}" 2>/dev/null \
    | python3 -c "import sys,json; [print('•',r.get('title','')) for r in json.load(sys.stdin).get('results',[])]" 2>/dev/null)
  
  PL_NEWS=$(curl -s "https://api.search.brave.com/res/v1/news/search?q=Planet+Labs+PL+stock+news&count=2&freshness=pd" \
    -H "Accept: application/json" -H "X-Subscription-Token: ${BRAVE_KEY}" 2>/dev/null \
    | python3 -c "import sys,json; [print('•',r.get('title','')) for r in json.load(sys.stdin).get('results',[])]" 2>/dev/null)
  
  BRIEF+="${MARKET_NEWS}
${PL_NEWS}"
  BRIEF+="
"
else
  BRIEF+="📅 Weekend — markets closed.
"
fi

BRIEF+="
🦀"

echo "$BRIEF"
