"""Public API requests through the actual MCP server and its recorded contracts."""

import asyncio
import base64
import copy
import json
import socket
import time
import unittest
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import httpx
from mcp import Client

from scripts.fixtures.workspace_api import APIFixture, FakeAuth
from src.integrations.auth import AuthFailure, AuthRequired
from src.integrations.mcp import MCPIntegration, MCPRuntime
from src.integrations.workspace import SCOPE_BASE, TOOLS
from src.integrations.workspace.auth import GoogleAPIAuthAdapter
from src.integrations.workspace.calendar import Calendar
from src.integrations.workspace.chat import Chat
from src.integrations.workspace.chat import message as chat_message
from src.integrations.workspace.client import APIError, GoogleAPI
from src.integrations.workspace.drive import binary_text
from src.integrations.workspace.gmail import Gmail
from src.integrations.workspace.gmail import message as gmail_message
from src.integrations.workspace.server import build_server
from tests.test_integrations import MemoryStorage


def cases():
    gmail = {
        "create_draft": {
            "to": ["recipient@example.com"],
            "subject": "Test",
            "body": "Draft only",
            "replyToMessageId": "message-1",
        },
        "list_drafts": {},
        "get_draft": {"draftId": "draft-1"},
        "get_thread": {"threadId": "thread-1"},
        "get_message": {"messageId": "message-1"},
        "search_threads": {"query": "is:unread"},
        "list_labels": {},
        "create_label": {"displayName": "Example"},
    }
    for kind, key in (("thread", "threadId"), ("message", "messageId")):
        for action in (
            "label",
            "unlabel",
            "apply_sensitive",
            "trash",
            "untrash",
            "mark",
            "unmark",
        ):
            name = (
                f"{action}_{kind}"
                if action not in {"apply_sensitive", "mark", "unmark"}
                else f"apply_sensitive_{kind}_label"
                if action == "apply_sensitive"
                else f"{action}_{kind}_spam"
            )
            args = {key: kind + "-1"}
            if action in {"label", "unlabel"}:
                args["labelIds"] = ["label-1"]
            if action == "apply_sensitive":
                args["labelOption"] = "TRASH"
            gmail[name] = args
    gmail["update_message_labels"] = {
        "messageId": "message-1",
        "addLabelIds": ["label-1"],
    }
    return {
        "gmail": gmail,
        "drive": {
            "get_file_metadata": {"fileId": "doc-1"},
            "get_file_permissions": {"fileId": "doc-1"},
            "search_files": {"query": "name = 'Example'"},
            "list_recent_files": {},
            "copy_file": {"fileId": "doc-1", "title": "Copy"},
            "create_file": {"title": "New"},
            "read_file_content": {"fileId": "doc-1", "includeComments": True},
            "download_file_content": {"fileId": "doc-1"},
        },
        "docs": {
            "read_doc": {"documentId": "doc-1", "commentsIncluded": True},
            "update_doc": {
                "documentId": "doc-1",
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {
                                "text": "Project Alpha",
                                "matchCase": True,
                            },
                            "replaceText": "Project Luna",
                        }
                    }
                ],
            },
        },
        "sheets": {
            "get_values": {"spreadsheetId": "sheet-1", "range": "Sheet1!A1"},
            "get_spreadsheet": {"spreadsheetId": "sheet-1"},
            "update_spreadsheet": {
                "spreadsheetId": "sheet-1",
                "requests": [
                    {
                        "updateSpreadsheetProperties": {
                            "properties": {"title": "New"},
                            "fields": "title",
                        }
                    }
                ],
            },
            "update_values": {
                "spreadsheetId": "sheet-1",
                "range": "Sheet1!A1",
                "values": [["literal"]],
            },
            "update_formulas": {
                "spreadsheetId": "sheet-1",
                "range": "Sheet1!A1",
                "formulas": [["=1+1"]],
            },
            "insert_dimension": {
                "spreadsheetId": "sheet-1",
                "sheetId": 0,
                "dimension": "ROWS",
                "startIndex": 1,
                "endIndex": 2,
            },
        },
        "slides": {
            "read_presentation": {"presentationId": "deck-1"},
            "read_slide_page": {"presentationId": "deck-1", "pageId": "page-1"},
            "read_slide_page_thumbnail": {
                "presentationId": "deck-1",
                "pageId": "page-1",
            },
            "update_presentation": {
                "presentationId": "deck-1",
                "requests": [{"deleteObject": {"objectId": "page-1"}}],
            },
        },
        "calendar": {
            "list_events": {"orderBy": "startTime"},
            "get_event": {"eventId": "event-1"},
            "list_calendars": {},
            "search_events": {"query": "Meeting"},
            "create_event": {
                "summary": "New",
                "startTime": "2026-11-03T09:00:00Z",
                "endTime": "2026-11-03T10:00:00Z",
            },
            "update_event": {"eventId": "event-1", "startTime": "2026-11-03T10:00:00Z"},
            "delete_event": {"eventId": "event-1"},
            "respond_to_event": {"eventId": "event-1", "responseStatus": "accepted"},
            "suggest_time": {
                "attendeeEmails": ["guest@example.com"],
                "startTime": "2026-11-03T09:00:00Z",
                "endTime": "2026-11-03T12:00:00Z",
            },
        },
        "chat": {
            "list_messages": {"conversationId": "spaces/space-1"},
            "search_messages": {"searchParameters": {"keywords": ["hello"]}},
            "search_conversations": {},
            "send_message": {
                "conversationId": "spaces/space-1",
                "messageText": "Hello",
            },
        },
        "people": {
            "search_contacts": {"query": "Example"},
            "search_directory_people": {"query": "Example"},
            "get_user_profile": {},
        },
    }


class WorkspaceContractTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    def test_declarations_match_recorded_google_contracts(self):
        snapshot = json.loads(
            (
                Path(__file__).parents[1] / "scripts/fixtures/mcp-catalog.json"
            ).read_text()
        )
        for service, tools in TOOLS.items():
            self.assertEqual(tools, snapshot["google_" + service]["tools"])

    async def test_every_tool_executes_through_mcp_and_validates_its_output(self):
        failures = []
        for service, group in cases().items():
            self.assertEqual(set(group), {t["name"] for t in TOOLS[service]}, service)
            fixture = APIFixture()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(fixture)
            ) as http:
                async with Client(
                    build_server(service, GoogleAPI(FakeAuth(service), http)),
                    cache=None,
                ) as client:
                    catalog = await client.list_tools()
                    self.assertEqual(len(catalog.tools), len(group))
                    for name, args in group.items():
                        try:
                            result = await client.call_tool(name, args)
                            if result.is_error:
                                failures.append(
                                    (service, name, result.structured_content)
                                )
                        except Exception as error:
                            failures.append((service, name, str(error)[:1400]))
            for call in fixture.calls:
                self.assertEqual(
                    call["headers"]["authorization"], "Bearer test-access-token"
                )
                if (
                    service == "chat"
                    and call["method"] == "GET"
                    and call["path"].endswith("/messages")
                ):
                    self.assertEqual(
                        call["params"]["markupSyntax"], "MARKUP_SYNTAX_MARKDOWN"
                    )
        self.assertEqual(failures, [])

    async def test_runtime_uses_local_mcp_with_validation_and_no_extra_auth_boundary(
        self,
    ):
        fixture = APIFixture()
        auth = FakeAuth("gmail")
        auth.classify_failure = lambda failure: None
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
            server = build_server("gmail", GoogleAPI(auth, http))
            async with MCPRuntime(
                [MCPIntegration("google_gmail", "Gmail", server, auth)]
            ) as runtime:
                result = await runtime.call_tool("google_gmail", "list_labels", {})
                self.assertEqual(result["status"], "completed")
                self.assertEqual(
                    result["structured_content"]["labels"][0]["labelType"], "SYSTEM"
                )
                bad = await runtime.call_tool("google_gmail", "get_message", {})
                self.assertEqual(bad["reason"], "invalid_arguments")
                missing = await runtime.call_tool("google_gmail", "send_message", {})
                self.assertEqual(missing["reason"], "unknown_tool")
                self.assertEqual(len(fixture.calls), 1)

    async def test_api_errors_scope_requests_and_no_action_retry(self):
        for status, reason in (
            (403, "ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
            (403, "SERVICE_DISABLED"),
            (429, "RATE_LIMIT_EXCEEDED"),
            (500, "INTERNAL"),
        ):
            calls = []

            def fail(request):
                calls.append(request)
                return httpx.Response(
                    status,
                    json={
                        "error": {
                            "status": "PERMISSION_DENIED",
                            "message": "Failure",
                            "details": [{"reason": reason}],
                        }
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
                api = GoogleAPI(FakeAuth("gmail"), http)
                with self.assertRaises(APIError) as raised:
                    await api.request(
                        "gmail", "POST", "/drafts", body={"message": {"raw": "hello"}}
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    raised.exception.result["status"],
                    "auth_required"
                    if reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
                    else "failed",
                )
                self.assertNotIn(
                    "test-access-token", json.dumps(raised.exception.result)
                )

    async def test_mutation_requests_and_pagination_are_preserved(self):
        fixture = APIFixture()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
            async with Client(
                build_server("calendar", GoogleAPI(FakeAuth("calendar"), http)),
                cache=None,
            ) as client:
                await client.list_tools()
                await client.call_tool(
                    "update_event",
                    {
                        "eventId": "event-1",
                        "startTime": "2026-11-03T10:00:00Z",
                        "notificationLevel": "NONE",
                    },
                )
                call = fixture.calls[-1]
                self.assertEqual(call["headers"]["if-match"], '"revision-1"')
                self.assertEqual(
                    call["body"]["end"]["dateTime"], "2026-11-03T11:00:00+00:00"
                )
                self.assertEqual(call["params"]["sendUpdates"], "none")
            async with Client(
                build_server("drive", GoogleAPI(FakeAuth("drive"), http)), cache=None
            ) as client:
                await client.list_tools()
                response = await client.call_tool(
                    "search_files",
                    {"query": "name = 'Example'", "pageToken": "previous"},
                )
                self.assertEqual(
                    response.structured_content["nextPageToken"], "drive-next"
                )
                self.assertEqual(fixture.calls[-1]["params"]["pageToken"], "previous")
            async with Client(
                build_server("sheets", GoogleAPI(FakeAuth("sheets"), http)), cache=None
            ) as client:
                await client.list_tools()
                await client.call_tool(
                    "update_values", cases()["sheets"]["update_values"]
                )
                self.assertEqual(fixture.calls[-1]["params"]["valueInputOption"], "RAW")
                await client.call_tool(
                    "update_formulas", cases()["sheets"]["update_formulas"]
                )
                self.assertEqual(
                    fixture.calls[-1]["params"]["valueInputOption"], "USER_ENTERED"
                )


class WorkspaceEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_preconditions_fail_before_mutation(self):
        fixture = APIFixture()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
            for service, name, args in (
                (
                    "sheets",
                    "update_spreadsheet",
                    {
                        "spreadsheetId": "sheet-1",
                        "requests": [],
                        "writeControl": {"requiredRevisionId": "revision"},
                    },
                ),
                (
                    "drive",
                    "download_file_content",
                    {"fileId": "doc-1", "revisionId": "old-revision"},
                ),
                (
                    "calendar",
                    "create_event",
                    {
                        "summary": "Invalid zone",
                        "startTime": "2026-11-03T10:00:00Z",
                        "endTime": "2026-11-03T11:00:00Z",
                        "timeZone": "Not/AZone",
                    },
                ),
            ):
                async with Client(
                    build_server(service, GoogleAPI(FakeAuth(service), http)),
                    cache=None,
                ) as client:
                    await client.list_tools()
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
                    self.assertEqual(
                        result.structured_content["reason"], "invalid_arguments"
                    )
            self.assertTrue(all(call["method"] == "GET" for call in fixture.calls))

    async def test_calendar_dst_and_midnight_use_elapsed_minutes(self):
        api = type("API", (), {})()
        api.request = AsyncMock(return_value={"calendars": {"primary": {"busy": []}}})
        calendar = Calendar(api)
        for start, end, duration, count in (
            ("2026-11-01T00:30:00-04:00", "2026-11-01T03:30:00-05:00", 60, 4),
            ("2026-03-08T00:30:00-05:00", "2026-03-08T04:30:00-04:00", 60, 3),
            ("2026-11-03T23:30:00-05:00", "2026-11-04T00:00:00-05:00", 30, 1),
        ):
            with self.subTest(start=start):
                result = await calendar.suggest_time(
                    [], start, end, duration, timeZone="America/Toronto"
                )
                slots = result["timeSlots"]
                self.assertEqual(len(slots), count)
                for slot in slots:
                    a = datetime.fromisoformat(slot["start"]["dateTime"]).astimezone(
                        timezone.utc
                    )
                    b = datetime.fromisoformat(slot["end"]["dateTime"]).astimezone(
                        timezone.utc
                    )
                    self.assertEqual((b - a).total_seconds(), duration * 60)
                    self.assertLessEqual(
                        b, datetime.fromisoformat(end).astimezone(timezone.utc)
                    )

    async def test_calendar_preferences_conflicts_and_output_mapping(self):
        api = type("API", (), {})()
        api.request = AsyncMock(
            return_value={
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-11-03T09:00:00-05:00",
                                "end": "2026-11-03T09:45:00-05:00",
                            }
                        ]
                    }
                }
            }
        )
        result = await Calendar(api).suggest_time(
            [],
            "2026-11-03T00:00:00-05:00",
            "2026-11-03T10:00:00-05:00",
            30,
            {"startHour": "09:00", "endHour": "10:00"},
            "America/Toronto",
        )
        self.assertEqual(result["timeSlots"], [])
        fixture = APIFixture()
        fixture.event.update(
            eventType="workingLocation",
            workingLocationProperties={
                "type": "customLocation",
                "customLocation": {"label": "Library"},
            },
            conferenceData={
                "conferenceSolution": {"name": "Google Meet"},
                "entryPoints": [
                    {
                        "entryPointType": "video",
                        "uri": "https://meet.google.com/abc-defg-hij",
                        "meetingCode": "abc-defg-hij",
                    },
                    {
                        "entryPointType": "phone",
                        "uri": "tel:+15551234567",
                        "pin": "123",
                        "regionCode": "CA",
                    },
                ],
            },
        )
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http,
            Client(
                build_server("calendar", GoogleAPI(FakeAuth("calendar"), http)),
                cache=None,
            ) as client,
        ):
            await client.list_tools()
            result = await client.call_tool("get_event", {"eventId": "event-1"})
            value = result.structured_content
            self.assertFalse(result.is_error)
            self.assertEqual(
                value["workingLocationProperties"],
                {"type": "CUSTOM_LOCATION", "customLocationLabel": "Library"},
            )
            self.assertEqual(
                value["conferenceData"]["phoneEntryPoints"][0]["codes"][0]["codeType"],
                "PIN",
            )
            calendar = Calendar(GoogleAPI(FakeAuth("calendar"), http))
            body = await calendar._body(
                {
                    "startTime": "2026-11-03T10:00:00+09:00",
                    "timeZone": "America/Toronto",
                },
                fixture.event,
            )
            self.assertEqual(body["start"]["dateTime"], "2026-11-03T10:00:00-05:00")
            self.assertEqual(body["end"]["dateTime"], "2026-11-03T11:00:00-05:00")

    async def test_chat_partial_rosters_pagination_and_enum_mapping(self):
        calls = []

        def respond(request):
            calls.append(request)
            if request.url.path.endswith(":findDirectMessage"):
                return httpx.Response(404, json={"error": {"message": "No DM"}})
            if "/members/" in request.url.path:
                return httpx.Response(200, json={"state": "JOINED"})
            return httpx.Response(
                200,
                json={
                    "spaces": [
                        {
                            "name": "spaces/group",
                            "spaceType": "GROUP_CHAT",
                            "displayName": "Extra colleagues",
                        }
                    ],
                    "nextPageToken": "next",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            chat = Chat(GoogleAPI(FakeAuth("chat"), http))
            first = await chat.search_conversations(participants=["person@example.com"])
            self.assertEqual(
                first["conversations"][0]["conversationId"], "spaces/group"
            )
            await chat.search_conversations(
                participants=["person@example.com"], pageToken=first["nextPageToken"]
            )
            self.assertEqual(calls[-2].url.params["pageToken"], "next")
            self.assertEqual(
                sum(r.url.path.endswith(":findDirectMessage") for r in calls), 1
            )
            cursor = base64.urlsafe_b64encode(
                json.dumps({"query": "wrong", "token": []}).encode()
            ).decode()
            with self.assertRaises(ValueError):
                await chat.search_conversations(
                    participants=["person@example.com"], pageToken=cursor
                )
        value = chat_message(
            {"sender": {"name": "users/app", "type": "BOT"}, "attachment": [{}]}
        )
        self.assertEqual(value["sender"]["userType"], "APP")
        self.assertEqual(value["sender"]["userId"], "users/app")
        self.assertEqual(value["attachments"][0]["source"], "SOURCE_UNSPECIFIED")

    async def test_gmail_metadata_and_utf8_draft_mime(self):
        fixture = APIFixture()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http:
            gmail = Gmail(GoogleAPI(FakeAuth("gmail"), http))
            await gmail.get_message("message-1", "MINIMAL")
            self.assertEqual(fixture.calls[-1]["params"]["format"], "metadata")
            await gmail.search_threads(query="is:unread", pageToken="next")
            request = next(c for c in fixture.calls if c["path"].endswith("/threads"))
            self.assertEqual(request["params"]["q"], "(is:unread) -in:draft")
            await gmail.create_draft(
                to=["reader@example.com"],
                subject="Café",
                body="Résumé",
                htmlBody="<b>Résumé</b>",
                replyToMessageId="message-1",
            )
            raw = fixture.calls[-1]["body"]["message"]["raw"]
            mail = BytesParser(policy=policy.default).parsebytes(
                base64.urlsafe_b64decode(raw)
            )
            self.assertEqual(str(mail["Subject"]), "Café")
            self.assertEqual(mail["In-Reply-To"], "<original@example.com>")
            self.assertIn(
                "Résumé", mail.get_body(preferencelist=("plain",)).get_content()
            )
            self.assertTrue(fixture.calls[-1]["path"].endswith("/drafts"))
        original = copy.deepcopy(fixture.message)
        original["payload"]["headers"].append(
            {"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}
        )
        original["payload"]["body"]["data"] = base64.urlsafe_b64encode(
            "Café".encode("iso-8859-1")
        ).decode()
        self.assertEqual(gmail_message(original)["plaintextBody"], "Café")

    async def test_partial_writes_and_timeouts_never_replay(self):
        calls = []

        def fail_after_parent(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"labels": []})
            if len(calls) == 2:
                return httpx.Response(200, json={"id": "parent", "name": "Parent"})
            raise httpx.ReadTimeout("hidden", request=request)

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(fail_after_parent)) as http,
            Client(
                build_server("gmail", GoogleAPI(FakeAuth("gmail"), http)),
                cache=None,
            ) as client,
        ):
            await client.list_tools()
            result = await client.call_tool(
                "create_label", {"displayName": "Parent/Child"}
            )
            self.assertTrue(result.is_error)
            self.assertTrue(result.structured_content["outcome_unknown"])
            self.assertEqual(len(result.structured_content["completed_requests"]), 1)
            self.assertEqual(len(calls), 3)

    async def test_comments_pagination_and_binary_limits(self):
        requests = []

        def comments(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "comments": [{"id": str(len(requests)), "content": "Comment"}],
                    **({"nextPageToken": "next"} if len(requests) == 1 else {}),
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(comments)) as http:
            result = await GoogleAPI(FakeAuth("drive"), http).comments("file-1")
            self.assertEqual(len(result), 2)
            self.assertEqual(requests[1].url.params["pageToken"], "next")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, content=b"x" * (32 * 1024 * 1024 + 1)
                )
            )
        ) as http:
            with self.assertRaises(APIError) as raised:
                await GoogleAPI(FakeAuth("drive"), http).request(
                    "drive", "GET", "/files/file-1", binary=True
                )
            self.assertEqual(raised.exception.result["reason"], "download_too_large")
        archive = BytesIO()
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("word/document.xml", "<root><t>Example</t></root>")
        self.assertEqual(
            binary_text(
                archive.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            "Example",
        )

    async def test_native_lifespan_and_stdio_catalog_need_no_credentials(self):
        fixture = APIFixture()
        original = httpx.AsyncClient
        with patch(
            "src.integrations.workspace.server.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(
                transport=httpx.MockTransport(fixture), **kwargs
            ),
        ):
            async with Client(
                build_server("gmail", auth=FakeAuth("gmail")), cache=None
            ) as client:
                await client.list_tools()
                self.assertFalse((await client.call_tool("list_labels", {})).is_error)
        import sys

        from mcp import StdioServerParameters

        async with Client(
            StdioServerParameters(
                command=sys.executable,
                args=["-m", "src.integrations.workspace", "--service", "gmail"],
            ),
            cache=None,
        ) as client:
            self.assertEqual(len((await client.list_tools()).tools), 23)


class WorkspaceAuthTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, expiry):
        record = {
            "client_info": {"client_id": "test-client", "client_secret": "test-secret"},
            "tokens": {
                "access_token": "current-token",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "scope": SCOPE_BASE + "gmail.readonly",
            },
            "expires_at": expiry,
        }
        return GoogleAPIAuthAdapter(
            "gmail",
            storage=MemoryStorage("https://gmailmcp.googleapis.com/mcp/v1", record),
        ), record

    async def test_valid_existing_token_needs_no_login_or_refresh(self):
        auth, record = self.adapter(time.time() + 3600)
        with patch("src.integrations.workspace.auth.Credentials.refresh") as refresh:
            self.assertEqual(await auth.access_token(), "current-token")
            refresh.assert_not_called()
        self.assertEqual(record["tokens"]["refresh_token"], "refresh")

    async def test_concurrent_expired_reads_refresh_once_and_preserve_client(self):
        import asyncio
        from datetime import datetime, timedelta, timezone

        auth, record = self.adapter(time.time() - 1)

        def refresh(credentials, request):
            credentials.token = "new-token"
            credentials.expiry = (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).replace(tzinfo=None)

        with patch("src.integrations.workspace.auth.Credentials.refresh", refresh):
            results = await asyncio.gather(auth.access_token(), auth.access_token())
        self.assertEqual(results, ["new-token", "new-token"])
        self.assertEqual(record["client_info"]["client_secret"], "test-secret")
        self.assertEqual(record["tokens"]["refresh_token"], "refresh")

    async def test_scope_challenges_cannot_request_arbitrary_permissions(self):
        from mcp.types import CallToolResult

        auth, _ = self.adapter(time.time() + 3600)
        result = CallToolResult(
            isError=True,
            content=[],
            structuredContent={
                "status": "auth_required",
                "reason": "additional_scope_required",
                "required_scopes": [
                    SCOPE_BASE + "gmail.modify",
                    "https://malicious.example/scope",
                ],
            },
        )
        self.assertIsInstance(auth.classify_failure(result), AuthRequired)
        self.assertEqual(auth._required_scopes, {SCOPE_BASE + "gmail.modify"})

    def callback_uri(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{listener.getsockname()[1]}/oauth/callback"

    async def test_google_flow_checks_state_pkce_and_reuses_grant_after_reconnect(self):
        import hashlib

        auth, record = self.adapter(time.time() - 1)
        auth.redirect_uri = self.callback_uri()
        loop = asyncio.get_running_loop()
        opened, responses, exchanges = [], [], []

        async def callback(query):
            async with httpx.AsyncClient() as http:
                wrong = await http.get(
                    auth.redirect_uri, params={"state": "wrong", "code": "test-code"}
                )
                responses.append(wrong.status_code)
                correct = await http.get(
                    auth.redirect_uri,
                    params={"state": query["state"][0], "code": "test-code"},
                )
                responses.append(correct.status_code)

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            opened.append(query)
            asyncio.run_coroutine_threadsafe(callback(query), loop)
            return True

        def exchange(session, token_url, **kwargs):
            exchanges.append(kwargs)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(kwargs["code_verifier"].encode()).digest()
                )
                .decode()
                .rstrip("=")
            )
            self.assertEqual(challenge, opened[0]["code_challenge"][0])
            self.assertEqual(opened[0]["code_challenge_method"], ["S256"])
            self.assertEqual(token_url, "https://oauth2.googleapis.com/token")
            self.assertEqual(kwargs["code"], "test-code")
            session.token = {
                "access_token": "authorized-token",
                "token_type": "Bearer",
                "expires_at": time.time() + 3600,
                "scope": session.scope,
            }
            return session.token

        with (
            patch("src.integrations.workspace.auth.webbrowser.open", browser),
            patch("requests_oauthlib.OAuth2Session.fetch_token", exchange),
        ):
            result = await asyncio.wait_for(auth.authorize(AsyncMock()), 3)
        self.assertEqual(result.status, "connected")
        self.assertEqual(responses, [400, 200])
        self.assertEqual(len(exchanges), 1)
        self.assertEqual(record["tokens"]["refresh_token"], "refresh")
        fixture = APIFixture()
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as http,
            MCPRuntime(
                [
                    MCPIntegration(
                        "google_gmail",
                        "Gmail",
                        build_server("gmail", GoogleAPI(auth, http)),
                        auth,
                    ),
                ]
            ) as runtime,
        ):
            self.assertEqual(
                (await runtime.call_tool("google_gmail", "list_labels", {}))["status"],
                "completed",
            )
            await runtime._connections["google_gmail"].close()
            self.assertEqual(
                (await runtime.connect("google_gmail"))["status"], "connected"
            )
            self.assertEqual(
                (await runtime.call_tool("google_gmail", "list_labels", {}))["status"],
                "completed",
            )
        self.assertEqual(len(fixture.calls), 2)
        self.assertTrue(
            all(
                c["headers"]["authorization"] == "Bearer authorized-token"
                for c in fixture.calls
            )
        )

    async def test_denial_and_cancellation_preserve_existing_tokens(self):
        auth, record = self.adapter(time.time() + 3600)
        auth.redirect_uri = self.callback_uri()
        original = copy.deepcopy(record)
        loop = asyncio.get_running_loop()

        async def decline(url):
            state = parse_qs(urlsplit(url).query)["state"][0]
            async with httpx.AsyncClient() as http:
                await http.get(
                    auth.redirect_uri, params={"state": state, "error": "access_denied"}
                )

        def browser(url):
            asyncio.run_coroutine_threadsafe(decline(url), loop)
            return True

        with patch("src.integrations.workspace.auth.webbrowser.open", browser):
            self.assertEqual(
                (await asyncio.wait_for(auth.authorize(AsyncMock()), 3)).status,
                "denied",
            )
        self.assertEqual(record, original)
        pending = asyncio.Event()

        async def progress(value):
            pending.set()

        with patch(
            "src.integrations.workspace.auth.webbrowser.open", return_value=True
        ):
            task = asyncio.create_task(auth.authorize(progress))
            await asyncio.wait_for(pending.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(record, original)
        self.assertFalse(auth.storage._interactive.get())
        async with httpx.AsyncClient() as http:
            with self.assertRaises(httpx.ConnectError):
                await http.get(auth.redirect_uri)

    async def test_only_revoked_refresh_clears_tokens(self):
        from google.auth.exceptions import RefreshError

        for reason, retryable, failure in (
            ("server_error", True, AuthFailure),
            ("invalid_grant", False, AuthRequired),
        ):
            auth, record = self.adapter(time.time() - 1)
            with patch(
                "src.integrations.workspace.auth.Credentials.refresh",
                side_effect=RefreshError(reason, retryable=retryable),
            ):
                with self.assertRaises(failure):
                    await auth.access_token()
            self.assertEqual(record["tokens"] is None, reason == "invalid_grant")
