"""Constants shared by the MA Debrid provider modules."""

from __future__ import annotations

from typing import Final

# configuration keys
CONF_TORBOX_API_KEY: Final[str] = "torbox_api_key"
CONF_PROWLARR_URL: Final[str] = "prowlarr_url"
CONF_PROWLARR_API_KEY: Final[str] = "prowlarr_api_key"
CONF_PREFERRED_QUALITY: Final[str] = "preferred_quality"

# quality preferences
QUALITY_FLAC: Final[str] = "flac"
QUALITY_MP3: Final[str] = "mp3"
QUALITY_ANY: Final[str] = "any"
PREFERRED_QUALITY_OPTIONS: Final[tuple[str, ...]] = (QUALITY_FLAC, QUALITY_MP3, QUALITY_ANY)
DEFAULT_PREFERRED_QUALITY: Final[str] = QUALITY_FLAC

# external services
DEEZER_BASE_URL: Final[str] = "https://api.deezer.com"
TORBOX_BASE_URL: Final[str] = "https://api.torbox.app/v1/api"
PROWLARR_AUDIO_CATEGORIES: Final[tuple[str, ...]] = ("3000", "3010", "3040")

# cache lifetimes (seconds)
CACHE_TTL_SEARCH: Final[int] = 300
CACHE_TTL_ITEM: Final[int] = 3600

# request tuning
REQUEST_TIMEOUT: Final[int] = 30
DEEZER_RATE_LIMIT: Final[int] = 8  # requests per second, Deezer quota is 50 per 5 seconds
TORBOX_RATE_LIMIT: Final[int] = 5  # conservative share of TorBox's shared API limit
MAX_CACHED_HASHES_PER_REQUEST: Final[int] = 50
