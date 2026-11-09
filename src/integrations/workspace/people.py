"""Contact, directory and own-profile reads through the People API."""


def person(value):
    return {
        "name": next((n.get("displayName", "") for n in value.get("names", [])), ""),
        "email": next(
            (e.get("value", "") for e in value.get("emailAddresses", [])), ""
        ),
    }


class People:
    def __init__(self, api):
        self.api = api

    async def search_directory_people(
        self, query="", pageSize=10, pageToken=None, sources=None
    ):
        params = {
            "query": query,
            "pageSize": min(pageSize, 500),
            "pageToken": pageToken,
            "readMask": "names,emailAddresses",
            "sources": [
                "DIRECTORY_SOURCE_TYPE_" + v for v in (sources or ["DOMAIN_PROFILE"])
            ],
        }
        path = (
            "/people:searchDirectoryPeople" if query else "/people:listDirectoryPeople"
        )
        if not query:
            params.pop("query")
        result = await self.api.request("people", "GET", path, params=params)
        return {
            "results": [person(v) for v in result.get("people", [])],
            **(
                {"nextPageToken": result["nextPageToken"]}
                if result.get("nextPageToken")
                else {}
            ),
        }

    async def search_contacts(self, query="", maxResults=10):
        result = await self.api.request(
            "people",
            "GET",
            "/people:searchContacts",
            params={
                "query": query,
                "pageSize": min(maxResults, 30),
                "readMask": "names,emailAddresses",
            },
        )
        return {
            "results": [person(v.get("person", {})) for v in result.get("results", [])]
        }

    async def get_user_profile(self):
        value = person(
            await self.api.request(
                "people",
                "GET",
                "/people/me",
                params={"personFields": "names,emailAddresses"},
            )
        )
        return {"name": value["name"], "emailAddress": value["email"]}
