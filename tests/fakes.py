"""Test helpers: fake aiohttp responses and shared API payloads."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

DEEZER_TRACK = {
    "id": 3135556,
    "readable": True,
    "title": "Harder, Better, Faster, Stronger",
    "duration": 224,
    "isrc": "USUM71703861",
    "track_position": 3,
    "disk_number": 1,
    "release_date": "2001-03-07",
    "artist": {
        "id": 27,
        "name": "Daft Punk",
        "picture_xl": "https://e-cdns-images.dzcdn.net/images/artist/daft-punk/1000x1000.jpg",
    },
    "album": {
        "id": 302127,
        "title": "Discovery",
        "cover_xl": "https://e-cdns-images.dzcdn.net/images/cover/discovery/1000x1000.jpg",
        "release_date": "2001-03-07",
        "artist": {"id": 27, "name": "Daft Punk"},
    },
}


class FakeResponse:
    """Minimal aiohttp ClientResponse stand-in."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        """Store the payload and status this response reports."""
        self.status = status
        self._payload = payload
        self._body = payload if isinstance(payload, str) else json.dumps(payload)

    async def __aenter__(self) -> FakeResponse:
        """Enter the response context."""
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        """Leave the response context."""
        return False

    async def text(self) -> str:
        """Return the raw response body."""
        return self._body

    async def json(self) -> Any:
        """Return the decoded payload."""
        return self._payload


class FakeSession:
    """aiohttp ClientSession stand-in keyed by URL fragments."""

    def __init__(self) -> None:
        """Start without routes or recorded calls."""
        self.calls: list[dict[str, Any]] = []
        self._routes: list[tuple[str, FakeResponse | Callable[[dict[str, Any]], Any]]] = []

    def route(self, fragment: str, payload: Any, status: int = 200) -> FakeSession:
        """
        Answer any request whose URL contains the fragment with the payload.

        A callable payload receives the request's keyword arguments and returns the payload,
        so one route can answer differently per request (for example per query parameter).
        """
        if callable(payload):
            self._routes.append((fragment, payload))
        else:
            self._routes.append((fragment, FakeResponse(payload, status)))
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        """Return the response of the first matching route."""
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        for fragment, response in self._routes:
            if fragment in str(url):
                if callable(response):
                    return FakeResponse(response(kwargs))
                return response
        raise AssertionError(f"Unexpected request: {method} {url}")

    @property
    def closed(self) -> bool:
        """Return False, like an open session."""
        return False
