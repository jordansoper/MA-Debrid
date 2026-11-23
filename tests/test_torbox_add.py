"""Tests for how torrents are added to TorBox."""

from __future__ import annotations

from fakes import FakeSession
from ma_debrid.torbox_client import TorBoxClient

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"


async def test_add_torrent_sends_a_clean_info_hash_magnet() -> None:
    """The indexer's magnet (with a messy display name) must not reach TorBox."""
    session = FakeSession().route(
        "/torrents/createtorrent",
        {"success": True, "data": {"torrent_id": 42}},
    )
    client = TorBoxClient(session, "token")  # type: ignore[arg-type]

    torrent_id = await client.add_torrent(
        INFO_HASH,
        f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Poppy - Negative Spaces â­ï¸ [PMEDIA]",
    )

    assert torrent_id == 42
    form = session.calls[0]["kwargs"]["data"]
    sent = {field[0]["name"]: field[2] for field in form._fields}
    assert sent["magnet"] == f"magnet:?xt=urn:btih:{INFO_HASH}"
    assert sent["add_only_if_cached"] == "true"
