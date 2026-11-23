"""Async client for the TorBox torrent API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import FormData

from .const import MAX_CACHED_HASHES_PER_REQUEST, TORBOX_BASE_URL
from .http_utils import request_json

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from aiohttp import ClientSession

    from .http_utils import ThrottlerLike


class TorBoxError(Exception):
    """Raised when TorBox cannot be reached or reports a failed request."""


@dataclass(frozen=True, slots=True)
class TorrentFile:
    """One file inside a torrent."""

    name: str
    size: int
    file_id: int | None = None


@dataclass(frozen=True, slots=True)
class CachedTorrent:
    """A torrent that TorBox confirmed is cached."""

    info_hash: str
    name: str
    size: int
    files: tuple[TorrentFile, ...] = ()


class TorBoxClient:
    """TorBox API operations needed to turn a cached torrent into a direct stream."""

    def __init__(
        self,
        session: ClientSession,
        api_key: str,
        *,
        throttler: ThrottlerLike | None = None,
        base_url: str = TORBOX_BASE_URL,
    ) -> None:
        """
        Initialize the TorBox client.

        :param session: The HTTP session of the Music Assistant server.
        :param api_key: TorBox API bearer token.
        :param throttler: Optional async rate limiter for TorBox requests.
        :param base_url: TorBox API base URL.
        """
        self._session = session
        self._api_key = api_key
        self._throttler = throttler
        self._base_url = base_url.rstrip("/")

    @property
    def _headers(self) -> dict[str, str]:
        """Return the authorization headers for TorBox."""
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    async def check_cached(self, info_hashes: Sequence[str]) -> dict[str, CachedTorrent]:
        """
        Return the subset of the given hashes that TorBox has cached.

        :param info_hashes: Hexadecimal info hashes to check.
        """
        hashes = [normalize_info_hash(value) for value in info_hashes]
        unique_hashes = list(dict.fromkeys(value for value in hashes if value))
        cached: dict[str, CachedTorrent] = {}
        for chunk in _chunked(unique_hashes, MAX_CACHED_HASHES_PER_REQUEST):
            payload = await request_json(
                self._session,
                "GET",
                f"{self._base_url}/torrents/checkcached",
                error_type=TorBoxError,
                params={
                    "hash": ",".join(chunk),
                    "format": "list",
                    "list_files": "true",
                },
                headers=self._headers,
                throttler=self._throttler,
            )
            self._check_success(payload, "check cached torrents")
            data = payload.get("data") if isinstance(payload, dict) else None
            cached.update(self._parse_cached(data))
        return cached

    async def add_torrent(self, info_hash: str, magnet_url: str | None = None) -> int:
        """
        Add a cached torrent to the TorBox account without starting a download.

        :param info_hash: Hexadecimal info hash of the torrent.
        :param magnet_url: Magnet link of the release, when known.
        """
        form = FormData()
        form.add_field("magnet", magnet_url or f"magnet:?xt=urn:btih:{info_hash}")
        form.add_field("add_only_if_cached", "true")
        payload = await request_json(
            self._session,
            "POST",
            f"{self._base_url}/torrents/createtorrent",
            error_type=TorBoxError,
            data=form,
            headers=self._headers,
            throttler=self._throttler,
        )
        self._check_success(payload, f"add torrent {info_hash}")
        data = payload.get("data")
        torrent_id = data.get("torrent_id") if isinstance(data, dict) else data
        if isinstance(torrent_id, dict):
            torrent_id = torrent_id.get("id")
        if torrent_id is None:
            raise TorBoxError(f"TorBox did not return a torrent id for {info_hash}")
        try:
            return int(torrent_id)
        except (TypeError, ValueError) as err:
            raise TorBoxError(f"TorBox returned an invalid torrent id for {info_hash}") from err

    async def get_torrent_files(self, torrent_id: int) -> tuple[TorrentFile, ...]:
        """
        Return the files of a torrent on the account, bypassing TorBox's cache.

        :param torrent_id: TorBox torrent id.
        """
        payload = await request_json(
            self._session,
            "GET",
            f"{self._base_url}/torrents/mylist",
            error_type=TorBoxError,
            params={"id": str(torrent_id), "bypass_cache": "true"},
            headers=self._headers,
            throttler=self._throttler,
        )
        self._check_success(payload, f"list files of torrent {torrent_id}")
        data = payload.get("data")
        torrent = _find_torrent(data, torrent_id)
        if torrent is None:
            return ()
        return self._parse_files(torrent.get("files"))

    async def request_download_link(self, torrent_id: int, file_id: int) -> str:
        """
        Return a direct download URL for one file of a torrent.

        :param torrent_id: TorBox torrent id.
        :param file_id: TorBox file id inside that torrent.
        """
        payload = await request_json(
            self._session,
            "GET",
            f"{self._base_url}/torrents/requestdl",
            error_type=TorBoxError,
            params={
                "token": self._api_key,
                "torrent_id": torrent_id,
                "file_id": file_id,
                "zip_link": "false",
            },
            headers=self._headers,
            throttler=self._throttler,
        )
        self._check_success(payload, f"request download link for torrent {torrent_id}")
        data = payload.get("data")
        url = data if isinstance(data, str) else data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise TorBoxError(f"TorBox returned no download link for torrent {torrent_id}")
        return url

    def _parse_cached(self, data: Any) -> dict[str, CachedTorrent]:
        """Map a checkcached payload to cached torrents keyed by info hash."""
        items: list[tuple[str | None, Any]] = []
        if isinstance(data, list):
            items = [(None, item) for item in data]
        elif isinstance(data, dict):
            if "files" in data or "hash" in data:
                items = [(None, data)]
            else:
                items = [(key, value) for key, value in data.items()]

        cached: dict[str, CachedTorrent] = {}
        for key, item in items:
            if not isinstance(item, dict):
                continue
            info_hash = normalize_info_hash(item.get("hash") or key)
            if info_hash is None:
                continue
            cached[info_hash] = CachedTorrent(
                info_hash=info_hash,
                name=str(item.get("name") or info_hash),
                size=_as_int(item.get("size")),
                files=self._parse_files(item.get("files")),
            )
        return cached

    @staticmethod
    def _parse_files(raw_files: Any) -> tuple[TorrentFile, ...]:
        """Map a TorBox files list to torrent files."""
        if not isinstance(raw_files, list):
            return ()
        files: list[TorrentFile] = []
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                continue
            name = str(raw_file.get("name") or raw_file.get("short_name") or "").strip()
            if not name:
                continue
            file_id = raw_file.get("id") or raw_file.get("file_id")
            files.append(
                TorrentFile(
                    name=name,
                    size=_as_int(raw_file.get("size")),
                    file_id=_as_int(file_id) if file_id is not None else None,
                )
            )
        return tuple(files)

    @staticmethod
    def _check_success(payload: Any, action: str) -> None:
        """Raise when TorBox reports a failed request."""
        if isinstance(payload, dict) and payload.get("success") is False:
            detail = payload.get("detail") or payload.get("error") or "unknown error"
            raise TorBoxError(f"TorBox could not {action}: {detail}")


def normalize_info_hash(value: Any) -> str | None:
    """Return a lowercase hexadecimal info hash, or None when the value is not usable."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if len(candidate) == 40 and all(char in "0123456789abcdef" for char in candidate):
        return candidate
    return None


def _chunked(values: Sequence[str], size: int) -> Iterator[list[str]]:
    """Yield the values in lists of at most the given size."""
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def _find_torrent(data: Any, torrent_id: int) -> dict[str, Any] | None:
    """Find one torrent in a mylist payload."""
    if isinstance(data, dict):
        if "files" in data:
            return data
        for value in data.values():
            if isinstance(value, dict) and str(value.get("id")) == str(torrent_id):
                return value
        return None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("id")) == str(torrent_id):
                return item
        for item in data:
            if isinstance(item, dict) and "files" in item:
                return item
    return None


def _as_int(value: Any) -> int:
    """Parse an integer defensively."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
