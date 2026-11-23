"""Setup flow for the MA Debrid provider: asks for the settings and checks them before saving."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .const import CONF_PROWLARR_API_KEY, CONF_PROWLARR_URL, CONF_TORBOX_API_KEY
from .torbox_client import TorBoxClient, TorBoxError

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession

LOGGER = logging.getLogger("music_assistant.providers.ma_debrid.setup")

CHECK_TIMEOUT = 15  # seconds per connectivity check
# A well-formed info hash that does not exist on TorBox: the cache check returns an
# empty result for a valid key, and an error for a bad key.
_PROBE_HASH = "0" * 40

_ENTRIES = (
    ConfigEntry(
        key=CONF_TORBOX_API_KEY,
        type=ConfigEntryType.SECURE_STRING,
        required=True,
        label="TorBox API key",
        description=(
            "Bearer token from your TorBox account, used for cache checks, "
            "torrent adds and download links."
        ),
    ),
    ConfigEntry(
        key=CONF_PROWLARR_URL,
        type=ConfigEntryType.STRING,
        required=True,
        label="Prowlarr URL",
        description=(
            "Base URL of your Prowlarr instance, reachable from the Music "
            "Assistant container (for example http://192.168.1.10:9696)."
        ),
    ),
    ConfigEntry(
        key=CONF_PROWLARR_API_KEY,
        type=ConfigEntryType.SECURE_STRING,
        required=True,
        label="Prowlarr API key",
        description="API key from Prowlarr under Settings, General.",
    ),
)


async def _check_prowlarr(http: aiohttp.ClientSession, url: str, api_key: str) -> str | None:
    """Return an error message for the Prowlarr settings, or None when they work."""
    if not url.startswith(("http://", "https://")):
        return "The Prowlarr URL must start with http:// or https://."
    status_url = f"{url.rstrip('/')}/api/v1/system/status"
    try:
        async with http.get(
            status_url,
            headers={"X-Api-Key": api_key},
            timeout=aiohttp.ClientTimeout(total=CHECK_TIMEOUT),
        ) as resp:
            if resp.status in (401, 403):
                return "Prowlarr rejected this API key. Check it under Settings, General."
            if resp.status != 200:
                return f"Prowlarr answered with HTTP {resp.status} at {url}."
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        LOGGER.warning("Prowlarr check failed for %s: %s", status_url, err)
        return (
            f"Could not reach Prowlarr at {url}. Use an address the Music Assistant "
            "container can reach, not localhost."
        )
    return None


async def _check_torbox(http: aiohttp.ClientSession, api_key: str) -> str | None:
    """Return an error message for the TorBox key, or None when it works."""
    client = TorBoxClient(http, api_key)
    try:
        await asyncio.wait_for(client.check_cached([_PROBE_HASH]), timeout=CHECK_TIMEOUT)
    except TorBoxError as err:
        LOGGER.warning("TorBox check rejected: %s", err)
        return f"TorBox rejected this API key: {err}"
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            return "TorBox rejected this API key. Copy it again from your TorBox account."
        return f"TorBox answered with HTTP {err.status}."
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        LOGGER.warning("TorBox check failed: %s", err)
        return "Could not reach TorBox. Check the internet connection of the Music Assistant host."
    return None


async def _validate(http: aiohttp.ClientSession, values: dict[str, Any]) -> dict[str, str]:
    """Check the submitted settings against the real services; return field errors."""
    errors: dict[str, str] = {}
    torbox_key = str(values.get(CONF_TORBOX_API_KEY) or "").strip()
    prowlarr_url = str(values.get(CONF_PROWLARR_URL) or "").strip()
    prowlarr_key = str(values.get(CONF_PROWLARR_API_KEY) or "").strip()

    if not torbox_key:
        errors[CONF_TORBOX_API_KEY] = "Enter the TorBox API key."
    if not prowlarr_url:
        errors[CONF_PROWLARR_URL] = "Enter the Prowlarr URL."
    if not prowlarr_key:
        errors[CONF_PROWLARR_API_KEY] = "Enter the Prowlarr API key."
    if errors:
        return errors

    prowlarr_error = await _check_prowlarr(http, prowlarr_url, prowlarr_key)
    if prowlarr_error:
        errors[CONF_PROWLARR_URL] = prowlarr_error
    torbox_error = await _check_torbox(http, torbox_key)
    if torbox_error:
        errors[CONF_TORBOX_API_KEY] = torbox_error
    return errors


async def run_setup(session: SetupSession) -> None:
    """Run the setup flow: collect the settings, check them, then create the provider."""
    errors: dict[str, str] | None = None
    setup_data = dict(session.context.setup_data)
    while True:
        submitted = await session.form(
            list(_ENTRIES), step_id="user", errors=errors, last_step=True
        )
        setup_data.update(submitted)
        errors = await _validate(session.mass.http_session, setup_data)
        if errors:
            LOGGER.info("MA Debrid setup check failed: %s", errors)
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            LOGGER.error("MA Debrid setup could not save the provider: %s", err, exc_info=err)
            errors = {"base": err.translation_key or str(err)}
