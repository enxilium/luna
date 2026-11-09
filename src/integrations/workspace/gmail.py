"""Gmail MCP contracts over the Gmail REST API. Drafts only; no send endpoint."""

import asyncio
import base64
from datetime import datetime, timezone
from email.message import EmailMessage, Message
from email.utils import getaddresses
from html import escape
from html.parser import HTMLParser

from .client import identifier

COLOR_PRESETS = {
    "BLACK": ("#000000", "#ffffff"),
    "DARK_GRAY": ("#434343", "#ffffff"),
    "GRAY": ("#666666", "#ffffff"),
    "LIGHT_GRAY": ("#cccccc", "#000000"),
    "WHITE": ("#ffffff", "#000000"),
    "RED": ("#fb4c2f", "#ffffff"),
    "ORANGE": ("#ffad47", "#000000"),
    "YELLOW": ("#fad165", "#000000"),
    "GREEN": ("#16a765", "#ffffff"),
    "MINT": ("#43d692", "#000000"),
    "TEAL": ("#2da2bb", "#ffffff"),
    "BLUE": ("#4a86e8", "#ffffff"),
    "PURPLE": ("#a479e2", "#ffffff"),
    "PINK": ("#f691b2", "#000000"),
    "DARK_RED": ("#822111", "#ffffff"),
    "DARK_ORANGE": ("#a46a21", "#ffffff"),
    "DARK_GREEN": ("#076239", "#ffffff"),
    "DARK_BLUE": ("#1c4587", "#ffffff"),
    "DARK_PURPLE": ("#41236d", "#ffffff"),
    "DARK_PINK": ("#83334c", "#ffffff"),
    "BROWN": ("#7a4706", "#ffffff"),
}


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def message(value, message_format="FULL_CONTENT", *, draft_id=None):
    payload = value.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    result = {
        k: value[k]
        for k in (
            "id",
            "threadId",
            "historyId",
            "internalDate",
            "labelIds",
            "sizeEstimate",
        )
        if k in value
    }
    result["viewUrl"] = "https://mail.google.com/mail/u/0/#all/" + value.get(
        "threadId", value.get("id", "")
    )
    result["sender"] = headers.get("from", "")
    for field, header in (
        ("toRecipients", "to"),
        ("ccRecipients", "cc"),
        ("bccRecipients", "bcc"),
    ):
        result[field] = [email for _, email in getaddresses([headers.get(header, "")])]
    if value.get("internalDate"):
        result["date"] = (
            datetime.fromtimestamp(int(value["internalDate"]) / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    if message_format != "METADATA_ONLY":
        result.update(
            subject=headers.get("subject", ""), snippet=value.get("snippet", "")
        )
    if message_format in {"FULL_CONTENT", "MESSAGE_FORMAT_UNSPECIFIED", "PLAIN_TEXT"}:
        texts, html, attachments = [], [], []

        def visit(part, part_id=""):
            body = part.get("body", {})
            if body.get("data") and not part.get("filename"):
                mime_headers = Message()
                for header in part.get("headers", []):
                    mime_headers[header["name"]] = header["value"]
                charset = mime_headers.get_content_charset() or "utf-8"
                try:
                    text = decode(body["data"]).decode(charset, errors="replace")
                except LookupError:
                    text = decode(body["data"]).decode("utf-8", errors="replace")
                if part.get("mimeType") == "text/plain":
                    texts.append(text)
                elif part.get("mimeType") == "text/html":
                    html.append(text)
            if body.get("attachmentId"):
                attachments.append(
                    {
                        "id": body["attachmentId"],
                        "partId": part_id,
                        "filename": part.get("filename", ""),
                        "mimeType": part.get("mimeType", ""),
                    }
                )
            for index, child in enumerate(part.get("parts", [])):
                visit(child, f"{part_id}.{index}" if part_id else str(index))

        visit(payload)
        if not texts and html:
            parser = _Text()
            parser.feed("\n".join(html))
            texts = [" ".join(parser.parts)]
        result.update(
            plaintextBody="\n".join(texts),
            attachmentIds=[a["id"] for a in attachments],
            attachments=attachments,
        )
        if message_format != "PLAIN_TEXT":
            result["htmlBody"] = "\n".join(html)
    if message_format == "RAW":
        result["raw"] = value.get("raw", "")
    if headers.get("message-id"):
        result["originalMessageId"] = headers["message-id"]
    if draft_id:
        result.update(
            id=draft_id,
            messageId=value.get("id", ""),
            viewUrl="https://mail.google.com/mail/u/0/#drafts/"
            + value.get("threadId", ""),
        )
        for key in (
            "historyId",
            "internalDate",
            "sizeEstimate",
            "attachmentIds",
            "attachments",
            "originalMessageId",
        ):
            result.pop(key, None)
    return result


class Gmail:
    def __init__(self, api):
        self.api = api

    @staticmethod
    def _format(message_format):
        return {
            "format": "raw"
            if message_format == "RAW"
            else "metadata"
            if message_format in {"MINIMAL", "METADATA_ONLY"}
            else "full",
            **(
                {
                    "metadataHeaders": [
                        "From",
                        "To",
                        "Cc",
                        "Bcc",
                        "Subject",
                        "Message-ID",
                    ]
                }
                if message_format in {"MINIMAL", "METADATA_ONLY"}
                else {}
            ),
        }

    async def get_message(self, messageId, messageFormat="FULL_CONTENT"):
        raw = await self.api.request(
            "gmail",
            "GET",
            f"/messages/{identifier(messageId)}",
            params=self._format(messageFormat),
        )
        return message(raw, messageFormat)

    async def get_draft(self, draftId, messageFormat="FULL_CONTENT"):
        raw = await self.api.request(
            "gmail",
            "GET",
            f"/drafts/{identifier(draftId)}",
            params=self._format(messageFormat),
        )
        return message(raw.get("message", {}), messageFormat, draft_id=raw["id"])

    async def get_thread(self, threadId, messageFormat="FULL_CONTENT"):
        if messageFormat == "RAW":
            raise ValueError("RAW is not supported for threads")
        raw = await self.api.request(
            "gmail",
            "GET",
            f"/threads/{identifier(threadId)}",
            params=self._format(messageFormat),
        )
        messages = [
            message(m, messageFormat)
            for m in raw.get("messages", [])
            if "DRAFT" not in m.get("labelIds", [])
        ]
        return {
            "id": raw["id"],
            "messages": messages,
            "messageCount": len(messages),
            "viewUrl": "https://mail.google.com/mail/u/0/#all/" + raw["id"],
        }

    async def search_threads(
        self, query=None, pageSize=20, pageToken=None, includeTrash=False, view=None
    ):
        page = await self.api.request(
            "gmail",
            "GET",
            "/threads",
            params={
                "q": (("(" + query + ") ") if query else "") + "-in:draft",
                "maxResults": min(pageSize, 50),
                "pageToken": pageToken,
                "includeSpamTrash": includeTrash,
            },
        )
        metadata = view == "THREAD_VIEW_METADATA_ONLY"
        threads = await asyncio.gather(
            *(
                self.get_thread(t["id"], "METADATA_ONLY" if metadata else "MINIMAL")
                for t in page.get("threads", [])
            )
        )
        return {
            "threads": threads,
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
            "resultCountEstimate": str(page.get("resultSizeEstimate", len(threads))),
        }

    async def list_drafts(self, query=None, pageSize=20, pageToken=None, view=None):
        page = await self.api.request(
            "gmail",
            "GET",
            "/drafts",
            params={
                "q": query,
                "maxResults": min(pageSize, 50),
                "pageToken": pageToken,
            },
        )
        drafts = await asyncio.gather(
            *(
                self.get_draft(
                    d["id"],
                    "FULL_CONTENT" if view == "DRAFT_VIEW_FULL" else "METADATA_ONLY",
                )
                for d in page.get("drafts", [])
            )
        )
        return {
            "drafts": drafts,
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def create_draft(self, **args):
        mail = EmailMessage()
        for key in ("to", "cc", "bcc"):
            if args.get(key):
                mail[key] = ", ".join(args[key])
        mail["Subject"] = args.get("subject", "")
        body, html = args.get("body", ""), args.get("htmlBody")
        thread_id = None
        if args.get("replyToMessageId"):
            original = await self.get_message(args["replyToMessageId"])
            thread_id = original.get("threadId")
            if original.get("originalMessageId"):
                mail["In-Reply-To"] = original["originalMessageId"]
                mail["References"] = original["originalMessageId"]
            body += "\n\n" + original.get("plaintextBody", "")
            if html is not None:
                html += (
                    "<blockquote>"
                    + original.get(
                        "htmlBody", escape(original.get("plaintextBody", ""))
                    )
                    + "</blockquote>"
                )
        mail.set_content(body)
        if html is not None:
            mail.add_alternative(html, subtype="html")
        size = 0
        for index, attachment in enumerate(args.get("attachments", [])):
            data = base64.b64decode(attachment["content"], validate=True)
            size += len(data)
            if size > 25 * 1024 * 1024:
                raise ValueError("Combined attachments exceed 25 MB")
            mime = attachment.get("mimeType", "application/octet-stream").split("/", 1)
            if len(mime) != 2:
                raise ValueError("Invalid attachment MIME type")
            if attachment.get("inline"):
                part = mail.get_body(preferencelist=("html", "plain"))
                part.add_related(
                    data,
                    maintype=mime[0],
                    subtype=mime[1],
                    cid="<" + attachment.get("filename", f"attachment-{index}") + ">",
                    filename=attachment.get("filename"),
                )
            else:
                mail.add_attachment(
                    data,
                    maintype=mime[0],
                    subtype=mime[1],
                    filename=attachment.get("filename"),
                )
        value = {"raw": base64.urlsafe_b64encode(mail.as_bytes()).decode()}
        if thread_id:
            value["threadId"] = thread_id
        draft = await self.api.request(
            "gmail", "POST", "/drafts", body={"message": value}
        )
        return {
            "id": draft["id"],
            "threadId": draft.get("message", {}).get("threadId", ""),
            "viewUrl": "https://mail.google.com/mail/u/0/#drafts/"
            + draft.get("message", {}).get("threadId", ""),
        }

    @staticmethod
    def label(value):
        result = {
            ("labelId" if k == "id" else "labelType" if k == "type" else k): v
            for k, v in value.items()
        }
        if "labelType" in result:
            result["labelType"] = result["labelType"].upper()
        if "labelListVisibility" in result:
            result["labelListVisibility"] = {
                "labelShow": "LABEL_SHOW",
                "labelShowIfUnread": "LABEL_SHOW_IF_UNREAD",
                "labelHide": "LABEL_HIDE",
            }.get(result["labelListVisibility"], result["labelListVisibility"])
        if "messageListVisibility" in result:
            result["messageListVisibility"] = result["messageListVisibility"].upper()
        return result

    async def list_labels(self):
        page = await self.api.request("gmail", "GET", "/labels")
        return {"labels": [self.label(v) for v in page.get("labels", [])]}

    async def _modify(self, kind, resource_id, add=(), remove=(), *, sensitive=False):
        if not sensitive and (set(add) | set(remove)) & {
            "TRASH",
            "SPAM",
            "DRAFT",
            "SENT",
        }:
            raise ValueError(
                "Use the dedicated trash/spam tools; DRAFT and SENT are read only"
            )
        return await self.api.request(
            "gmail",
            "POST",
            f"/{kind}/{identifier(resource_id)}/modify",
            body={"addLabelIds": list(add), "removeLabelIds": list(remove)},
        )

    async def label_thread(self, threadId, labelIds):
        await self._modify("threads", threadId, add=labelIds)
        return {}

    async def unlabel_thread(self, threadId, labelIds):
        await self._modify("threads", threadId, remove=labelIds)
        return {}

    async def label_message(self, messageId, labelIds):
        await self._modify("messages", messageId, add=labelIds)
        return {}

    async def unlabel_message(self, messageId, labelIds):
        await self._modify("messages", messageId, remove=labelIds)
        return {}

    async def update_message_labels(self, messageId, addLabelIds=(), removeLabelIds=()):
        result = await self._modify("messages", messageId, addLabelIds, removeLabelIds)
        return {"messageId": result["id"], "labelIds": result.get("labelIds", [])}

    async def apply_sensitive_thread_label(self, threadId, labelOption):
        if labelOption not in {"TRASH", "SPAM"}:
            raise ValueError("Select TRASH or SPAM")
        return await (
            self.trash_thread(threadId)
            if labelOption == "TRASH"
            else self.mark_thread_spam(threadId)
        )

    async def apply_sensitive_message_label(self, messageId, labelOption):
        if labelOption not in {"TRASH", "SPAM"}:
            raise ValueError("Select TRASH or SPAM")
        return await (
            self.trash_message(messageId)
            if labelOption == "TRASH"
            else self.mark_message_spam(messageId)
        )

    async def trash_thread(self, threadId):
        await self.api.request(
            "gmail", "POST", f"/threads/{identifier(threadId)}/trash"
        )
        return {}

    async def untrash_thread(self, threadId):
        await self.api.request(
            "gmail", "POST", f"/threads/{identifier(threadId)}/untrash"
        )
        return {}

    async def trash_message(self, messageId):
        await self.api.request(
            "gmail", "POST", f"/messages/{identifier(messageId)}/trash"
        )
        return {}

    async def untrash_message(self, messageId):
        await self.api.request(
            "gmail", "POST", f"/messages/{identifier(messageId)}/untrash"
        )
        return {}

    async def mark_thread_spam(self, threadId):
        await self._modify("threads", threadId, ["SPAM"], ["INBOX"], sensitive=True)
        return {}

    async def unmark_thread_spam(self, threadId):
        await self._modify("threads", threadId, ["INBOX"], ["SPAM"], sensitive=True)
        return {}

    async def mark_message_spam(self, messageId):
        await self._modify("messages", messageId, ["SPAM"], ["INBOX"], sensitive=True)
        return {}

    async def unmark_message_spam(self, messageId):
        await self._modify("messages", messageId, ["INBOX"], ["SPAM"], sensitive=True)
        return {}

    async def create_label(
        self,
        displayName,
        autoCreateParentLabels=True,
        color=None,
        colorPreset=None,
        labelListVisibility="LABEL_SHOW",
        messageListVisibility="SHOW",
    ):
        if colorPreset and colorPreset != "LABEL_COLOR_PRESET_UNSPECIFIED":
            colors = COLOR_PRESETS.get(colorPreset.removeprefix("LABEL_COLOR_PRESET_"))
            if colors is None:
                raise ValueError("Unsupported label color preset")
            color = {"backgroundColor": colors[0], "textColor": colors[1]}
        names = [displayName]
        if autoCreateParentLabels and "/" in displayName:
            existing = {v["name"] for v in (await self.list_labels())["labels"]}
            if displayName in existing:
                raise ValueError("Label already exists")
            parts = displayName.split("/")
            names = [
                "/".join(parts[:i])
                for i in range(1, len(parts) + 1)
                if "/".join(parts[:i]) not in existing
            ]
        result = None
        for name in names:
            body = {
                "name": name,
                "labelListVisibility": {
                    "LABEL_SHOW": "labelShow",
                    "LABEL_SHOW_IF_UNREAD": "labelShowIfUnread",
                    "LABEL_HIDE": "labelHide",
                    "LABEL_LIST_VISIBILITY_UNSPECIFIED": "labelShow",
                }[labelListVisibility],
                "messageListVisibility": "hide"
                if messageListVisibility == "HIDE"
                else "show",
            }
            if color and name == displayName:
                body["color"] = color
            result = await self.api.request("gmail", "POST", "/labels", body=body)
        if result is None:
            raise ValueError("Label already exists")
        converted = self.label(result)
        if colorPreset:
            converted["colorPreset"] = colorPreset
        return converted
