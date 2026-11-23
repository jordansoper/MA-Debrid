"""Resolve a Deezer track to a streamable TorBox direct link."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, ResourceTemporarilyUnavailable
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .matching import FileSelection, rank_selections, similarity
from .prowlarr_client import ProwlarrError
from .torbox_client import TorBoxError, TorrentFile

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .deezer_client import DeezerClient
    from .prowlarr_client import ProwlarrClient, Release
    from .torbox_client import TorBoxClient

MAX_RELEASES = 100
MAX_ADD_ATTEMPTS = 5
FILE_POLL_ATTEMPTS = 4
FILE_POLL_DELAY = 0.75
FILE_NAME_MATCH_THRESHOLD = 0.5


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
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the resolver.

        :param deezer: Client used to look up the target track metadata.
        :param prowlarr: Client used to find torrent releases.
        :param torbox: Client used to check caches and create direct links.
        :param provider_instance_id: Provider instance id placed on the StreamDetails.
        :param preferred_quality: One of the configured quality preferences.
        :param logger: Optional logger for resolution failures.
        """
        self._deezer = deezer
        self._prowlarr = prowlarr
        self._torbox = torbox
        self._provider_instance_id = provider_instance_id
        self._preferred_quality = preferred_quality
        self._logger = logger or logging.getLogger(__name__)

    async def resolve(self, item_id: str) -> StreamDetails:
        """
        Resolve a Deezer track id to StreamDetails pointing at a TorBox direct URL.

        :param item_id: Deezer track id.
        :raises MediaNotFoundError: No cached release with a matching track file was found.
        :raises ResourceTemporarilyUnavailable: Prowlarr or TorBox could not be used.
        """
        track = await self._deezer.get_track(item_id)
        artist_name = track.artists[0].name if track.artists else ""
        album_title = track.album.name if track.album is not None else ""

        releases = await self._search_releases(artist_name, track.name, album_title)
        if not releases:
            raise MediaNotFoundError(f"No release found for {artist_name} - {track.name}")

        release_by_hash = {release.info_hash: release for release in releases}
        try:
            cached = await self._torbox.check_cached(list(release_by_hash))
        except TorBoxError as err:
            raise ResourceTemporarilyUnavailable(f"TorBox cache check failed: {err}") from err

        selections = rank_selections(
            cached,
            release_by_hash,
            title=track.name,
            track_number=track.track_number,
            album_title=album_title,
            preferred_quality=self._preferred_quality,
        )
        if not selections:
            raise MediaNotFoundError(
                f"No cached release contains a matching file for "
                f"{artist_name} - {track.name}"
            )

        failures: list[str] = []
        for selection in selections[:MAX_ADD_ATTEMPTS]:
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

        raise ResourceTemporarilyUnavailable(
            "TorBox could not provide a download link for a cached release"
            + (f": {failures[-1]}" if failures else "")
        )

    async def _search_releases(
        self, artist_name: str, track_title: str, album_title: str
    ) -> list[Release]:
        """Search Prowlarr for the track, then for its album."""
        queries = [" ".join(part for part in (artist_name, track_title) if part)]
        if album_title:
            queries.append(" ".join(part for part in (artist_name, album_title) if part))

        releases: dict[str, Release] = {}
        errors: list[str] = []
        for query in queries:
            if not query:
                continue
            try:
                found = await self._prowlarr.search(query, limit=MAX_RELEASES)
            except ProwlarrError as err:
                errors.append(str(err))
                self._logger.debug("MA Debrid Prowlarr search for %r failed: %s", query, err)
                continue
            for release in found:
                releases.setdefault(release.info_hash, release)
            if releases:
                break

        if not releases and errors:
            raise ResourceTemporarilyUnavailable(f"Prowlarr search failed: {errors[-1]}")
        return list(releases.values())

    async def _resolve_selection(
        self, selection: FileSelection, track: Track
    ) -> StreamDetails:
        """Add one cached torrent, find its file and request the direct link."""
        release = selection.release
        torrent_id = await self._torbox.add_torrent(
            selection.torrent.info_hash,
            release.magnet_url if release is not None else None,
        )
        file = await self._resolve_file(torrent_id, selection.file)
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
                "info_hash": selection.torrent.info_hash,
            },
        )

    async def _resolve_file(self, torrent_id: int, expected: TorrentFile) -> TorrentFile:
        """Return the file of the added torrent with a usable file id."""
        if expected.file_id is not None:
            return expected
        for attempt in range(FILE_POLL_ATTEMPTS):
            files = await self._torbox.get_torrent_files(torrent_id)
            match = self._match_added_file(files, expected)
            if match is not None and match.file_id is not None:
                return match
            if attempt + 1 < FILE_POLL_ATTEMPTS:
                await asyncio.sleep(FILE_POLL_DELAY * (attempt + 1))
        raise TorBoxError(f"TorBox torrent {torrent_id} has no file matching {expected.name!r}")

    @staticmethod
    def _match_added_file(
        files: tuple[TorrentFile, ...], expected: TorrentFile
    ) -> TorrentFile | None:
        """Find the file returned by TorBox that corresponds to the selected file."""
        expected_name = expected.name.replace("\\", "/").casefold()
        for file in files:
            if file.name.replace("\\", "/").casefold() == expected_name and file.file_id is not None:
                return file
        best: TorrentFile | None = None
        best_score = FILE_NAME_MATCH_THRESHOLD
        for file in files:
            if file.file_id is None:
                continue
            score = similarity(expected.name, file.name)
            if score > best_score or (
                score == best_score and best is not None and file.size > best.size
            ):
                best = file
                best_score = score
        return best
