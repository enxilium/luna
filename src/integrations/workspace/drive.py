"""Drive files, uploads, exports, and explicit text-conversion limitations."""

import base64
import json
from io import BytesIO
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from .client import identifier

FILE_FIELDS = "id,name,mimeType,size,createdTime,modifiedTime,viewedByMeTime,sharedWithMeTime,webViewLink,parents,owners(emailAddress),capabilities(canAddChildren),description,fileExtension"
NATIVE = "application/vnd.google-apps."
CONVERSIONS = {
    "text/plain": NATIVE + "document",
    "text/html": NATIVE + "document",
    "text/csv": NATIVE + "spreadsheet",
    "application/msword": NATIVE + "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": NATIVE
    + "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": NATIVE
    + "spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": NATIVE
    + "presentation",
}


def file(value):
    result = {
        k: v
        for k, v in value.items()
        if k
        in {
            "id",
            "mimeType",
            "createdTime",
            "modifiedTime",
            "viewedByMeTime",
            "sharedWithMeTime",
            "description",
            "fileExtension",
        }
    }
    for source, target in (
        ("name", "title"),
        ("webViewLink", "viewUrl"),
        ("size", "fileSize"),
    ):
        if source in value:
            result[target] = value[source]
    if value.get("parents"):
        result["parentId"] = value["parents"][0]
    if value.get("owners"):
        result["owner"] = value["owners"][0].get("emailAddress", "")
    result["canAddChildren"] = value.get("capabilities", {}).get(
        "canAddChildren", False
    )
    result["resourceUri"] = (
        f"https://www.googleapis.com/drive/v3/files/{identifier(value['id'])}"
    )
    return result


def text_nodes(value):
    if isinstance(value, list):
        return "".join(text_nodes(v) for v in value)
    if not isinstance(value, dict):
        return ""
    if "textRun" in value:
        return value["textRun"].get("content", "")
    return "".join(text_nodes(v) for v in value.values())


def binary_text(data, mime):
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return data.decode("utf-8", errors="replace")
    if mime == "application/pdf":
        from pypdf import PdfReader

        return "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages
        )
    if "officedocument" in mime or "opendocument" in mime:
        with ZipFile(BytesIO(data)) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 32 * 1024 * 1024:
                raise ValueError("Expanded office document exceeds 32 MiB")
            names = archive.namelist()
            selected = [
                n
                for n in names
                if n == "word/document.xml"
                or n == "content.xml"
                or (n.startswith("ppt/slides/slide") and n.endswith(".xml"))
            ]
            if selected:
                return "\n".join(
                    " ".join(
                        e.text or ""
                        for e in ElementTree.fromstring(archive.read(n)).iter()
                        if e.tag.rsplit("}", 1)[-1] in {"t", "p", "span"}
                    )
                    for n in sorted(selected)
                )
            if "xl/workbook.xml" in names:
                shared = []
                if "xl/sharedStrings.xml" in names:
                    shared = [
                        "".join(v.itertext())
                        for v in ElementTree.fromstring(
                            archive.read("xl/sharedStrings.xml")
                        )
                    ]
                sheets = []
                for name in sorted(
                    n
                    for n in names
                    if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
                ):
                    rows = []
                    for row in ElementTree.fromstring(archive.read(name)).iter():
                        if row.tag.rsplit("}", 1)[-1] != "row":
                            continue
                        cells = []
                        for cell in row:
                            value = "".join(cell.itertext())
                            cells.append(
                                shared[int(value)] if cell.get("t") == "s" else value
                            )
                        rows.append("\t".join(cells))
                    sheets.append(name + "\n" + "\n".join(rows))
                return "\n\n".join(sheets)
    return None


class Drive:
    def __init__(self, api):
        self.api = api

    async def get_file_metadata(
        self, fileId, excludeContentSnippets=False, snippetVerbosity=None
    ):
        value = await self.api.request(
            "drive",
            "GET",
            f"/files/{identifier(fileId)}",
            params={"fields": FILE_FIELDS, "supportsAllDrives": True},
        )
        return file(value)

    async def search_files(
        self,
        query=None,
        pageSize=100,
        pageToken=None,
        excludeContentSnippets=False,
        snippetVerbosity=None,
    ):
        page = await self.api.request(
            "drive",
            "GET",
            "/files",
            params={
                "q": query,
                "pageSize": min(pageSize, 1000),
                "pageToken": pageToken,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            },
        )
        return {
            "files": [file(v) for v in page.get("files", [])],
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def list_recent_files(
        self,
        orderBy="viewedByMeTime desc",
        pageSize=100,
        pageToken=None,
        excludeContentSnippets=False,
        snippetVerbosity=None,
    ):
        page = await self.api.request(
            "drive",
            "GET",
            "/files",
            params={
                "q": "trashed = false",
                "orderBy": orderBy,
                "pageSize": min(pageSize, 1000),
                "pageToken": pageToken,
                "fields": f"nextPageToken,files({FILE_FIELDS})",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
            },
        )
        return {
            "files": [file(v) for v in page.get("files", [])],
            **(
                {"nextPageToken": page["nextPageToken"]}
                if page.get("nextPageToken")
                else {}
            ),
        }

    async def get_file_permissions(self, fileId):
        result, token, seen = [], None, set()
        while True:
            page = await self.api.request(
                "drive",
                "GET",
                f"/files/{identifier(fileId)}/permissions",
                params={
                    "fields": "nextPageToken,permissions(*)",
                    "supportsAllDrives": True,
                    "pageToken": token,
                },
            )
            result.extend(page.get("permissions", []))
            token = page.get("nextPageToken")
            if not token:
                return {"permissions": result}
            if token in seen:
                raise ValueError("Drive returned a repeated permissions page token")
            seen.add(token)

    async def copy_file(self, fileId, parentId=None, title=None):
        body = {}
        if title is not None:
            body["name"] = title
        if parentId is not None:
            body["parents"] = [parentId]
        return file(
            await self.api.request(
                "drive",
                "POST",
                f"/files/{identifier(fileId)}/copy",
                params={"fields": FILE_FIELDS, "supportsAllDrives": True},
                body=body,
            )
        )

    async def create_file(
        self,
        title,
        base64Content=None,
        textContent=None,
        content=None,
        contentMimeType=None,
        mimeType=None,
        parentId=None,
        disableConversionToGoogleType=False,
    ):
        variants = [v is not None for v in (base64Content, textContent, content)]
        if sum(variants) > 1:
            raise ValueError("Supply only one content representation")
        mime = contentMimeType or mimeType
        if any(variants) and not mime:
            raise ValueError("contentMimeType is required for file content")
        metadata = {"name": title}
        if parentId is not None:
            metadata["parents"] = [parentId]
        if mime:
            metadata["mimeType"] = (
                mime if disableConversionToGoogleType else CONVERSIONS.get(mime, mime)
            )
        if not any(variants):
            return file(
                await self.api.request(
                    "drive",
                    "POST",
                    "/files",
                    body=metadata,
                    params={"fields": FILE_FIELDS, "supportsAllDrives": True},
                )
            )
        data = (
            textContent.encode()
            if textContent is not None
            else base64.b64decode(
                base64Content if base64Content is not None else content, validate=True
            )
        )
        boundary = uuid4().hex
        payload = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        return file(
            await self.api.request(
                "drive_upload",
                "POST",
                "/files",
                content=payload,
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
                params={
                    "uploadType": "multipart",
                    "fields": FILE_FIELDS,
                    "supportsAllDrives": True,
                },
            )
        )

    async def _download(self, file_id, mime, export_mime=None, revision_id=None):
        native = mime.startswith(NATIVE)
        if native:
            if revision_id:
                raise ValueError(
                    "The public Drive API cannot export a specific native-file revision"
                )
            export_mime = export_mime or (
                "text/csv" if mime == NATIVE + "spreadsheet" else "text/plain"
            )
            data = await self.api.request(
                "drive",
                "GET",
                f"/files/{identifier(file_id)}/export",
                params={"mimeType": export_mime},
                binary=True,
            )
            return data, export_mime
        path = f"/files/{identifier(file_id)}" + (
            f"/revisions/{identifier(revision_id)}" if revision_id else ""
        )
        return await self.api.request(
            "drive",
            "GET",
            path,
            params={"alt": "media", "supportsAllDrives": True}
            if not revision_id
            else {"alt": "media"},
            binary=True,
        ), mime

    async def download_file_content(self, fileId, exportMimeType=None, revisionId=None):
        metadata = await self.get_file_metadata(fileId)
        data, mime = await self._download(
            fileId, metadata["mimeType"], exportMimeType, revisionId
        )
        return {"content": base64.b64encode(data).decode(), "mimeType": mime}

    async def read_file_content(self, fileId, includeComments=False):
        metadata = await self.get_file_metadata(fileId)
        mime = metadata["mimeType"]
        if mime == NATIVE + "document":
            data = await self.api.request(
                "docs",
                "GET",
                f"/documents/{identifier(fileId)}",
                params={"includeTabsContent": True},
            )
            text = text_nodes(data)
        elif mime == NATIVE + "presentation":
            data = await self.api.request(
                "slides", "GET", f"/presentations/{identifier(fileId)}"
            )
            text = "\n".join(text_nodes(slide) for slide in data.get("slides", []))
        elif mime == NATIVE + "spreadsheet":
            data = await self.api.request(
                "sheets",
                "GET",
                f"/spreadsheets/{identifier(fileId)}",
                params={"includeGridData": True},
            )
            text = "\n\n".join(
                sheet.get("properties", {}).get("title", "")
                + "\n"
                + "\n".join(
                    "\t".join(
                        str(c.get("formattedValue", "")) for c in row.get("values", [])
                    )
                    for grid in sheet.get("data", [])
                    for row in grid.get("rowData", [])
                )
                for sheet in data.get("sheets", [])
            )
        else:
            raw, actual_mime = await self._download(fileId, mime)
            text = binary_text(raw, actual_mime)
        result = {
            "title": metadata.get("title", ""),
            "viewUrl": metadata.get("viewUrl", ""),
            "fileContent": text or "",
            "textFormattingNotSupported": text is None,
        }
        if includeComments:
            if mime in {
                NATIVE + "document",
                NATIVE + "spreadsheet",
                NATIVE + "presentation",
            }:
                result["commentThreads"] = await self.api.comments(fileId)
            else:
                result["commentsNotSupported"] = True
        return result
