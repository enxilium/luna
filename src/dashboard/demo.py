"""Seeded sample life for `--demo`: lives in memory and a temporary folder, never in the real dashboard data."""

import asyncio
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import wave
import zlib

from src.dashboard.messages import ProviderError

REPO = Path(__file__).resolve().parents[2]


def _wav(path, seconds, chords, bpm=88, seed=1):
    """A short synthesized pad-and-pulse loop, so the audio player has something real to draw and play."""
    rate = 22050
    rng = random.Random(seed)
    frames = bytearray()
    beat = 60 / bpm
    for i in range(int(seconds * rate)):
        t = i / rate
        chord = chords[int(t / (beat * 4)) % len(chords)]
        pad = sum(math.sin(2 * math.pi * f * t) for f in chord) / len(chord)
        swell = 0.55 + 0.45 * math.sin(2 * math.pi * t / (beat * 8))
        phase = (t % beat) / beat
        kick = math.sin(2 * math.pi * 52 * t) * math.exp(-phase * 9)
        hat = (rng.random() * 2 - 1) * math.exp(-((t + beat / 2) % beat) / beat * 30) * 0.18
        sample = 0.32 * pad * swell + 0.45 * kick + hat
        frames += struct.pack("<h", int(max(-1, min(1, sample)) * 26000))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))


def _png(path, width, height, colors):
    """A soft diagonal gradient PNG used as cover art."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            t = (x / width + y / height) / 2
            a, b = (colors[0], colors[1]) if t < 0.5 else (colors[1], colors[2])
            k = (t * 2) % 1 if t < 0.5 else (t - 0.5) * 2
            glow = math.exp(-(((x - width * 0.7) / width) ** 2 + ((y - height * 0.3) / height) ** 2) * 6) * 60
            row += bytes(min(255, int(a[i] + (b[i] - a[i]) * k + glow)) for i in range(3))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows)))
                           + chunk(b"IEND", b""))


CHAPTER = """# {title}

The station hummed the way old refrigerators hum, a sound you only hear once it stops. Mara counted the seconds
between the pulses of the beacon and wrote them in the margin of her notebook, the way her father had taught her to
count thunder. Eleven. Twelve. The orbit was decaying slower than anyone had predicted, which meant there was time,
which meant there was nothing left to blame for not deciding.

Below, the planet turned its night side toward her, and the cities came on like a held breath let go.
"""


def make_files(root):
    root = Path(root)
    music = root / "Midnight Static"
    (music / "Bounces").mkdir(parents=True)
    (music / "Stems").mkdir()
    (music / "Art").mkdir()
    (music / "Midnight Static Project").mkdir()
    (music / "Midnight Static Project" / "Midnight Static.als").write_bytes(b"\x1f\x8b demo")
    am, f, c, g = (220, 261.6, 329.6), (174.6, 220, 261.6), (261.6, 329.6, 392), (196, 246.9, 293.7)
    _wav(music / "Bounces" / "Midnight Static v7.wav", 16, [am, f, c, g], 92, 1)
    _wav(music / "Bounces" / "Glass Hours (rough).wav", 12, [f, c, am, g], 78, 2)
    _wav(music / "Bounces" / "Neon Rain idea.wav", 9, [c, g, am, f], 104, 3)
    _wav(music / "Stems" / "Pads.wav", 6, [am, f], 92, 4)
    _png(music / "Art" / "cover.png", 240, 240, ((18, 16, 44), (92, 70, 210), (60, 190, 210)))
    (music / "lyrics.md").write_text("# Midnight Static\n\nStatic on the line, your voice in pieces\n"
                                     "I keep the lamp on for the version of you that calls back\n")
    writing = root / "The Quiet Orbit"
    (writing / "Draft").mkdir(parents=True)
    for i, title in enumerate(("Decay", "The Beacon", "Night Side", "Counting Thunder", "Burn Window"), 1):
        (writing / "Draft" / f"{i:02d} {title}.md").write_text(CHAPTER.format(title=title) * (2 + i))
    (writing / "outline.md").write_text("# Outline\n\n1. Decay\n2. The Beacon\n3. Night Side\n")
    _png(writing / "moodboard.png", 200, 140, ((10, 12, 30), (40, 60, 140), (200, 170, 255)))
    beats = root / "Lo-fi tape"
    beats.mkdir()
    _wav(beats / "tape loop 03.wav", 8, [f, am], 72, 5)
    return {"music": music, "writing": writing, "beats": beats}


def seed(store, today=None):
    rng = random.Random(42)
    today = today or date.today()
    folder = Path(tempfile.mkdtemp(prefix="luna-demo-"))
    files = make_files(folder)
    day = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731

    store.sandbox = True  # notes and media stay in the store's temp folder, never in the linked (real) folders

    def project(**data):
        return store.create("projects", data)

    ms = project(title="Midnight Static", category="music", pinned=True, folder=str(files["music"]),
                 description="Four-track synthwave EP. Mixing the title track; vocals left on Glass Hours.")
    luna = project(title="Luna", category="software", pinned=True, folder=str(REPO),
                   description="Voice assistant in the notch, plus this dashboard.")
    orbit = project(title="The Quiet Orbit", category="writing", folder=str(files["writing"]),
                    description="Novella about a station keeper deciding whether to bring the orbit down.")
    tape = project(title="Lo-fi beat tape", category="music", folder=str(files["beats"]),
                   description="Twelve short loops sampled from field recordings around the city.")
    project(title="Night Market", category="photography", description="Film portraits from the Saturday market, for a zine.")
    project(title="Portfolio site", category="other", category_label="Design", description="One page that links the music, the code and the writing.")
    project(title="Aurora (single)", category="music", archived=True, description="Released in September.")

    def course(**data):
        return store.create("courses", data)

    ml = course(code="CSC311", description="Intro to Machine Learning · Prof. Grosse · MW 11–12 in BA 1130")
    calc = course(code="MAT237", description="Multivariable Calculus · TR 10–11")
    os_ = course(code="CSC369", description="Operating Systems · Tue 2–3, tutorial Fri 1–2")
    course(code="STA247", description="Probability with Computer Applications · Wed 3–4")
    course(code="CSC209", description="Software Tools and Systems Programming", active=False)

    tasks = [
        ("Bounce Glass Hours instrumental for the vocalist", ms, None, 3, day(0), "doing"),
        ("Send mix v7 to Theo for notes", ms, None, 3, day(-1), "todo"),
        ("Bounce stems for mastering", ms, None, 2, day(2), "todo"),
        ("Write liner notes", ms, None, 1, None, "todo"),
        ("Write chapter 6 opening scene", orbit, None, 2, day(1), "todo"),
        ("Dashboard: messaging polish", luna, None, 2, day(0), "todo"),
        ("Review recovery test flakiness", luna, None, 1, day(3), "todo"),
        ("Timed past midterm", None, ml, 3, day(1), "doing"),
        ("One-page cheat sheet", None, ml, 2, day(3), "todo"),
        ("HW4: neural nets from scratch", None, ml, 2, day(16), "todo"),
        ("Problem set 8", None, calc, 2, day(4), "todo"),
        ("A2: page tables", None, os_, 3, day(12), "todo"),
        ("Book a dentist appointment", None, None, 0, day(5), "todo"),
        ("Renew passport", None, None, 2, day(12), "todo"),
        ("Pay studio rent", None, None, 2, day(2), "todo"),
        ("Call grandma", None, None, 1, day(0), "todo"),
        ("Reply to Lumen Labs about the workshop", None, None, 1, day(0), "todo"),
        ("Back up sample library", None, None, 0, None, "todo"),
    ]
    for title, p, c, priority, due, status in tasks:
        store.create("tasks", {"title": title, "project_id": p and p["id"], "course_id": c and c["id"], "priority": priority,
                               "due": due, "status": status})
    store.create("tasks", {"title": "Weekly prep", "course_id": ml["id"], "repeat": "weekly", "due": day(2), "priority": 1})
    store.create("tasks", {"title": "Morning pages", "repeat": "daily", "due": day(0)})
    for title, owner in (("Upload Aurora stems to the label", None), ("Title track mixdown", ms), ("Finish act one", orbit),
                         ("Lecture 8 review", None)):
        row = store.create("tasks", {"title": title, "project_id": owner and owner["id"]})
        store.complete_task(row["id"])
    store.update("tasks", store.one("SELECT id FROM tasks WHERE title = 'Bounce Glass Hours instrumental for the vocalist'")["id"],
                 {"description": "Print it **without** the lead vocal, but keep the backing *oohs*.\n\n- [x] Mute the lead bus\n- [ ] Print at -1 dBTP\n- [ ] Send to Nadia"})

    from src.dashboard import notes as note_files

    def note(owner_kind, owner, title, body):
        folder = note_files.root(store, owner_kind, owner)
        path = note_files.new_path(folder, title)
        note_files.write(path, body)
        store.create("notes", {"title": title, f"{owner_kind}_id": owner["id"]}, {"path": str(path)})

    note("course", ml, "Kernels and the kernel trick", (
        "A linear model only sees linear structure. Map each input through a feature map $\\phi$, then notice the algorithm "
        "only needs inner products $\\langle \\phi(x), \\phi(x') \\rangle$, never $\\phi$ itself.\n\n"
        "::: columns\n## Why it works\n- Any PSD kernel is an inner product somewhere (Mercer)\n- RBF has an infinite-dimensional $\\phi$ "
        "at finite cost\n- Cost scales with $N$, not $D$\n+++\n$$K(x, x') = \\exp\\left(-\\frac{\\lVert x - x' \\rVert^2}{2\\sigma^2}\\right)$$\n:::\n\n"
        "::: tip On the midterm\nShow a kernel is valid by building it from valid ones: sums, products, scaling and `exp`.\n:::\n"))
    note("course", ml, "Midterm review", "## Topics\n\n- [x] Bias and variance\n- [ ] Regularisation\n- [ ] SVM duals\n")
    note("course", os_, "Paging", (
        "::: definition Page Table\n> Maps each **virtual page** to the **physical frame** that holds it.\n\n"
        "Each entry also carries permission bits: present, writable, user.\n:::\n\n"
        "::: theorem Belady's Anomaly\nUnder FIFO replacement, giving a process **more frames** can cause **more page faults**.\n:::\n\n"
        "::: warning Page Fault\nAn access to a page that isn't present traps to the kernel, which loads it and retries.\n:::\n\n"
        "::: example Translating an address\nWith 4 KB pages, the low 12 bits are the offset:\n\n"
        "```c\nuint32_t vpn = vaddr >> 12;       // virtual page number\nuint32_t offset = vaddr & 0xfff;\n"
        "pte_t *pte = &page_table[vpn];\nif (!pte->present) handle_fault(vaddr);\n```\n:::\n\n"
        "::: note\nTLB misses are handled in hardware on x86.\n:::\n\n"
        "```sql\nSELECT pid, COUNT(*) AS faults\nFROM page_faults\nWHERE kind = 'major' -- disk reads only\nGROUP BY pid\nORDER BY faults DESC;\n```\n\n"
        "```py\ndef working_set(trace, window=100):\n    return {page for page in trace[-window:]}\n```\n"))
    note("course", calc, "Divergence theorem", "$$\\iiint_V \\nabla \\cdot \\mathbf{F} \\, dV = \\oiint_{\\partial V} \\mathbf{F} \\cdot d\\mathbf{S}$$\n")
    note("project", ms, "Mix notes", "Kick fights the bass around **60 Hz**: sidechain more or carve the bass.\n\n"
         "- Chorus needs width; try the Juno chorus on the pad bus\n- Reference: early M83\n")
    note("project", orbit, "Voice", "Keep Mara's chapters close third; the station logs are first person.\n")

    def event(title, start, hours, course_=None, **extra):
        begins = datetime.combine(today, datetime.min.time()) + start
        store.create("events", {"title": title, "start_at": begins.isoformat(timespec="minutes"),
                                "end_at": (begins + timedelta(hours=hours)).isoformat(timespec="minutes"),
                                "course_id": course_ and course_["id"], **extra})

    for n in range(0, 14):
        weekday = (today + timedelta(days=n)).weekday()
        if weekday in (0, 2):
            event("CSC311 lecture", timedelta(days=n, hours=11), 1, ml, location="BA 1130")
        if weekday in (1, 3):
            event("MAT237 lecture", timedelta(days=n, hours=10), 1, calc, location="MP 102")
        if weekday == 1:
            event("CSC369 lecture", timedelta(days=n, hours=14), 1, os_, location="BA 1160")
    event("CSC311 midterm", timedelta(days=4, hours=18), 2, ml, location="EX 100", important=True,
          description="Covers lectures 1–8. Bring a calculator and one page of notes.")
    event("Office hours", timedelta(days=1, hours=16), 1, ml, location="PT 290C")
    # As if read from Quercus: the newest still unread.
    for n, (title, body, read) in enumerate((
            ("Midterm reminders", "The midterm is on Friday in EX 100, 6–8 pm. It covers lectures 1–8.\n\nBring your T-card and "
             "one double-sided page of notes.", False),
            ("Tutorial moved to MP 137", "This week's Thursday tutorial meets in MP 137 instead of UC 161.", False),
            ("HW3 released", "Homework 3 (neural networks) is on the course page, due in two weeks.", True))):
        posted = datetime.combine(today - timedelta(days=n * 3), datetime.min.time()) + timedelta(hours=9 + n)
        store.put_announcement(ml["id"], f"demo:announcement:{n}", title, "Prof. Grosse", body, "", posted.isoformat(), read=read)
    event("Study group: kernels", timedelta(days=5, hours=14), 2, ml, location="Robarts Library, 4th floor", rsvp="pending",
          organizer={"name": "Maya Kim", "email": "maya@example.com"},
          description="Bring past-midterm attempts. We'll split Mercer and SVM duals.",
          attendees=[{"name": "Maya Kim", "email": "maya@example.com", "rsvp": "accepted", "organizer": True},
                     {"name": "Arjun Lal", "email": "arjun@example.com", "rsvp": "accepted"},
                     {"name": "Rosa Santos", "email": "rosa@example.com", "rsvp": "maybe"},
                     {"name": "Kai Okafor", "email": "kai@example.com", "rsvp": "declined"}])

    for name, symbol, target, rate in (("Meditate", "brain.head.profile", 7, 0.7), ("Practice piano", "pianokeys", 5, 0.6),
                                       ("Read", "book", 7, 0.75), ("Stretch", "figure.flexibility", 4, 0.5),
                                       ("Phone off by 11", "moon.zzz", 7, 0.55)):
        habit = store.create("habits", {"name": name, "symbol": symbol, "target": target})
        for n in range(1, 84):
            if rng.random() < rate:
                store.toggle_habit(habit["id"], day(-n))
        if rng.random() < 0.5:
            store.toggle_habit(habit["id"], day(0))

    mood = 3.4
    for n in range(200, 0, -1):
        if rng.random() < 0.82:
            mood = max(1, min(5, mood + rng.uniform(-0.9, 0.9) + (3.6 - mood) * 0.2))
            store.write_journal(day(-n), {"mood": round(mood), "energy": max(1, min(5, round(mood + rng.uniform(-1, 1)))),
                                          "text": rng.choice(["Long mix session; the chorus finally opened up.",
                                                              "Slow day. Walked by the water and wrote a page.",
                                                              "Shipped the recovery fix. Climbing in the evening.",
                                                              "Tired, but dinner with Sam was good.",
                                                              "Wrote 1,200 words before lunch."]),
                                          "gratitude": rng.choice(["Quiet morning", "Good coffee", "Sam's call", ""])})

    lifts = {"Bench press": 70, "Back squat": 95, "Deadlift": 120, "Overhead press": 45, "Pull-up": 0, "Barbell row": 65}
    for n in range(84, -1, -1):
        when = today - timedelta(days=n)
        weekday = when.weekday()
        start = datetime.combine(when, datetime.min.time()) + timedelta(hours=rng.choice([7, 8, 18, 19]))
        if weekday in (0, 2, 4) and rng.random() < 0.85:
            progress = (84 - n) / 84
            names = rng.sample(list(lifts), 4)
            sets = []
            for exercise in names:
                weight = round((lifts[exercise] * (1 + 0.08 * progress)) / 2.5) * 2.5
                for _ in range(3):
                    sets.append({"exercise": exercise, "reps": rng.choice([5, 6, 8]), "weight_kg": weight or None})
            store.save_workout({"kind": "strength", "title": rng.choice(["Push", "Pull", "Legs", "Full body"]),
                                "started": start.isoformat(), "duration_s": rng.randint(50, 75) * 60,
                                "energy_kcal": rng.randint(280, 420), "avg_hr": rng.randint(112, 132),
                                "effort": rng.randint(6, 9), "sets": sets})
        elif weekday in (1, 5) and rng.random() < 0.8:
            km = rng.uniform(4.5, 10.5) if weekday == 5 else rng.uniform(4, 7)
            pace = rng.uniform(5.1, 5.9)
            store.save_workout({"kind": "run", "title": "Long run" if weekday == 5 else "Easy run",
                                "started": start.isoformat(), "duration_s": int(km * pace * 60), "distance_m": km * 1000,
                                "energy_kcal": int(km * 68), "avg_hr": rng.randint(142, 158), "max_hr": rng.randint(168, 182),
                                "effort": rng.randint(4, 7), "source": "apple_health"})
        elif weekday == 3 and rng.random() < 0.5:
            store.save_workout({"kind": rng.choice(["climb", "yoga"]), "title": "", "started": start.isoformat(),
                                "duration_s": rng.randint(45, 90) * 60, "energy_kcal": rng.randint(180, 380),
                                "effort": rng.randint(3, 7)})
    weight = 74.8
    for n in range(120, -1, -1):
        d = day(-n)
        store.set_metric("steps", d, int(rng.gauss(8600, 2600)), "apple_health")
        store.set_metric("sleep_minutes", d, int(rng.gauss(430, 45)), "apple_health")
        store.set_metric("resting_hr", d, round(rng.gauss(56 - n * -0.01, 2.2)), "apple_health")
        store.set_metric("hrv", d, round(rng.gauss(58 + (120 - n) * 0.04, 8)), "apple_health")
        store.set_metric("active_energy", d, int(rng.gauss(560, 140)), "apple_health")
        if n % 3 == 0:
            weight += rng.uniform(-0.35, 0.25)
            store.set_metric("weight_kg", d, round(weight, 1), "manual")
    store.set_metric("vo2max", day(-3), 47.2, "apple_health")

    checking = store.create("accounts", {"name": "Everyday", "kind": "checking", "balance": 4210.55})
    store.create("accounts", {"name": "High-yield savings", "kind": "savings", "balance": 18350})
    store.create("accounts", {"name": "Index funds", "kind": "investment", "balance": 26840.12})
    store.create("accounts", {"name": "Visa", "kind": "credit", "balance": 812.40})
    for category, amount in (("Groceries", 450), ("Dining", 260), ("Transport", 140), ("Subscriptions", 90),
                             ("Music gear", 200), ("Shopping", 180), ("Housing", 1650)):
        store.set_budget(category, amount)
    month_first = today.replace(day=1)
    for back in range(5, -1, -1):
        start = (month_first - timedelta(days=back * 30)).replace(day=1)
        last = today.day if back == 0 else 28
        add = lambda d, amount, payee, category: store.create("transactions", {  # noqa: E731
            "account_id": checking["id"], "day": start.replace(day=d).isoformat(), "amount": amount, "payee": payee,
            "category": category})
        add(1, -1650, "Studio Apartment Rent", "Housing")
        add(1, 5200, "Payroll", "Income") if last >= 1 else None
        if last >= 15:
            add(15, 5200, "Payroll", "Income")
        for d, amount, payee in ((3, -10.99, "Spotify"), (5, -20, "Splice"), (8, -9.99, "iCloud+"), (12, -14, "GitHub")):
            if d <= last:
                add(d, amount, payee, "Subscriptions")
        for d in range(1, last + 1):
            if rng.random() < 0.45:
                add(d, -round(rng.uniform(6, 48), 2), rng.choice(["Blue Bottle", "Sweetgreen", "Joe's Pizza", "Ramen Ya"]), "Dining")
            if rng.random() < 0.18:
                add(d, -round(rng.uniform(30, 120), 2), rng.choice(["Trader Joe's", "Whole Foods"]), "Groceries")
            if rng.random() < 0.12:
                add(d, -round(rng.uniform(8, 30), 2), rng.choice(["Uber", "Metro card"]), "Transport")
        if last >= 18 and rng.random() < 0.7:
            add(18, -round(rng.uniform(49, 299), 2), rng.choice(["Sweetwater", "Native Instruments", "Reverb"]), "Music gear")

    people = [("Sam Rivera", "partner", "06-14", 1, [("imessage", "+15550100001")], 0),
              ("Mom", "family", day(1)[5:], 7, [("imessage", "+15550100002")], 5),
              ("Grandma Lu", "family", "11-29", 14, [("imessage", "+15550100003")], 26),
              ("Theo Park", "music", "09-09", 14, [("discord", "theo.wav")], 3),
              ("Nadia Okafor", "music", None, 30, [("instagram", "nadia.sings")], 41),
              ("Jonas Weber", "work", None, 21, [("discord", "jonasw")], 8),
              ("Priya Shah", "friend", day(9)[5:], 21, [("imessage", "+15550100007")], 33),
              ("Leo Martins", "friend", None, 45, [("instagram", "leo.climbs")], 12),
              ("Ava Chen", "friend", "01-21", 30, [("whatsapp", "+15550100009")], 64)]
    for name, relation, birthday, cadence, handles, ago in people:
        store.create("people", {"name": name, "relation": relation, "birthday": birthday, "cadence_days": cadence,
                                "handles": [{"network": n, "value": v} for n, v in handles], "last_contact": day(-ago),
                                "notes": {"Theo Park": "Mix engineer. Owes me notes on v7.",
                                          "Nadia Okafor": "Vocalist for Glass Hours.",
                                          "Grandma Lu": "Likes calls on Sunday afternoons."}.get(name, "")})
    store.set_setting("name", "Jace")


class DemoMessages:
    """In-memory chats across networks; sending works, and a friend answers a few seconds later."""

    id = "demo"
    label = "Demo inbox"

    def __init__(self):
        now = datetime.now().astimezone()
        rng = random.Random(3)
        self._dirty = False
        self.threads = {}
        seeds = [
            ("imessage", "Sam Rivera", False, ["are we still on for dinner?", "yes!! 7:30?", "perfect, I'll book the ramen place",
                                                "🍜 can't wait", "also bring the vinyl you said you'd lend me"], 2),
            ("discord", "Theo Park", False, ["listened to v7 twice", "kick is way better", "chorus still feels narrow tho",
                                              "try widening the pad bus?", "ok will do, sending v8 tonight"], 1),
            ("instagram", "nadia.sings", False, ["omg the Glass Hours demo 😭", "when do you want to track vocals?",
                                                  "maybe next weekend?", "saturday works!"], 1),
            ("discord", "#mix-feedback · Bedroom Producers", True, ["anyone tried the new Soundtoys bundle?",
                                                                    "dropped my track in #wip", "that bassline is sick",
                                                                    "thanks! still fighting the low end"], 3),
            ("imessage", "Mom", False, ["Did you eat?", "yes mom", "Call grandma this weekend ❤️"], 0),
            ("whatsapp", "Ava Chen", False, ["back in town in December!", "we should climb"], 0),
            ("imessage", "Climbing crew", True, ["V5 at the new gym is brutal", "Thursday?", "I'm in", "same"], 0),
            ("instagram", "leo.climbs", False, ["sent you a reel", "lol that fall"], 0),
        ]
        for n, (network, title, group, lines, unread) in enumerate(seeds):
            chat_id = f"demo:{n}"
            start = now - timedelta(minutes=12 + n * 97 + rng.randint(0, 40))
            messages = []
            for i, text in enumerate(lines):
                mine = (i % 2 == 1) if not group else (i == len(lines) - 2)
                sender = "You" if mine else (rng.choice(["Kai", "Mo", "Rin"]) if group else title)
                messages.append({"id": f"{chat_id}:{i}", "chat_id": chat_id, "from_me": mine, "sender": sender,
                                 "text": text, "at": (start + timedelta(minutes=i * 3)).isoformat(timespec="seconds"),
                                 "reactions": [{"emoji": "❤️", "from_me": True, "sender": "You"}] if text.startswith("🍜") else [],
                                 "attachments": [], "status": "read" if mine else None})
            self.threads[chat_id] = {"id": chat_id, "network": network, "provider": self.id, "title": title,
                                     "group": group, "unread": unread, "participants": [{"name": title, "handle": ""}],
                                     "messages": messages}

    def status(self):
        return {"id": self.id, "label": self.label, "state": "ready",
                "networks": sorted({t["network"] for t in self.threads.values()})}

    def chats(self):
        out = []
        for thread in self.threads.values():
            last = thread["messages"][-1]
            out.append({k: v for k, v in thread.items() if k != "messages"} |
                       {"at": last["at"], "preview": {"text": last["text"], "from_me": last["from_me"]}})
        return out

    def messages(self, chat_id, limit=120, before=None):
        if chat_id not in self.threads:
            raise ProviderError("chat_not_found")
        self.threads[chat_id]["unread"] = 0
        return self.threads[chat_id]["messages"][-limit:]

    def _append(self, chat_id, text, mine):
        thread = self.threads[chat_id]
        thread["messages"].append({
            "id": f"{chat_id}:{len(thread['messages'])}", "chat_id": chat_id, "from_me": mine,
            "sender": "You" if mine else thread["title"], "text": text, "reactions": [], "attachments": [],
            "at": datetime.now().astimezone().isoformat(timespec="seconds"), "status": "delivered" if mine else None})
        if not mine:
            thread["unread"] += 1
        self._dirty = True

    async def send(self, chat_id, text):
        if chat_id not in self.threads:
            raise ProviderError("chat_not_found")
        self._append(chat_id, text, True)

        async def reply():
            await asyncio.sleep(3.5)
            self._append(chat_id, random.choice(["haha yes", "sounds good!", "👀", "ok sending it now", "love that"]), False)

        if not self.threads[chat_id]["group"]:
            asyncio.get_running_loop().create_task(reply())

    def changed(self):
        dirty, self._dirty = self._dirty, False
        return dirty
