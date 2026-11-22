"""Luna's get_weather tool: Open-Meteo's forecast for where the user is (the place the dashboard last saw) or a named place."""

from datetime import date, datetime, timedelta
import sqlite3

import aiohttp
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

from src.dashboard import weather
from src.dashboard.store import DASHBOARD_PATH

DESCRIPTION = (
    "Get the weather: now, hour by hour and by day, from two days ago to a week ahead. Use it for any weather question "
    "(rain, snow, temperature, wind chill, what to wear, whether to bring an umbrella). Without a place it uses the "
    "user's current area. Answer from the result; times are local to that place."
)


def saved_place(path=DASHBOARD_PATH):
    """The rounded place and units the dashboard saved, read-only. (None, units) when it never had a location."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "metric"
    try:
        rows = dict(db.execute("SELECT key, value FROM settings WHERE key IN ('weather_place', 'units')").fetchall())
    except sqlite3.Error:
        return None, "metric"
    finally:
        db.close()
    import json

    place = json.loads(rows["weather_place"]) if "weather_place" in rows else None
    units = json.loads(rows["units"]) if "units" in rows else "metric"
    return ((place["lat"], place["lon"]) if place else None), units


def summarize(body, first, count, unit, now=None):
    """Only the asked-for days; hours for up to two of them (today's from this hour on)."""
    now = now or datetime.now()
    wanted = {(first + timedelta(days=n)).isoformat() for n in range(count)}
    rows = [d for d in weather.days(body) if d["date"] in wanted]
    by_day = {}
    for h in weather.hours(body):
        by_day.setdefault(h["t"][:10], []).append(h)
    for row in rows[:2]:
        row["hours"] = [{"time": h["t"][11:16], "temp": h["temp"], "rain_chance": h["rain"], "rain": h["mm"], "label": h["label"]}
                        for h in by_day.get(row["date"], []) if row["date"] != now.date().isoformat() or h["t"][11:13] >= f"{now.hour:02d}"]
    result = {"unit": unit, "days": rows}
    if now.date().isoformat() in wanted and body.get("current"):
        current = weather.parse(body, unit)
        result["now"] = {"temp": current["temp"], "label": current["label"]}
    return result


async def get_weather(params, *, path=DASHBOARD_PATH, today=None):
    args = params.arguments or {}
    today = today or date.today()
    try:
        first = date.fromisoformat(args["date"]) if args.get("date") else today
        count = int(args.get("days") or 1)
        place = str(args.get("place") or "").strip()[:120]
    except (TypeError, ValueError):
        await params.result_callback({"status": "failed", "reason": "invalid_arguments"})
        return
    if not -weather.PAST_DAYS <= (first - today).days < weather.FORECAST_DAYS or not 1 <= count <= 7:
        await params.result_callback({"status": "failed", "reason": "outside_forecast_range",
                                      "range": [(today - timedelta(days=weather.PAST_DAYS)).isoformat(),
                                                (today + timedelta(days=weather.FORECAST_DAYS - 1)).isoformat()]})
        return
    count = min(count, weather.FORECAST_DAYS - (first - today).days)
    point, units = saved_place(path)
    unit = "°F" if units == "imperial" else "°C"
    try:
        if place:
            found = await weather.geocode(place)
            if found is None:
                await params.result_callback({"status": "failed", "reason": "place_not_found"})
                return
            label, lat, lon = found
        elif point is None:
            await params.result_callback({"status": "failed", "reason": "location_unavailable",
                                          "hint": "Ask which city, or the user can allow location in the dashboard."})
            return
        else:
            label, (lat, lon) = "the user's current area", point
        body = await weather.fetch(lat, lon, units)
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as error:
        logger.warning(f"Weather tool unavailable ({type(error).__name__})")
        await params.result_callback({"status": "failed", "reason": "weather_unavailable"})
        return
    await params.result_callback({"status": "ok", "place": label, **summarize(body, first, count, unit)})


def tools():
    return [FunctionSchema("get_weather", DESCRIPTION, {
        "place": {"type": "string", "maxLength": 120, "description": "A city or place. Leave out for the user's current area."},
        "date": {"type": "string", "format": "date", "description": "First day (YYYY-MM-DD). Defaults to today."},
        "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "How many days from that date. Defaults to 1."},
    }, [], get_weather)]
