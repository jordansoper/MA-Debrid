"""MA Debrid: search Deezer metadata and stream cached TorBox releases via Prowlarr."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.models.music_provider import MusicProvider
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType, MediaType, ProviderFeature
from music_assistant_models.errors import SetupFailedError, UnsupportedFeaturedException
from music_assistant_models.media_items import Album, Artist, SearchResults, Track
from music_assistant_models.streamdetails import StreamDetails

from .const import (
    CACHE_TTL_ITEM,
    CACHE_TTL_SEARCH,
    CONF_PREFERRED_QUALITY,
    CONF_PROWLARR_API_KEY,
    CONF_PROWLARR_URL,
    CONF_TORBOX_API_KEY,
    DEFAULT_PREFERRED_QUALITY,
    DEEZER_RATE_LIMIT,
    PREFERRED_QUALITY_OPTIONS,
    QUALITY_ANY,
    QUALITY_FLAC,
    QUALITY_MP3,
    TORBOX_RATE_LIMIT,
)
from .deezer_client import DeezerClient
from .prowlarr_client import ProwlarrClient
from .resolver import StreamResolver
from .torbox_client import TorBoxClient

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider for one settings entry."""
    return MADebridProvider(mass, manifest, config, SUPPORTED_FEATURES)


class MADebridProvider(MusicProvider):
    """On-demand music source backed by Deezer metadata and TorBox cached torrents."""

    _deezer: DeezerClient
    _resolver: StreamResolver

    @property
    def is_streaming_provider(self) -> bool:
        """Return True: the catalog is far larger than the user's library."""
        return True

    @property
    def supported_media_types(self) -> set[MediaType]:
        """Return the media types this source can search and stream."""
        return {MediaType.TRACK, MediaType.ALBUM}

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the settings of this provider instance."""
        # The TorBox and Prowlarr credentials are collected by the setup flow
        # (setup_flow.py) and stored as setup data, so they are not config entries.
        return (
            ConfigEntry(
                key=CONF_PREFERRED_QUALITY,
                type=ConfigEntryType.STRING,
                required=False,
                default_value=DEFAULT_PREFERRED_QUALITY,
                label="Preferred audio quality",
                description=(
                    "Which cached format is preferred when a release exists in several "
                    "formats. Tracks are matched by title and track number either way."
                ),
                options=[
                    ConfigValueOption(value=QUALITY_FLAC, title="FLAC / lossless"),
                    ConfigValueOption(value=QUALITY_MP3, title="MP3"),
                    ConfigValueOption(value=QUALITY_ANY, title="Any audio format"),
                ],
            ),
        )

    def _setting(self, key: str) -> Any:
        """Return a setting saved by the setup flow, falling back to the config values."""
        value = self.get_setup_value(key)
        if value is None:
            value = self.config.get_value(key)
        return value

    async def handle_async_init(self) -> None:
        """Validate the settings and create the API clients."""
        torbox_api_key = str(self._setting(CONF_TORBOX_API_KEY) or "").strip()
        prowlarr_url = str(self._setting(CONF_PROWLARR_URL) or "").strip()
        prowlarr_api_key = str(self._setting(CONF_PROWLARR_API_KEY) or "").strip()
        preferred_quality = str(
            self.config.get_value(CONF_PREFERRED_QUALITY) or DEFAULT_PREFERRED_QUALITY
        ).strip()

        missing = [
            key
            for key, value in (
                (CONF_TORBOX_API_KEY, torbox_api_key),
                (CONF_PROWLARR_URL, prowlarr_url),
                (CONF_PROWLARR_API_KEY, prowlarr_api_key),
            )
            if not value
        ]
        if missing:
            raise SetupFailedError(
                f"MA Debrid is missing required configuration: {', '.join(missing)}"
            )
        if not prowlarr_url.startswith(("http://", "https://")):
            raise SetupFailedError("The Prowlarr URL must start with http:// or https://")
        if preferred_quality not in PREFERRED_QUALITY_OPTIONS:
            self.logger.warning(
                "Unknown preferred_quality %r, using %r",
                preferred_quality,
                DEFAULT_PREFERRED_QUALITY,
            )
            preferred_quality = DEFAULT_PREFERRED_QUALITY

        self._deezer = DeezerClient(
            self.mass.http_session,
            provider_instance_id=self.instance_id,
            provider_domain=self.domain,
            throttler=Throttler(rate_limit=DEEZER_RATE_LIMIT, period=1),
        )
        prowlarr = ProwlarrClient(
            self.mass.http_session,
            base_url=prowlarr_url,
            api_key=prowlarr_api_key,
        )
        torbox = TorBoxClient(
            self.mass.http_session,
            torbox_api_key,
            throttler=Throttler(rate_limit=TORBOX_RATE_LIMIT, period=1),
        )
        self._resolver = StreamResolver(
            deezer=self._deezer,
            prowlarr=prowlarr,
            torbox=torbox,
            provider_instance_id=self.instance_id,
            preferred_quality=preferred_quality,
            logger=self.logger,
        )

    @use_cache(expiration=CACHE_TTL_SEARCH)
    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """
        Search Deezer for tracks and albums.

        :param search_query: The text to search for.
        :param media_types: The media types to include.
        :param limit: The maximum number of items per media type.
        """
        result = SearchResults()
        query = search_query.strip()
        if not query:
            return result
        if MediaType.TRACK in media_types:
            result.tracks = await self._deezer.search_tracks(query, limit)
        if MediaType.ALBUM in media_types:
            result.albums = await self._deezer.search_albums(query, limit)
        return result

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_track(self, prov_track_id: str) -> Track:
        """
        Return the full metadata of a Deezer track.

        :param prov_track_id: Deezer track id.
        """
        return await self._deezer.get_track(prov_track_id)

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_album(self, prov_album_id: str) -> Album:
        """
        Return the full metadata of a Deezer album.

        :param prov_album_id: Deezer album id.
        """
        return await self._deezer.get_album(prov_album_id)

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """
        Return the tracks of a Deezer album, so a whole album can be played.

        :param prov_album_id: Deezer album id.
        """
        return await self._deezer.get_album_tracks(prov_album_id)

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """
        Return the Deezer metadata of an artist.

        :param prov_artist_id: Deezer artist id.
        """
        return await self._deezer.get_artist(prov_artist_id)

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """
        Return the albums of a Deezer artist.

        :param prov_artist_id: Deezer artist id.
        """
        return await self._deezer.get_artist_albums(prov_artist_id)

    @use_cache(expiration=CACHE_TTL_ITEM)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """
        Return the most popular tracks of a Deezer artist.

        :param prov_artist_id: Deezer artist id.
        """
        return await self._deezer.get_artist_toptracks(prov_artist_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return a direct TorBox stream for a cached release of the given track.

        :param item_id: Deezer track id.
        :param media_type: Media type of the item that should be streamed.
        """
        if media_type is not MediaType.TRACK:
            raise UnsupportedFeaturedException(
                f"MA Debrid can only stream tracks, not {media_type.value}"
            )
        return await self._resolver.resolve(item_id)
