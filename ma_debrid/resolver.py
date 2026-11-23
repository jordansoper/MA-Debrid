"""Resolve a Deezer track to a streamable TorBox direct link."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .matching import (
    FileSelection,
    clean_title,
    file_stem,
    rank_downloads,
    rank_selections,
    select_best_file,
    similarity,
    strip_track_number,
)
from .prowlarr_client import ProwlarrError
from .torbox_client import CachedTorrent, TorBoxError, TorrentFile

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .deezer_client import DeezerClient
    from .prowlarr_client import ProwlarrClient, Release
    from .torbox_client import TorBoxClient, TorrentStatus

MAX_RELEASES = 100
MAX_ADD_ATTEMPTS = 5
FILE_POLL_ATTEMPTS = 4
FILE_POLL_DELAY = 0.75
FILE_NAME_MATCH_THRESHOLD = 0.5
DOWNLOAD_WAIT_SECONDS = 75.0
DOWNLOAD_POLL_SECONDS = 4.0
MAX_DOWNLOAD_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class _SearchResult:
    """What the indexer search found: cached file matches and every release seen."""

    selections: list[FileSelection]
    releases: dict[str, Release]


class StreamResolver:
    """Find a cached release for a track and turn it into a direct stream."""

    def __init__(
        self,
        *,
        deezer: DeezerClient,
        prowlarr: ProwlarrClient,
        torbox: TorBoxClient,
        provider_instance_id: str,
        preferred_quality: str,
        download_uncached: bool = True,
        download_wait: float = DOWNLOAD_WAIT_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the resolver.

        :param deezer: Client used to look up the target track metadata.
        :param prowlarr: Client used to find torrent releases.
        :param torbox: Client used to check caches and create direct links.
        :param provider_instance_id: Provider instance id placed on the StreamDetails.
        :param preferred_quality: One of the configured quality preferences.
        :param download_uncached: Download the best release on TorBox when none is cached.
        :param download_wait: Seconds to wait for such a download before giving up.
        :param logger: Optional logger for resolution failures.
        """
        self._deezer = deezer
        self._prowlarr = prowlarr
        self._torbox = torbox
        self._provider_instance_id = provider_instance_id
        self._preferred_quality = preferred_quality
        self._download_uncached = download_uncached
        self._download_wait = download_wait
        self._logger = logger or logging.getLogger(__name__)

    async def resolve(self, item_id: str) -> StreamDetails:
        """
        Resolve a Deezer track id to StreamDetails pointing at a TorBox direct URL.

        :param item_id: Deezer track id.
        :raises MediaNotFoundError: No release with a matching track file was found.
        :raises ResourceTemporarilyUnavailable: Prowlarr or TorBox could not be used, or the
            release is still downloading on TorBox.
        """
        track = await self._deezer.get_track(item_id)
        artist_name = track.artists[0].name if track.artists else ""
        album_title = track.album.name if track.album is not None else ""

        found = await self._find_selections(track, artist_name, album_title)
        if not found.releases:
            raise MediaNotFoundError(f"No release found for {artist_name} - {track.name}")

        failures: list[str] = []
        for selection in found.selections[:MAX_ADD_ATTEMPTS]:
            try:
                return await self._resolve_selection(selection, track)
            except TorBoxError as err:
                failures.append(str(err))
                self._logger.warning(
                    "MA Debrid could not stream %s from %s: %s",
                    track.name,
                    selection.torrent.name,
                    err,
                )

        if found.selections:
            raise ResourceTemporarilyUnavailable(
                "TorBox could not provide a download link for a cached release"
                + (f": {failures[-1]}" if failures else "")
            )
        if self._download_uncached:
            return await self._download_and_resolve(found.releases, track, artist_name, album_title)
        raise MediaNotFoundError(
            f"No cached release contains a matching file for {artist_name} - {track.name}"
        )

    def _queries(self, track: Track, artist_name: str, album_title: str) -> list[str]:
        """Return the indexer queries to try, most specific first."""
        title = clean_title(track.name) or track.name
        album = clean_title(album_title) or album_title
        candidates = [
            (artist_name, track.name),
            (artist_name, title),
            (artist_name, album_title),
            (artist_name, album),
            # a remix or single often is not what the indexers carry: the artist's other
            # releases still hold the song, and the file is matched by its title
            (artist_name,),
        ]
        queries: list[str] = []
        for parts in candidates:
            query = " ".join(part for part in parts if part).strip()
            if query and query.casefold() not in {value.casefold() for value in queries}:
                queries.append(query)
        return queries

    async def _find_selections(
        self, track: Track, artist_name: str, album_title: str
    ) -> _SearchResult:
        """
        Search Prowlarr and return the cached files that match the track, best first.

        Each query is searched in turn until one yields a cached release that holds the track.

        :raises ResourceTemporarilyUnavailable: Prowlarr or TorBox could not be used.
        """
        release_by_hash: dict[str, Release] = {}
        errors: list[str] = []
        for query in self._queries(track, artist_name, album_title):
            try:
                found = await self._prowlarr.search(query, limit=MAX_RELEASES)
            except ProwlarrError as err:
                errors.append(str(err))
                self._logger.warning("MA Debrid Prowlarr search for %r failed: %s", query, err)
                continue
            new_releases = {
                release.info_hash: release
                for release in found
                if release.info_hash not in release_by_hash
            }
            self._logger.info(
                "MA Debrid: Prowlarr found %d releases for %r (%d new)",
                len(found),
                query,
                len(new_releases),
            )
            if not new_releases:
                continue
            release_by_hash.update(new_releases)

            try:
                cached = await self._torbox.check_cached(list(new_releases))
            except TorBoxError as err:
                raise ResourceTemporarilyUnavailable(
                    f"TorBox cache check failed: {err}"
                ) from err
            selections = rank_selections(
                cached,
                release_by_hash,
                title=track.name,
                track_number=track.track_number,
                album_title=album_title,
                preferred_quality=self._preferred_quality,
            )
            self._logger.info(
                "MA Debrid: %d of %d releases are cached on TorBox, %d with a matching file",
                len(cached),
                len(new_releases),
                len(selections),
            )
            if selections:
                return _SearchResult(selections, release_by_hash)

        if not release_by_hash and errors:
            raise ResourceTemporarilyUnavailable(f"Prowlarr search failed: {errors[-1]}")
        return _SearchResult([], release_by_hash)

    async def _download_and_resolve(
        self,
        releases: dict[str, Release],
        track: Track,
        artist_name: str,
        album_title: str,
    ) -> StreamDetails:
        """Download the best uncached release on TorBox and stream the track from it."""
        candidates = rank_downloads(
            releases,
            artist=artist_name,
            album=album_title,
            title=track.name,
            preferred_quality=self._preferred_quality,
            limit=MAX_DOWNLOAD_ATTEMPTS,
        )
        if not candidates:
            raise MediaNotFoundError(
                f"No cached or downloadable release found for {artist_name} - {track.name}"
            )

        deadline = time.monotonic() + self._download_wait
        pending: str | None = None
        for release in candidates:
            self._logger.info(
                "MA Debrid: nothing cached for %s - %s, downloading %r on TorBox",
                artist_name,
                track.name,
                release.title,
            )
            try:
                torrent_id = await self._torbox.add_torrent(
                    release.info_hash, release.magnet_url, only_if_cached=False
                )
            except TorBoxError as err:
                self._logger.warning("MA Debrid could not add %r: %s", release.title, err)
                continue

            status = await self._wait_for_download(torrent_id, deadline)
            if status is None:
                pending = release.title
                break
            selected = select_best_file(
                CachedTorrent(
                    info_hash=release.info_hash,
                    name=status.name or release.title,
                    size=release.size,
                    files=status.files,
                ),
                title=track.name,
                track_number=track.track_number,
                preferred_quality=self._preferred_quality,
                album_title=album_title,
            )
            if selected is None:
                self._logger.warning(
                    "MA Debrid: downloaded %r holds no file matching %s", release.title, track.name
                )
                continue
            file, _score = selected
            if file.file_id is None:
                continue
            try:
                return await self._stream_details(torrent_id, file, track, release.info_hash)
            except TorBoxError as err:
                self._logger.warning("MA Debrid could not stream %r: %s", release.title, err)

        if pending is not None:
            raise ResourceTemporarilyUnavailable(
                f"{pending!r} is still downloading on TorBox; play the track again in a minute"
            )
        raise MediaNotFoundError(
            f"No cached or downloadable release found for {artist_name} - {track.name}"
        )

    async def _wait_for_download(
        self, torrent_id: int, deadline: float
    ) -> TorrentStatus | None:
        """Return the finished torrent, or None when it is still downloading at the deadline."""
        while True:
            status = await self._torbox.get_torrent(torrent_id)
            if status.finished and status.files:
                return status
            self._logger.info(
                "MA Debrid: TorBox download %s at %.0f%% (%s)",
                torrent_id,
                status.progress * 100,
                status.state or "unknown state",
            )
            if time.monotonic() + DOWNLOAD_POLL_SECONDS >= deadline:
                return None
            await asyncio.sleep(DOWNLOAD_POLL_SECONDS)

    async def _resolve_selection(
        self, selection: FileSelection, track: Track
    ) -> StreamDetails:
        """Add one cached torrent, find its file and request the direct link."""
        release = selection.release
        torrent_id = await self._torbox.add_torrent(
            selection.torrent.info_hash,
            release.magnet_url if release is not None else None,
        )
        file = await self._resolve_file(torrent_id, selection.file, track)
        return await self._stream_details(torrent_id, file, track, selection.torrent.info_hash)

    async def _stream_details(
        self, torrent_id: int, file: TorrentFile, track: Track, info_hash: str
    ) -> StreamDetails:
        """Request the direct link of one file and wrap it as StreamDetails."""
        if file.file_id is None:
            raise TorBoxError(f"TorBox returned no file id for {file.name}")
        url = await self._torbox.request_download_link(torrent_id, file.file_id)

        return StreamDetails(
            provider=self._provider_instance_id,
            item_id=track.item_id,
            audio_format=AudioFormat(content_type=ContentType.try_parse(file.name)),
            stream_type=StreamType.HTTP,
            media_type=MediaType.TRACK,
            path=url,
            duration=track.duration or None,
            size=file.size or None,
            allow_seek=True,
            can_seek=True,
            data={
                "torrent_id": torrent_id,
                "file_id": file.file_id,
                "info_hash": info_hash,
            },
        )

    async def _resolve_file(
        self, torrent_id: int, expected: TorrentFile, track: Track
    ) -> TorrentFile:
        """Return the file of the added torrent with a usable file id."""
        if expected.file_id is not None:
            return expected
        files: tuple[TorrentFile, ...] = ()
        for attempt in range(FILE_POLL_ATTEMPTS):
            files = await self._torbox.get_torrent_files(torrent_id)
            match = self._match_added_file(files, expected, track)
            if match is not None and match.file_id is not None:
                return match
            if attempt + 1 < FILE_POLL_ATTEMPTS:
                await asyncio.sleep(FILE_POLL_DELAY * (attempt + 1))
        self._logger.warning(
            "MA Debrid: TorBox torrent %s lists %d files, none matching %r: %s",
            torrent_id,
            len(files),
            expected.name,
            ", ".join(file.name for file in files[:8]) or "(no files)",
        )
        raise TorBoxError(f"TorBox torrent {torrent_id} has no file matching {expected.name!r}")

    def _match_added_file(
        self, files: tuple[TorrentFile, ...], expected: TorrentFile, track: Track
    ) -> TorrentFile | None:
        """Find the file returned by TorBox that corresponds to the selected file."""
        usable = [file for file in files if file.file_id is not None]
        expected_path = expected.name.replace("\\", "/").casefold()
        for file in usable:
            if file.name.replace("\\", "/").casefold() == expected_path:
                return file

        # TorBox may list the file without (or with another) folder prefix than checkcached
        expected_base = expected_path.rsplit("/", 1)[-1]
        for file in usable:
            if file.name.replace("\\", "/").casefold().rsplit("/", 1)[-1] == expected_base:
                return file

        best: TorrentFile | None = None
        best_score = FILE_NAME_MATCH_THRESHOLD
        for file in usable:
            score = similarity(
                strip_track_number(file_stem(expected.name)),
                strip_track_number(file_stem(file.name)),
            )
            if score > best_score or (
                score == best_score and best is not None and file.size > best.size
            ):
                best = file
                best_score = score
        if best is not None:
            return best

        # last resort: pick the file that fits the track itself
        fallback = select_best_file(
            CachedTorrent(info_hash="", name="", size=0, files=tuple(usable)),
            title=track.name,
            track_number=track.track_number,
            preferred_quality=self._preferred_quality,
        )
        return fallback[0] if fallback is not None else None
