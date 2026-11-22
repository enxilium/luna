"""Calendar events from macOS Calendar through EventKit: read-only, covering every account synced to this Mac."""

from datetime import date, datetime, time, timedelta
import threading

from loguru import logger

STATES = {0: "not_determined", 1: "restricted", 2: "denied", 3: "ready", 4: "write_only"}
RSVP = {1: "pending", 2: "accepted", 3: "declined", 4: "maybe"}  # EKParticipantStatus; the rest have no answer to give


def _local(ns_date):
    return datetime.fromtimestamp(ns_date.timeIntervalSince1970())


def _rsvp(event):
    """Your answer to an invitation, or None when there's nothing to answer (no guests, or you organised it)."""
    if not event.hasAttendees():
        return None
    organizer = event.organizer()
    if organizer is not None and organizer.isCurrentUser():
        return None
    for person in event.attendees() or []:
        if person.isCurrentUser():
            return RSVP.get(int(person.participantStatus()))
    return None


class Calendar:
    """Events between two days. Access is asked for only when you press Allow in Settings, never in the background."""

    def __init__(self):
        import EventKit as EK

        self.EK = EK
        self.store = EK.EKEventStore.alloc().init()

    def status(self):
        return STATES.get(int(self.EK.EKEventStore.authorizationStatusForEntityType_(self.EK.EKEntityTypeEvent)), "unavailable")

    def request(self, timeout=120):
        """Show macOS's Calendar access prompt and wait for the answer. Returns the resulting status."""
        done = threading.Event()

        def answered(granted, error):
            if error is not None:
                logger.warning("Calendar access request failed")
            done.set()

        self.store.requestFullAccessToEventsWithCompletion_(answered)
        done.wait(timeout)
        self.store = self.EK.EKEventStore.alloc().init()  # a store made before access was granted stays empty
        return self.status()

    def events(self, first, last):
        """Events overlapping first..last (inclusive days), each as a plain dict. Empty without access."""
        if self.status() != "ready":
            return []
        from Foundation import NSDate

        start = NSDate.dateWithTimeIntervalSince1970_(datetime.combine(first, time()).timestamp())
        end = NSDate.dateWithTimeIntervalSince1970_(datetime.combine(last + timedelta(days=1), time()).timestamp())
        predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None)
        out = []
        for event in self.store.eventsMatchingPredicate_(predicate) or []:
            begins, ends = _local(event.startDate()), _local(event.endDate())
            out.append({"id": str(event.calendarItemIdentifier()), "title": str(event.title() or "Untitled"),
                        "start": begins.isoformat(timespec="minutes"), "end": ends.isoformat(timespec="minutes"),
                        "all_day": bool(event.isAllDay()), "location": str(event.location() or ""),
                        "calendar": str(event.calendar().title() if event.calendar() is not None else ""),
                        "rsvp": _rsvp(event), "external_id": str(event.calendarItemExternalIdentifier() or "")})
        return out


class DemoCalendar:
    """A believable week for --demo, relative to today."""

    def status(self):
        return "ready"

    def request(self, timeout=0):
        return "ready"

    def events(self, first, last):
        now = date.today()
        plan = [
            (0, "14:00", "16:00", "Studio session with Theo", "Bounce Studio, room B", "Music", None),
            (0, "19:30", "21:00", "Dinner with Sam", "Ramen place", "Personal", "accepted"),
            (1, "11:00", "12:30", "Family brunch", "Mom's place", "Family", "maybe"),
            (2, "10:00", "10:30", "Stand-up with Lumen Labs", "Video call", "Work", "pending"),
            (2, "10:15", "11:00", "Design review", "Video call", "Work", "accepted"),
            (3, None, None, "Theo in town", "", "Personal", None),
            (4, "15:00", "16:00", "Mastering call", "Video call", "Music", None),
            (7, "10:00", "12:00", "Climbing with Ava", "Boulder gym", "Personal", "accepted"),
            (9, "07:00", "08:00", "Early flight check-in", "", "Personal", None),
            (12, "19:00", "22:00", "Label showcase", "The Loft", "Music", "pending"),
            (16, "13:00", "14:00", "Workshop planning", "Lumen Labs", "Work", "maybe"),
            (-2, "13:00", "14:00", "Lunch with Priya", "Café Lune", "Work", "accepted"),
            (-4, "18:00", "20:00", "Bedroom Producers meetup", "The Loft", "Music", None),
            (-9, "23:00", "23:59", "Late session", "Home studio", "Music", None),
        ]
        out = []
        for offset, begins, ends, title, location, calendar, rsvp in plan:
            day = now + timedelta(days=offset)
            if not first <= day <= last:
                continue
            all_day = begins is None
            start = datetime.combine(day, time.fromisoformat(begins or "00:00"))
            end = datetime.combine(day + timedelta(days=all_day), time.fromisoformat(ends or "00:00"))
            out.append({"id": f"demo-{offset}-{title}", "title": title, "start": start.isoformat(timespec="minutes"),
                        "end": end.isoformat(timespec="minutes"), "all_day": all_day, "location": location, "calendar": calendar,
                        "rsvp": rsvp, "external_id": ""})
        return out


class NoCalendar:
    def status(self):
        return "unavailable"

    def request(self, timeout=0):
        return "unavailable"

    def events(self, first, last):
        return []


def open_calendar(demo=False):
    if demo:
        return DemoCalendar()
    try:
        return Calendar()
    except Exception:
        logger.warning("Calendar unavailable; the timeline will show tasks only")
        return NoCalendar()
