"""Tests for the album and artist lookups that playing an album or opening an artist needs."""

from __future__ import annotations

from fakes import FakeSession
from ma_debrid.deezer_client import DeezerClient

ALBUM = {
    "id": 302127,
    "title": "Discovery",
    "cover_xl": "https://e-cdns-images.dzcdn.net/images/cover/discovery/1000x1000.jpg",
    "release_date": "2001-03-07",
    "artist": {"id": 27, "name": "Daft Punk"},
}

# Deezer's album track list carries no album object of its own
ALBUM_TRACKS = {
    "data": [
        {
            "id": 3135553,
            "title": "One More Time",
            "duration": 320,
            "track_position": 1,
            "disk_number": 1,
            "artist": {"id": 27, "name": "Daft Punk"},
        },
        {
            "id": 3135554,
            "title": "Aerodynamic",
            "duration": 212,
            "artist": {"id": 27, "name": "Daft Punk"},
        },
    ]
}

ARTIST = {
    "id": 27,
    "name": "Daft Punk",
    "picture_xl": "https://e-cdns-images.dzcdn.net/images/artist/daft-punk/1000x1000.jpg",
}


def _client(session: FakeSession) -> DeezerClient:
    """Return a Deezer client for the fake session."""
    return DeezerClient(
        session,  # type: ignore[arg-type]
        provider_instance_id="ma_debrid",
        provider_domain="ma_debrid",
    )


async def test_get_album_tracks_attaches_the_album_to_each_track() -> None:
    """Every track of an album pack carries the album, artist and its track number."""
    session = (
        FakeSession()
        .route("/album/302127/tracks", ALBUM_TRACKS)
        .route("/album/302127", ALBUM)
    )

    tracks = await _client(session).get_album_tracks("302127")

    assert [track.name for track in tracks] == ["One More Time", "Aerodynamic"]
    # a missing track_position falls back to the order in the list
    assert [track.track_number for track in tracks] == [1, 2]
    assert all(track.album is not None and track.album.name == "Discovery" for track in tracks)
    assert tracks[0].album is not None
    assert tracks[0].album.image is not None
    assert [artist.name for artist in tracks[0].artists] == ["Daft Punk"]


async def test_get_artist_maps_name_and_picture() -> None:
    """An artist lookup returns the artist with its Deezer id and picture."""
    session = FakeSession().route("/artist/27", ARTIST)

    artist = await _client(session).get_artist("27")

    assert artist.item_id == "27"
    assert artist.name == "Daft Punk"
    assert artist.metadata.images is not None


async def test_get_artist_albums_and_top_tracks() -> None:
    """The artist page lists the artist's albums and most popular tracks."""
    session = (
        FakeSession()
        .route("/artist/27/albums", {"data": [{"id": 302127, "title": "Discovery"}]})
        .route("/artist/27/top", {"data": [ALBUM_TRACKS["data"][0]]})
    )
    client = _client(session)

    albums = await client.get_artist_albums("27")
    top = await client.get_artist_toptracks("27")

    assert [album.name for album in albums] == ["Discovery"]
    assert [track.name for track in top] == ["One More Time"]
