"""End-to-end tests for StreamResolver, the code path get_stream_details uses."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError

from fakes import DEEZER_TRACK, FakeSession
from ma_debrid.deezer_client import DeezerClient
from ma_debrid.prowlarr_client import ProwlarrClient
from ma_debrid.resolver import StreamResolver
from ma_debrid.torbox_client import TorBoxClient

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
TRACK_FILE = "03 - Harder, Better, Faster, Stronger.flac"
TRACK_FILE_SIZE = 30_000_000
CDN_URL = "https://cdn.torbox.app/dl/token/abc.flac"

PROWLARR_RESPONSE = [
    {
        "guid": "https://indexer.test/details/123",
        "title": "Daft Punk - Discovery (2001) [FLAC]",
        "indexerId": 1,
        "indexer": "Test Indexer",
        "categories": [{"id": 3040, "name": "Audio/Lossless"}],
        "size": 420_000_000,
        "seeders": 18,
        "leechers": 2,
        "protocol": "torrent",
        "infoHash": INFO_HASH.upper(),
        "magnetUrl": f"magnet:?xt=urn:btih:{INFO_HASH}",
        "downloadUrl": "https://indexer.test/download/123",
    }
]

CACHED_RESPONSE = {
    "success": True,
    "error": None,
    "detail": "Found 1 cached torrents",
    "data": [
        {
            "hash": INFO_HASH,
            "name": "Daft Punk - Discovery (2001) [FLAC]",
            "size": 420_000_000,
            "files": [
                {"name": "01 - One More Time.flac", "size": 32_000_000},
                {"name": "02 - Aerodynamic.flac", "size": 28_000_000},
                {"name": TRACK_FILE, "size": TRACK_FILE_SIZE},
                {"name": "Discovery.cue", "size": 1_200},
                {"name": "sample.mp3", "size": 400_000},
            ],
        }
    ],
}

CREATE_RESPONSE = {
    "success": True,
    "detail": "Torrent added",
    "data": {"torrent_id": 42, "hash": INFO_HASH, "auth_id": "abc"},
}

MYLIST_RESPONSE = {
    "success": True,
    "data": [
        {
            "id": 42,
            "hash": INFO_HASH,
            "name": "Daft Punk - Discovery (2001) [FLAC]",
            "files": [
                {"id": 1, "name": "01 - One More Time.flac", "size": 32_000_000},
                {"id": 2, "name": "02 - Aerodynamic.flac", "size": 28_000_000},
                {"id": 7, "name": TRACK_FILE, "size": TRACK_FILE_SIZE},
            ],
        }
    ],
}

REQUESTDL_RESPONSE = {"success": True, "detail": "Link generated", "data": CDN_URL}


def _resolver(session: FakeSession) -> StreamResolver:
    """Return a resolver wired to the fake session."""
    return StreamResolver(
        deezer=DeezerClient(
            session,  # type: ignore[arg-type]
            provider_instance_id="ma_debrid",
            provider_domain="ma_debrid",
        ),
        prowlarr=ProwlarrClient(
            session,  # type: ignore[arg-type]
            base_url="http://prowlarr.lan:9696",
            api_key="prowlarr-key",
        ),
        torbox=TorBoxClient(session, "torbox-key"),  # type: ignore[arg-type]
        provider_instance_id="ma_debrid",
        preferred_quality="flac",
    )


def _happy_session() -> FakeSession:
    """Return a fake session that answers the complete resolution flow."""
    return (
        FakeSession()
        .route("https://api.deezer.com/track/3135556", DEEZER_TRACK)
        .route("http://prowlarr.lan:9696/api/v1/search", PROWLARR_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/checkcached", CACHED_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/createtorrent", CREATE_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/mylist", MYLIST_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/requestdl", REQUESTDL_RESPONSE)
    )


async def test_get_stream_details_returns_direct_flac_stream() -> None:
    """The full resolution flow returns a playable HTTP StreamDetails object."""
    details = await _resolver(_happy_session()).resolve("3135556")

    assert details.provider == "ma_debrid"
    assert details.item_id == "3135556"
    assert details.stream_type is StreamType.HTTP
    assert details.media_type is MediaType.TRACK
    assert details.audio_format.content_type is ContentType.FLAC
    assert details.path == CDN_URL
    assert details.duration == 224
    assert details.size == TRACK_FILE_SIZE
    assert details.can_seek is True
    assert details.allow_seek is True
    assert details.data["torrent_id"] == 42
    assert details.data["file_id"] == 7
    assert details.data["info_hash"] == INFO_HASH


async def test_resolution_uses_cache_check_and_direct_link_endpoints() -> None:
    """The resolver checks the whole release list before adding one torrent."""
    session = _happy_session()

    await _resolver(session).resolve("3135556")

    urls = [call["url"] for call in session.calls]
    assert "https://api.torbox.app/v1/api/torrents/checkcached" in urls
    check_call = next(
        call for call in session.calls if call["url"].endswith("/torrents/checkcached")
    )
    assert check_call["kwargs"]["params"] == {
        "hash": INFO_HASH,
        "format": "list",
        "list_files": "true",
    }
    assert check_call["kwargs"]["headers"]["Authorization"] == "Bearer torbox-key"

    requestdl_call = next(
        call for call in session.calls if call["url"].endswith("/torrents/requestdl")
    )
    assert requestdl_call["kwargs"]["params"]["torrent_id"] == 42
    assert requestdl_call["kwargs"]["params"]["file_id"] == 7
    assert requestdl_call["kwargs"]["params"]["zip_link"] == "false"


async def test_uncached_track_raises_media_not_found() -> None:
    """An empty cache response ends in MediaNotFoundError."""
    session = (
        FakeSession()
        .route("https://api.deezer.com/track/3135556", DEEZER_TRACK)
        .route("http://prowlarr.lan:9696/api/v1/search", PROWLARR_RESPONSE)
        .route(
            "https://api.torbox.app/v1/api/torrents/checkcached",
            {"success": True, "data": []},
        )
    )

    with pytest.raises(MediaNotFoundError):
        await _resolver(session).resolve("3135556")


async def test_album_query_is_tried_when_the_track_query_finds_nothing_cached() -> None:
    """A first search full of uncached releases must not stop the album search."""
    other_hash = "f" * 40
    other_release = {**PROWLARR_RESPONSE[0], "infoHash": other_hash, "magnetUrl": None}

    def prowlarr(kwargs: dict) -> list[dict]:
        """Answer the track query with an uncached release, the album query with the pack."""
        query = kwargs["params"]["query"]
        return PROWLARR_RESPONSE if query == "Daft Punk Discovery" else [other_release]

    def torbox_cache(kwargs: dict) -> dict:
        """Only the album pack is cached on TorBox."""
        if INFO_HASH in kwargs["params"]["hash"]:
            return CACHED_RESPONSE
        return {"success": True, "data": []}

    session = (
        FakeSession()
        .route("https://api.deezer.com/track/3135556", DEEZER_TRACK)
        .route("http://prowlarr.lan:9696/api/v1/search", prowlarr)
        .route("https://api.torbox.app/v1/api/torrents/checkcached", torbox_cache)
        .route("https://api.torbox.app/v1/api/torrents/createtorrent", CREATE_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/mylist", MYLIST_RESPONSE)
        .route("https://api.torbox.app/v1/api/torrents/requestdl", REQUESTDL_RESPONSE)
    )

    details = await _resolver(session).resolve("3135556")

    assert details.path == CDN_URL
    queries = [
        call["kwargs"]["params"]["query"]
        for call in session.calls
        if call["url"].endswith("/api/v1/search")
    ]
    assert queries == ["Daft Punk Harder, Better, Faster, Stronger", "Daft Punk Discovery"]
