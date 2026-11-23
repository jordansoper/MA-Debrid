"""Async client for the public Deezer API, mapped to Music Assistant media models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Track,
    UniqueList,
)

from .const import DEEZER_BASE_URL
from .http_utils import request_json

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .http_utils import ThrottlerLike

DEEZER_MAX_LIMIT = 100
DEEZER_QUOTA_ERROR = 4
DEEZER_NOT_FOUND_ERRORS = frozenset({800, 801})


class DeezerClient:
    """Search and look up Deezer tracks and albums."""

    def __init__(
        self,
        session: ClientSession,
        *,
        provider_instance_id: str,
        provider_domain: str,
        throttler: ThrottlerLike | None = None,
        base_url: str = DEEZER_BASE_URL,
    ) -> None:
        """
        Initialize the Deezer client.

        :param session: The HTTP session of the Music Assistant server.
        :param provider_instance_id: Provider instance id used on created media items.
        :param provider_domain: Provider domain used in provider mappings.
        :param throttler: Optional async rate limiter for Deezer requests.
        :param base_url: Deezer API base URL.
        """
        self._session = session
        self._provider_instance_id = provider_instance_id
        self._provider_domain = provider_domain
        self._throttler = throttler
        self._base_url = base_url.rstrip("/")

    async def search_tracks(self, query: str, limit: int = 25) -> list[Track]:
        """
        Search Deezer for tracks.

        :param query: Free text search query.
        :param limit: Maximum number of results, capped at Deezer's page size.
        """
        payload = await self._get(
            "/search/track",
            {"q": query, "limit": _clamp_limit(limit)},
        )
        return [self.parse_track(item) for item in _data_items(payload) if item.get("id")]

    async def search_albums(self, query: str, limit: int = 25) -> list[Album]:
        """
        Search Deezer for albums.

        :param query: Free text search query.
        :param limit: Maximum number of results, capped at Deezer's page size.
        """
        payload = await self._get(
            "/search/album",
            {"q": query, "limit": _clamp_limit(limit)},
        )
        return [self.parse_album(item) for item in _data_items(payload) if item.get("id")]

    async def get_track(self, prov_track_id: str) -> Track:
        """
        Return the full Deezer metadata of a track.

        :param prov_track_id: Deezer track id.
        """
        payload = await self._get(f"/track/{quote(str(prov_track_id), safe='')}")
        return self.parse_track(payload, fallback_id=str(prov_track_id))

    async def get_album(self, prov_album_id: str) -> Album:
        """
        Return the full Deezer metadata of an album.

        :param prov_album_id: Deezer album id.
        """
        payload = await self._get(f"/album/{quote(str(prov_album_id), safe='')}")
        return self.parse_album(payload, fallback_id=str(prov_album_id))

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Return the tracks of a Deezer album, in album order.

        :param prov_album_id: Deezer album id.
        """
        album_id = quote(str(prov_album_id), safe="")
        album_payload = await self._get(f"/album/{album_id}")
        tracks_payload = await self._get(
            f"/album/{album_id}/tracks", params={"limit": DEEZER_MAX_LIMIT}
        )
        tracks: list[Track] = []
        for position, item in enumerate(_data_items(tracks_payload), start=1):
            # the album track list carries no album of its own: reuse the album payload
            merged = {"track_position": position, **item, "album": album_payload}
            tracks.append(self.parse_track(merged))
        return tracks

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Return the Deezer metadata of an artist.

        :param prov_artist_id: Deezer artist id.
        """
        payload = await self._get(f"/artist/{quote(str(prov_artist_id), safe='')}")
        artist = self._parse_artist(payload) if isinstance(payload, Mapping) else None
        if artist is None:
            raise MediaNotFoundError(f"Deezer has no artist {prov_artist_id}")
        return artist

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Return the albums of a Deezer artist.

        :param prov_artist_id: Deezer artist id.
        """
        payload = await self._get(
            f"/artist/{quote(str(prov_artist_id), safe='')}/albums",
            params={"limit": DEEZER_MAX_LIMIT},
        )
        return [self.parse_album(item) for item in _data_items(payload)]

    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Return the most popular tracks of a Deezer artist.

        :param prov_artist_id: Deezer artist id.
        """
        payload = await self._get(
            f"/artist/{quote(str(prov_artist_id), safe='')}/top",
            params={"limit": 50},
        )
        return [self.parse_track(item) for item in _data_items(payload)]

    def parse_track(self, item: Mapping[str, Any], *, fallback_id: str | None = None) -> Track:
        """
        Map a Deezer track payload to a Music Assistant track.

        :param item: Deezer track object.
        :param fallback_id: Id to use when the payload itself has none.
        """
        track_id = _as_id(item.get("id")) or fallback_id
        if not track_id:
            raise InvalidDataError("Deezer track payload has no id")

        artist_payload = item.get("artist")
        artist = self._parse_artist(artist_payload) if isinstance(artist_payload, Mapping) else None
        album_payload = item.get("album")
        album = (
            self.parse_album(album_payload, fallback_artist=artist)
            if isinstance(album_payload, Mapping)
            else None
        )
        # Deezer exposes ISRC on tracks but Music Assistant's Track model has no field for it;
        # keep it on the provider mapping so it is not lost.
        isrc = str(item.get("isrc") or "").strip() or None

        return Track(
            item_id=track_id,
            provider=self._provider_instance_id,
            name=str(item.get("title") or item.get("title_short") or "Unknown track"),
            duration=_as_int(item.get("duration")),
            artists=UniqueList([artist]) if artist is not None else UniqueList(),
            album=album,
            disc_number=_as_int(item.get("disk_number"), default=1),
            track_number=_as_int(item.get("track_position")),
            provider_mappings={
                self._mapping(
                    track_id,
                    f"https://www.deezer.com/track/{track_id}",
                    details=isrc,
                )
            },
        )

    def parse_album(
        self,
        item: Mapping[str, Any],
        *,
        fallback_id: str | None = None,
        fallback_artist: Artist | None = None,
    ) -> Album:
        """
        Map a Deezer album payload to a Music Assistant album.

        :param item: Deezer album object.
        :param fallback_id: Id to use when the payload itself has none.
        :param fallback_artist: Artist to use when the payload has none.
        """
        album_id = _as_id(item.get("id")) or fallback_id
        if not album_id:
            raise InvalidDataError("Deezer album payload has no id")

        artist_payload = item.get("artist")
        artist = (
            self._parse_artist(artist_payload)
            if isinstance(artist_payload, Mapping)
            else fallback_artist
        )
        metadata = MediaItemMetadata()
        if image := _first_link(item, "cover_xl", "cover_big", "cover_medium", "cover"):
            metadata = MediaItemMetadata(images=UniqueList([self._image(image)]))

        return Album(
            item_id=album_id,
            provider=self._provider_instance_id,
            name=str(item.get("title") or "Unknown album"),
            artists=UniqueList([artist]) if artist is not None else UniqueList(),
            year=_parse_year(item.get("release_date")),
            metadata=metadata,
            provider_mappings={
                self._mapping(album_id, f"https://www.deezer.com/album/{album_id}")
            },
        )

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Perform a GET request and translate Deezer's in-body error object."""
        payload = await request_json(
            self._session,
            "GET",
            f"{self._base_url}{path}",
            error_type=ResourceTemporarilyUnavailable,
            params=params,
            throttler=self._throttler,
        )
        error = payload.get("error") if isinstance(payload, Mapping) else None
        if isinstance(error, Mapping):
            code = _as_int(error.get("code"))
            message = str(error.get("message") or error.get("type") or "unknown error")
            if code == DEEZER_QUOTA_ERROR:
                raise ResourceTemporarilyUnavailable(f"Deezer rate limit reached: {message}")
            if code in DEEZER_NOT_FOUND_ERRORS:
                raise MediaNotFoundError(f"Deezer returned no data: {message}")
            raise ResourceTemporarilyUnavailable(f"Deezer API error: {message} (code {code})")
        return payload

    def _parse_artist(self, payload: Mapping[str, Any]) -> Artist | None:
        """Map a Deezer artist object to a Music Assistant artist."""
        artist_id = _as_id(payload.get("id"))
        name = payload.get("name")
        if not artist_id or not name:
            return None
        metadata = MediaItemMetadata()
        if image := _first_link(payload, "picture_xl", "picture_big", "picture_medium", "picture"):
            metadata = MediaItemMetadata(images=UniqueList([self._image(image)]))
        return Artist(
            item_id=artist_id,
            provider=self._provider_instance_id,
            name=str(name),
            metadata=metadata,
            provider_mappings={
                self._mapping(artist_id, f"https://www.deezer.com/artist/{artist_id}")
            },
        )

    def _mapping(
        self, item_id: str, url: str, *, details: str | None = None
    ) -> ProviderMapping:
        """Return the provider mapping for a Deezer item."""
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self._provider_domain,
            provider_instance=self._provider_instance_id,
            url=url,
            details=details,
        )

    def _image(self, path: str) -> MediaItemImage:
        """Return a remotely accessible Deezer image."""
        return MediaItemImage(
            type=ImageType.THUMB,
            path=path,
            provider=self._provider_instance_id,
            remotely_accessible=True,
        )


def _data_items(payload: Any) -> list[Mapping[str, Any]]:
    """Return the ``data`` list of a Deezer search payload."""
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, Mapping)]


def _clamp_limit(limit: int) -> int:
    """Clamp a result limit to Deezer's page size."""
    return max(1, min(limit, DEEZER_MAX_LIMIT))


def _as_id(value: Any) -> str | None:
    """Return a non-empty string id."""
    if value is None or value == "":
        return None
    return str(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    """Parse an integer defensively."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_link(payload: Mapping[str, Any], *keys: str) -> str | None:
    """Return the first non-empty string among the given keys."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _parse_year(value: Any) -> int | None:
    """Extract a year from a Deezer date string."""
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None
