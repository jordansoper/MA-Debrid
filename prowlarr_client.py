"""Client for Prowlarr's JSON search endpoint."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .const import PROWLARR_AUDIO_CATEGORIES
from .http_utils import request_json

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .http_utils import ThrottlerLike


class ProwlarrError(Exception):
    """Raised when Prowlarr cannot be reached or returns an unusable response."""


_BTIH_PATTERN = re.compile(r"urn:btih:([A-Za-z0-9]+)", re.IGNORECASE)
_HEX_HASH = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class Release:
    """A torrent release returned by Prowlarr."""

    title: str
    info_hash: str
    magnet_url: str | None
    size: int
    seeders: int
    indexer: str
    categories: tuple[int, ...] = ()
    download_url: str | None = None


class ProwlarrClient:
    """Search a Prowlarr instance for torrent releases."""

    def __init__(
        self,
        session: ClientSession,
        *,
        base_url: str,
        api_key: str,
        throttler: ThrottlerLike | None = None,
    ) -> None:
        """
        Initialize the Prowlarr client.

        :param session: The HTTP session of the Music Assistant server.
        :param base_url: Base URL of the Prowlarr instance.
        :param api_key: Prowlarr API key.
        :param throttler: Optional async rate limiter for Prowlarr requests.
        """
        base = base_url.strip().rstrip("/")
        if base.endswith("/api/v1"):
            base = base[: -len("/api/v1")]
        if not base:
            raise ProwlarrError("Prowlarr URL is empty")
        self._session = session
        self._search_url = f"{base}/api/v1/search"
        self._api_key = api_key
        self._throttler = throttler

    async def search(
        self,
        query: str,
        *,
        limit: int = 100,
        categories: tuple[str, ...] = PROWLARR_AUDIO_CATEGORIES,
    ) -> list[Release]:
        """
        Search Prowlarr for torrent releases.

        :param query: Free text search query.
        :param limit: Maximum number of releases requested.
        :param categories: Prowlarr category ids to search in.
        """
        payload = await request_json(
            self._session,
            "GET",
            self._search_url,
            error_type=ProwlarrError,
            params={
                "query": query,
                "type": "search",
                "categories": ",".join(categories),
                "limit": max(1, limit),
            },
            headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
            throttler=self._throttler,
        )
        items: list[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, Mapping):
            raw_items = payload.get("releases") or payload.get("results") or []
            items = raw_items if isinstance(raw_items, list) else []
        else:
            items = []

        releases = [self._parse_release(item) for item in items if isinstance(item, Mapping)]
        return [release for release in releases if release is not None]

    def _parse_release(self, item: Mapping[str, Any]) -> Release | None:
        """Map a Prowlarr search result to a release, if it is a usable torrent."""
        protocol = str(item.get("protocol") or "torrent").casefold()
        if protocol != "torrent":
            return None

        magnet_url = item.get("magnetUrl")
        magnet_url = magnet_url if isinstance(magnet_url, str) and magnet_url else None
        info_hash = parse_info_hash(item.get("infoHash")) or parse_info_hash(magnet_url)
        if info_hash is None:
            return None

        title = str(item.get("title") or "").strip()
        if not title:
            return None

        categories: list[int] = []
        raw_categories = item.get("categories")
        if isinstance(raw_categories, list):
            for category in raw_categories:
                if isinstance(category, Mapping) and isinstance(category.get("id"), int):
                    categories.append(category["id"])

        download_url = item.get("downloadUrl")
        return Release(
            title=title,
            info_hash=info_hash,
            magnet_url=magnet_url,
            size=_as_int(item.get("size")),
            seeders=_as_int(item.get("seeders")),
            indexer=str(item.get("indexer") or "Prowlarr"),
            categories=tuple(categories),
            download_url=download_url if isinstance(download_url, str) else None,
        )


def parse_info_hash(value: Any) -> str | None:
    """
    Return a lowercase hexadecimal BitTorrent info hash from a hash or magnet value.

    Accepts a plain 40 character hex hash, a base32 hash, or a magnet link.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if match := _BTIH_PATTERN.search(candidate):
        candidate = match.group(1)
    candidate = candidate.strip().casefold()
    if _HEX_HASH.match(candidate):
        return candidate
    if len(candidate) == 32:
        try:
            decoded = base64.b32decode(candidate.upper())
        except (binascii.Error, ValueError):
            return None
        if len(decoded) == 20:
            return decoded.hex()
    return None


def _as_int(value: Any) -> int:
    """Parse an integer defensively."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
