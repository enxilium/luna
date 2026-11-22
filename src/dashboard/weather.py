"""Weather where you are, from Open-Meteo (free, no account). Coordinates leave the Mac rounded to about 10 km.

The dashboard keeps the current reading and an hourly forecast (two days back, a week ahead). It also saves the rounded
place in its settings (`weather_place`) so Luna's get_weather tool, in the notch app's process, can use the same spot."""

from datetime import date
import time

import aiohttp
from loguru import logger

URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
FRESH = 30 * 60
PAST_DAYS, FORECAST_DAYS = 2, 8
# WMO weather codes (what Open-Meteo reports) → label, day symbol, night symbol.
CODES = {
    0: ("Clear", "sun.max", "moon.stars"), 1: ("Mostly clear", "sun.max", "moon.stars"),
    2: ("Partly cloudy", "cloud.sun", "cloud.moon"), 3: ("Overcast", "cloud", "cloud"),
    45: ("Fog", "cloud.fog", "cloud.fog"), 48: ("Freezing fog", "cloud.fog", "cloud.fog"),
    51: ("Light drizzle", "cloud.drizzle", "cloud.drizzle"), 53: ("Drizzle", "cloud.drizzle", "cloud.drizzle"),
    55: ("Heavy drizzle", "cloud.drizzle", "cloud.drizzle"), 56: ("Freezing drizzle", "cloud.sleet", "cloud.sleet"),
    57: ("Freezing drizzle", "cloud.sleet", "cloud.sleet"), 61: ("Light rain", "cloud.rain", "cloud.rain"),
    63: ("Rain", "cloud.rain", "cloud.rain"), 65: ("Heavy rain", "cloud.heavyrain", "cloud.heavyrain"),
    66: ("Freezing rain", "cloud.sleet", "cloud.sleet"), 67: ("Freezing rain", "cloud.sleet", "cloud.sleet"),
    71: ("Light snow", "cloud.snow", "cloud.snow"), 73: ("Snow", "cloud.snow", "cloud.snow"),
    75: ("Heavy snow", "cloud.snow", "cloud.snow"), 77: ("Snow grains", "snowflake", "snowflake"),
    80: ("Showers", "cloud.sun.rain", "cloud.moon.rain"), 81: ("Showers", "cloud.rain", "cloud.rain"),
    82: ("Heavy showers", "cloud.heavyrain", "cloud.heavyrain"), 85: ("Snow showers", "cloud.snow", "cloud.snow"),
    86: ("Snow showers", "cloud.snow", "cloud.snow"), 95: ("Thunderstorm", "cloud.bolt.rain", "cloud.bolt.rain"),
    96: ("Thunderstorm, hail", "cloud.bolt.rain", "cloud.bolt.rain"), 99: ("Thunderstorm, hail", "cloud.bolt.rain", "cloud.bolt.rain"),
}


def describe(code, is_day=True):
    label, day, night = CODES.get(int(code), ("Weather", "cloud", "cloud"))
    return label, day if is_day else night


def _round(value):
    return None if value is None else round(value)


def parse(body, unit):
    """Open-Meteo's response → the few numbers Home shows. The daily row is today's (the response may start earlier)."""
    current, daily = body["current"], body.get("daily") or {}
    today = str(current.get("time") or date.today().isoformat())[:10]
    index = (daily.get("time") or []).index(today) if today in (daily.get("time") or []) else 0

    def pick(key):
        values = daily.get(key) or []
        return _round(values[index]) if index < len(values) else None

    label, symbol = describe(current["weather_code"], bool(current.get("is_day", 1)))
    return {"temp": round(current["temperature_2m"]), "label": label, "symbol": symbol, "unit": unit,
            "high": pick("temperature_2m_max"), "low": pick("temperature_2m_min"), "rain": pick("precipitation_probability_max")}


def hours(body):
    """Hourly rows: local time ("2026-11-15T14:00"), temperature, chance of rain, rain in mm, label and symbol."""
    hourly = body.get("hourly") or {}
    out = []
    for n, at in enumerate(hourly.get("time") or []):
        def get(key):
            values = hourly.get(key) or []
            return values[n] if n < len(values) else None
        if get("temperature_2m") is None:
            continue
        label, symbol = describe(get("weather_code") or 0, bool(get("is_day") if get("is_day") is not None else 1))
        out.append({"t": at, "temp": _round(get("temperature_2m")), "rain": _round(get("precipitation_probability")),
                    "mm": get("precipitation"), "label": label, "symbol": symbol})
    return out


def days(body):
    """Daily rows: date, label, high, low, chance of rain, rain in mm, sunrise and sunset."""
    daily = body.get("daily") or {}
    out = []
    for n, day in enumerate(daily.get("time") or []):
        def get(key):
            values = daily.get(key) or []
            return values[n] if n < len(values) else None
        out.append({"date": day, "label": describe(get("weather_code") or 0)[0], "high": _round(get("temperature_2m_max")),
                    "low": _round(get("temperature_2m_min")), "rain": _round(get("precipitation_probability_max")),
                    "mm": get("precipitation_sum"), "sunrise": (get("sunrise") or "")[11:16] or None,
                    "sunset": (get("sunset") or "")[11:16] or None})
    return out


def params(lat, lon, units, *, past_days=PAST_DAYS, forecast_days=FORECAST_DAYS):
    return {"latitude": round(lat, 1), "longitude": round(lon, 1), "timezone": "auto",
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
            "past_days": past_days, "forecast_days": forecast_days,
            "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
            "precipitation_unit": "inch" if units == "imperial" else "mm"}


async def fetch(lat, lon, units, *, url=URL, **kwargs):
    """One forecast request (raises aiohttp errors). Only the rounded coordinates and units are sent."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(url, params=params(lat, lon, units, **kwargs)) as response:
            response.raise_for_status()
            return await response.json()


async def geocode(name, *, url=GEOCODE):
    """A place name → (label, lat, lon), or None. Only the name is sent."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(url, params={"name": name, "count": 1, "format": "json"}) as response:
            response.raise_for_status()
            found = ((await response.json()).get("results") or [None])[0]
    if not found:
        return None
    label = ", ".join(str(found[k]) for k in ("name", "admin1", "country") if found.get(k))
    return label, found["latitude"], found["longitude"]


class Weather:
    """The latest reading for the current place, refreshed every 30 minutes or when you move or change units."""

    def __init__(self, location, units, url=URL, on_place=None):
        self.location, self.units, self.url = location, units, url  # units: () -> "metric" | "imperial"
        self.on_place = on_place  # called with (lat, lon) rounded, for Luna's weather tool
        self.data, self.hours, self.key, self.at = None, [], None, 0.0

    def hourly(self):
        snap = self.snapshot()
        return {"state": snap["state"], "unit": snap.get("unit"), "hours": self.hours if snap["state"] == "ready" else []}

    def snapshot(self):
        status = self.location.status()
        state = {"ready": "ready" if self.data else "loading", "not_determined": "ask"}.get(status, "off" if status in (
            "denied", "restricted", "off") else "unavailable")
        return {"state": state, **(self.data or {})} if status == "ready" else {"state": state}

    def _key(self):
        point = self.location.coordinate()
        if point is None:
            return None
        return round(point[0], 1), round(point[1], 1), self.units()

    def due(self):
        key = self._key()
        return key is not None and (key != self.key or time.time() - self.at > FRESH)

    async def update(self):
        """Fetch when due. True when there's a new reading. Failures keep the last one and log no provider text."""
        if not self.due():
            return False
        key = self._key()
        lat, lon, units = key
        unit = "F" if units == "imperial" else "C"
        if self.on_place and key[:2] != (self.key or (None, None))[:2]:
            self.on_place(lat, lon)
        try:
            body = await fetch(lat, lon, units, url=self.url)
            data, hourly = parse(body, unit), hours(body)
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as error:
            logger.warning(f"Weather unavailable ({type(error).__name__})")
            self.key, self.at = key, time.time() - FRESH + 300  # try again in five minutes, not on every request
            return False
        self.data, self.hours, self.key, self.at = data, hourly, key, time.time()
        return True


class DemoWeather:
    def hourly(self):
        from datetime import datetime, timedelta
        import math

        start = datetime.combine(date.today() - timedelta(days=PAST_DAYS), datetime.min.time())
        rows = []
        for n in range(24 * (PAST_DAYS + FORECAST_DAYS)):
            at = start + timedelta(hours=n)
            temp = 3 + 4 * math.sin((at.hour - 9) / 24 * 2 * math.pi) + (n // 24) % 3 - 1
            rain = (30 + n * 7 % 50) if (n // 24) % 3 == 1 and 10 <= at.hour <= 18 else (n * 13) % 20
            label, symbol = describe(61 if rain >= 50 else 2 if rain >= 20 else 1, 7 <= at.hour < 17)
            rows.append({"t": at.strftime("%Y-%m-%dT%H:00"), "temp": round(temp), "rain": rain, "mm": 0.4 if rain >= 50 else 0,
                         "label": label, "symbol": symbol})
        return {"state": "ready", "unit": "C", "hours": rows}

    def snapshot(self):
        return {"state": "ready", "temp": 4, "label": "Partly cloudy", "symbol": "cloud.sun", "unit": "C", "high": 7, "low": 1, "rain": 20}

    def due(self):
        return False

    async def update(self):
        return False
