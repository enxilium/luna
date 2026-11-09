"""Chat message and conversation contracts using user-authorized Chat APIs."""

import asyncio
import base64
import hashlib
import json
import re

from .client import APIError, identifier


def resource(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(prefix, value):
        raise ValueError("Invalid Google Chat resource name")
    return value


def message(value):
    result = {
        "messageId": value.get("name", ""),
        "threadId": value.get("thread", {}).get("name", ""),
        "plaintextBody": value.get("formattedText", value.get("text", "")),
        "createTime": value.get("createTime", ""),
    }
    if "sender" in value:
        sender = value["sender"]
        result["sender"] = {
            "userId": sender.get("name", ""),
            "displayName": sender.get("displayName", ""),
            "userType": {"BOT": "APP", "HUMAN": "HUMAN"}.get(
                sender.get("type"), "USER_TYPE_UNSPECIFIED"
            ),
            **({"email": sender["email"]} if sender.get("email") else {}),
        }
    if "threadReply" in value:
        result["threadedReply"] = value["threadReply"]
    if "attachment" in value:
        result["attachments"] = [
            {
                "attachmentId": v.get("name", ""),
                "filename": v.get("contentName", ""),
                "mimeType": v.get("contentType", ""),
                "source": v.get("source", "SOURCE_UNSPECIFIED"),
            }
            for v in value["attachment"]
        ]
    if "emojiReactionSummaries" in value:
        result["reactionSummaries"] = [
            {
                "count": v.get("reactionCount", 0),
                "emoji": v.get("emoji", {}).get(
                    "unicode", v.get("emoji", {}).get("customEmoji", {}).get("name", "")
                ),
            }
            for v in value["emojiReactionSummaries"]
        ]
    return result


def page(values, name, transform):
    return {
        name: [
            transform(v)
            for v in values.get("messages" if name == "messages" else "spaces", [])
        ],
        **(
            {"nextPageToken": values["nextPageToken"]}
            if values.get("nextPageToken")
            else {}
        ),
    }


class Chat:
    def __init__(self, api):
        self.api = api

    async def _participant_conversations(
        self, participants, page_size, token=None, name_query=None
    ):
        if not 1 <= len(participants) <= 49 or page_size < 1:
            raise ValueError("Use 1–49 participants and a positive pageSize")
        fingerprint = hashlib.sha256(
            json.dumps([participants, page_size, name_query]).encode()
        ).hexdigest()
        group_token = None
        if token:
            if len(token) > 8192:
                raise ValueError("Invalid conversation page token")
            try:
                cursor = json.loads(base64.urlsafe_b64decode(token))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("Invalid conversation page token") from None
            if (
                not isinstance(cursor, dict)
                or cursor.get("query") != fingerprint
                or not isinstance(cursor.get("token"), (str, type(None)))
            ):
                raise ValueError("Page token belongs to a different query")
            group_token = cursor.get("token")

        def cursor(value):
            return base64.urlsafe_b64encode(
                json.dumps({"query": fingerprint, "token": value}).encode()
            ).decode()

        spaces = []
        if not token and len(participants) == 1:
            try:
                direct = await self.api.request(
                    "chat",
                    "GET",
                    "/spaces:findDirectMessage",
                    params={"name": "users/" + participants[0].removeprefix("users/")},
                )
                if (
                    not name_query
                    or name_query.casefold() in direct.get("displayName", "").casefold()
                ):
                    spaces.append(direct)
            except APIError as error:
                if error.result.get("http_status") != 404:
                    raise
        if len(spaces) == page_size:
            return {"spaces": spaces, "nextPageToken": cursor(None)}
        # findGroupChats requires an exact roster, so it misses larger groups.
        page = await self.api.request(
            "chat",
            "GET",
            "/spaces",
            params={
                "filter": 'space_type = "GROUP_CHAT"',
                "pageSize": min(
                    page_size - 1
                    if len(participants) == 1 and page_size > 1
                    else page_size,
                    100,
                ),
                "pageToken": group_token,
            },
        )
        semaphore = asyncio.Semaphore(8)

        async def matches(space):
            if (
                name_query
                and name_query.casefold() not in space.get("displayName", "").casefold()
            ):
                return False
            async with semaphore:
                name = resource(space["name"], r"spaces/[A-Za-z0-9_-]+")
                for participant in participants:
                    try:
                        membership = await self.api.request(
                            "chat",
                            "GET",
                            f"/{name}/members/{identifier(participant.removeprefix('users/'))}",
                        )
                    except APIError as error:
                        if error.result.get("http_status") == 404:
                            return False
                        raise
                    if membership.get("state") != "JOINED":
                        return False
            return True

        candidates = page.get("spaces", [])
        included = await asyncio.gather(*(matches(v) for v in candidates))
        spaces.extend(v for v, keep in zip(candidates, included) if keep)
        return {
            "spaces": spaces,
            **(
                {"nextPageToken": cursor(page["nextPageToken"])}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def list_messages(
        self,
        conversationId,
        endTime=None,
        pageSize=20,
        pageToken=None,
        startTime=None,
        threadId=None,
    ):
        space = resource(conversationId, r"spaces/[A-Za-z0-9_-]+")
        filters = []
        if startTime:
            filters.append("create_time > " + json.dumps(startTime))
        if endTime:
            filters.append("create_time < " + json.dumps(endTime))
        if threadId:
            thread = resource(threadId, r"spaces/[A-Za-z0-9_-]+/threads/[A-Za-z0-9_-]+")
            if not thread.startswith(space + "/"):
                raise ValueError("Thread does not belong to the conversation")
            filters.append("thread.name = " + json.dumps(thread))
        result = await self.api.request(
            "chat",
            "GET",
            f"/{space}/messages",
            params={
                "pageSize": min(pageSize, 50),
                "pageToken": pageToken,
                "filter": " AND ".join(filters) or None,
                "orderBy": "createTime ASC",
                "markupSyntax": "MARKUP_SYNTAX_MARKDOWN",
            },
        )
        return page(result, "messages", message)

    async def search_messages(self, searchParameters, pageSize=25, pageToken=None):
        p = searchParameters
        filters = [json.dumps(k) for k in p.get("keywords", [])]
        for source, target, operator in (
            ("conversationId", "space.name", "="),
            ("sender", "sender.name", "="),
            ("startTime", "create_time", ">="),
            ("endTime", "create_time", "<"),
        ):
            if p.get(source):
                value = p[source]
                if source == "sender" and not value.startswith("users/"):
                    value = "users/" + value
                filters.append(f"{target} {operator} {json.dumps(value)}")
        if p.get("spaceDisplayNames"):
            filters.append(
                "("
                + " OR ".join(
                    "space.display_name:" + json.dumps(v)
                    for v in p["spaceDisplayNames"]
                )
                + ")"
            )
        if p.get("conversationTypes"):
            filters.append(
                "("
                + " OR ".join(
                    "space.space_type="
                    + json.dumps("SPACE" if v == "NAMED_SPACE" else v)
                    for v in p["conversationTypes"]
                )
                + ")"
            )
        if p.get("mentionsMe"):
            filters.append("annotations.user_mentions.user.name:users/me")
        for field, func in (("hasLink", "has_link"), ("isUnread", "is_unread")):
            if field in p:
                filters.append(("" if p[field] else "NOT ") + func + "()")
        if p.get("conversationIncludesUser"):
            spaces, token, seen = [], None, set()
            while True:
                page_result = await self._participant_conversations(
                    [p["conversationIncludesUser"]], 30, token
                )
                spaces.extend(v["name"] for v in page_result["spaces"])
                token = page_result.get("nextPageToken")
                if not token:
                    break
                if token in seen:
                    raise ValueError("Chat returned a repeated conversation page token")
                seen.add(token)
            if not spaces:
                return {"messages": []}
            filters.append(
                "(" + " OR ".join("space.name=" + json.dumps(v) for v in spaces) + ")"
            )
        result = await self.api.request(
            "chat",
            "POST",
            "/spaces/-/messages:search",
            write=False,
            body={
                "filter": " AND ".join(filters),
                "pageSize": min(pageSize, 100),
                "pageToken": pageToken or "",
                "markupSyntax": "MARKUP_SYNTAX_MARKDOWN",
            },
        )
        return page(result, "messages", message)

    async def search_conversations(
        self, pageSize=20, pageToken=None, participants=None, spaceNameQuery=None
    ):
        if participants:
            values = await self._participant_conversations(
                participants, pageSize, pageToken, spaceNameQuery
            )
        else:
            values = await self.api.request(
                "chat",
                "GET",
                "/spaces",
                params={"pageSize": min(pageSize, 1000), "pageToken": pageToken},
            )
            if spaceNameQuery:
                values["spaces"] = [
                    v
                    for v in values.get("spaces", [])
                    if spaceNameQuery.casefold() in v.get("displayName", "").casefold()
                ]

        def conversation(v):
            return {
                "conversationId": v["name"],
                "conversationType": {"SPACE": "NAMED_SPACE"}.get(
                    v.get("spaceType"),
                    v.get("spaceType", "CONVERSATION_TYPE_UNSPECIFIED"),
                ),
                "displayName": v.get("displayName", ""),
                **(
                    {"lastActiveTimestamp": v["lastActiveTime"]}
                    if v.get("lastActiveTime")
                    else {}
                ),
            }

        return page(values, "conversations", conversation)

    async def send_message(self, conversationId, messageText, threadId=None):
        space = resource(conversationId, r"spaces/[A-Za-z0-9_-]+")
        body = {"text": messageText, "markupSyntax": "MARKUP_SYNTAX_MARKDOWN"}
        params = {}
        if threadId:
            thread = resource(threadId, r"spaces/[A-Za-z0-9_-]+/threads/[A-Za-z0-9_-]+")
            if not thread.startswith(space + "/"):
                raise ValueError("Thread does not belong to the conversation")
            body["thread"] = {"name": thread}
            params["messageReplyOption"] = "REPLY_MESSAGE_OR_FAIL"
        return {
            "message": message(
                await self.api.request(
                    "chat", "POST", f"/{space}/messages", body=body, params=params
                )
            )
        }
