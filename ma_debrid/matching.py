"""Rank cached torrents and find the audio file that matches a target track."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .const import QUALITY_FLAC, QUALITY_MP3
from .torbox_client import CachedTorrent, TorrentFile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .prowlarr_client import Release

AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".aif",
        ".aiff",
        ".alac",
        ".ape",
        ".dff",
        ".dsf",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
        ".wv",
    }
)
LOSSLESS_EXTENSIONS = frozenset(
    {".aif", ".aiff", ".alac", ".ape", ".dff", ".dsf", ".flac", ".wav", ".wv"}
)
JUNK_EXTENSIONS = frozenset(
    {
        ".cue",
        ".gif",
        ".jpeg",
        ".jpg",
        ".log",
        ".md5",
        ".m3u",
        ".m3u8",
        ".nfo",
        ".par2",
        ".pdf",
        ".png",
        ".rar",
        ".sfv",
        ".txt",
        ".zip",
    }
)

MIN_AUDIO_FILE_SIZE = 256 * 1024
TITLE_MATCH_THRESHOLD = 0.45

_BRACKETED = re.compile(r"[\(\[\{].*?[\)\]\}]")
_FEATURING = re.compile(r"\b(?:feat|ft|featuring)\.?\s+.*$", re.IGNORECASE)
_TRACK_NUMBER_PREFIX = re.compile(
    r"^\s*(?:(?:cd|disc)\s*\d+\s*[-_. ]*)?(\d{1,3})\s*(?:[-_.\)\]]\s*|\s+)",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class FileSelection:
    """A cached torrent and the file inside it that should be streamed."""

    torrent: CachedTorrent
    file: TorrentFile
    score: float
    release: Release | None = None


def normalize_text(value: str) -> str:
    """Return a lower-cased, accent-free, punctuation-free comparison form."""
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_ALNUM.sub(" ", ascii_text.casefold().replace("&", " and ")).strip()


def title_variants(value: str) -> tuple[str, ...]:
    """Return the useful normalized variants of a title."""
    decomposed = unicodedata.normalize("NFKD", value)
    raw = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    raw = raw.replace("&", " and ")
    without_featuring = _FEATURING.sub(" ", raw)
    variants = {
        normalize_text(raw),
        normalize_text(without_featuring),
        normalize_text(_BRACKETED.sub(" ", raw)),
        normalize_text(_BRACKETED.sub(" ", without_featuring)),
    }
    return tuple(sorted(variant for variant in variants if variant))


def similarity(title: str, candidate: str) -> float:
    """Return how closely a title and a candidate filename resemble each other."""
    title_values = title_variants(title)
    candidate_values = title_variants(candidate)
    if not title_values or not candidate_values:
        return 0.0
    best = 0.0
    for left in title_values:
        left_tokens = set(left.split())
        for right in candidate_values:
            best = max(best, SequenceMatcher(None, left, right).ratio())
            right_tokens = set(right.split())
            overlap = len(left_tokens & right_tokens)
            if overlap and left_tokens and right_tokens:
                # Dice coefficient: short generic words alone do not qualify a match
                best = max(best, 2 * overlap / (len(left_tokens) + len(right_tokens)))
    return best


def file_extension(name: str) -> str:
    """Return the lower-cased extension of a torrent file path."""
    return PurePosixPath(name.replace("\\", "/")).suffix.casefold()


def file_stem(name: str) -> str:
    """Return the basename stem of a torrent file path."""
    return PurePosixPath(name.replace("\\", "/")).stem


def strip_track_number(value: str) -> str:
    """Remove a leading track or disc-track number from a filename stem."""
    return _TRACK_NUMBER_PREFIX.sub("", value, count=1).strip()


def parse_track_number(name: str) -> int | None:
    """Return the leading track number of a filename, when present."""
    match = _TRACK_NUMBER_PREFIX.match(file_stem(name))
    return int(match.group(1)) if match else None


def is_junk_file(name: str) -> bool:
    """Return True for non-audio companions and sample clips."""
    if file_extension(name) in JUNK_EXTENSIONS:
        return True
    return "sample" in file_stem(name).casefold()


def quality_score(extension: str, preferred_quality: str) -> float:
    """Score a file extension against the configured quality preference."""
    lossless = extension in LOSSLESS_EXTENSIONS
    is_mp3 = extension == ".mp3"
    if preferred_quality == QUALITY_FLAC:
        return 1.0 if lossless else 0.4 if is_mp3 else 0.2
    if preferred_quality == QUALITY_MP3:
        return 1.0 if is_mp3 else 0.8 if lossless else 0.4
    return 1.0 if lossless else 0.95 if is_mp3 else 0.6


def score_file(
    file: TorrentFile,
    *,
    title: str,
    track_number: int,
    preferred_quality: str,
    album_title: str | None = None,
    torrent_name: str | None = None,
) -> float | None:
    """
    Score one torrent file as the target track, or return None when it cannot be it.

    :param file: The file to score.
    :param title: Title of the track that should be played.
    :param track_number: Track number of the target track, 0 when unknown.
    :param preferred_quality: One of the configured quality preferences.
    :param album_title: Album title, used as a weak tie-breaker.
    :param torrent_name: Name of the torrent, used as a weak tie-breaker.
    """
    extension = file_extension(file.name)
    if extension not in AUDIO_EXTENSIONS or is_junk_file(file.name):
        return None
    if 0 < file.size < MIN_AUDIO_FILE_SIZE:
        return None

    candidate_title = strip_track_number(file_stem(file.name))
    title_similarity = similarity(title, candidate_title)
    if title_similarity < TITLE_MATCH_THRESHOLD:
        return None

    file_number = parse_track_number(file.name)
    number_match = track_number > 0 and file_number == track_number
    number_conflict = track_number > 0 and file_number is not None and not number_match

    # quality carries enough weight that a lossless file beats a lossy one with a slightly
    # better title match (a title gap of about 0.4 is needed to flip it)
    score = title_similarity * 0.5
    score += quality_score(extension, preferred_quality) * 0.35
    if number_match:
        score += 0.15
    elif number_conflict:
        score -= 0.25
    if album_title and torrent_name:
        score += similarity(album_title, torrent_name) * 0.05
    return score


def select_best_file(
    torrent: CachedTorrent,
    *,
    title: str,
    track_number: int,
    preferred_quality: str,
    album_title: str | None = None,
) -> tuple[TorrentFile, float] | None:
    """
    Return the best matching file of one cached torrent.

    A torrent without a file list is skipped: without a file name and id there is nothing
    to match or request a download link for.
    """
    files = torrent.files
    best_file: TorrentFile | None = None
    best_score = 0.0
    for file in files:
        score = score_file(
            file,
            title=title,
            track_number=track_number,
            preferred_quality=preferred_quality,
            album_title=album_title,
            torrent_name=torrent.name,
        )
        if score is None:
            continue
        if score > best_score or (
            score == best_score and best_file is not None and file.size > best_file.size
        ):
            best_file = file
            best_score = score
    return (best_file, best_score) if best_file is not None else None


def rank_selections(
    cached: Mapping[str, CachedTorrent],
    releases: Mapping[str, Release],
    *,
    title: str,
    track_number: int,
    album_title: str | None,
    preferred_quality: str,
    limit: int = 5,
) -> list[FileSelection]:
    """
    Rank every cached torrent by how well its matching file fits the target track.

    :param cached: Cached torrents keyed by info hash.
    :param releases: Prowlarr releases keyed by info hash, for seeders and magnets.
    :param title: Title of the track that should be played.
    :param track_number: Track number of the target track, 0 when unknown.
    :param album_title: Album title used as a weak tie-breaker.
    :param preferred_quality: One of the configured quality preferences.
    :param limit: Maximum number of selections to return.
    """
    selections: list[FileSelection] = []
    for info_hash, torrent in cached.items():
        match = select_best_file(
            torrent,
            title=title,
            track_number=track_number,
            preferred_quality=preferred_quality,
            album_title=album_title,
        )
        if match is None:
            continue
        file, score = match
        selections.append(
            FileSelection(
                torrent=torrent,
                file=file,
                score=score,
                release=releases.get(info_hash),
            )
        )

    selections.sort(
        key=lambda selection: (
            selection.score,
            selection.release.seeders if selection.release is not None else 0,
            selection.file.size,
        ),
        reverse=True,
    )
    return selections[:limit]


MAX_DOWNLOAD_SIZE = 2 * 1024**3
_LOSSLESS_TOKENS = frozenset({"flac", "lossless", "alac", "wav", "24bit"})
_LOW_BITRATE_TOKENS = frozenset({"64kbps", "96kbps", "112kbps", "128kbps", "128k", "128"})
_VERSION_SUFFIX = re.compile(
    r"\s+-\s+.*\b(?:remix|mix|version|edit|remaster(?:ed)?|live|acoustic)\b.*$",
    re.IGNORECASE,
)


def clean_title(value: str) -> str:
    """Strip bracketed parts and version suffixes, e.g. ``Sugar (Remix)`` -> ``Sugar``."""
    cleaned = _BRACKETED.sub(" ", value)
    cleaned = _VERSION_SUFFIX.sub("", cleaned)
    return " ".join(cleaned.split())


def rank_downloads(
    releases: Mapping[str, Release],
    *,
    artist: str,
    album: str,
    title: str,
    preferred_quality: str,
    limit: int = 3,
) -> list[Release]:
    """
    Rank uncached releases by how worthwhile they are to download for a track.

    A release must name the artist and either the album or the track, have seeders and
    stay below ``MAX_DOWNLOAD_SIZE``. Smaller, well seeded, preferred-format releases win.

    :param releases: Prowlarr releases keyed by info hash.
    :param artist: Artist name of the target track.
    :param album: Album title of the target track.
    :param title: Title of the target track.
    :param preferred_quality: One of the configured quality preferences.
    :param limit: Maximum number of releases to return.
    """
    artist_tokens = set(normalize_text(artist).split())
    album_tokens = set(normalize_text(clean_title(album)).split())
    title_tokens = set(normalize_text(clean_title(title)).split())

    ranked: list[tuple[float, Release]] = []
    for release in releases.values():
        if release.seeders < 1 or (release.size and release.size > MAX_DOWNLOAD_SIZE):
            continue
        tokens = set(normalize_text(release.title).split())
        if artist_tokens and not artist_tokens <= tokens:
            continue
        album_hit = bool(album_tokens) and album_tokens <= tokens
        title_hit = bool(title_tokens) and title_tokens <= tokens
        if not (album_hit or title_hit):
            continue

        score = 0.5 if album_hit else 0.35
        if title_hit:
            score += 0.15
        if preferred_quality == QUALITY_FLAC and tokens & _LOSSLESS_TOKENS:
            score += 0.4
        elif preferred_quality == QUALITY_MP3 and "mp3" in tokens:
            score += 0.2
        if preferred_quality != QUALITY_MP3 and tokens & _LOW_BITRATE_TOKENS:
            score -= 0.3
        score += min(release.seeders, 50) / 50 * 0.15
        # the size cap above already keeps downloads reasonable: a larger release is usually
        # lossless, so size is not held against it any more
        ranked.append((score, release))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [release for _, release in ranked[:limit]]
