"""Calendar requests, conditional edits, and free/busy slot selection."""

from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from .client import APIError, identifier

EVENT_TYPES = {
    "DEFAULT": "default",
    "OUT_OF_OFFICE": "outOfOffice",
    "FOCUS_TIME": "focusTime",
    "WORKING_LOCATION": "workingLocation",
    "BIRTHDAY": "birthday",
    "FROM_GMAIL": "fromGmail",
}
NOTIFICATIONS = {
    "NONE": "none",
    "EXTERNAL_ONLY": "externalOnly",
    "ALL": "all",
    "NOTIFICATION_LEVEL_UNSPECIFIED": "all",
}
WORKING_LOCATIONS = {
    "HOME_OFFICE": "homeOffice",
    "CUSTOM_LOCATION": "customLocation",
    "OFFICE_LOCATION": "officeLocation",
}


def timestamp(value, zone=None):
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        if not zone:
            raise ValueError("A time-zone offset or timeZone is required")
        value = value.replace(tzinfo=ZoneInfo(zone))
    return value


def event(value):
    result = dict(value)
    result.pop("etag", None)
    result["eventType"] = {v: k for k, v in EVENT_TYPES.items()}.get(
        value.get("eventType", "default"), "DEFAULT"
    )
    result["availability"] = (
        "AVAILABILITY_FREE"
        if value.get("transparency") == "transparent"
        else "AVAILABILITY_BUSY"
    )
    result["guestPermissions"] = {
        k: value[k]
        for k in ("guestsCanInviteOthers", "guestsCanModify", "guestsCanSeeGuests")
        if k in value
    }
    if "attendees" in value:
        result["attendees"] = [
            {("optionalAttendee" if k == "optional" else k): v for k, v in a.items()}
            for a in value["attendees"]
        ]
    if "reminders" in value:
        result.update(
            useDefaultReminders=value["reminders"].get("useDefault", False),
            overrideReminders=value["reminders"].get("overrides", []),
        )
        result.pop("reminders", None)
    if value.get("hangoutLink"):
        result["conferenceUrl"] = value["hangoutLink"]
    if "workingLocationProperties" in value:
        location = value["workingLocationProperties"]
        result["workingLocationProperties"] = {
            "type": {v: k for k, v in WORKING_LOCATIONS.items()}.get(
                location.get("type"), "WORKING_LOCATION_TYPE_UNSPECIFIED"
            ),
            **(
                {"customLocationLabel": location["customLocation"]["label"]}
                if location.get("customLocation", {}).get("label")
                else {}
            ),
            **(
                {"officeLocation": location["officeLocation"]}
                if "officeLocation" in location
                else {}
            ),
        }
    if "conferenceData" in value:
        conference = value["conferenceData"]
        mapped = {
            "solutionName": conference.get("conferenceSolution", {}).get("name", ""),
            "joiningNotesText": conference.get("notes", ""),
        }
        for entry in conference.get("entryPoints", []):
            codes = [
                {"codeType": kind, "value": entry[key]}
                for key, kind in (
                    ("accessCode", "ACCESS_CODE"),
                    ("passcode", "PASSCODE"),
                    ("password", "PASSWORD"),
                    ("meetingCode", "MEETING_CODE"),
                    ("pin", "PIN"),
                )
                if entry.get(key)
            ]
            if entry.get("entryPointType") == "phone":
                mapped.setdefault("phoneEntryPoints", []).append(
                    {
                        "phoneNumber": entry.get("uri", "").removeprefix("tel:"),
                        "regionCode": entry.get("regionCode", ""),
                        "codes": codes,
                    }
                )
            else:
                target = {
                    "video": "videoEntryPoint",
                    "sip": "sipEntryPoint",
                    "more": "moreDetailsEntryPoint",
                }.get(entry.get("entryPointType"))
                if target:
                    mapped[target] = {"uri": entry.get("uri", ""), "codes": codes}
        result["conferenceData"] = mapped
    return result


class Calendar:
    def __init__(self, api):
        self.api = api

    @staticmethod
    def _path(calendar_id, event_id=None):
        path = f"/calendars/{identifier(calendar_id)}/events"
        return path + ("/" + identifier(event_id) if event_id else "")

    async def list_events(
        self,
        calendarId="primary",
        startTime=None,
        endTime=None,
        eventType=None,
        eventTypeFilter=None,
        fullText=None,
        orderBy=None,
        pageSize=100,
        pageToken=None,
        timeZone=None,
    ):
        types = (
            eventType
            or eventTypeFilter
            or ["DEFAULT", "OUT_OF_OFFICE", "FOCUS_TIME", "FROM_GMAIL"]
        )
        if orderBy not in {None, "default", "startTime", "updated"}:
            raise ValueError("Unsupported Calendar event ordering")
        page = await self.api.request(
            "calendar",
            "GET",
            self._path(calendarId),
            params={
                "timeMin": timestamp(startTime, timeZone).isoformat()
                if startTime
                else datetime.now(timezone.utc).isoformat(),
                "timeMax": timestamp(endTime, timeZone).isoformat()
                if endTime
                else None,
                "q": fullText,
                "orderBy": None if orderBy == "default" else orderBy,
                "maxResults": min(pageSize, 250),
                "pageToken": pageToken,
                "timeZone": timeZone,
                "singleEvents": True,
                "eventTypes": [EVENT_TYPES[t] for t in types if t in EVENT_TYPES],
            },
        )
        return {
            **{
                k: v
                for k, v in page.items()
                if k not in {"items", "kind", "etag", "nextSyncToken"}
            },
            "events": [event(e) for e in page.get("items", [])],
        }

    async def get_event(self, eventId, calendarId="primary"):
        return event(
            await self.api.request("calendar", "GET", self._path(calendarId, eventId))
        )

    async def list_calendars(self, pageSize=100, pageToken=None):
        page = await self.api.request(
            "calendar",
            "GET",
            "/users/me/calendarList",
            params={"maxResults": min(pageSize, 250), "pageToken": pageToken},
        )
        return {
            "calendars": page.get("items", []),
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def search_events(self, query, pageSize=100, pageToken=None):
        page = await self.api.request(
            "calendar",
            "GET",
            self._path("primary"),
            params={
                "q": query,
                "maxResults": min(pageSize, 250),
                "pageToken": pageToken,
                "singleEvents": True,
            },
        )
        return {
            "events": [event(e) for e in page.get("items", [])],
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def _body(self, args, previous=None):
        previous = previous or {}
        body = {
            k: args[k]
            for k in (
                "summary",
                "description",
                "location",
                "colorId",
                "visibility",
                "attachments",
            )
            if k in args
        }
        zone = args.get("timeZone")
        if any(k in args for k in ("startTime", "endTime")):
            all_day = args.get("allDay", "date" in previous.get("start", {}))
            if not zone and not all_day:
                calendar = await self.api.request(
                    "calendar",
                    "GET",
                    f"/calendars/{identifier(args.get('calendarId', 'primary'))}",
                )
                zone = calendar["timeZone"]
            for source, target in (("startTime", "start"), ("endTime", "end")):
                if source in args:
                    body[target] = (
                        {"date": args[source][:10]}
                        if all_day
                        else {
                            "dateTime": timestamp(args[source], zone)
                            .replace(tzinfo=ZoneInfo(zone))
                            .isoformat()
                        }
                    )
                    if zone and not all_day:
                        body[target]["timeZone"] = zone
            if "start" in body and "endTime" not in args and previous.get("end"):
                key = "date" if all_day else "dateTime"
                if key not in previous.get("start", {}):
                    raise ValueError(
                        "Changing allDay requires both startTime and endTime"
                    )
                old_start = datetime.fromisoformat(previous["start"][key])
                old_end = datetime.fromisoformat(previous["end"][key])
                new_start = datetime.fromisoformat(body["start"][key])
                if all_day:
                    ending = new_start + (old_end - old_start)
                else:
                    duration = old_end.astimezone(timezone.utc) - old_start.astimezone(
                        timezone.utc
                    )
                    ending = (new_start.astimezone(timezone.utc) + duration).astimezone(
                        ZoneInfo(zone)
                    )
                body["end"] = {
                    key: ending.date().isoformat() if all_day else ending.isoformat()
                }
        if (
            "allDay" in args
            and previous
            and not ("startTime" in args and "endTime" in args)
        ):
            raise ValueError("Changing allDay requires startTime and endTime")
        if "eventType" in args:
            if args["eventType"] not in EVENT_TYPES:
                raise ValueError("Select a valid eventType")
            body["eventType"] = EVENT_TYPES[args["eventType"]]
        if "availability" in args:
            if args["availability"] not in {
                "AVAILABILITY_FREE",
                "AVAILABILITY_BUSY",
                "AVAILABILITY_UNSPECIFIED",
            }:
                raise ValueError("Select free or busy availability")
            body["transparency"] = (
                "transparent"
                if args["availability"] == "AVAILABILITY_FREE"
                else "opaque"
            )
        if "guestPermissions" in args:
            body.update(args["guestPermissions"])
        if "recurrenceData" in args:
            body["recurrence"] = args["recurrenceData"]
        if args.get("useDefaultReminders") and args.get("overrideReminders"):
            raise ValueError("Default and override reminders are mutually exclusive")
        if "overrideReminders" in args or "useDefaultReminders" in args:
            body["reminders"] = {"useDefault": args.get("useDefaultReminders", False)}
            if not body["reminders"]["useDefault"]:
                body["reminders"]["overrides"] = args.get("overrideReminders", [])
        if "workingLocationProperties" in args:
            location = args["workingLocationProperties"]
            kind = WORKING_LOCATIONS.get(location.get("type", "HOME_OFFICE"))
            if location.get("type") == "WORKING_LOCATION_TYPE_UNSPECIFIED":
                kind = "homeOffice"
            if not kind:
                raise ValueError("Invalid working location type")
            body["workingLocationProperties"] = {"type": kind}
            if kind == "customLocation":
                body["workingLocationProperties"][kind] = {
                    "label": location.get("customLocationLabel", "")
                }
            elif kind == "officeLocation":
                body["workingLocationProperties"][kind] = location.get(
                    "officeLocation", {}
                )
            else:
                body["workingLocationProperties"][kind] = {}
        if args.get("googleMeetUrl"):
            link = args["googleMeetUrl"]
            if not link.startswith("https://meet.google.com/"):
                link = "https://meet.google.com/" + link
            body["conferenceData"] = {
                "conferenceSolution": {"key": {"type": "hangoutsMeet"}},
                "entryPoints": [{"entryPointType": "video", "uri": link}],
            }
        elif args.get("addGoogleMeetUrl"):
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        for supplied, emails, target in (
            ("attendees", "attendeeEmails", "attendees"),
            ("addedAttendees", "addedAttendeeEmails", "attendees"),
        ):
            if supplied in args or emails in args:
                values = args.get(
                    supplied, [{"email": e} for e in args.get(emails, [])]
                )
                new = [
                    {
                        ("optional" if k == "optionalAttendee" else k): v
                        for k, v in a.items()
                    }
                    for a in values
                ]
                body[target] = (
                    previous.get(target, []) + new
                    if supplied.startswith("added")
                    else new
                )
        if "removedAttendeeEmails" in args:
            removed = set(args["removedAttendeeEmails"])
            body["attendees"] = [
                a
                for a in body.get("attendees", previous.get("attendees", []))
                if a.get("email") not in removed
            ]
        if "addedAttachments" in args or "removedAttachmentFileUrls" in args:
            removed = set(args.get("removedAttachmentFileUrls", []))
            body["attachments"] = [
                a
                for a in previous.get("attachments", [])
                if a.get("fileUrl") not in removed
            ] + args.get("addedAttachments", [])
        return body

    async def create_event(self, **args):
        body = await self._body(args)
        result = await self.api.request(
            "calendar",
            "POST",
            self._path(args.get("calendarId", "primary")),
            body=body,
            params={
                "sendUpdates": NOTIFICATIONS[args.get("notificationLevel", "ALL")],
                "conferenceDataVersion": 1,
                "supportsAttachments": True,
            },
        )
        return event(result)

    async def update_event(self, eventId, calendarId="primary", **args):
        path = self._path(calendarId, eventId)
        previous = await self.api.request("calendar", "GET", path)
        body = await self._body({**args, "calendarId": calendarId}, previous)
        result = await self.api.request(
            "calendar",
            "PATCH",
            path,
            body=body,
            headers={"If-Match": previous["etag"]},
            params={
                "sendUpdates": NOTIFICATIONS[args.get("notificationLevel", "ALL")],
                "conferenceDataVersion": 1,
                "supportsAttachments": True,
            },
        )
        return event(result)

    async def delete_event(
        self, eventId, calendarId="primary", notificationLevel="ALL"
    ):
        path = self._path(calendarId, eventId)
        previous = await self.api.request("calendar", "GET", path)
        await self.api.request(
            "calendar",
            "DELETE",
            path,
            params={"sendUpdates": NOTIFICATIONS[notificationLevel]},
            headers={"If-Match": previous["etag"]},
        )
        return event({**previous, "status": "cancelled"})

    async def respond_to_event(
        self,
        eventId,
        responseStatus,
        calendarId="primary",
        notificationLevel="ALL",
        responseComment=None,
    ):
        if responseStatus not in {"accepted", "declined", "tentative", "needsAction"}:
            raise ValueError("Invalid response status")
        path = self._path(calendarId, eventId)
        previous = await self.api.request("calendar", "GET", path)
        attendees = previous.get("attendees", [])
        own = next((a for a in attendees if a.get("self")), None)
        if not own:
            raise ValueError("The current user is not an attendee of this event")
        own["responseStatus"] = responseStatus
        if responseComment is not None:
            own["comment"] = responseComment
        return event(
            await self.api.request(
                "calendar",
                "PATCH",
                path,
                body={"attendees": attendees},
                params={"sendUpdates": NOTIFICATIONS[notificationLevel]},
                headers={"If-Match": previous["etag"]},
            )
        )

    async def suggest_time(
        self,
        attendeeEmails,
        startTime,
        endTime,
        durationMinutes=30,
        preferences=None,
        timeZone=None,
    ):
        if (
            not timeZone
            and datetime.fromisoformat(startTime.replace("Z", "+00:00")).tzinfo is None
        ):
            timeZone = (
                await self.api.request("calendar", "GET", "/calendars/primary")
            )["timeZone"]
        start, end = timestamp(startTime, timeZone), timestamp(endTime, timeZone)
        if end <= start or durationMinutes <= 0:
            raise ValueError("A positive time range and duration are required")
        zone = ZoneInfo(timeZone) if timeZone else start.tzinfo
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        prefs = preferences or {}
        limit = prefs.get("pageSize", 5)
        if not 1 <= limit <= 100:
            raise ValueError("Slot pageSize must be between 1 and 100")
        ids = list(dict.fromkeys(["primary", *attendeeEmails]))
        page = await self.api.request(
            "calendar",
            "POST",
            "/freeBusy",
            write=False,
            body={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": e} for e in ids],
            },
        )
        calendars = page.get("calendars", {})
        if any(i not in calendars or calendars[i].get("errors") for i in ids):
            raise APIError(
                {
                    "status": "failed",
                    "reason": "availability_incomplete",
                    "notice": "Availability could not be established for every attendee",
                }
            )
        busy = sorted(
            (
                timestamp(b["start"]).astimezone(timezone.utc),
                timestamp(b["end"]).astimezone(timezone.utc),
            )
            for c in calendars.values()
            for b in c.get("busy", [])
        )
        duration = timedelta(minutes=durationMinutes)
        slots = []
        cursor = start
        while cursor + duration <= end and len(slots) < limit:
            local = cursor.astimezone(zone)
            lower = datetime.combine(
                local.date(), time.fromisoformat(prefs.get("startHour", "00:00")), zone
            ).astimezone(timezone.utc)
            midnight = datetime.combine(
                local.date() + timedelta(days=1), time.min, zone
            ).astimezone(timezone.utc)
            upper = (
                datetime.combine(
                    local.date(), time.fromisoformat(prefs["endHour"]), zone
                ).astimezone(timezone.utc)
                if prefs.get("endHour")
                else midnight
            )
            if lower >= upper:
                raise ValueError("Preferred endHour must follow startHour")
            if prefs.get("excludeWeekends") and local.weekday() >= 5:
                cursor = midnight
                continue
            cursor = max(cursor, lower)
            if cursor + duration > end:
                break
            if cursor + duration > upper:
                cursor = midnight
                continue
            conflict = next(
                ((s, e) for s, e in busy if s < cursor + duration and e > cursor), None
            )
            if conflict:
                cursor = conflict[1]
                continue
            slots.append(
                {
                    "start": {"dateTime": cursor.astimezone(zone).isoformat()},
                    "end": {
                        "dateTime": (cursor + duration).astimezone(zone).isoformat()
                    },
                }
            )
            cursor += duration
        return {"timeSlots": slots}
