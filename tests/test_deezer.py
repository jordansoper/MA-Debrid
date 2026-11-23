"""Tests for Deezer metadata parsing into Music Assistant models."""

from __future__ import annotations

from fakes import DEEZER_TRACK, FakeSession
from ma_debrid.deezer_client import DeezerClient


def _client(session: FakeSession) -> DeezerClient:
    """Return a Deezer client for the fake session."""
    return DeezerClient(
        session,  # type: ignore[arg-type]
        provider_instance_id="ma_debrid",
        provider_domain="ma_debrid",
    )


async def test_get_track_maps_deezer_metadata() -> None:
    """A Deezer track lookup becomes a fully populated Music Assistant Track."""
    session = FakeSession().route(
        "https://api.deezer.com/track/3135556",
        DEEZER_TRACK,
    )

    track = await _client(session).get_track("3135556")

    assert track.item_id == "3135556"
    assert track.name == "Harder, Better, Faster, Stronger"
    assert track.duration == 224
    assert track.track_number == 3
    assert track.disc_number == 1
    assert [artist.name for artist in track.artists] == ["Daft Punk"]
    assert track.album is not None
    assert track.album.name == "Discovery"
    assert track.album.year == 2001
    assert track.album.artist_str == "Daft Punk"
    assert track.album.image is not None
    assert (
        track.album.image.path
        == "https://e-cdns-images.dzcdn.net/images/cover/discovery/1000x1000.jpg"
    )

    mapping = next(iter(track.provider_mappings))
    assert mapping.item_id == "3135556"
    assert mapping.provider_domain == "ma_debrid"
    assert mapping.provider_instance == "ma_debrid"
    assert mapping.url == "https://www.deezer.com/track/3135556"
    assert mapping.details == "USUM71703861"

    artist_mapping = next(iter(track.artists[0].provider_mappings))
    assert artist_mapping.item_id == "27"
    assert track.artists[0].metadata.images is not None


async def test_search_tracks_sends_limit_and_maps_results() -> None:
    """Track search maps Deezer's data list and forwards the requested limit."""
    session = FakeSession().route(
        "https://api.deezer.com/search/track",
        {"data": [DEEZER_TRACK]},
    )

    tracks = await _client(session).search_tracks("daft punk", limit=7)

    assert [track.item_id for track in tracks] == ["3135556"]
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["kwargs"]["params"] == {"q": "daft punk", "limit": 7}


async def test_search_albums_maps_album_payload() -> None:
    """Album search keeps Deezer album ids and cover art."""
    session = FakeSession().route(
        "https://api.deezer.com/search/album",
        {"data": [DEEZER_TRACK["album"]]},
    )

    albums = await _client(session).search_albums("discovery", limit=5)

    assert [album.item_id for album in albums] == ["302127"]
    assert albums[0].name == "Discovery"
    assert albums[0].year == 2001
    assert albums[0].image is not None
