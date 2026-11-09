"""Bounded, authenticated requests to fixed Google API origins. No action retries."""

from urllib.parse import quote

import httpx

from . import SCOPE_BASE, WRITE_SCOPES

ORIGINS = {
    "gmail": "https://gmail.googleapis.com/gmail/v1/users/me",
    "drive": "https://www.googleapis.com/drive/v3",
    "drive_upload": "https://www.googleapis.com/upload/drive/v3",
    "docs": "https://docs.googleapis.com/v1",
    "sheets": "https://sheets.googleapis.com/v4",
    "slides": "https://slides.googleapis.com/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
    "chat": "https://chat.googleapis.com/v1",
    "people": "https://people.googleapis.com/v1",
}


def identifier(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("A nonempty resource identifier is required")
    return quote(value, safe="")


class APIError(Exception):
    def __init__(self, result):
        self.result = result


class GoogleAPI:
    def __init__(self, auth, http):
        self.auth, self.http = auth, http
        self.completed_writes = []

    async def request(
        self,
        service,
        method,
        path,
        *,
        params=None,
        body=None,
        headers=None,
        content=None,
        binary=False,
        write=None,
    ):
        token = await self.auth.access_token()
        query = [
            (k, str(v).lower() if isinstance(v, bool) else v)
            for k, value in (params or {}).items()
            if value is not None
            for v in (value if isinstance(value, list) else [value])
        ]
        mutates = (
            method in {"POST", "PUT", "PATCH", "DELETE"} if write is None else write
        )
        try:
            async with self.http.stream(
                method,
                ORIGINS[service] + path,
                params=query,
                json=body,
                content=content,
                headers={"Authorization": "Bearer " + token, **(headers or {})},
                timeout=30,
            ) as response:
                if binary and response.is_success:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 32 * 1024 * 1024:
                            raise APIError(
                                {
                                    "status": "failed",
                                    "reason": "download_too_large",
                                    "message": "Downloads above 32 MiB require an external download; no truncated data is returned.",
                                }
                            )
                        data.extend(chunk)
                    return bytes(data)
                await response.aread()
        except httpx.HTTPError:
            raise APIError(
                {
                    "status": "failed",
                    "reason": "api_connection_failed",
                    "outcome_unknown": mutates,
                    "notice": "Do not automatically repeat a mutation whose outcome is unknown.",
                }
            ) from None
        if response.is_error:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if not isinstance(error, dict):
                error = {}
            reasons = {
                v.get("reason") for v in error.get("errors", []) if isinstance(v, dict)
            }
            reasons.update(
                v.get("reason") for v in error.get("details", []) if isinstance(v, dict)
            )
            if response.status_code == 401:
                result = {"status": "auth_required", "reason": "sign_in_required"}
            elif response.status_code == 403 and reasons & {
                "insufficientPermissions",
                "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            }:
                result = {
                    "status": "auth_required",
                    "reason": "additional_scope_required",
                    "required_scopes": [
                        SCOPE_BASE + s for s in WRITE_SCOPES[self.auth.service]
                    ],
                }
            else:
                result = {
                    "status": "failed",
                    "reason": "google_api_error",
                    "http_status": response.status_code,
                    "code": error.get("status"),
                    "message": error.get("message", "Google API request failed"),
                }
            if self.completed_writes:
                result["completed_requests"] = self.completed_writes.copy()
            if mutates and response.status_code >= 500:
                result["outcome_unknown"] = True
            raise APIError(result)
        if mutates:
            self.completed_writes.append(
                {"method": method, "service": service, "path": path}
            )
        return response.json() if response.content else {}

    async def comments(self, file_id):
        result, token, seen = [], None, set()
        while True:
            page = await self.request(
                "drive",
                "GET",
                f"/files/{identifier(file_id)}/comments",
                params={
                    "fields": "nextPageToken,comments(id,content,author,modifiedTime,resolved,replies)",
                    "pageToken": token,
                    "pageSize": 100,
                },
            )

            def post(value):
                return {
                    "postId": value.get("id", ""),
                    "content": value.get("content", ""),
                    "authorName": value.get("author", {}).get("displayName", ""),
                    "modifiedTime": value.get("modifiedTime", ""),
                }

            result.extend(
                {
                    "commentId": c["id"],
                    "headPost": post(c),
                    "replies": [post(r) for r in c.get("replies", [])],
                    "status": "RESOLVED" if c.get("resolved") else "OPEN",
                }
                for c in page.get("comments", [])
            )
            token = page.get("nextPageToken")
            if not token:
                return result
            if token in seen:
                raise APIError({"status": "failed", "reason": "repeated_page_token"})
            seen.add(token)
