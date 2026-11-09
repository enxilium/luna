"""Docs, Sheets and Slides batch operations preserve their upstream JSON payloads."""

from .client import identifier


def field_mask(fields):
    return ",".join(field.replace(".", "/") for field in fields) if fields else None


def comments_mode(included):
    return "COMMENTS_VIEW_MODE_INCLUDED" if included else "COMMENTS_VIEW_MODE_OMITTED"


class Docs:
    def __init__(self, api):
        self.api = api

    async def read_doc(self, documentId, commentsIncluded=False):
        result = await self.api.request(
            "docs",
            "GET",
            f"/documents/{identifier(documentId)}",
            params={
                "includeTabsContent": True,
                "commentsViewMode": comments_mode(commentsIncluded),
            },
        )
        return {"content": result}

    async def update_doc(self, documentId, requests=None, writeControl=None):
        body = {"requests": requests or []}
        if writeControl is not None:
            body["writeControl"] = writeControl
        return await self.api.request(
            "docs",
            "POST",
            f"/documents/{identifier(documentId)}:batchUpdate",
            body=body,
        )


class Sheets:
    def __init__(self, api):
        self.api = api

    async def get_values(self, spreadsheetId, range):
        result = await self.api.request(
            "sheets",
            "GET",
            f"/spreadsheets/{identifier(spreadsheetId)}/values/{identifier(range)}",
        )
        return {k: result[k] for k in ("range", "values") if k in result}

    async def get_spreadsheet(
        self,
        spreadsheetId,
        commentsIncluded=False,
        fields=None,
        includeGridData=False,
        ranges=None,
    ):
        result = await self.api.request(
            "sheets",
            "GET",
            f"/spreadsheets/{identifier(spreadsheetId)}",
            params={
                "fields": field_mask(fields),
                "commentsViewMode": comments_mode(commentsIncluded),
                "includeGridData": includeGridData,
                "ranges": ranges,
            },
        )
        return {"fields": result}

    async def update_spreadsheet(self, spreadsheetId, requests, writeControl=None):
        if writeControl:
            raise ValueError(
                "The public Sheets API does not support writeControl; it cannot be silently ignored"
            )
        result = await self.api.request(
            "sheets",
            "POST",
            f"/spreadsheets/{identifier(spreadsheetId)}:batchUpdate",
            body={"requests": requests},
        )
        return {"fields": result}

    async def _values(self, spreadsheet_id, cell_range, values, option):
        result = await self.api.request(
            "sheets",
            "PUT",
            f"/spreadsheets/{identifier(spreadsheet_id)}/values/{identifier(cell_range)}",
            params={"valueInputOption": option},
            body={"range": cell_range, "values": values},
        )
        return {"fields": result}

    async def update_values(self, spreadsheetId, range, values):
        return await self._values(spreadsheetId, range, values, "RAW")

    async def update_formulas(self, spreadsheetId, range, formulas):
        return await self._values(spreadsheetId, range, formulas, "USER_ENTERED")

    async def insert_dimension(
        self,
        spreadsheetId,
        sheetId,
        dimension,
        startIndex,
        endIndex,
        inheritFromBefore=False,
    ):
        if (
            dimension not in {"ROWS", "COLUMNS"}
            or startIndex < 0
            or endIndex <= startIndex
        ):
            raise ValueError("Invalid dimension range")
        return await self.update_spreadsheet(
            spreadsheetId,
            [
                {
                    "insertDimension": {
                        "range": {
                            "sheetId": sheetId,
                            "dimension": dimension,
                            "startIndex": startIndex,
                            "endIndex": endIndex,
                        },
                        "inheritFromBefore": inheritFromBefore,
                    }
                }
            ],
        )


class Slides:
    def __init__(self, api):
        self.api = api

    async def read_presentation(
        self, presentationId, commentsIncluded=False, fields=None
    ):
        result = await self.api.request(
            "slides",
            "GET",
            f"/presentations/{identifier(presentationId)}",
            params={
                "fields": field_mask(fields),
                "commentsViewMode": comments_mode(commentsIncluded),
            },
        )
        return {"content": result}

    async def read_slide_page(
        self, presentationId, pageId, commentsIncluded=False, fields=None
    ):
        result = await self.api.request(
            "slides",
            "GET",
            f"/presentations/{identifier(presentationId)}/pages/{identifier(pageId)}",
            params={
                "fields": field_mask(fields),
                "commentsViewMode": comments_mode(commentsIncluded),
            },
        )
        return {"content": result}

    async def read_slide_page_thumbnail(
        self, presentationId, pageId, thumbnailProperties=None
    ):
        properties = thumbnailProperties or {}
        return await self.api.request(
            "slides",
            "GET",
            f"/presentations/{identifier(presentationId)}/pages/{identifier(pageId)}/thumbnail",
            params={"thumbnailProperties." + k: v for k, v in properties.items()},
        )

    async def update_presentation(self, presentationId, requests, writeControl=None):
        body = {"requests": requests}
        if writeControl is not None:
            body["writeControl"] = writeControl
        return await self.api.request(
            "slides",
            "POST",
            f"/presentations/{identifier(presentationId)}:batchUpdate",
            body=body,
        )
