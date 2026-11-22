"""Importers: Apple Health exports (workouts and daily metrics) and bank CSV statements."""

from collections import defaultdict
import csv
from datetime import datetime
import hashlib
import io
from pathlib import Path
import re
import zipfile
from xml.etree.ElementTree import iterparse

# Summed per day. Health keeps samples from every device, so the day's value is the largest single-source total:
# iPhone and Watch both count steps, and adding them would double count.
SUMS = {"HKQuantityTypeIdentifierStepCount": ("steps", 1), "HKQuantityTypeIdentifierActiveEnergyBurned": ("active_energy", 1),
        "HKQuantityTypeIdentifierAppleExerciseTime": ("exercise_minutes", 1),
        "HKQuantityTypeIdentifierDietaryWater": ("water_ml", 1), "HKQuantityTypeIdentifierDietaryProtein": ("protein_g", 1),
        "HKQuantityTypeIdentifierDietaryEnergyConsumed": ("calories", 1)}
AVERAGES = {"HKQuantityTypeIdentifierRestingHeartRate": "resting_hr", "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv"}
LATEST = {"HKQuantityTypeIdentifierBodyMass": "weight_kg", "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat",
          "HKQuantityTypeIdentifierVO2Max": "vo2max"}
ASLEEP = ("HKCategoryValueSleepAnalysisAsleep",)
WORKOUT_KINDS = {"Running": "run", "Walking": "walk", "Cycling": "cycle", "Swimming": "swim", "Hiking": "hike",
                 "Yoga": "yoga", "HighIntensityIntervalTraining": "hiit", "TraditionalStrengthTraining": "strength",
                 "FunctionalStrengthTraining": "strength", "CoreTraining": "strength", "Climbing": "climb",
                 "Basketball": "sport", "Soccer": "sport", "Tennis": "sport", "Badminton": "sport", "Volleyball": "sport",
                 "TableTennis": "sport", "Boxing": "sport", "MartialArts": "sport", "Rowing": "other",
                 "Elliptical": "other", "StairClimbing": "other", "Pilates": "yoga", "Flexibility": "yoga"}
UNIT_KG = {"kg": 1.0, "lb": 0.45359237, "g": 0.001}
UNIT_M = {"km": 1000.0, "m": 1.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}
UNIT_KCAL = {"kcal": 1.0, "Cal": 1.0, "kJ": 0.239006}


def _health_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")


def _open_export(path):
    path = Path(path).expanduser()
    if path.suffix.lower() == ".zip":
        archive = zipfile.ZipFile(path)
        name = next((n for n in archive.namelist() if n.endswith("export.xml") and "cda" not in n), None)
        if name is None:
            raise ValueError("That archive doesn't contain an Apple Health export.")
        return archive.open(name)
    if path.is_dir():
        path = path / "export.xml"
    if not path.exists():
        raise ValueError("Choose the export.zip or export.xml file from Apple Health.")
    return open(path, "rb")


def read_health(path, since=None):
    """Stream the export once and return (workouts, metrics); a multi-gigabyte export never sits in memory."""
    sums = defaultdict(lambda: defaultdict(float))  # (kind, day) -> source -> total
    averages = defaultdict(list)
    latest = {}
    sleep = defaultdict(lambda: defaultdict(float))
    workouts = []
    with _open_export(path) as stream:
        for _, element in iterparse(stream, events=("end",)):
            tag = element.tag
            if tag == "Record":
                kind = element.get("type")
                start = element.get("startDate", "")
                if since and start[:10] < since:
                    element.clear()
                    continue
                source = element.get("sourceName", "")
                try:
                    value = float(element.get("value", "nan"))
                except ValueError:
                    value = None
                day = start[:10]
                if kind in SUMS and value is not None:
                    sums[(SUMS[kind][0], day)][source] += value
                elif kind in AVERAGES and value is not None:
                    averages[(AVERAGES[kind], day)].append(value)
                elif kind in LATEST and value is not None:
                    unit = element.get("unit", "")
                    if kind.endswith("BodyMass"):
                        value *= UNIT_KG.get(unit, 1.0)
                    elif kind.endswith("BodyFatPercentage") and value <= 1:
                        value *= 100
                    latest[(LATEST[kind], day)] = value
                elif kind == "HKCategoryTypeIdentifierSleepAnalysis" and (element.get("value") or "").startswith(ASLEEP):
                    end = _health_time(element.get("endDate"))
                    minutes = (end - _health_time(start)).total_seconds() / 60
                    sleep[end.date().isoformat()][source] += minutes
                element.clear()
            elif tag == "Workout":
                start = element.get("startDate", "")
                if not since or start[:10] >= since:
                    workouts.append(_workout(element))
                element.clear()
    metrics = []
    for (kind, day), by_source in sums.items():
        metrics.append((kind, day, round(max(by_source.values()), 1)))
    for (kind, day), values in averages.items():
        metrics.append((kind, day, round(sum(values) / len(values), 1)))
    for (kind, day), value in latest.items():
        metrics.append((kind, day, round(value, 2)))
    for day, by_source in sleep.items():
        metrics.append(("sleep_minutes", day, round(max(by_source.values()))))
    return workouts, metrics


def _workout(element):
    activity = (element.get("workoutActivityType") or "").replace("HKWorkoutActivityType", "")
    start, end = _health_time(element.get("startDate")), _health_time(element.get("endDate"))
    duration = float(element.get("duration") or 0)
    duration *= {"min": 60, "h": 3600, "s": 1}.get(element.get("durationUnit", "min"), 60)
    distance = _scaled(element.get("totalDistance"), UNIT_M.get(element.get("totalDistanceUnit", "km"), 1000))
    energy = _scaled(element.get("totalEnergyBurned"), UNIT_KCAL.get(element.get("totalEnergyBurnedUnit", "kcal"), 1))
    avg_hr = max_hr = None
    for stat in element.iter("WorkoutStatistics"):
        kind = stat.get("type", "")
        if "Distance" in kind and stat.get("sum"):
            distance = _scaled(stat.get("sum"), UNIT_M.get(stat.get("unit", "km"), 1000))
        elif kind.endswith("ActiveEnergyBurned") and stat.get("sum"):
            energy = _scaled(stat.get("sum"), UNIT_KCAL.get(stat.get("unit", "kcal"), 1))
        elif kind.endswith("HeartRate"):
            avg_hr, max_hr = _scaled(stat.get("average"), 1), _scaled(stat.get("maximum"), 1)
    title = re.sub(r"(?<!^)(?=[A-Z])", " ", activity) or "Workout"
    return {"kind": WORKOUT_KINDS.get(activity, "other"), "title": title, "source": "apple_health",
            "started": start.astimezone().isoformat(timespec="minutes"),
            "duration_s": int(duration or (end - start).total_seconds()), "distance_m": distance or None,
            "energy_kcal": energy, "avg_hr": avg_hr, "max_hr": max_hr,
            "external_id": f"health:{element.get('startDate')}:{activity}"}


def _scaled(value, factor):
    try:
        return round(float(value) * factor, 1) if value not in (None, "") else None
    except ValueError:
        return None


def import_health(store, path, since=None):
    workouts, metrics = read_health(path, since)
    added = 0
    for workout in workouts:
        existing = store.one("SELECT id FROM workouts WHERE external_id = ?", (workout["external_id"],))
        if existing:
            store.update("workouts", existing["id"], workout)
        else:
            store.create("workouts", workout)
            added += 1
    with store.lock, store.db:
        store.db.executemany("INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, 'apple_health')", metrics)
    store.set_setting("health_imported", datetime.now().isoformat(timespec="minutes"))
    return {"workouts": len(workouts), "new_workouts": added, "metrics": len(metrics)}


# Bank CSVs -------------------------------------------------------------------------------------------------------

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%b %d, %Y", "%d %b %Y")
COLUMN_HINTS = {
    "day": ("date", "transaction date", "posted date", "posting date", "booking date", "trans. date"),
    "amount": ("amount", "amount (usd)", "transaction amount", "value"),
    "debit": ("debit", "withdrawal", "withdrawals", "money out", "paid out"),
    "credit": ("credit", "deposit", "deposits", "money in", "paid in"),
    "payee": ("description", "payee", "merchant", "name", "details", "memo", "narrative"),
    "category": ("category", "type"),
}
CATEGORY_RULES = [
    ("Groceries", r"whole foods|trader joe|safeway|kroger|costco|aldi|grocery|market|loblaws|no frills"),
    ("Dining", r"restaurant|cafe|coffee|starbucks|doordash|uber eats|ubereats|grubhub|mcdonald|chipotle|pizza|bar\b"),
    ("Transport", r"uber|lyft|shell|chevron|esso|petro|gas|transit|parking|metro|presto|clipper"),
    ("Subscriptions", r"spotify|netflix|apple\.com|icloud|youtube|hulu|disney|adobe|github|notion|openai|anthropic|patreon|splice"),
    ("Music gear", r"sweetwater|guitar center|reverb|plugin|native instruments|ableton|splice|soundtoys|waves"),
    ("Shopping", r"amazon|target|walmart|best buy|ikea|etsy|ebay"),
    ("Health", r"pharmacy|cvs|walgreens|gym|fitness|clinic|dental|shoppers drug"),
    ("Housing", r"rent|mortgage|hydro|electric|utility|internet|comcast|rogers|bell|verizon|at&t|t-mobile"),
    ("Travel", r"airline|air canada|delta|united|airbnb|hotel|marriott|expedia"),
    ("Income", r"payroll|salary|direct dep|deposit from|interest"),
]


def _parse_day(value):
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date: {value[:20]}")


def _parse_amount(value):
    value = (value or "").strip()
    if not value:
        return None
    negative = value.startswith("(") and value.endswith(")") or value.startswith("-") or value.endswith("-")
    number = float(re.sub(r"[^\d.]", "", value) or 0)
    return -number if negative else number


def categorize(payee, learned=None):
    if learned and payee in learned:
        return learned[payee]
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, payee, re.I):
            return category
    return "Uncategorized"


def read_csv(text):
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ValueError("That file is empty.")
    header = [h.strip().lower() for h in rows[0]]
    columns = {}
    for field, hints in COLUMN_HINTS.items():
        for i, name in enumerate(header):
            if name in hints and field not in columns:
                columns[field] = i
    if "day" not in columns or not ({"amount"} <= columns.keys() or {"debit", "credit"} & columns.keys()):
        raise ValueError("Couldn't find date and amount columns in that CSV.")
    out = []
    for row in rows[1:]:
        cell = lambda field: row[columns[field]] if field in columns and columns[field] < len(row) else ""  # noqa: E731
        try:
            day = _parse_day(cell("day"))
        except ValueError:
            continue
        amount = _parse_amount(cell("amount")) if "amount" in columns else None
        if amount is None:
            debit, credit = _parse_amount(cell("debit")), _parse_amount(cell("credit"))
            amount = (credit or 0) - abs(debit or 0)
        out.append({"day": day, "amount": amount, "payee": " ".join(cell("payee").split())[:120],
                    "category": cell("category").strip()})
    return out


def import_transactions(store, text, account_id=None):
    rows = read_csv(text)
    learned = {r["payee"]: r["category"] for r in store.query(
        "SELECT payee, category FROM transactions WHERE category != 'Uncategorized' GROUP BY payee")}
    added, seen = 0, defaultdict(int)
    for row in rows:
        key = (account_id, row["day"], row["amount"], row["payee"])
        seen[key] += 1
        # The occurrence index keeps two identical same-day charges distinct but makes re-imports idempotent.
        digest = hashlib.sha256(repr((*key, seen[key])).encode()).hexdigest()[:24]
        if store.one("SELECT 1 FROM transactions WHERE external_id = ?", (digest,)):
            continue
        category = row["category"] or categorize(row["payee"], learned)
        if row["amount"] > 0 and category == "Uncategorized":
            category = "Income"
        store.create("transactions", {"account_id": account_id, "day": row["day"], "amount": row["amount"],
                                      "payee": row["payee"], "category": category, "external_id": digest})
        added += 1
    return {"rows": len(rows), "added": added}

