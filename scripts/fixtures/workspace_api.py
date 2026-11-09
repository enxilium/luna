"""Synthetic Google REST effects shared by contract tests and latency probes."""

import base64
import copy
import json

import httpx


class FakeAuth:
    def __init__(self, service):
        self.service = service

    async def access_token(self):
        return "test-access-token"


class APIFixture:
    def __init__(self):
        self.calls = []
        self.replaced = False
        self.file = {
            "id": "doc-1",
            "name": "Example",
            "mimeType": "application/vnd.google-apps.document",
            "size": "20",
            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
        }
        self.message = {
            "id": "message-1",
            "threadId": "thread-1",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "Example"},
                    {"name": "From", "value": "sender@example.com"},
                    {"name": "Message-ID", "value": "<original@example.com>"},
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(b"Original message").decode()
                },
            },
        }
        self.event = {
            "id": "event-1",
            "etag": '"revision-1"',
            "summary": "Meeting",
            "status": "confirmed",
            "eventType": "default",
            "start": {"dateTime": "2026-11-03T09:00:00Z"},
            "end": {"dateTime": "2026-11-03T10:00:00Z"},
            "attendees": [{"email": "me@example.com", "self": True}],
        }

    def __call__(self, request):
        try:
            body = json.loads(request.content) if request.content else None
        except (UnicodeDecodeError, ValueError):
            body = None
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "params": dict(request.url.params),
                "body": body,
                "headers": dict(request.headers),
            }
        )
        path, verb = request.url.path, request.method
        value = None
        if request.url.host == "gmail.googleapis.com":
            path = path.removeprefix("/gmail/v1/users/me")
            if path == "/labels":
                value = (
                    {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]}
                    if verb == "GET"
                    else {"id": "label-1", "name": body["name"], "type": "user"}
                )
            elif path == "/threads":
                value = {
                    "threads": [{"id": "thread-1"}],
                    "nextPageToken": "gmail-next",
                    "resultSizeEstimate": 1,
                }
            elif path == "/threads/thread-1" and verb == "GET":
                value = {"id": "thread-1", "messages": [self.message]}
            elif path == "/drafts":
                value = (
                    {"drafts": [{"id": "draft-1"}], "nextPageToken": "draft-next"}
                    if verb == "GET"
                    else {"id": "draft-1", "message": self.message}
                )
            elif path == "/drafts/draft-1":
                value = {"id": "draft-1", "message": self.message}
            elif path == "/messages/message-1" or path == "/messages/message-1/modify":
                value = self.message
            elif path.endswith(("/modify", "/trash", "/untrash")):
                value = {}
        elif request.url.host == "docs.googleapis.com":
            if verb == "GET":
                document_id = path.rsplit("/", 1)[-1]
                text = (
                    "Reference notes. " * 1000 + "Validation code: LUNA-7429"
                    if document_id == "doc-large"
                    else "Project Luna"
                    if self.replaced
                    else "Project Alpha"
                )
                value = {
                    "documentId": document_id,
                    "revisionId": "rev-1",
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [{"textRun": {"content": text}}]
                                }
                            }
                        ]
                    },
                }
            else:
                self.replaced = bool(body.get("requests")) and all(
                    r.get("replaceAllText", {}).get("containsText", {}).get("text")
                    == "Project Alpha"
                    and r["replaceAllText"].get("replaceText") == "Project Luna"
                    and r["replaceAllText"]["containsText"].get("matchCase") is True
                    for r in body["requests"]
                )
                if not self.replaced:
                    return httpx.Response(
                        400,
                        json={
                            "error": {
                                "status": "INVALID_ARGUMENT",
                                "message": "The fixture requires the exact requested replacement",
                            }
                        },
                    )
                value = {"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]}
        elif request.url.host == "sheets.googleapis.com":
            if "/values/" in path:
                value = (
                    {"range": "Sheet1!A1", "values": [["value"]]}
                    if verb == "GET"
                    else {"updatedCells": 1}
                )
            else:
                value = {
                    "spreadsheetId": "sheet-1",
                    "properties": {"title": "Sheet"},
                    "sheets": [],
                    "replies": [],
                }
        elif request.url.host == "slides.googleapis.com":
            value = (
                {
                    "contentUrl": "https://example.com/thumbnail",
                    "heightPx": 200,
                    "widthPx": 300,
                }
                if path.endswith("/thumbnail")
                else {"presentationId": "deck-1", "slides": [], "replies": []}
            )
        elif request.url.host == "people.googleapis.com":
            person = {
                "names": [{"displayName": "Example"}],
                "emailAddresses": [{"value": "me@example.com"}],
            }
            value = (
                person
                if path == "/v1/people/me"
                else {"results": [{"person": person}]}
                if path.endswith("searchContacts")
                else {"people": [person], "nextPageToken": "people-next"}
            )
        elif request.url.host == "chat.googleapis.com":
            message = {
                "name": "spaces/space-1/messages/message-1",
                "text": "Hello",
                "createTime": "2026-11-03T09:00:00Z",
                "sender": {"name": "users/1", "type": "HUMAN"},
                "thread": {"name": "spaces/space-1/threads/thread-1"},
            }
            if "messages" in path:
                value = (
                    message
                    if verb == "POST" and not path.endswith(":search")
                    else {"messages": [message], "nextPageToken": "chat-next"}
                )
            elif path.endswith(":findDirectMessage"):
                value = {
                    "name": "spaces/direct-1",
                    "displayName": "Team direct",
                    "spaceType": "DIRECT_MESSAGE",
                }
            elif path.endswith(":findGroupChats"):
                value = {
                    "spaces": [
                        {
                            "name": "spaces/group-1",
                            "displayName": "Team group",
                            "spaceType": "GROUP_CHAT",
                        }
                    ]
                }
            else:
                value = {
                    "spaces": [
                        {
                            "name": "spaces/space-1",
                            "displayName": "Team",
                            "spaceType": "SPACE",
                        }
                    ],
                    "nextPageToken": "spaces-next",
                }
        elif request.url.host == "www.googleapis.com":
            if "/drive/v3" in path:
                if path.endswith("/permissions"):
                    value = {"permissions": []}
                elif path.endswith("/comments"):
                    value = {"comments": []}
                elif path.endswith("/files") and verb == "GET":
                    value = {"files": [self.file], "nextPageToken": "drive-next"}
                else:
                    value = dict(self.file)
                    if "/files/" in path:
                        value["id"] = path.split("/files/", 1)[-1].split("/")[0]
            elif "/calendar/v3" in path:
                if path.endswith("/freeBusy"):
                    value = {
                        "calendars": {v["id"]: {"busy": []} for v in body["items"]}
                    }
                elif path.endswith("/calendarList"):
                    value = {
                        "items": [
                            {
                                "id": "primary",
                                "summary": "Calendar",
                                "timeZone": "UTC",
                                "accessRole": "owner",
                            }
                        ],
                        "nextPageToken": "calendar-next",
                    }
                elif path.endswith("/calendars/primary"):
                    value = {"timeZone": "UTC"}
                elif path.endswith("/events") and verb == "GET":
                    events = (
                        [
                            dict(
                                self.event,
                                id=f"review-{i}",
                                summary=f"Design review {i}",
                                start={
                                    "dateTime": f"2026-10-05T{9 + i:02}:00:00-04:00"
                                },
                                end={"dateTime": f"2026-10-05T{10 + i:02}:00:00-04:00"},
                            )
                            for i in range(5)
                        ]
                        if "2026-10-05" in request.url.params.get("timeMin", "")
                        else [self.event]
                    )
                    value = {"items": events, "nextPageToken": "events-next"}
                elif verb == "DELETE":
                    return httpx.Response(204)
                else:
                    value = self.event
        if value is None:
            return httpx.Response(
                500,
                json={
                    "error": {
                        "message": "Unhandled fixture route",
                        "status": "INTERNAL",
                    }
                },
            )
        return httpx.Response(200, json=copy.deepcopy(value))
