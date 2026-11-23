"""Tests for ranking cached torrents and matching the exact track inside an album pack."""

from __future__ import annotations

from ma_debrid.matching import rank_selections, score_file
from ma_debrid.prowlarr_client import Release
from ma_debrid.torbox_client import CachedTorrent, TorrentFile

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
TORRENT_NAME = "Daft Punk - Discovery (2001) [FLAC]"
FLAC_FILE = "03 - Harder, Better, Faster, Stronger.flac"
MP3_FILE = "03 - Harder, Better, Faster, Stronger.mp3"
TITLE = "Harder, Better, Faster, Stronger"


def _torrent(files: list[TorrentFile]) -> CachedTorrent:
    """Return a cached album-pack torrent holding the given files."""
    return CachedTorrent(
        info_hash=INFO_HASH,
        name=TORRENT_NAME,
        size=sum(file.size for file in files),
        files=tuple(files),
    )


def _release() -> Release:
    """Return the Prowlarr release that led to the cached torrent."""
    return Release(
        title=TORRENT_NAME,
        info_hash=INFO_HASH,
        magnet_url=f"magnet:?xt=urn:btih:{INFO_HASH}",
        size=420_000_000,
        seeders=18,
        indexer="Test Indexer",
        categories=(3040,),
    )


def _album_pack() -> list[TorrentFile]:
    """Return a full album pack including junk companion files."""
    return [
        TorrentFile("01 - One More Time.flac", 32_000_000, 1),
        TorrentFile("02 - Aerodynamic.flac", 28_000_000, 2),
        TorrentFile(FLAC_FILE, 30_000_000, 3),
        TorrentFile(MP3_FILE, 9_000_000, 4),
        TorrentFile("Discovery.cue", 1_200, 5),
        TorrentFile("Discovery.log", 4_000, 6),
        TorrentFile("sample.mp3", 400_000, 7),
        TorrentFile("cover.jpg", 500_000, 8),
    ]


def test_album_pack_matches_exact_track_file() -> None:
    """The FLAC of track 3 is picked, not a companion file or sample."""
    selections = rank_selections(
        {INFO_HASH: _torrent(_album_pack())},
        {INFO_HASH: _release()},
        title=TITLE,
        track_number=3,
        album_title="Discovery",
        preferred_quality="flac",
    )

    assert selections
    best = selections[0]
    assert best.file.name == FLAC_FILE
    assert best.file.file_id == 3
    assert best.release is not None
    assert best.release.seeders == 18


def test_mp3_preference_picks_the_mp3_of_the_same_track() -> None:
    """The quality preference decides between two encodings of the same track."""
    selections = rank_selections(
        {INFO_HASH: _torrent(_album_pack())},
        {INFO_HASH: _release()},
        title=TITLE,
        track_number=3,
        album_title="Discovery",
        preferred_quality="mp3",
    )

    assert selections
    assert selections[0].file.name == MP3_FILE
    assert selections[0].file.file_id == 4


def test_junk_files_are_never_scored() -> None:
    """Cue sheets, logs and sample clips cannot be selected."""
    for name in ("Discovery.cue", "Discovery.log", "sample.mp3", "cover.jpg"):
        assert (
            score_file(
                TorrentFile(name, 30_000_000),
                title=TITLE,
                track_number=3,
                preferred_quality="flac",
            )
            is None
        )


def test_album_pack_without_target_track_returns_nothing() -> None:
    """A pack that does not contain the requested track yields no selection."""
    files = [
        TorrentFile("01 - One More Time.flac", 32_000_000, 1),
        TorrentFile("02 - Aerodynamic.flac", 28_000_000, 2),
    ]

    selections = rank_selections(
        {INFO_HASH: _torrent(files)},
        {INFO_HASH: _release()},
        title="Veridis Quo",
        track_number=12,
        album_title="Discovery",
        preferred_quality="flac",
    )

    assert selections == []


def test_torrent_without_file_list_is_skipped() -> None:
    """A cache entry without files cannot be matched or requested."""
    torrent = CachedTorrent(
        info_hash=INFO_HASH,
        name="Daft Punk - Discovery (2001) [FLAC]",
        size=400_000_000,
    )

    selections = rank_selections(
        {INFO_HASH: torrent},
        {INFO_HASH: _release()},
        title=TITLE,
        track_number=3,
        album_title="Discovery",
        preferred_quality="flac",
    )

    assert selections == []
