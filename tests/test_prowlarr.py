"""Tests for the Prowlarr client's request format."""

from __future__ import annotations

from fakes import FakeSession
from ma_debrid.prowlarr_client import ProwlarrClient


async def test_search_sends_each_category_as_its_own_parameter() -> None:
    """Prowlarr rejects comma-separated categories with HTTP 400, so each is repeated."""
    session = FakeSession().route("/api/v1/search", [])
    client = ProwlarrClient(
        session,  # type: ignore[arg-type]
        base_url="http://prowlarr:9696",
        api_key="key",
    )

    await client.search("Daft Punk Discovery", categories=("3000", "3010", "3040"))

    params = session.calls[0]["kwargs"]["params"]
    assert params.getall("categories") == ["3000", "3010", "3040"]
    assert params["query"] == "Daft Punk Discovery"
