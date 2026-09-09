#!/usr/bin/env python3
"""Current weather for the panel, from open-meteo — no API key, and once the
location is configured nothing identifying is sent (unlike an IP lookup).

Location lives in weather.json next to this script:
    {"lat": 47.01, "lon": 7.69, "name": "Lützelflüh"}
Prints one JSON line the panel parses, or nothing at all if there is no config
or the fetch fails — the panel then omits the weather slot, exactly as it omits
Claude without a subscription.
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "weather.json")

WMO = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


def load_conf():
    try:
        with open(CONF) as f:
            c = json.load(f)
        return float(c["lat"]), float(c["lon"]), str(c.get("name", ""))
    except Exception:
        return None


def fetch(lat, lon):
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,apparent_temperature,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min"
           "&timezone=auto&forecast_days=1")
    req = urllib.request.Request(url, headers={"User-Agent": "linux-mint-hud"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read())


def main():
    conf = load_conf()
    if not conf:
        return
    lat, lon, name = conf
    try:
        d = fetch(lat, lon)
        cur = d["current"]
        daily = d["daily"]
        out = {
            "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]),
            "hi": round(daily["temperature_2m_max"][0]),
            "lo": round(daily["temperature_2m_min"][0]),
            "desc": WMO.get(cur["weather_code"], "—"),
            "code": cur["weather_code"],
            "name": name,
        }
        print(json.dumps(out))
    except Exception as e:
        print(f"weather: unreachable ({type(e).__name__})", file=sys.stderr)


if __name__ == "__main__":
    main()
