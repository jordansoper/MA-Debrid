"""Shared async HTTP helpers for the MA Debrid API clients."""

from __future__ import annotations

import json
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp

from .const import REQUEST_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from aiohttp import ClientSession


class ThrottlerLike(Protocol):
    """Minimal interface of an async rate limiter, such as Music Assistant's Throttler."""

    async def __aenter__(self) -> object:
        """Acquire a slot."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> object:
        """Release a slot."""


async def request_json(
    session: ClientSession,
    method: str,
    url: str,
    *,
    error_type: type[Exception],
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    data: Any = None,
    throttler: ThrottlerLike | None = None,
    timeout: int = REQUEST_TIMEOUT,
) -> Any:
    """
    Perform an HTTP request and return the decoded JSON body.

    :param session: The HTTP session of the Music Assistant server.
    :param method: HTTP method, e.g. ``GET`` or ``POST``.
    :param url: Absolute request URL.
    :param error_type: Exception raised for transport errors and non-2xx responses.
    :param params: Optional query parameters.
    :param headers: Optional request headers.
    :param data: Optional request body, for example an ``aiohttp.FormData``.
    :param throttler: Optional async rate limiter acquired before the request.
    :param timeout: Total request timeout in seconds.
    """
    throttle_context = throttler if throttler is not None else nullcontext()
    try:
        async with throttle_context:
            async with session.request(
                method,
                url,
                params=params,
                headers=headers,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                body = await response.text()
                if not 200 <= response.status < 300:
                    raise error_type(
                        f"{method} {url} failed with HTTP {response.status}: {body[:300]}"
                    )
    except (TimeoutError, aiohttp.ClientError) as err:
        raise error_type(f"{method} {url} failed: {err}") from err

    try:
        return json.loads(body)
    except ValueError as err:
        raise error_type(f"{method} {url} returned invalid JSON") from err
