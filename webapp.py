from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, unquote

import requests

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, USLT
from mutagen.mp4 import MP4

from scrape_artist_images import (
    AlbumFolder,
    ArtistImageResolver,
    IMAGE_EXTENSIONS,
    build_artist_alias_map,
    collect_album_folders,
    load_json,
    probe_tags,
    sha1_text,
    safe_slug,
)

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_CANDIDATES = [
    Path(os.environ.get("CONFIG_PATH", "/config/config.json")).expanduser(),
    APP_DIR / "config" / "config.json",
    APP_DIR / "config.json",
]


def pick_existing_path(candidates):
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return Path(candidates[0]).resolve()


CONFIG_PATH = pick_existing_path(DEFAULT_CONFIG_CANDIDATES)
REPORT_PATH = Path(os.environ.get("REPORT_PATH", str(CONFIG_PATH.parent / "last-run-report.json"))).expanduser().resolve()
LYRICS_SCAN_CACHE_PATH = Path(os.environ.get("LYRICS_SCAN_CACHE_PATH", str(CONFIG_PATH.parent / "lyrics-scan-cache.json"))).expanduser().resolve()
ALBUM_ART_SCAN_CACHE_PATH = Path(os.environ.get("ALBUM_ART_SCAN_CACHE_PATH", str(CONFIG_PATH.parent / "album-art-scan-cache.json"))).expanduser().resolve()
ARTIST_SCAN_COUNT_CACHE_PATH = Path(os.environ.get("ARTIST_SCAN_COUNT_CACHE_PATH", str(CONFIG_PATH.parent / "artist-scan-count-cache.json"))).expanduser().resolve()
ARTIST_SCAN_COUNT_CACHE_MAX_AGE_SECONDS = int(os.environ.get("ARTIST_SCAN_COUNT_CACHE_MAX_AGE_SECONDS", "600"))
HOST = os.environ.get("WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
DEBUG = os.environ.get("WEB_DEBUG", "0") == "1"
ASSET_VERSION = str(int(time.time()))
PAGE_UPDATED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

MUSIC_TAG_WEB_DB = Path("/config/music_tag_web/music_tag.db")
MUSIC_TAG_WEB_MEDIA_ROOT = Path("/music")
MIXED_FOLDER_EXACT_PATHS = {
    "/music/自己下载",
}

app = Flask(__name__)
job_lock = threading.Lock()
lyrics_candidate_store: dict[str, dict] = {}
job_state = {
    "running": False,
    "mode": None,
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "log": [],
    "command": None,
    "auto_configure": False,
    "target": None,
}
home_bootstrap_state = {
    "running": False,
    "stage": None,
    "started_at": None,
    "finished_at": None,
    "done": False,
    "error": "",
}


def load_json_file(path: Path, default: dict | list | None = None):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    if default is None:
        return {}
    return default


def write_json_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_config() -> dict:
    return load_json(CONFIG_PATH)


def resolve_music_root(config: dict) -> Path:
    music_root = Path(config["music_root"]).expanduser()
    if not music_root.is_absolute():
        music_root = (CONFIG_PATH.parent / music_root).resolve()
    return music_root.resolve()


def resolve_cache_dir(config: dict) -> Path:
    cache_dir = Path(config.get("cache_dir", "./cache")).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = (CONFIG_PATH.parent / cache_dir).resolve()
    return cache_dir.resolve()


def resolve_export_dir(config: dict) -> Optional[Path]:
    export_dir = config.get("navidrome_export_dir")
    if not export_dir:
        return None
    path = Path(export_dir).expanduser()
    if not path.is_absolute():
        path = (CONFIG_PATH.parent / path).resolve()
    return path.resolve()


def collect_library_file_stats(music_root: Path, extensions: set[str], skip_dirs: List[str]) -> dict:
    song_count = 0
    album_dirs = set()
    for current_root, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        matched = [name for name in filenames if (current / name).suffix.lower() in extensions]
        if not matched:
            continue
        song_count += len(matched)
        album_dirs.add(str(current))
    return {
        "songs": song_count,
        "album_dirs": len(album_dirs),
    }


def collect_library_preview_rows(music_root: Path, extensions: set[str], skip_dirs: List[str], limit: int = 120) -> dict:
    song_rows = []
    album_rows = []
    album_seen = set()
    for current_root, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        matched = sorted([name for name in filenames if (current / name).suffix.lower() in extensions])
        if not matched:
            continue
        artist_name = current.name
        if str(current) not in album_seen and len(album_rows) < limit:
            album_rows.append({
                "artist": artist_name,
                "path": str(current),
                "source_file": str(current / matched[0]),
                "status": "unknown",
                "mode": "file-scan",
                "has_artist_image": False,
            })
            album_seen.add(str(current))
        for name in matched:
            if len(song_rows) >= limit:
                break
            song_rows.append({
                "artist": artist_name,
                "source_file": str(current / name),
                "folder_path": str(current),
                "status": "unknown",
                "has_artist_image": False,
            })
        if len(song_rows) >= limit and len(album_rows) >= limit:
            break
    return {
        "song_rows": song_rows,
        "album_rows": album_rows,
    }


def collect_song_detail_rows(music_root: Path, extensions: set[str], skip_dirs: List[str], limit: int = 300) -> list[dict]:
    rows = []
    for current_root, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        matched = sorted([name for name in filenames if (current / name).suffix.lower() in extensions])
        if not matched:
            continue
        for name in matched:
            if len(rows) >= limit:
                return rows
            audio_path = current / name
            try:
                tags = extract_audio_tags(audio_path)
            except Exception:
                tags = {}
            flags = read_audio_metadata_flags(str(audio_path.resolve()))
            title = str(tags.get("title") or audio_path.stem).strip()
            artist = str(tags.get("artist") or tags.get("album_artist") or tags.get("albumartist") or current.name).strip()
            album = str(tags.get("album") or current.name).strip()
            rows.append({
                "id": str(audio_path.resolve()),
                "title": title,
                "artist": artist,
                "album": album,
                "filename": audio_path.name,
                "source_file": str(audio_path),
                "folder_path": str(current),
                "duration": int(flags.get("duration") or 0),
                "has_embedded_cover": bool(flags.get("has_embedded_cover")),
                "has_embedded_lyrics": bool(flags.get("has_embedded_lyrics")),
                "has_sidecar_lyrics": has_sidecar_lyric(audio_path),
                "suffix": audio_path.suffix.lower(),
                "track": str(tags.get("track") or "").strip(),
                "disc": str(tags.get("disc") or "").strip(),
                "year": str(tags.get("year") or tags.get("date") or "").strip(),
                "genre": str(tags.get("genre") or "").strip(),
                "cover_url": f"/api/song-cover?path={quote(str(audio_path.resolve()), safe='')}",
            })
    return rows


@lru_cache(maxsize=1)
def load_music_tag_web_lyrics_map() -> dict[str, str]:
    if not MUSIC_TAG_WEB_DB.exists():
        return {}
    rows: dict[str, str] = {}
    try:
        conn = sqlite3.connect(MUSIC_TAG_WEB_DB)
        cur = conn.cursor()
        for track_path, lyrics in cur.execute(
            """
            SELECT path, lyrics
            FROM music_track
            WHERE lyrics IS NOT NULL
              AND TRIM(lyrics) != ''
              AND path IS NOT NULL
            """
        ):
            track_path = str(track_path or "").strip()
            lyrics = str(lyrics or "").strip()
            if track_path and lyrics:
                rows[track_path] = lyrics
        conn.close()
    except Exception:
        return {}
    return rows


@lru_cache(maxsize=1)
def load_music_tag_web_lyrics_paths() -> set[str]:
    return set(load_music_tag_web_lyrics_map().keys())


def to_music_tag_web_track_path(audio_path: Path) -> str:
    try:
        relative = audio_path.resolve().relative_to(MUSIC_TAG_WEB_MEDIA_ROOT.resolve())
    except Exception:
        return ""
    return f"/app/media/{relative.as_posix()}"


def has_music_tag_web_db_lyric(audio_path: Path) -> bool:
    track_path = to_music_tag_web_track_path(audio_path)
    if not track_path:
        return False
    return track_path in load_music_tag_web_lyrics_paths()


def _read_id3_fallback_flags(audio_path: Path) -> dict:
    result = {
        "has_embedded_lyrics": False,
        "has_embedded_cover": False,
    }
    try:
        tags = ID3(str(audio_path))
    except Exception:
        return result
    try:
        for key in tags.keys():
            upper = str(key).upper()
            if any(token in upper for token in ["USLT", "SYLT", "LYRICS", "UNSYNCEDLYRICS", "©LYR"]):
                value = tags.get(key)
                if value and str(value).strip():
                    result["has_embedded_lyrics"] = True
            if "APIC" in upper:
                value = tags.get(key)
                if value:
                    result["has_embedded_cover"] = True
    except Exception:
        pass
    return result


@lru_cache(maxsize=4096)
def _read_audio_metadata_flags_cached(audio_path_str: str, stat_signature: tuple[int, int]) -> dict:
    audio_path = Path(audio_path_str)
    result = {
        "has_embedded_lyrics": False,
        "has_embedded_cover": False,
    }
    try:
        audio = MutagenFile(audio_path)
    except Exception:
        return _read_id3_fallback_flags(audio_path)
    if audio is None:
        return _read_id3_fallback_flags(audio_path)

    tags = getattr(audio, "tags", None)
    if tags:
        try:
            for key in tags.keys():
                upper = str(key).upper()
                if any(token in upper for token in ["USLT", "SYLT", "LYRICS", "UNSYNCEDLYRICS", "©LYR"]):
                    value = tags.get(key)
                    if value and str(value).strip():
                        result["has_embedded_lyrics"] = True
                if any(token in upper for token in ["APIC", "COVR", "METADATA_BLOCK_PICTURE"]):
                    value = tags.get(key)
                    if value:
                        result["has_embedded_cover"] = True
        except Exception:
            pass

    if not result["has_embedded_lyrics"]:
        lyrics_attr = getattr(audio, "lyrics", None)
        if lyrics_attr and str(lyrics_attr).strip():
            result["has_embedded_lyrics"] = True

    if not result["has_embedded_cover"]:
        pictures = getattr(audio, "pictures", None)
        if pictures:
            result["has_embedded_cover"] = len(pictures) > 0

    return result


def read_audio_metadata_flags(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    try:
        stat = audio_path.stat()
        stat_signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except Exception:
        stat_signature = (0, 0)
    return _read_audio_metadata_flags_cached(audio_path_str, stat_signature)


def clear_audio_metadata_flags_cache() -> None:
    _read_audio_metadata_flags_cached.cache_clear()


def has_sidecar_lyric(audio_path: Path) -> bool:
    stem = audio_path.with_suffix("")
    candidates = [
        stem.with_suffix(".lrc"),
        stem.with_suffix(".txt"),
    ]
    return any(p.exists() and p.is_file() for p in candidates)


def has_any_lyric(audio_path: Path, include_embedded: bool = True) -> bool:
    if has_sidecar_lyric(audio_path):
        return True
    if not include_embedded:
        return False
    flags = read_audio_metadata_flags(str(audio_path.resolve()))
    return bool(flags.get("has_embedded_lyrics"))


def build_http_session() -> requests.Session:
    session = requests.Session()
    try:
        config = get_config()
        session.headers.update({
            "User-Agent": config.get("user_agent", "navidrome-artist-image-scraper/0.1")
        })
    except Exception:
        session.headers.update({"User-Agent": "navidrome-artist-image-scraper/0.1"})
    return session


def request_timeout() -> int:
    try:
        return int(get_config().get("request_timeout", 20))
    except Exception:
        return 20


def safe_candidate_token(audio_path: Path, source: str, candidate_id: str) -> str:
    raw = f"{audio_path.resolve()}|{source}|{candidate_id}|{int(time.time())}"
    return sha1_text(raw)


def _candidate_title_artist_score(title_cf: str, artist_cf: str, part_cfs: list[str], candidate_title: str, candidate_artists: list[str]) -> int:
    score = 0
    cand_title_cf = candidate_title.casefold()
    cand_artists_cf = [a.casefold() for a in candidate_artists if a]
    if cand_title_cf == title_cf:
        score += 60
    elif cand_title_cf.startswith(title_cf) or title_cf.startswith(cand_title_cf):
        score += 48
    elif cand_title_cf and (cand_title_cf in title_cf or title_cf in cand_title_cf):
        score += 35

    if any(a == artist_cf for a in cand_artists_cf):
        score += 40
    elif any(any(a == p or a in p or p in a for p in part_cfs) for a in cand_artists_cf):
        score += 32
    elif any(a in artist_cf or artist_cf in a for a in cand_artists_cf):
        score += 20
    return score


def build_lyric_candidates(audio_path: Path, max_candidates: int = 8) -> list[dict]:
    context = track_search_context(audio_path)
    title = str(context.get("title") or "").strip()
    artist = str(context.get("artist") or "").strip()
    artist_parts = context.get("artist_parts") or []
    album = str(context.get("album") or "").strip()
    filename_candidates = context.get("filename_candidates") or []
    session = build_http_session()
    candidates: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def add_candidate(source: str, candidate_id: str, candidate_title: str, candidate_artists: list[str], album_name: str, lyrics: str, score: int):
        key = (source, candidate_id, lyrics[:120])
        if key in seen_keys or not lyrics.strip():
            return
        seen_keys.add(key)
        token = safe_candidate_token(audio_path, source, candidate_id)
        item = {
            "token": token,
            "source": source,
            "candidate_id": candidate_id,
            "title": candidate_title,
            "artists": candidate_artists,
            "artist_text": " / ".join([a for a in candidate_artists if a]),
            "album": album_name,
            "score": score,
            "lyrics_preview": lyrics[:180].replace("\n", " / "),
        }
        lyrics_candidate_store[token] = {
            "audio_path": str(audio_path.resolve()),
            "source": source,
            "candidate_id": candidate_id,
            "lyrics": lyrics,
            "title": candidate_title,
            "artists": candidate_artists,
            "album": album_name,
        }
        candidates.append(item)

    title_cf = title.casefold()
    artist_cf = artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]

    try:
        lrclib_queries = []
        seen_queries = set()

        def add_lrclib_query(track_name: str, artist_name: str, album_name: str = ""):
            key = (track_name.strip(), artist_name.strip(), album_name.strip())
            if not key[0] or key in seen_queries:
                return
            seen_queries.add(key)
            lrclib_queries.append({"track_name": key[0], "artist_name": key[1], "album_name": key[2]})

        for cand in filename_candidates:
            add_lrclib_query(str(cand.get("title") or ""), str(cand.get("artist") or ""), "")
            add_lrclib_query(str(cand.get("title") or ""), str(cand.get("artist") or ""), album)
        add_lrclib_query(title, artist, album)
        for part in artist_parts:
            add_lrclib_query(title, part, album)
            add_lrclib_query(title, part, "")
        add_lrclib_query(title, artist, "")

        for params in lrclib_queries[:8]:
            resp = session.get("https://lrclib.net/api/search", params=params, timeout=min(request_timeout(), 8))
            resp.raise_for_status()
            items = resp.json() or []
            for item in items[:6]:
                lyrics = str((item.get("syncedLyrics") or item.get("plainLyrics") or "")).strip()
                if not lyrics:
                    continue
                candidate_title = str(item.get("trackName") or "").strip()
                candidate_artist = str(item.get("artistName") or "").strip()
                candidate_album = str(item.get("albumName") or "").strip()
                score = _candidate_title_artist_score(title_cf, artist_cf, part_cfs, candidate_title, [candidate_artist])
                add_candidate("lrclib", str(item.get("id") or f"{candidate_title}|{candidate_artist}"), candidate_title, [candidate_artist], candidate_album, lyrics, score)
    except Exception:
        pass

    try:
        netease_terms = []
        seen_terms = set()

        def add_term(term: str):
            term = term.strip()
            if term and term not in seen_terms:
                seen_terms.add(term)
                netease_terms.append(term)

        for cand in filename_candidates:
            add_term(f"{cand.get('title', '')} {cand.get('artist', '')}")
            add_term(str(cand.get("title") or ""))
        for part in artist_parts:
            add_term(f"{title} {part}")
        add_term(f"{title} {artist}")
        add_term(title)

        for term in netease_terms[:6]:
            resp = session.get(
                "https://music.163.com/api/cloudsearch/pc",
                params={"s": term, "type": "1", "limit": "10", "offset": "0"},
                headers={"Referer": "https://music.163.com/"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            songs = ((resp.json() or {}).get("result") or {}).get("songs") or []
            for song in songs[:6]:
                song_id = str(song.get("id") or "").strip()
                if not song_id:
                    continue
                lyric_resp = session.get(
                    "https://music.163.com/api/song/lyric",
                    params={"id": song_id, "lv": "1", "kv": "1", "tv": "-1"},
                    headers={"Referer": "https://music.163.com/"},
                    timeout=min(request_timeout(), 8),
                )
                lyric_resp.raise_for_status()
                lyric_data = lyric_resp.json() or {}
                lyrics = str(((lyric_data.get("lrc") or {}).get("lyric") or (lyric_data.get("klyric") or {}).get("lyric") or "")).strip()
                if not lyrics:
                    continue
                candidate_title = str(song.get("name") or "").strip()
                candidate_artists = [str(a.get("name") or "").strip() for a in (song.get("ar") or []) if a.get("name")]
                candidate_album = str(((song.get("al") or {}).get("name") or "")).strip()
                score = _candidate_title_artist_score(title_cf, artist_cf, part_cfs, candidate_title, candidate_artists)
                candidate_title_cf = candidate_title.casefold()
                candidate_album_cf = candidate_album.casefold()
                if candidate_title_cf == "渡情" or candidate_title_cf.endswith("渡情"):
                    score += 12
                if candidate_album_cf and ("新白娘子传奇" in candidate_album_cf or "白娘子" in candidate_album_cf):
                    score += 10
                if is_lyric_title_mismatch_guard(title, candidate_title):
                    score = min(score, 34)
                add_candidate("netease", song_id, candidate_title, candidate_artists, candidate_album, lyrics, score)
    except Exception:
        pass

    try:
        kuwo_candidates = search_kuwo_song_candidates(str(audio_path.resolve()), limit=6)
        if kuwo_candidates.get("ok"):
            for match in (kuwo_candidates.get("matches") or [])[:4]:
                item = match.get("item") or {}
                lyrics = fetch_kuwo_candidate_lyric(item)
                add_candidate(
                    "kuwo",
                    str(item.get("rid") or item.get("musicrid") or item.get("id") or "kuwo"),
                    str(item.get("name") or title),
                    [str(item.get("artist") or artist).strip()],
                    str(item.get("album") or "").strip(),
                    lyrics,
                    int(match.get("score") or 0),
                )
    except Exception:
        pass

    try:
        kugou_candidates = search_kugou_song_candidates(str(audio_path.resolve()), limit=6)
        if kugou_candidates.get("ok"):
            for match in (kugou_candidates.get("matches") or [])[:4]:
                item = match.get("item") or {}
                lyrics = fetch_kugou_candidate_lyric(item)
                add_candidate(
                    "kugou",
                    str(item.get("id") or item.get("accesskey") or "kugou"),
                    str(item.get("song") or title),
                    [str(item.get("singer") or artist).strip()],
                    str(item.get("album") or "").strip(),
                    lyrics,
                    int(match.get("score") or 0),
                )
    except Exception:
        pass

    try:
        qq_candidates = search_qq_song_candidates(str(audio_path.resolve()), limit=6)
        if qq_candidates.get("ok"):
            for match in (qq_candidates.get("matches") or [])[:4]:
                item = match.get("item") or {}
                singers = item.get("singer") or []
                lyrics = fetch_qq_candidate_lyric(item)
                add_candidate(
                    "qq",
                    str(item.get("songmid") or item.get("songid") or "qq"),
                    str(item.get("songname") or item.get("title") or title),
                    [str(s.get("name") or "").strip() for s in singers if s.get("name")] or [artist],
                    str(item.get("albumname") or "").strip(),
                    lyrics,
                    int(match.get("score") or 0),
                )
    except Exception:
        pass

    candidates.sort(key=lambda item: (-int(item.get("score") or 0), item.get("source") or "", item.get("title") or ""))
    return candidates[:max_candidates]


def track_search_context(audio_path: Path) -> dict:
    tags = probe_tags(audio_path)
    title = tags.get("title") or audio_path.stem
    artist = tags.get("artist") or tags.get("album_artist") or tags.get("albumartist") or audio_path.parent.name
    album = tags.get("album") or audio_path.parent.name

    raw_artists = [str(artist).strip()] if artist else []
    artist_parts = []
    for chunk in raw_artists:
        for piece in chunk.replace(';', ',').replace('；', ',').split(','):
            piece = piece.strip()
            if piece and piece not in artist_parts:
                artist_parts.append(piece)
    if not artist_parts and artist:
        artist_parts = [str(artist).strip()]

    filename_candidates = []
    stem = audio_path.stem.strip()
    separators = [' – ', ' - ']
    for sep in separators:
        if sep in stem:
            left, right = stem.split(sep, 1)
            filename_candidates.append({'artist': left.strip(), 'title': right.strip()})
            right_clean = right.replace('(Single Version)', '').replace('（Single Version）', '').strip()
            right_parts = [p for p in right_clean.split() if p.strip()]
            if len(right_parts) >= 2:
                filename_candidates.append({'artist': right_parts[0].strip(), 'title': ' '.join(right_parts[1:]).strip()})
            break

    normalized_candidates = []
    seen_candidates = set()

    def add_candidate(cand_artist: str, cand_title: str):
        cand_artist = cand_artist.strip()
        cand_title = cand_title.strip()
        cand_title = cand_title.replace('(Single Version)', '').replace('（Single Version）', '').strip()
        key = (cand_artist, cand_title)
        if cand_artist and cand_title and key not in seen_candidates:
            seen_candidates.add(key)
            normalized_candidates.append({'artist': cand_artist, 'title': cand_title})

    raw_title = str(title).strip()
    if '《' in raw_title and '》' in raw_title:
        before = raw_title.split('《', 1)[0].strip()
        inside = raw_title.split('《', 1)[1].split('》', 1)[0].strip()
        if before and inside:
            add_candidate(before, inside)

    for item in filename_candidates:
        cand_artist = item['artist'].strip()
        cand_title = item['title'].strip()
        if '《' in cand_title and '》' in cand_title:
            before = cand_title.split('《', 1)[0].strip()
            inside = cand_title.split('《', 1)[1].split('》', 1)[0].strip()
            if before and inside:
                add_candidate(before, inside)
        add_candidate(cand_artist, cand_title)

    return {
        "title": raw_title,
        "artist": str(artist).strip(),
        "artist_parts": artist_parts,
        "album": str(album).strip(),
        "filename_candidates": normalized_candidates,
    }


@lru_cache(maxsize=4096)
def search_lrclib_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    artist = context["artist"]
    album = context["album"]
    artist_parts = context.get("artist_parts") or []
    filename_candidates = context.get("filename_candidates") or []
    if not title or not artist:
        return {"ok": False, "error": "missing-title-or-artist"}

    session = build_http_session()
    query_variants = []
    seen = set()

    def add_query(track_name: str, artist_name: str, album_name: str = ""):
        key = (track_name.strip(), artist_name.strip(), album_name.strip())
        if key in seen:
            return
        seen.add(key)
        query_variants.append({
            "track_name": track_name.strip(),
            "artist_name": artist_name.strip(),
            "album_name": album_name.strip(),
        })

    for cand in filename_candidates:
        add_query(cand['title'], cand['artist'], "")
        add_query(cand['title'], cand['artist'], album)

    add_query(title, artist, album)
    for part in artist_parts:
        add_query(title, part, album)
        add_query(title, part, "")
    add_query(title, artist, "")

    filename_candidate_keys = {
        (cand['title'].strip(), cand['artist'].strip(), '' ) for cand in filename_candidates
    } | {
        (cand['title'].strip(), cand['artist'].strip(), album.strip()) for cand in filename_candidates
    }

    ordered_queries = []
    seen_ordered = set()
    for q in query_variants:
        artist_name = q.get('artist_name', '').strip()
        track_name = q.get('track_name', '').strip()
        album_name = q.get('album_name', '').strip()
        priority = 0
        if (track_name, artist_name, album_name) in filename_candidate_keys:
            priority = 0 if album_name else 1
        elif artist_name in artist_parts:
            priority = 2 if album_name else 3
        elif artist_name == artist:
            priority = 4 if album_name else 5
        else:
            priority = 6
        key = (priority, track_name, artist_name, album_name)
        if key in seen_ordered:
            continue
        seen_ordered.add(key)
        ordered_queries.append((priority, q))
    ordered_queries.sort(key=lambda x: x[0])

    best = None
    near_matches = []
    errors = []
    title_cf = title.casefold()
    artist_cf = artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]

    def score_lrclib_item(params: dict, item: dict):
        plain = (item.get("plainLyrics") or "").strip()
        synced = (item.get("syncedLyrics") or "").strip()
        lyrics = synced or plain
        if not lyrics:
            return None
        candidate_title = str(item.get("trackName") or "").strip().casefold()
        candidate_artist = str(item.get("artistName") or "").strip().casefold()
        score = 0
        if candidate_title == title_cf:
            score += 60
        elif candidate_title.startswith(title_cf) or title_cf.startswith(candidate_title):
            score += 48
        elif candidate_title and (candidate_title in title_cf or title_cf in candidate_title):
            score += 35

        if candidate_artist == artist_cf:
            score += 40
        elif candidate_artist and any(candidate_artist == p or candidate_artist in p or p in candidate_artist for p in part_cfs):
            score += 32
        elif candidate_artist and (candidate_artist in artist_cf or artist_cf in candidate_artist):
            score += 20

        if params.get('artist_name') and params['artist_name'].casefold() == candidate_artist:
            score += 8
        return (score, lyrics, item, params)

    for _, params in ordered_queries:
        append_job_log(f"[INFO] lrclib query -> track={params['track_name']} artist={params['artist_name']} album={params['album_name']}")
        try:
            resp = session.get("https://lrclib.net/api/search", params=params, timeout=min(request_timeout(), 8))
            resp.raise_for_status()
            items = resp.json() or []
            append_job_log(f"[INFO] lrclib result count={len(items)} for artist={params['artist_name'] or '-'}")
            for item in items:
                scored = score_lrclib_item(params, item)
                if not scored:
                    continue
                if best is None or scored[0] > best[0]:
                    best = scored
            if best and best[0] >= 72:
                append_job_log(f"[INFO] lrclib early match score={best[0]} artist={params['artist_name'] or '-'}")
                break
        except Exception as exc:
            errors.append(str(exc))
            append_job_log(f"[WARN] lrclib query failed for artist={params['artist_name'] or '-'} :: {exc}")

    if not best or best[0] < 55:
        return {"ok": False, "error": "lrclib-no-confident-match", "context": context, "queries": query_variants, "errors": errors}
    return {
        "ok": True,
        "lyrics": best[1],
        "source": "lrclib",
        "context": context,
        "candidate": best[2],
        "query": best[3],
    }


def search_netease_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    artist = context["artist"]
    artist_parts = context.get("artist_parts") or []
    filename_candidates = context.get("filename_candidates") or []
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    session = build_http_session()
    query_terms = []
    seen = set()

    def add_term(term: str):
        t = term.strip()
        if not t or t in seen:
            return
        seen.add(t)
        query_terms.append(t)

    for cand in filename_candidates:
        add_term(f"{cand['title']} {cand['artist']}")
        add_term(cand['title'])
    for part in artist_parts:
        add_term(f"{title} {part}")
    add_term(f"{title} {artist}")
    add_term(title)

    title_cf = title.casefold()
    artist_cf = artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]
    preferred_title = title
    preferred_artist = artist
    if filename_candidates:
        top = filename_candidates[0]
        preferred_title = str(top.get('title') or preferred_title).strip() or preferred_title
        preferred_artist = str(top.get('artist') or preferred_artist).strip() or preferred_artist
    preferred_title_cf = preferred_title.casefold()
    preferred_artist_cf = preferred_artist.casefold()
    best = None
    near_matches = []
    errors = []

    for term in query_terms:
        append_job_log(f"[INFO] netease query -> {term}")
        try:
            resp = session.get(
                "https://music.163.com/api/cloudsearch/pc",
                params={"s": term, "type": "1", "limit": "10", "offset": "0"},
                headers={"Referer": "https://music.163.com/"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            songs = ((resp.json() or {}).get("result") or {}).get("songs") or []
            append_job_log(f"[INFO] netease result count={len(songs)} for term={term}")
        except Exception as exc:
            errors.append(str(exc))
            append_job_log(f"[WARN] netease query failed for term={term} :: {exc}")
            continue

        for song in songs:
            song_name = str(song.get("name") or "").strip().casefold()
            candidate_title_raw = str(song.get("name") or "").strip()
            artists = [str(a.get("name") or "").strip() for a in (song.get("ar") or []) if a.get("name")]
            artists_cf = [a.casefold() for a in artists]
            score = 0
            if song_name == preferred_title_cf:
                score += 80
            elif song_name == title_cf:
                score += 60
            elif song_name.startswith(preferred_title_cf) or preferred_title_cf.startswith(song_name):
                score += 64
            elif song_name and (song_name in preferred_title_cf or preferred_title_cf in song_name):
                score += 48
            elif song_name.startswith(title_cf) or title_cf.startswith(song_name):
                score += 36
            elif song_name and (song_name in title_cf or title_cf in song_name):
                score += 24

            if any(a == preferred_artist_cf for a in artists_cf):
                score += 55
            elif any(a == artist_cf for a in artists_cf):
                score += 40
            elif any(any(a == p or a in p or p in a for p in part_cfs) for a in artists_cf):
                score += 32
            elif any(a in preferred_artist_cf or preferred_artist_cf in a for a in artists_cf):
                score += 28
            elif any(a in artist_cf or artist_cf in a for a in artists_cf):
                score += 20

            if term == f"{preferred_title} {preferred_artist}" and song_name == preferred_title_cf and any(a == preferred_artist_cf for a in artists_cf):
                score += 25

            if is_lyric_title_mismatch_guard(preferred_title, candidate_title_raw) or is_lyric_title_mismatch_guard(title, candidate_title_raw):
                score = min(score, 34)

            near_matches.append({"score": score, "song": song, "term": term})

            if best is None or score > best[0]:
                best = (score, song, term)
        if best and best[0] >= 100:
            append_job_log(f"[INFO] netease early match score={best[0]} term={best[2]}")
            break

    near_matches.sort(key=lambda item: -int(item.get("score") or 0))

    if not best or best[0] < 35:
        if near_matches[:5]:
            append_job_log("[INFO] netease top near matches -> " + "; ".join([
                f"score={m.get('score')} title={str((m.get('song') or {}).get('name') or '').strip()} artists={'/'.join([str(a.get('name') or '').strip() for a in (((m.get('song') or {}).get('ar') or [])) if a.get('name')])}"
                for m in near_matches[:5]
            ]))
        return {"ok": False, "error": "netease-no-confident-match", "context": context, "errors": errors}

    song_id = best[1].get("id")
    if not song_id:
        return {"ok": False, "error": "netease-missing-song-id", "context": context}

    try:
        resp = session.get(
            "https://music.163.com/api/song/lyric",
            params={"id": str(song_id), "lv": "1", "kv": "1", "tv": "-1"},
            headers={"Referer": "https://music.163.com/"},
            timeout=min(request_timeout(), 8),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        lyric = ((data.get("lrc") or {}).get("lyric") or "").strip()
        if not lyric:
            lyric = ((data.get("klyric") or {}).get("lyric") or "").strip()
    except Exception as exc:
        return {"ok": False, "error": f"netease-lyric-fetch-failed: {exc}", "context": context}

    if not lyric:
        return {"ok": False, "error": "netease-empty-lyric", "context": context}
    return {
        "ok": True,
        "lyrics": lyric,
        "source": "netease",
        "context": context,
        "candidate": best[1],
    }


def _score_song_candidate(title_cf: str, artist_cf: str, part_cfs: list[str], preferred_title_cf: str, preferred_artist_cf: str, song_name: str, artists: list[str], boost_exact_combo: bool = False) -> int:
    song_name_cf = song_name.casefold().strip()
    artists_cf = [a.casefold().strip() for a in artists if a]
    score = 0
    if song_name_cf == preferred_title_cf:
        score += 80
    elif song_name_cf == title_cf:
        score += 60
    elif song_name_cf.startswith(preferred_title_cf) or preferred_title_cf.startswith(song_name_cf):
        score += 64
    elif song_name_cf and (song_name_cf in preferred_title_cf or preferred_title_cf in song_name_cf):
        score += 48
    elif song_name_cf.startswith(title_cf) or title_cf.startswith(song_name_cf):
        score += 36
    elif song_name_cf and (song_name_cf in title_cf or title_cf in song_name_cf):
        score += 24

    if any(a == preferred_artist_cf for a in artists_cf):
        score += 55
    elif any(a == artist_cf for a in artists_cf):
        score += 40
    elif any(any(a == p or a in p or p in a for p in part_cfs) for a in artists_cf):
        score += 32
    elif any(a in preferred_artist_cf or preferred_artist_cf in a for a in artists_cf):
        score += 28
    elif any(a in artist_cf or artist_cf in a for a in artists_cf):
        score += 20

    if boost_exact_combo and song_name_cf == preferred_title_cf and any(a == preferred_artist_cf for a in artists_cf):
        score += 25
    return score


def is_lyric_title_mismatch_guard(source_title: str, candidate_title: str) -> bool:
    source_title = str(source_title or "").strip()
    candidate_title = str(candidate_title or "").strip()
    source_cf = source_title.casefold()
    candidate_cf = candidate_title.casefold()
    if not source_cf or not candidate_cf:
        return False
    if source_cf == candidate_cf:
        return False
    if candidate_cf in source_cf or source_cf in candidate_cf:
        return False
    if (source_title, candidate_title) == ("同船共渡情意深", "渡情"):
        return True
    if source_cf.endswith("情意深") and candidate_cf == "渡情":
        return True
    return False


def is_lyric_title_mismatch_guard(source_title: str, candidate_title: str) -> bool:
    source_cf = str(source_title or "").strip().casefold()
    candidate_cf = str(candidate_title or "").strip().casefold()
    if not source_cf or not candidate_cf:
        return False
    guarded_pairs = {
        ("同船共渡情意深", "渡情"),
    }
    if (source_title.strip(), candidate_title.strip()) in guarded_pairs:
        return True
    if source_cf == candidate_cf:
        return False
    if candidate_cf in source_cf or source_cf in candidate_cf:
        return False
    if source_cf.endswith("情意深") and candidate_cf == "渡情":
        return True
    return False


def _build_lyric_query_terms(context: dict) -> tuple[list[str], str, str, list[str], str, str]:
    title = context["title"]
    artist = context["artist"]
    artist_parts = context.get("artist_parts") or []
    filename_candidates = context.get("filename_candidates") or []
    query_terms = []
    seen = set()

    def add_term(term: str):
        t = term.strip()
        if not t or t in seen:
            return
        seen.add(t)
        query_terms.append(t)

    for cand in filename_candidates:
        add_term(f"{cand['title']} {cand['artist']}")
        add_term(cand['title'])
    for part in artist_parts:
        add_term(f"{title} {part}")
    add_term(f"{title} {artist}")
    add_term(title)

    preferred_title = title
    preferred_artist = artist
    if filename_candidates:
        top = filename_candidates[0]
        preferred_title = str(top.get('title') or preferred_title).strip() or preferred_title
        preferred_artist = str(top.get('artist') or preferred_artist).strip() or preferred_artist
    return query_terms, preferred_title, preferred_artist, artist_parts, title, artist


def search_qq_song_candidates(audio_path_str: str, limit: int = 10) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    session = build_http_session()
    query_terms, preferred_title, preferred_artist, artist_parts, raw_title, raw_artist = _build_lyric_query_terms(context)
    title_cf = raw_title.casefold()
    artist_cf = raw_artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]
    preferred_title_cf = preferred_title.casefold()
    preferred_artist_cf = preferred_artist.casefold()
    matches = []
    seen = set()
    errors = []

    for term in query_terms[:6]:
        try:
            resp = session.get(
                "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                params={"w": term, "n": str(limit), "p": "1", "format": "json"},
                headers={"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            items = (((resp.json() or {}).get("data") or {}).get("song") or {}).get("list") or []
        except Exception as exc:
            errors.append(str(exc))
            continue

        for item in items:
            songmid = str(item.get("songmid") or "").strip()
            if not songmid or songmid in seen:
                continue
            seen.add(songmid)
            song_name = str(item.get("songname") or item.get("title") or "").strip()
            singers = item.get("singer") or []
            artists = [str(s.get("name") or "").strip() for s in singers if s.get("name")]
            score = _score_song_candidate(title_cf, artist_cf, part_cfs, preferred_title_cf, preferred_artist_cf, song_name, artists, boost_exact_combo=True)
            matches.append({"score": score, "item": item, "term": term})

    matches.sort(key=lambda x: -int(x.get("score") or 0))
    if not matches:
        return {"ok": False, "error": "qq-no-candidates", "context": context, "errors": errors}
    return {"ok": True, "context": context, "matches": matches[:limit]}


def search_kugou_song_candidates(audio_path_str: str, limit: int = 10) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    session = build_http_session()
    query_terms, preferred_title, preferred_artist, artist_parts, raw_title, raw_artist = _build_lyric_query_terms(context)
    title_cf = raw_title.casefold()
    artist_cf = raw_artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]
    preferred_title_cf = preferred_title.casefold()
    preferred_artist_cf = preferred_artist.casefold()
    matches = []
    seen = set()
    errors = []

    for term in query_terms[:6]:
        try:
            resp = session.get(
                "http://lyrics.kugou.com/search",
                params={"keyword": term, "page": "1", "pagesize": str(limit), "ver": "1", "client": "pc"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("candidates") or []
        except Exception as exc:
            errors.append(str(exc))
            continue

        for item in items:
            candidate_id = f"{item.get('id') or ''}|{item.get('accesskey') or ''}"
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            song_name = str(item.get("song") or "").strip()
            artists = [str(item.get("singer") or "").strip()]
            score = _score_song_candidate(title_cf, artist_cf, part_cfs, preferred_title_cf, preferred_artist_cf, song_name, artists, boost_exact_combo=True)
            matches.append({"score": score, "item": item, "term": term})

    matches.sort(key=lambda x: -int(x.get("score") or 0))
    if not matches:
        return {"ok": False, "error": "kugou-no-candidates", "context": context, "errors": errors}
    return {"ok": True, "context": context, "matches": matches[:limit]}


def search_kuwo_song_candidates(audio_path_str: str, limit: int = 10) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    session = build_http_session()
    session.headers.update({"Referer": "https://www.kuwo.cn/", "csrf": ""})
    query_terms, preferred_title, preferred_artist, artist_parts, raw_title, raw_artist = _build_lyric_query_terms(context)
    title_cf = raw_title.casefold()
    artist_cf = raw_artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]
    preferred_title_cf = preferred_title.casefold()
    preferred_artist_cf = preferred_artist.casefold()
    matches = []
    seen = set()
    errors = []

    for term in query_terms[:6]:
        try:
            resp = session.get(
                "https://www.kuwo.cn/api/www/search/searchMusicBykeyWord",
                params={"key": term, "pn": "1", "rn": str(limit), "httpsStatus": "1"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            items = (((resp.json() or {}).get("data") or {}).get("list") or [])
        except Exception as exc:
            errors.append(str(exc))
            continue

        for item in items:
            candidate_id = str(item.get("rid") or item.get("musicrid") or item.get("id") or "").strip()
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            song_name = str(item.get("name") or "").strip()
            artists = [str(item.get("artist") or "").strip()]
            score = _score_song_candidate(title_cf, artist_cf, part_cfs, preferred_title_cf, preferred_artist_cf, song_name, artists, boost_exact_combo=True)
            matches.append({"score": score, "item": item, "term": term})

    matches.sort(key=lambda x: -int(x.get("score") or 0))
    if not matches:
        return {"ok": False, "error": "kuwo-no-candidates", "context": context, "errors": errors}
    return {"ok": True, "context": context, "matches": matches[:limit]}


def fetch_qq_candidate_lyric(candidate: dict) -> str:
    session = build_http_session()
    songmid = str(candidate.get("songmid") or "").strip()
    if not songmid:
        return ""
    try:
        resp = session.get(
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={"songmid": songmid, "format": "json", "nobase64": "0"},
            headers={"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
            timeout=min(request_timeout(), 8),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        lyric_b64 = str(data.get("lyric") or "").strip()
        return base64.b64decode(lyric_b64).decode("utf-8", errors="ignore").strip() if lyric_b64 else ""
    except Exception:
        return ""


def fetch_kugou_candidate_lyric(candidate: dict) -> str:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    lyric_id = str(candidate.get("id") or candidate.get("accesskey") or "")
    accesskey = str(candidate.get("accesskey") or "")
    if not lyric_id or not accesskey:
        return ""
    try:
        resp = session.get(
            "http://lyrics.kugou.com/download",
            params={"id": lyric_id, "accesskey": accesskey, "fmt": "lrc", "charset": "utf8", "ver": "1", "client": "pc"},
            timeout=min(request_timeout(), 8),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        if int(data.get("error_code") or 0) != 0:
            return ""
        content = str(data.get("content") or "").strip()
        if not content:
            return ""
        payload = base64.b64decode(content)
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                text = payload.decode(encoding).strip()
                if text:
                    return text
            except Exception:
                continue
        return payload.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def fetch_kuwo_candidate_lyric(candidate: dict) -> str:
    session = build_http_session()
    session.headers.update({"Referer": "https://www.kuwo.cn/", "csrf": ""})
    rid = str(candidate.get("rid") or candidate.get("musicrid") or "")
    if rid.startswith("MUSIC_"):
        rid = rid.split("MUSIC_", 1)[1]
    if not rid:
        return ""
    try:
        resp = session.get(
            "https://www.kuwo.cn/openapi/v1/www/lyric/getlyric",
            params={"musicId": rid, "httpsStatus": "1"},
            timeout=min(request_timeout(), 8),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        lyric = str(((data.get("data") or {}).get("lrclist") or "")).strip()
        if lyric:
            return lyric
        lrclist = (data.get("data") or {}).get("lrclist") or []
        if isinstance(lrclist, list):
            rows = []
            for item in lrclist:
                line = str(item.get("lineLyric") or "").strip()
                if not line:
                    continue
                time_str = str(item.get("time") or item.get("lineTime") or "").strip()
                rows.append(f"[{time_str}]{line}" if time_str else line)
            return "\n".join(rows).strip()
    except Exception:
        return ""
    return ""


def search_kuwo_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    session = build_http_session()
    session.headers.update({"Referer": "https://www.kuwo.cn/", "csrf": ""})
    query_terms, preferred_title, preferred_artist, artist_parts, raw_title, raw_artist = _build_lyric_query_terms(context)
    title_cf = raw_title.casefold()
    artist_cf = raw_artist.casefold()
    part_cfs = [p.casefold() for p in artist_parts]
    preferred_title_cf = preferred_title.casefold()
    preferred_artist_cf = preferred_artist.casefold()
    best = None
    errors = []

    for term in query_terms:
        append_job_log(f"[INFO] kuwo query -> {term}")
        try:
            resp = session.get(
                "https://www.kuwo.cn/api/www/search/searchMusicBykeyWord",
                params={"key": term, "pn": "1", "rn": "10", "httpsStatus": "1"},
                timeout=min(request_timeout(), 8),
            )
            resp.raise_for_status()
            songs = (((resp.json() or {}).get("data") or {}).get("list") or [])
            append_job_log(f"[INFO] kuwo result count={len(songs)} for term={term}")
        except Exception as exc:
            errors.append(str(exc))
            append_job_log(f"[WARN] kuwo query failed for term={term} :: {exc}")
            continue

        for song in songs:
            song_name = str(song.get("name") or "").strip()
            artists = [str(song.get("artist") or "").strip()]
            score = _score_song_candidate(title_cf, artist_cf, part_cfs, preferred_title_cf, preferred_artist_cf, song_name, artists, boost_exact_combo=True)
            if best is None or score > best[0]:
                best = (score, song, term)
        if best and best[0] >= 100:
            append_job_log(f"[INFO] kuwo early match score={best[0]} term={best[2]}")
            break

    if not best or best[0] < 55:
        return {"ok": False, "error": "kuwo-no-confident-match", "context": context, "errors": errors}

    rid = str(best[1].get("rid") or best[1].get("musicrid") or "")
    if rid.startswith("MUSIC_"):
        rid = rid.split("MUSIC_", 1)[1]
    if not rid:
        return {"ok": False, "error": "kuwo-missing-rid", "context": context}

    try:
        resp = session.get(
            "https://www.kuwo.cn/openapi/v1/www/lyric/getlyric",
            params={"musicId": rid, "httpsStatus": "1"},
            timeout=min(request_timeout(), 8),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        lyric = str(((data.get("data") or {}).get("lrclist") or "")).strip()
        if not lyric and isinstance((data.get("data") or {}).get("lrclist"), list):
            rows = []
            for item in (data.get("data") or {}).get("lrclist") or []:
                line = str(item.get("lineLyric") or "").strip()
                if not line:
                    continue
                time_str = str(item.get("time") or item.get("lineTime") or "").strip()
                if time_str:
                    rows.append(f"[{time_str}]{line}")
                else:
                    rows.append(line)
            lyric = "\n".join(rows).strip()
    except Exception as exc:
        return {"ok": False, "error": f"kuwo-lyric-fetch-failed: {exc}", "context": context}

    if not lyric:
        return {"ok": False, "error": "kuwo-empty-lyric", "context": context}
    return {"ok": True, "lyrics": lyric, "source": "kuwo", "context": context, "candidate": best[1]}


def search_kugou_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    candidate_result = search_kugou_song_candidates(audio_path_str, limit=10)
    if not candidate_result.get("ok"):
        return {"ok": False, "error": "kugou-no-confident-match", "context": context, "errors": candidate_result.get("errors") or []}

    best = None
    for match in candidate_result.get("matches") or []:
        item = match.get("item") or {}
        score = int(match.get("score") or 0)
        if best is None or score > best[0]:
            best = (score, item, match.get("term") or "")
        if score >= 100:
            break

    if not best or best[0] < 45:
        return {"ok": False, "error": "kugou-no-confident-match", "context": context}

    lyric = fetch_kugou_candidate_lyric(best[1])

    if not lyric:
        return {"ok": False, "error": "kugou-empty-lyric", "context": context}
    return {"ok": True, "lyrics": lyric, "source": "kugou", "context": context, "candidate": best[1]}


def search_qq_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    title = context["title"]
    if not title:
        return {"ok": False, "error": "missing-title", "context": context}

    candidate_result = search_qq_song_candidates(audio_path_str, limit=10)
    if not candidate_result.get("ok"):
        return {"ok": False, "error": "qq-no-confident-match", "context": context, "errors": candidate_result.get("errors") or []}

    best = None
    for match in candidate_result.get("matches") or []:
        item = match.get("item") or {}
        score = int(match.get("score") or 0)
        if best is None or score > best[0]:
            best = (score, item, match.get("term") or "")
        if score >= 100:
            break

    if not best or best[0] < 45:
        return {"ok": False, "error": "qq-no-confident-match", "context": context}

    lyric = fetch_qq_candidate_lyric(best[1])

    if not lyric:
        return {"ok": False, "error": "qq-lyric-unavailable", "context": context}
    return {"ok": True, "lyrics": lyric, "source": "qq", "context": context, "candidate": best[1]}


def search_multisource_lyrics(audio_path_str: str) -> dict:
    audio_path = Path(audio_path_str)
    prefer_netease = should_prefer_netease_lyrics(audio_path)
    providers = [
        ("lrclib", search_lrclib_lyrics),
        ("netease", search_netease_lyrics),
        ("qq", search_qq_lyrics),
        ("kugou", search_kugou_lyrics),
        ("kuwo", search_kuwo_lyrics),
    ]
    if prefer_netease:
        providers = [
            ("netease", search_netease_lyrics),
            ("qq", search_qq_lyrics),
            ("kugou", search_kugou_lyrics),
            ("kuwo", search_kuwo_lyrics),
            ("lrclib", search_lrclib_lyrics),
        ]

    last_error = None
    errors = []
    for source_name, func in providers:
        result = func(audio_path_str)
        if result.get("ok"):
            return result
        last_error = result.get("error") or f"{source_name}-unknown-error"
        errors.append({"source": source_name, "error": last_error})
        append_job_log(f"[WARN] {source_name} fallback -> {audio_path} :: {last_error}")
    return {"ok": False, "error": last_error or "all-providers-failed", "errors": errors}


    audio_path = Path(audio_path_str)
    context = track_search_context(audio_path)
    term = f"{context['artist']} {context['album']}"
    if not context['artist'] or not context['album']:
        return {"ok": False, "error": "missing-artist-or-album", "context": context}
    session = build_http_session()
    params = {
        "term": term,
        "entity": "album",
        "limit": 10,
        "country": "CN",
    }
    try:
        resp = session.get("https://itunes.apple.com/search", params=params, timeout=request_timeout())
        resp.raise_for_status()
        items = (resp.json() or {}).get("results") or []
    except Exception as exc:
        return {"ok": False, "error": f"itunes-request-failed: {exc}", "context": context}

    best = None
    for item in items:
        art = item.get("artworkUrl100") or item.get("artworkUrl60")
        if not art:
            continue
        cand_artist = str(item.get("artistName") or "").strip().casefold()
        cand_album = str(item.get("collectionName") or "").strip().casefold()
        score = 0
        if cand_artist == context['artist'].casefold():
            score += 45
        elif cand_artist and (cand_artist in context['artist'].casefold() or context['artist'].casefold() in cand_artist):
            score += 20
        if cand_album == context['album'].casefold():
            score += 55
        elif cand_album and (cand_album in context['album'].casefold() or context['album'].casefold() in cand_album):
            score += 25
        if best is None or score > best[0]:
            best = (score, art.replace('100x100bb', '1200x1200bb'), item)
    if not best or best[0] < 55:
        return {"ok": False, "error": "itunes-no-confident-match", "context": context}
    try:
        image_resp = session.get(best[1], timeout=request_timeout())
        image_resp.raise_for_status()
    except Exception as exc:
        return {"ok": False, "error": f"itunes-image-download-failed: {exc}", "context": context}
    return {
        "ok": True,
        "image_bytes": image_resp.content,
        "source": "itunes",
        "context": context,
        "candidate": best[2],
    }


    candidates = [
        folder_path / "cover.jpg",
        folder_path / "cover.png",
        folder_path / "Cover.jpg",
        folder_path / "Cover.png",
        folder_path / "folder.jpg",
        folder_path / "folder.png",
        folder_path / "Folder.jpg",
        folder_path / "Folder.png",
    ]
    return any(p.exists() and p.is_file() for p in candidates)


def should_prefer_netease_lyrics(audio_path: Path) -> bool:
    context = track_search_context(audio_path)
    filename_candidates = context.get("filename_candidates") or []
    title = str(context.get("title") or "").strip()
    artist = str(context.get("artist") or "").strip()
    if '《' in title and '》' in title:
        return True
    if filename_candidates:
        top = filename_candidates[0]
        if top.get('artist') and top.get('artist') != artist:
            return True
        if top.get('title') and top.get('title') != title:
            return True
    return False


def is_mixed_album_art_folder(folder_path: Path) -> bool:
    folder_str = str(folder_path.resolve() if folder_path.exists() else folder_path).strip()
    return folder_str in MIXED_FOLDER_EXACT_PATHS


def album_art_target_path(folder_path: Path) -> Path:
    return folder_path / "cover.jpg"


def has_album_art_file(folder_path: Path) -> bool:
    candidates = [
        folder_path / "cover.jpg",
        folder_path / "cover.png",
        folder_path / "Cover.jpg",
        folder_path / "Cover.png",
        folder_path / "folder.jpg",
        folder_path / "folder.png",
        folder_path / "Folder.jpg",
        folder_path / "Folder.png",
    ]
    return any(p.exists() and p.is_file() for p in candidates)


def has_album_art(audio_path: Path, folder_path: Optional[Path] = None, include_embedded: bool = True) -> bool:
    if folder_path and has_album_art_file(folder_path):
        return True
    if not include_embedded:
        return False
    flags = read_audio_metadata_flags(str(audio_path.resolve()))
    return bool(flags.get("has_embedded_cover"))


def collect_missing_lyrics_rows(music_root: Path, extensions: set[str], skip_dirs: List[str], limit: int = 200, include_embedded: bool = True) -> dict:
    missing_rows = []
    total_missing = 0
    for current_root, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        matched = sorted([current / name for name in filenames if (current / name).suffix.lower() in extensions])
        if not matched:
            continue
        artist_name = current.name
        for audio_path in matched:
            if has_any_lyric(audio_path, include_embedded=include_embedded):
                continue
            total_missing += 1
            stem_name = audio_path.stem
            search_hint = f"{current.name} {stem_name} 歌词"
            if len(missing_rows) < limit:
                missing_rows.append({
                    "artist": artist_name,
                    "title": stem_name,
                    "filename": audio_path.name,
                    "source_file": str(audio_path),
                    "folder_path": str(current),
                    "search_hint": search_hint,
                    "target_mode": "embedded-lyrics-preferred",
                })
    return {
        "missing_lyrics_count": total_missing,
        "missing_lyrics_rows": missing_rows,
    }


def collect_missing_album_art_rows(music_root: Path, extensions: set[str], skip_dirs: List[str], limit: int = 200, include_embedded: bool = True) -> dict:
    missing_rows = []
    missing_song_rows = []
    mtw_available_song_rows = []
    total_missing = 0
    total_missing_songs = 0
    total_mtw_available_songs = 0
    mtw_cover_lookup = load_music_tag_web_cover_lookup()
    for current_root, dirnames, filenames in os.walk(music_root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        matched = sorted([current / name for name in filenames if (current / name).suffix.lower() in extensions])
        if not matched:
            continue

        if has_album_art_file(current):
            continue

        target_file = album_art_target_path(current)
        explicit_mixed_folder = is_mixed_album_art_folder(current)
        mtw_cover_item = mtw_cover_lookup.get(str(current.resolve())) or mtw_cover_lookup.get(str(current))
        mtw_is_mixed_folder = explicit_mixed_folder or bool((mtw_cover_item or {}).get("mtw_folder_is_mixed"))
        mtw_has_cover = bool(mtw_cover_item) and not mtw_is_mixed_folder

        first_embedded_audio = None
        embedded_cover_count = 0
        for audio_path in matched:
            song_embedded_cover = False
            if include_embedded:
                flags = read_audio_metadata_flags(str(audio_path.resolve()))
                song_embedded_cover = bool(flags.get("has_embedded_cover"))
                if song_embedded_cover:
                    embedded_cover_count += 1
                    if first_embedded_audio is None:
                        first_embedded_audio = audio_path

            base_row = {
                "artist": current.name,
                "title": audio_path.stem,
                "filename": audio_path.name,
                "source_file": str(audio_path),
                "folder_path": str(current),
                "target_file": str(target_file),
                "has_embedded_cover": song_embedded_cover,
            }
            if mtw_has_cover:
                total_mtw_available_songs += 1
                if len(mtw_available_song_rows) < max(limit * 5, 500):
                    mtw_available_song_rows.append({
                        **base_row,
                        "mtw_attachment_source": str(mtw_cover_item.get("attachment_source") or ""),
                        "mtw_album_name": str(mtw_cover_item.get("album_name") or ""),
                    })
            else:
                total_missing_songs += 1
                if len(missing_song_rows) < max(limit * 5, 500):
                    missing_song_rows.append({
                        **base_row,
                        "mixed_folder": mtw_is_mixed_folder,
                    })

        total_missing += 1
        if len(missing_rows) < limit:
            source_audio = first_embedded_audio or matched[0]
            mixed_folder = explicit_mixed_folder or mtw_is_mixed_folder or (len(matched) > 1 and embedded_cover_count not in (0, len(matched)))
            if mixed_folder:
                navidrome_cover_status = "mixed-folder-skip"
            elif embedded_cover_count > 0:
                navidrome_cover_status = "embedded-available"
            elif mtw_has_cover:
                navidrome_cover_status = "mtw-available"
            else:
                navidrome_cover_status = "needs-online-scrape"
            missing_rows.append({
                "artist": current.name,
                "source_file": str(source_audio),
                "folder_path": str(current),
                "target_file": str(target_file),
                "has_embedded_cover": embedded_cover_count > 0,
                "has_mtw_cover": mtw_has_cover,
                "navidrome_cover_status": navidrome_cover_status,
                "mtw_attachment_source": str((mtw_cover_item or {}).get("attachment_source") or ""),
                "mtw_album_name": str((mtw_cover_item or {}).get("album_name") or ""),
                "mixed_folder": mixed_folder,
                "embedded_cover_count": embedded_cover_count,
                "audio_file_count": len(matched),
                "mtw_folder_candidate_count": int((mtw_cover_item or {}).get("mtw_folder_candidate_count") or 0),
                "mtw_folder_album_id_count": int((mtw_cover_item or {}).get("mtw_folder_album_id_count") or 0),
                "mtw_folder_album_name_count": int((mtw_cover_item or {}).get("mtw_folder_album_name_count") or 0),
                "mtw_folder_album_names": list((mtw_cover_item or {}).get("mtw_folder_album_names") or []),
            })

    return {
        "missing_album_art_count": total_missing,
        "missing_album_art_rows": missing_rows,
        "missing_album_art_song_count": total_missing_songs,
        "missing_album_art_song_rows": missing_song_rows,
        "mtw_album_art_song_count": total_mtw_available_songs,
        "mtw_album_art_song_rows": mtw_available_song_rows,
    }


def collect_music_tag_web_cover_candidates(limit: int = 200, include_existing: bool = True) -> dict:
    rows = []
    if not MUSIC_TAG_WEB_DB.exists():
        return {"count": 0, "rows": rows, "source": "db-missing"}

    try:
        conn = sqlite3.connect(MUSIC_TAG_WEB_DB)
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT a.id, a.name, at.file, t.path
            FROM music_album a
            LEFT JOIN music_attachment at ON at.id = a.attachment_cover_id
            LEFT JOIN music_track t ON t.album_id = a.id
            WHERE a.attachment_cover_id IS NOT NULL
              AND at.file IS NOT NULL
              AND t.path IS NOT NULL
            ORDER BY a.id ASC
            '''
        )
        seen_targets = set()
        seen_album_ids = set()
        for album_id, album_name, attachment_rel, track_path in cur.fetchall():
            if album_id in seen_album_ids:
                continue
            track_path = str(track_path)
            if not track_path.startswith('/app/media/'):
                continue
            relative_track = track_path.removeprefix('/app/media/').lstrip('/')
            host_track = (MUSIC_TAG_WEB_MEDIA_ROOT / relative_track).resolve()
            folder_path = host_track.parent
            target_file = album_art_target_path(folder_path).resolve()
            attachment_host = (MUSIC_TAG_WEB_MEDIA_ROOT / attachment_rel).resolve()
            if not attachment_host.exists():
                continue
            if not include_existing and target_file.exists():
                continue
            if str(target_file) in seen_targets:
                continue
            seen_targets.add(str(target_file))
            seen_album_ids.add(album_id)
            rows.append({
                "album_id": album_id,
                "album_name": album_name,
                "track_path": str(host_track),
                "folder_path": str(folder_path),
                "attachment_source": str(attachment_host),
                "target_file": str(target_file),
                "target_exists": target_file.exists(),
            })
            if len(rows) >= limit:
                break
        conn.close()
    except Exception as exc:
        return {"count": 0, "rows": rows, "source": f"error:{exc}"}

    return {"count": len(rows), "rows": rows, "source": "music-tag-web-db"}


def load_music_tag_web_cover_lookup() -> dict[str, dict]:
    plan = collect_music_tag_web_cover_candidates(limit=100000, include_existing=True)
    lookup: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = {}
    for item in plan.get("rows", []):
        folder_path = str(item.get("folder_path") or "").strip()
        if not folder_path:
            continue
        grouped.setdefault(folder_path, []).append(item)

    for folder_path, items in grouped.items():
        album_ids = {str(item.get("album_id") or "").strip() for item in items if str(item.get("album_id") or "").strip()}
        album_names = {str(item.get("album_name") or "").strip() for item in items if str(item.get("album_name") or "").strip()}
        mixed = len(items) > 1 and (len(album_ids) > 1 or len(album_names) > 1)
        primary = dict(items[0])
        primary["mtw_folder_candidate_count"] = len(items)
        primary["mtw_folder_album_id_count"] = len(album_ids)
        primary["mtw_folder_album_name_count"] = len(album_names)
        primary["mtw_folder_is_mixed"] = mixed
        primary["mtw_folder_album_names"] = sorted(album_names)
        lookup[folder_path] = primary
    return lookup


def write_music_tag_web_album_art(overwrite: bool = False) -> dict:
    plan = collect_music_tag_web_cover_candidates(limit=100000, include_existing=True)
    rows = plan.get("rows", [])
    result = {
        "ok": True,
        "source": plan.get("source", ""),
        "planned": len(rows),
        "written": 0,
        "skipped_existing": 0,
        "failed": 0,
    }
    append_job_log(f"[INFO] music-tag-web cover plan source: {result['source']}")
    append_job_log(f"[INFO] planned cover targets: {result['planned']}")

    for item in rows:
        source = Path(item["attachment_source"])
        target = Path(item["target_file"])
        album_name = item.get("album_name") or target.parent.name
        if target.exists() and not overwrite:
            result["skipped_existing"] += 1
            append_job_log(f"[SKIP] {album_name} -> {target} (cover.jpg already exists)")
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                "ffmpeg", "-y",
                "-i", str(source),
                "-an",
                "-q:v", "2",
                str(target),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout.strip() or f"ffmpeg exit {proc.returncode}")
            result["written"] += 1
            append_job_log(f"[WRITE] {album_name} -> {target}")
        except Exception as exc:
            result["failed"] += 1
            append_job_log(f"[FAIL] {album_name} -> {target} :: {exc}")

    append_job_log(
        f"[DONE] written={result['written']} skipped_existing={result['skipped_existing']} failed={result['failed']}"
    )
    result["ok"] = result["failed"] == 0
    return result


def write_lyric_to_audio_file(audio_path: Path, lyrics: str, overwrite: bool = False) -> tuple[bool, str]:
    suffix = audio_path.suffix.lower()
    if suffix == '.mp3':
        try:
            try:
                tags = ID3(str(audio_path))
            except ID3NoHeaderError:
                tags = ID3()
            if not overwrite:
                for key in tags.keys():
                    if str(key).startswith('USLT') and str(tags.get(key)).strip():
                        return False, 'embedded lyrics already exist'
            for key in list(tags.keys()):
                if str(key).startswith('USLT'):
                    del tags[key]
            tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics))
            tags.save(str(audio_path))
            return True, 'embedded mp3 lyrics written'
        except Exception as exc:
            return False, str(exc)

    if suffix == '.flac':
        try:
            audio = FLAC(str(audio_path))
            if not overwrite and audio.get('LYRICS'):
                existing = ' '.join(audio.get('LYRICS', [])).strip()
                if existing:
                    return False, 'embedded lyrics already exist'
            audio['LYRICS'] = [lyrics]
            audio.save()
            return True, 'embedded flac lyrics written'
        except Exception as exc:
            return False, str(exc)

    if suffix in {'.m4a', '.mp4', '.aac'}:
        try:
            audio = MP4(str(audio_path))
            if not overwrite and audio.get('©lyr'):
                existing = ' '.join(str(x) for x in audio.get('©lyr', [])).strip()
                if existing:
                    return False, 'embedded mp4 lyrics already exist'
            audio['©lyr'] = [lyrics]
            audio.save()
            return True, 'embedded mp4 lyrics written'
        except Exception as exc:
            return False, str(exc)

    return False, f'unsupported embedded lyric format: {suffix}'


def write_music_tag_web_lyrics(overwrite: bool = False) -> dict:
    lyrics_map = load_music_tag_web_lyrics_map()
    result = {
        'ok': True,
        'planned': len(lyrics_map),
        'written_embedded': 0,
        'written_sidecar': 0,
        'skipped_existing': 0,
        'failed': 0,
    }
    append_job_log(f"[INFO] music-tag-web lyric tracks: {result['planned']}")

    for track_path, lyrics in lyrics_map.items():
        if not track_path.startswith('/app/media/'):
            continue
        relative_track = track_path.removeprefix('/app/media/').lstrip('/')
        audio_path = (MUSIC_TAG_WEB_MEDIA_ROOT / relative_track).resolve()
        if not audio_path.exists():
            result['failed'] += 1
            append_job_log(f"[FAIL] missing audio file :: {audio_path}")
            continue

        ok, reason = write_lyric_to_audio_file(audio_path, lyrics, overwrite=overwrite)
        if ok:
            clear_audio_metadata_flags_cache()
            result['written_embedded'] += 1
            append_job_log(f"[WRITE] embedded lyrics -> {audio_path}")
            continue
        if 'already exist' in reason and not overwrite:
            result['skipped_existing'] += 1
            append_job_log(f"[SKIP] {audio_path} ({reason})")
            continue

        try:
            sidecar = audio_path.with_suffix('.lrc')
            if sidecar.exists() and not overwrite:
                result['skipped_existing'] += 1
                append_job_log(f"[SKIP] {sidecar} (sidecar lyric already exists)")
                continue
            sidecar.write_text(lyrics, encoding='utf-8')
            clear_audio_metadata_flags_cache()
            result['written_sidecar'] += 1
            append_job_log(f"[WRITE] sidecar lyric -> {sidecar}")
        except Exception as exc:
            result['failed'] += 1
            append_job_log(f"[FAIL] lyric write -> {audio_path} :: {reason}; sidecar fallback failed: {exc}")

    append_job_log(
        f"[DONE] embedded={result['written_embedded']} sidecar={result['written_sidecar']} skipped_existing={result['skipped_existing']} failed={result['failed']}"
    )
    result['ok'] = result['failed'] == 0
    return result


def write_online_lyrics(overwrite: bool = False, limit: int = 200, only_audio_path: Optional[str] = None, search_keyword: Optional[str] = None) -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))
    result = {
        'ok': True,
        'planned': 0,
        'written_embedded': 0,
        'written_sidecar': 0,
        'skipped_existing': 0,
        'failed': 0,
        'fetched': 0,
    }
    if only_audio_path:
        rows = [{"source_file": only_audio_path}]
    else:
        scan = collect_missing_lyrics_rows(music_root, extensions, skip_dirs, limit=limit, include_embedded=True)
        rows = scan.get('missing_lyrics_rows', [])
    result['planned'] = len(rows)
    append_job_log(f"[INFO] online lyric targets: {result['planned']}")

    for item in rows:
        actual_audio_path = Path(item['source_file'])
        lookup_audio_path = actual_audio_path
        if search_keyword:
            try:
                lookup_audio_path = actual_audio_path.with_name(f"{search_keyword}{actual_audio_path.suffix or '.mp3'}")
            except Exception:
                lookup_audio_path = actual_audio_path
        fetched = search_multisource_lyrics(str(lookup_audio_path.resolve()))
        if not fetched.get('ok'):
            result['failed'] += 1
            candidate_rows = build_lyric_candidates(lookup_audio_path)
            if candidate_rows:
                result.setdefault('manual_candidates', {})[str(actual_audio_path)] = candidate_rows
                append_job_log(f"[INFO] lyric candidates available -> {actual_audio_path} :: {len(candidate_rows)}")
            append_job_log(f"[FAIL] lyric fetch -> {actual_audio_path} :: {fetched.get('error')}")
            continue
        result['fetched'] += 1
        lyrics = str(fetched.get('lyrics') or '').strip()
        if not lyrics:
            result['failed'] += 1
            append_job_log(f"[FAIL] empty lyric payload -> {actual_audio_path}")
            continue
        ok, reason = write_lyric_to_audio_file(actual_audio_path, lyrics, overwrite=overwrite)
        if ok:
            clear_audio_metadata_flags_cache()
            result['written_embedded'] += 1
            remove_song_from_lyrics_scan_cache(actual_audio_path)
            append_job_log(f"[WRITE] online embedded lyrics -> {actual_audio_path}")
            continue
        if 'already exist' in reason and not overwrite:
            result['skipped_existing'] += 1
            remove_song_from_lyrics_scan_cache(actual_audio_path)
            append_job_log(f"[SKIP] {actual_audio_path} ({reason})")
            continue
        try:
            sidecar = actual_audio_path.with_suffix('.lrc')
            if sidecar.exists() and not overwrite:
                result['skipped_existing'] += 1
                remove_song_from_lyrics_scan_cache(actual_audio_path)
                append_job_log(f"[SKIP] {sidecar} (sidecar lyric already exists)")
                continue
            sidecar.write_text(lyrics, encoding='utf-8')
            clear_audio_metadata_flags_cache()
            result['written_sidecar'] += 1
            remove_song_from_lyrics_scan_cache(actual_audio_path)
            append_job_log(f"[WRITE] online sidecar lyric -> {sidecar}")
        except Exception as exc:
            result['failed'] += 1
            append_job_log(f"[FAIL] online lyric write -> {audio_path} :: {reason}; sidecar fallback failed: {exc}")

    append_job_log(
        f"[DONE] fetched={result['fetched']} embedded={result['written_embedded']} sidecar={result['written_sidecar']} skipped_existing={result['skipped_existing']} failed={result['failed']}"
    )
    result['ok'] = result['failed'] == 0
    return result


def write_online_album_art(overwrite: bool = False, limit: int = 120, only_audio_path: Optional[str] = None, only_folder_path: Optional[str] = None) -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))
    result = {
        'ok': True,
        'planned': 0,
        'written': 0,
        'skipped_existing': 0,
        'failed': 0,
        'fetched': 0,
    }
    if only_audio_path and only_folder_path:
        rows = [{
            'source_file': only_audio_path,
            'folder_path': only_folder_path,
        }]
    else:
        scan = collect_missing_album_art_rows(music_root, extensions, skip_dirs, limit=limit, include_embedded=True)
        rows = scan.get('missing_album_art_rows', [])
    result['planned'] = len(rows)
    append_job_log(f"[INFO] online album art targets: {result['planned']}")

    for item in rows:
        audio_path = Path(item['source_file'])
        folder_path = Path(item['folder_path'])
        target = album_art_target_path(folder_path)
        if is_mixed_album_art_folder(folder_path):
            result['failed'] += 1
            append_job_log(f"[SKIP] {target} (mixed folder is excluded from directory-level album art auto-write)")
            continue
        if target.exists() and not overwrite:
            result['skipped_existing'] += 1
            append_job_log(f"[SKIP] {target} (cover.jpg already exists)")
            continue
        fetched = search_itunes_album_art(str(audio_path.resolve()))
        if not fetched.get('ok'):
            result['failed'] += 1
            append_job_log(f"[FAIL] album art fetch -> {folder_path} :: {fetched.get('error')}")
            continue
        result['fetched'] += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp.jpg')
            tmp.write_bytes(fetched['image_bytes'])
            cmd = ['ffmpeg', '-y', '-i', str(tmp), '-an', '-q:v', '2', str(target)]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tmp.unlink(missing_ok=True)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout.strip() or f'ffmpeg exit {proc.returncode}')
            result['written'] += 1
            append_job_log(f"[WRITE] online album art -> {target}")
        except Exception as exc:
            result['failed'] += 1
            append_job_log(f"[FAIL] album art write -> {target} :: {exc}")

    append_job_log(
        f"[DONE] fetched={result['fetched']} written={result['written']} skipped_existing={result['skipped_existing']} failed={result['failed']}"
    )
    result['ok'] = result['failed'] == 0
    return result


def get_report() -> dict:
    if REPORT_PATH.exists():
        try:
            return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def build_report_state() -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    cache_dir = resolve_cache_dir(config)
    export_dir = resolve_export_dir(config)
    report = get_report()
    report_artists = report.get("artists", {}) or {}
    resolver = ArtistImageResolver(config=config, cache_dir=cache_dir)

    artist_rows = []
    success_folders = []
    missing_folders = []
    failed_folders = []
    export_rows = []
    success_artist_count = 0
    failed_artist_count = 0

    for artist, report_item in sorted(report_artists.items()):
        folders = report_item.get("folders", []) or []
        written = set(report_item.get("written", []) or [])
        skipped = set(report_item.get("skipped", []) or [])
        cache_file = resolver._find_cached(artist, sha1_text(artist))
        cache_preview = str(cache_file) if cache_file else None
        exported_files = [str(Path(p).resolve()) for p in (report_item.get("navidrome_exported", []) or [])]
        folder_rows = []
        preview = cache_preview

        for folder_path in folders:
            existing_image = None
            for ext in IMAGE_EXTENSIONS:
                candidate = Path(folder_path) / f"artist{ext}"
                if candidate.exists():
                    existing_image = candidate
                    break
            target_path = str((Path(folder_path) / f"artist{report_item.get('ext', '.jpg')}").resolve())
            status = "success" if existing_image else ("failed" if report_item.get("ok") is False else "missing")
            if target_path in written or target_path in skipped:
                status = "success" if report_item.get("ok") else status
            row = {
                "path": str(folder_path),
                "source_file": "",
                "has_artist_image": bool(existing_image),
                "existing_image": str(existing_image) if existing_image else None,
                "status": status,
            }
            if existing_image and not preview:
                preview = str(existing_image)
            if status == "success":
                success_folders.append({"artist": artist, **row})
            elif status == "failed":
                failed_folders.append({"artist": artist, **row, "error": report_item.get("error", "")})
            else:
                missing_folders.append({"artist": artist, **row})
            folder_rows.append(row)

        status = "success" if (any(r["has_artist_image"] for r in folder_rows) or exported_files) else ("failed" if report_item.get("ok") is False else "missing")
        if status == "success":
            success_artist_count += 1
        elif status == "failed":
            failed_artist_count += 1
        artist_rows.append({
            "artist": artist,
            "status": status,
            "folder_count": len(folders),
            "folders": folder_rows,
            "report": report_item,
            "preview": preview,
            "cache_preview": cache_preview,
        })
        export_rows.append({
            "artist": artist,
            "aliases": report_item.get("navidrome_aliases", []) or [],
            "files": exported_files,
            "count": len(exported_files),
            "preview": exported_files[0] if exported_files else cache_preview,
        })

    stats = {
        "artists": report.get("stats", {}).get("artists", len(report_artists)),
        "success_artists": success_artist_count,
        "failed_artists": failed_artist_count,
        "last_run_success_artists": report.get("stats", {}).get("resolved", 0),
        "report_written": report.get("stats", {}).get("written", 0),
        "report_failed_artists": report.get("stats", {}).get("failed", 0),
        "report_skipped_existing": report.get("stats", {}).get("skipped_existing", 0),
        "navidrome_exported_files": sum(item["count"] for item in export_rows),
    }

    return {
        "config_path": str(CONFIG_PATH),
        "report_path": str(REPORT_PATH),
        "music_root": str(music_root),
        "cache_dir": str(cache_dir),
        "navidrome_export_dir": str(export_dir) if export_dir else "",
        "stats": stats,
        "artists": artist_rows,
        "navidrome_exports": export_rows,
        "success_folders": success_folders,
        "missing_folders": missing_folders,
        "failed_folders": failed_folders,
        "report": report,
        "job": dict(job_state),
        "data_mode": "report",
    }


def load_scan_cache(path: Path) -> dict:
    data = load_json_file(path, default={})
    return data if isinstance(data, dict) else {}


def save_scan_cache(path: Path, payload: dict) -> None:
    write_json_file(path, payload)


def is_scan_cache_stale(data: Optional[dict], ttl_seconds: int = 300) -> bool:
    if not isinstance(data, dict) or not data:
        return True
    updated_at = str(data.get("updated_at") or "").strip()
    if not updated_at:
        return True
    try:
        updated = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return True
    return (datetime.now() - updated).total_seconds() > ttl_seconds


def remove_song_from_lyrics_scan_cache(audio_path: Path) -> None:
    cache = load_scan_cache(LYRICS_SCAN_CACHE_PATH)
    if not cache:
        return
    rows = list(cache.get("missing_lyrics_rows") or [])
    source_file = str(audio_path)
    filtered_rows = [row for row in rows if str(row.get("source_file") or "") != source_file]
    removed = len(rows) - len(filtered_rows)
    if removed <= 0:
        attempts = dict(cache.get("auto_lyrics_attempts") or {})
        if source_file in attempts:
            attempts.pop(source_file, None)
            cache["auto_lyrics_attempts"] = attempts
            cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_scan_cache(LYRICS_SCAN_CACHE_PATH, cache)
        return
    cache["missing_lyrics_rows"] = filtered_rows
    cache["missing_lyrics_count"] = max(0, int(cache.get("missing_lyrics_count") or 0) - removed)
    attempts = dict(cache.get("auto_lyrics_attempts") or {})
    if source_file in attempts:
        attempts.pop(source_file, None)
        cache["auto_lyrics_attempts"] = attempts
    cache["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_scan_cache(LYRICS_SCAN_CACHE_PATH, cache)


def try_auto_scrape_lyric_once(audio_path: Path, overwrite: bool = False) -> dict:
    fetched = search_multisource_lyrics(str(audio_path.resolve()))
    if not fetched.get("ok"):
        return {
            "ok": False,
            "error": str(fetched.get("error") or "lyric-fetch-failed"),
            "source": str(fetched.get("source") or ""),
        }

    lyrics = str(fetched.get("lyrics") or "").strip()
    if not lyrics:
        return {"ok": False, "error": "empty-lyrics-payload", "source": str(fetched.get("source") or "")}

    ok, reason = write_lyric_to_audio_file(audio_path, lyrics, overwrite=overwrite)
    if ok:
        clear_audio_metadata_flags_cache()
        return {"ok": True, "write_mode": "embedded", "source": str(fetched.get("source") or "")}

    if 'already exist' in reason and not overwrite:
        clear_audio_metadata_flags_cache()
        return {"ok": True, "write_mode": "already-exists", "source": str(fetched.get("source") or "")}

    try:
        sidecar = audio_path.with_suffix('.lrc')
        if sidecar.exists() and not overwrite:
            clear_audio_metadata_flags_cache()
            return {"ok": True, "write_mode": "sidecar-already-exists", "source": str(fetched.get("source") or "")}
        sidecar.write_text(lyrics, encoding='utf-8')
        clear_audio_metadata_flags_cache()
        return {"ok": True, "write_mode": "sidecar", "source": str(fetched.get("source") or "")}
    except Exception as exc:
        return {"ok": False, "error": f"{reason}; sidecar fallback failed: {exc}", "source": str(fetched.get("source") or "")}


def run_lyrics_scan_job() -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))
    append_job_log("[INFO] starting lyrics scan job")
    scan = collect_missing_lyrics_rows(music_root, extensions, skip_dirs, limit=1000, include_embedded=True)
    previous_cache = load_scan_cache(LYRICS_SCAN_CACHE_PATH)
    auto_attempts = dict(previous_cache.get("auto_lyrics_attempts") or {})
    failed_rows = []
    auto_attempted_now = 0
    auto_fixed_now = 0
    auto_failed_now = 0

    for row in scan.get("missing_lyrics_rows", []) or []:
        source_file = str(row.get("source_file") or "")
        if not source_file:
            continue
        audio_path = Path(source_file)
        attempt_info = auto_attempts.get(source_file)

        if attempt_info:
            failed_rows.append({
                **row,
                "auto_attempted": True,
                "auto_attempt_status": str(attempt_info.get("status") or "failed"),
                "auto_attempted_at": str(attempt_info.get("attempted_at") or ""),
                "auto_attempt_error": str(attempt_info.get("error") or ""),
            })
            continue

        auto_attempted_now += 1
        append_job_log(f"[INFO] auto lyric scrape -> {audio_path}")
        result = try_auto_scrape_lyric_once(audio_path)
        if result.get("ok"):
            auto_fixed_now += 1
            append_job_log(f"[WRITE] auto lyric success -> {audio_path} :: {result.get('write_mode')}")
            continue

        auto_failed_now += 1
        error_text = str(result.get("error") or "lyric-auto-scrape-failed")
        attempted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        auto_attempts[source_file] = {
            "status": "failed",
            "attempted_at": attempted_at,
            "error": error_text,
        }
        append_job_log(f"[FAIL] auto lyric scrape -> {audio_path} :: {error_text}")
        failed_rows.append({
            **row,
            "auto_attempted": True,
            "auto_attempt_status": "failed",
            "auto_attempted_at": attempted_at,
            "auto_attempt_error": error_text,
        })

    stale_attempts = {str(row.get("source_file") or "") for row in failed_rows if str(row.get("source_file") or "")}
    auto_attempts = {k: v for k, v in auto_attempts.items() if k in stale_attempts}

    payload = {
        "ok": True,
        "scan_type": "lyrics",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "missing_lyrics_count": len(failed_rows),
        "missing_lyrics_rows": failed_rows,
        "auto_lyrics_attempts": auto_attempts,
        "auto_lyrics_attempted_now": auto_attempted_now,
        "auto_lyrics_fixed_now": auto_fixed_now,
        "auto_lyrics_failed_now": auto_failed_now,
        "raw_missing_lyrics_count": scan.get("missing_lyrics_count", 0),
    }
    save_scan_cache(LYRICS_SCAN_CACHE_PATH, payload)
    append_job_log(
        f"[DONE] lyrics scan raw_missing={payload['raw_missing_lyrics_count']} auto_attempted={auto_attempted_now} auto_fixed={auto_fixed_now} failed_list={payload['missing_lyrics_count']}"
    )
    return payload


def run_album_art_scan_job() -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))
    append_job_log("[INFO] starting album art scan job")
    scan = collect_missing_album_art_rows(music_root, extensions, skip_dirs, limit=200, include_embedded=True)
    payload = {
        "ok": True,
        "scan_type": "album-art",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "missing_album_art_count": scan.get("missing_album_art_count", 0),
        "missing_album_art_rows": scan.get("missing_album_art_rows", []),
        "missing_album_art_song_count": scan.get("missing_album_art_song_count", 0),
        "missing_album_art_song_rows": scan.get("missing_album_art_song_rows", []),
        "mtw_album_art_song_count": scan.get("mtw_album_art_song_count", 0),
        "mtw_album_art_song_rows": scan.get("mtw_album_art_song_rows", []),
    }
    save_scan_cache(ALBUM_ART_SCAN_CACHE_PATH, payload)
    append_job_log(f"[DONE] album art scan missing={payload['missing_album_art_count']}")
    return payload


def build_library_state(scan_live: bool = False, lyrics_scan_only: bool = False) -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    cache_dir = resolve_cache_dir(config)
    report = get_report()
    report_artists = report.get("artists", {}) or {}
    resolver = ArtistImageResolver(config=config, cache_dir=cache_dir)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))

    if not scan_live:
        artist_rows = []
        success_folders = []
        missing_folders = []
        failed_folders = []
        album_rows = []
        success_artist_count = 0
        failed_artist_count = 0

        for artist, report_item in sorted(report_artists.items()):
            folders = report_item.get("folders", []) or []
            cache_file = resolver._find_cached(artist, sha1_text(artist))
            cache_preview = str(cache_file) if cache_file else None
            preview = cache_preview
            folder_rows = []

            for folder_path in folders:
                existing_image = None
                for ext in IMAGE_EXTENSIONS:
                    candidate = Path(folder_path) / f"artist{ext}"
                    if candidate.exists():
                        existing_image = candidate
                        break
                status = "success" if existing_image else ("failed" if report_item.get("ok") is False else "missing")
                row = {
                    "path": str(folder_path),
                    "source_file": "",
                    "has_artist_image": bool(existing_image),
                    "existing_image": str(existing_image) if existing_image else None,
                    "status": status,
                }
                if existing_image and not preview:
                    preview = str(existing_image)
                if status == "success":
                    success_folders.append({"artist": artist, **row})
                elif status == "failed":
                    failed_folders.append({"artist": artist, **row, "error": report_item.get("error", "")})
                else:
                    missing_folders.append({"artist": artist, **row})
                folder_rows.append(row)
                album_rows.append({
                    "artist": artist,
                    "path": str(folder_path),
                    "source_file": "",
                    "status": status,
                    "mode": "report-snapshot",
                    "has_artist_image": bool(existing_image),
                })

            status = "success" if any(r["has_artist_image"] for r in folder_rows) else ("failed" if report_item.get("ok") is False else "missing")
            if status == "success":
                success_artist_count += 1
            elif status == "failed":
                failed_artist_count += 1
            artist_rows.append({
                "artist": artist,
                "status": status,
                "folder_count": len(folders),
                "folders": folder_rows,
                "report": report_item,
                "preview": preview,
                "cache_preview": cache_preview,
            })

        file_stats = collect_library_file_stats(music_root, extensions, skip_dirs)
        preview_rows = collect_library_preview_rows(music_root, extensions, skip_dirs, limit=120)
        lyric_scan = load_scan_cache(LYRICS_SCAN_CACHE_PATH)
        if not lyric_scan:
            lyric_scan = run_lyrics_scan_job()
        album_art_scan = load_scan_cache(ALBUM_ART_SCAN_CACHE_PATH)
        music_tag_cover_plan = collect_music_tag_web_cover_candidates(limit=200)
        lyric_scan_mode = True

        stats = {
            "artists": report.get("stats", {}).get("artists", len(report_artists)),
            "songs": file_stats.get("songs", 0),
            "album_dirs": file_stats.get("album_dirs", len(album_rows)),
            "missing_lyrics": lyric_scan.get("missing_lyrics_count", 0),
            "lyrics_with_count": max(file_stats.get("songs", 0) - lyric_scan.get("missing_lyrics_count", 0), 0),
            "missing_album_art": album_art_scan.get("missing_album_art_count", 0),
            "music_tag_cover_candidates": music_tag_cover_plan.get("count", 0),
            "success_artists": success_artist_count,
            "failed_artists": failed_artist_count,
            "missing_artists": max(len(report_artists) - success_artist_count - failed_artist_count, 0),
            "last_run_success_artists": report.get("stats", {}).get("resolved", 0),
            "report_written": report.get("stats", {}).get("written", 0),
            "report_failed_artists": report.get("stats", {}).get("failed", 0),
            "report_skipped_existing": report.get("stats", {}).get("skipped_existing", 0),
            "folder_modes": {"report-snapshot": len(album_rows)},
        }

        roadmap = [
            {
                "title": "歌词刮削",
                "status": "coming-soon",
                "description": "后续这里会加扫描歌曲缺失歌词、批量刮削歌词、查看失败项。",
            },
            {
                "title": "专辑图片刮削",
                "status": "coming-soon",
                "description": "后续这里会加扫描专辑封面缺失情况，并支持批量补齐专辑图片。",
            },
        ]

        future_actions = [
            {
                "title": "扫描歌词刮削失败",
                "status": "ready",
                "description": f"最近一次后台扫描结果显示有 {lyric_scan.get('missing_lyrics_count', 0)} 首自动刮削失败、待手动处理歌曲。",
                "action_url": "/?page=music-library&view=lyrics#missing-lyrics",
                "action_label": "查看刮削失败结果",
            },
            {
                "title": "扫描缺失专辑图片",
                "status": "ready",
                "description": f"最近一次后台扫描结果显示有 {album_art_scan.get('missing_album_art_count', 0)} 个缺少专辑图的目录，目标落盘为目录内 cover.jpg。",
                "action_url": "/?page=music-library&view=album-art#missing-album-art",
                "action_label": "查看专辑图结果",
            },
            {
                "title": "复用 Music Tag Web 专辑图",
                "status": "ready",
                "description": f"已找到 {music_tag_cover_plan.get('count', 0)} 条可复用专辑图映射，可继续写回专辑目录 cover.jpg。",
                "action_url": "/?page=music-library#music-tag-cover-plan",
                "action_label": "查看复用计划",
            },
            {
                "title": "媒体资料完整度检查",
                "status": "planning",
                "description": "后续整合歌手头像、歌词、专辑图、失败重试，做成一键巡检。",
            },
        ]

        return {
            "config_path": str(CONFIG_PATH),
            "report_path": str(REPORT_PATH),
            "music_root": str(music_root),
            "cache_dir": str(cache_dir),
            "stats": stats,
            "artists": artist_rows,
            "success_folders": success_folders,
            "missing_folders": missing_folders,
            "failed_folders": failed_folders,
            "report": report,
            "job": dict(job_state),
            "library_preview": artist_rows[:12],
            "roadmap": roadmap,
            "future_actions": future_actions,
            "song_rows": preview_rows.get("song_rows", []),
            "album_rows": album_rows[:120] or preview_rows.get("album_rows", []),
            "missing_lyrics_rows": lyric_scan.get("missing_lyrics_rows", []),
            "missing_album_art_rows": album_art_scan.get("missing_album_art_rows", []),
            "missing_album_art_song_rows": album_art_scan.get("missing_album_art_song_rows", []),
            "mtw_album_art_song_rows": album_art_scan.get("mtw_album_art_song_rows", []),
            "music_tag_cover_plan_rows": music_tag_cover_plan.get("rows", []),
            "music_tag_cover_plan_source": music_tag_cover_plan.get("source", ""),
            "lyrics_scan_mode": lyric_scan_mode,
            "lyrics_scan_updated_at": lyric_scan.get("updated_at", ""),
            "album_art_scan_updated_at": album_art_scan.get("updated_at", ""),
            "data_mode": "report",
        }

    artist_aliases = build_artist_alias_map(config)
    album_folders: List[AlbumFolder] = collect_album_folders(music_root, extensions, skip_dirs, alias_map=artist_aliases)

    grouped: Dict[str, List[AlbumFolder]] = {}
    for item in album_folders:
        grouped.setdefault(item.artist, []).append(item)

    artist_rows = []
    pending_artist_rows = []
    success_folders = []
    missing_folders = []
    failed_folders = []
    success_artist_count = 0
    failed_artist_count = 0

    source_file_paths = []
    folder_modes = Counter()
    song_rows = []
    album_rows = []

    for artist, folders in sorted(grouped.items()):
        report_item = report_artists.get(artist, {})
        cache_file = resolver._find_cached(artist, sha1_text(artist))
        cache_preview = str(cache_file) if cache_file else None
        preview = None
        folder_rows = []
        has_any_image = False
        for folder in folders:
            source_file_paths.append(str(folder.source_file))
            folder_modes[folder.mode or "unknown"] += 1
            existing_image = None
            for ext in IMAGE_EXTENSIONS:
                candidate = folder.path / f"artist{ext}"
                if candidate.exists():
                    existing_image = candidate
                    break
            row = {
                "path": str(folder.path),
                "source_file": str(folder.source_file),
                "has_artist_image": bool(existing_image),
                "existing_image": str(existing_image) if existing_image else None,
                "status": "success" if existing_image else ("failed" if report_item.get("ok") is False else "missing"),
            }
            song_rows.append({
                "artist": artist,
                "source_file": str(folder.source_file),
                "folder_path": str(folder.path),
                "status": row["status"],
                "has_artist_image": row["has_artist_image"],
            })
            album_rows.append({
                "artist": artist,
                "path": str(folder.path),
                "source_file": str(folder.source_file),
                "status": row["status"],
                "mode": folder.mode,
                "has_artist_image": row["has_artist_image"],
            })
            if existing_image and not preview:
                preview = str(existing_image)
            if existing_image:
                has_any_image = True
                success_folders.append({"artist": artist, **row})
            elif report_item.get("ok") is False:
                failed_folders.append({"artist": artist, **row, "error": report_item.get("error", "")})
            else:
                missing_folders.append({"artist": artist, **row})
            folder_rows.append(row)

        if not preview and cache_preview:
            preview = cache_preview

        status = "success" if has_any_image else ("failed" if report_item.get("ok") is False else "missing")
        if status == "success":
            success_artist_count += 1
        elif status == "failed":
            failed_artist_count += 1
        artist_row = {
            "artist": artist,
            "status": status,
            "folder_count": len(folders),
            "folders": folder_rows,
            "report": report_item,
            "preview": preview,
            "cache_preview": cache_preview,
        }
        artist_rows.append(artist_row)
        if artist not in report_artists:
            pending_artist_rows.append(artist_row)

    unique_song_files = sorted(set(source_file_paths))
    unique_album_dirs = sorted({row["path"] for row in album_rows})
    lyric_scan = load_scan_cache(LYRICS_SCAN_CACHE_PATH)
    album_art_scan = load_scan_cache(ALBUM_ART_SCAN_CACHE_PATH)
    music_tag_cover_plan = collect_music_tag_web_cover_candidates(limit=200)
    lyric_scan_mode = True

    stats = {
        "artists": len(grouped),
        "songs": len(unique_song_files),
        "album_dirs": len(unique_album_dirs),
        "missing_lyrics": lyric_scan.get("missing_lyrics_count", 0),
        "missing_album_art": album_art_scan.get("missing_album_art_count", 0),
        "music_tag_cover_candidates": music_tag_cover_plan.get("count", 0),
        "success_artists": success_artist_count,
        "failed_artists": failed_artist_count,
        "missing_artists": max(len(grouped) - success_artist_count - failed_artist_count, 0),
        "last_run_success_artists": report.get("stats", {}).get("resolved", 0),
        "report_written": report.get("stats", {}).get("written", 0),
        "report_failed_artists": report.get("stats", {}).get("failed", 0),
        "report_skipped_existing": report.get("stats", {}).get("skipped_existing", 0),
        "folder_modes": dict(folder_modes),
    }

    roadmap = [
        {
            "title": "歌词刮削",
            "status": "coming-soon",
            "description": "后续这里会加扫描歌曲缺失歌词、批量刮削歌词、查看失败项。",
        },
        {
            "title": "专辑图片刮削",
            "status": "coming-soon",
            "description": "后续这里会加扫描专辑封面缺失情况，并支持批量补齐专辑图片。",
        },
    ]

    future_actions = [
        {
            "title": "扫描歌词刮削失败",
            "status": "ready",
            "description": f"最近一次后台扫描结果显示有 {lyric_scan.get('missing_lyrics_count', 0)} 首自动刮削失败、待手动处理歌曲。",
            "action_url": "/?page=music-library&view=lyrics#missing-lyrics",
            "action_label": "查看刮削失败结果",
        },
        {
            "title": "扫描缺失专辑图片",
            "status": "ready",
            "description": f"最近一次后台扫描结果显示有 {album_art_scan.get('missing_album_art_count', 0)} 个缺少专辑图的目录，目标落盘为目录内 cover.jpg。",
            "action_url": "/?page=music-library&view=album-art#missing-album-art",
            "action_label": "查看专辑图结果",
        },
        {
            "title": "复用 Music Tag Web 专辑图",
            "status": "ready",
            "description": f"已找到 {music_tag_cover_plan.get('count', 0)} 条可复用专辑图映射，可继续写回专辑目录 cover.jpg。",
            "action_url": "/?page=music-library&scan=1#music-tag-cover-plan",
            "action_label": "查看复用计划",
        },
        {
            "title": "媒体资料完整度检查",
            "status": "planning",
            "description": "后续整合歌手头像、歌词、专辑图、失败重试，做成一键巡检。",
        },
    ]

    return {
        "config_path": str(CONFIG_PATH),
        "report_path": str(REPORT_PATH),
        "music_root": str(music_root),
        "cache_dir": str(cache_dir),
        "stats": stats,
        "artists": artist_rows,
        "pending_artist_rows": pending_artist_rows,
        "success_folders": success_folders,
        "missing_folders": missing_folders,
        "failed_folders": failed_folders,
        "report": report,
        "job": dict(job_state),
        "library_preview": artist_rows[:12],
        "roadmap": roadmap,
        "future_actions": future_actions,
        "song_rows": song_rows[:120],
        "album_rows": album_rows[:120],
        "missing_lyrics_rows": lyric_scan.get("missing_lyrics_rows", []),
        "missing_album_art_rows": album_art_scan.get("missing_album_art_rows", []),
        "missing_album_art_song_rows": album_art_scan.get("missing_album_art_song_rows", []),
        "mtw_album_art_song_rows": album_art_scan.get("mtw_album_art_song_rows", []),
        "music_tag_cover_plan_rows": music_tag_cover_plan.get("rows", []),
        "music_tag_cover_plan_source": music_tag_cover_plan.get("source", ""),
        "lyrics_scan_mode": lyric_scan_mode,
        "lyrics_scan_updated_at": lyric_scan.get("updated_at", ""),
        "album_art_scan_updated_at": album_art_scan.get("updated_at", ""),
        "data_mode": "report",
    }


def allowed_file(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except Exception:
            continue
    return False


def append_job_log(line: str):
    job_state["log"].append(line.rstrip())
    if len(job_state["log"]) > 500:
        job_state["log"] = job_state["log"][-500:]


def save_scan_cache(path: Path, payload: dict) -> None:
    write_json_file(path, payload)


def load_artist_scan_count_cache() -> dict:
    data = load_json_file(ARTIST_SCAN_COUNT_CACHE_PATH, default={})
    return data if isinstance(data, dict) else {}


def get_artist_scan_cache_age_seconds(data: Optional[dict]) -> Optional[int]:
    if not isinstance(data, dict):
        return None
    updated_at = data.get("updated_at")
    if not updated_at:
        return None
    try:
        dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        return max(int(time.time() - dt.timestamp()), 0)
    except Exception:
        return None


def is_artist_scan_cache_stale(data: Optional[dict]) -> bool:
    age = get_artist_scan_cache_age_seconds(data)
    if age is None:
        return True
    return age >= ARTIST_SCAN_COUNT_CACHE_MAX_AGE_SECONDS


def run_artist_scan_count_job() -> dict:
    append_job_log("[INFO] starting artist scan count job")
    result = build_scan_count_state()
    payload = dict(result)
    payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    save_scan_cache(ARTIST_SCAN_COUNT_CACHE_PATH, payload)
    stats = payload.get("stats", {}) or {}
    append_job_log(
        f"[DONE] artists={stats.get('artists', 0)} success={stats.get('success_artists', 0)} failed={stats.get('failed_artists', 0)}"
    )
    return payload


def ensure_home_bootstrap_started() -> bool:
    if home_bootstrap_state.get("running"):
        return False
    home_bootstrap_state.update({
        "running": True,
        "stage": "lyrics-scan",
        "started_at": int(time.time()),
        "finished_at": None,
        "done": False,
        "error": "",
    })

    def worker():
        try:
            deadline = time.time() + 900
            launched_lyrics = False
            launched_scrape = False
            while time.time() < deadline:
                if not launched_lyrics:
                    if not job_state.get("running") and run_job("lyrics-scan"):
                        launched_lyrics = True
                        home_bootstrap_state["stage"] = "lyrics-scan"
                elif launched_lyrics and not launched_scrape:
                    if not job_state.get("running"):
                        if job_state.get("mode") == "lyrics-scan":
                            if run_job("scrape"):
                                launched_scrape = True
                                home_bootstrap_state["stage"] = "scrape"
                        elif job_state.get("mode") is None:
                            if run_job("scrape"):
                                launched_scrape = True
                                home_bootstrap_state["stage"] = "scrape"
                elif launched_scrape:
                    if not job_state.get("running") and job_state.get("mode") == "scrape":
                        home_bootstrap_state["done"] = True
                        break
                time.sleep(0.5)
        except Exception as exc:
            home_bootstrap_state["error"] = str(exc)
        finally:
            home_bootstrap_state["running"] = False
            home_bootstrap_state["finished_at"] = int(time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


def run_job(mode: str, overwrite: bool = False, target: Optional[dict] = None):
    with job_lock:
        if job_state["running"]:
            return False
        auto_configure = mode in {"write", "scrape"}
        job_state.update({
            "running": True,
            "mode": mode,
            "started_at": int(time.time()),
            "finished_at": None,
            "returncode": None,
            "log": [],
            "command": None,
            "auto_configure": auto_configure,
            "target": target,
        })

    def worker():
        try:
            if mode == "artist-scan-count":
                job_state["command"] = ["artist-scan-count"]
                result = run_artist_scan_count_job()
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            if mode == "lyrics-scan":
                job_state["command"] = ["lyrics-scan"]
                result = run_lyrics_scan_job()
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            if mode == "album-art-scan":
                job_state["command"] = ["album-art-scan"]
                result = run_album_art_scan_job()
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            if mode in {"album-art-write", "album-art-write-overwrite"}:
                job_state["command"] = ["music-tag-web-cover-write", f"overwrite={overwrite}"]
                append_job_log("[INFO] starting album art write job from Music Tag Web attachments")
                result = write_music_tag_web_album_art(overwrite=overwrite)
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            if mode in {"lyrics-online-write", "lyrics-online-write-overwrite"}:
                job_state["command"] = ["online-lyrics-write", f"overwrite={overwrite}"]
                append_job_log("[INFO] starting online lyric scrape job")
                only_audio_path = (target or {}).get("audio_path") if target else None
                search_keyword = (target or {}).get("search_keyword") if target else None
                result = write_online_lyrics(overwrite=overwrite, only_audio_path=only_audio_path, search_keyword=search_keyword)
                if isinstance(result, dict):
                    for key, value in result.items():
                        if key not in {"ok", "log"}:
                            job_state[key] = value
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            if mode in {"album-art-online-write", "album-art-online-write-overwrite"}:
                job_state["command"] = ["online-album-art-write", f"overwrite={overwrite}"]
                append_job_log("[INFO] starting online album art scrape job")
                only_audio_path = (target or {}).get("audio_path") if target else None
                only_folder_path = (target or {}).get("folder_path") if target else None
                result = write_online_album_art(overwrite=overwrite, only_audio_path=only_audio_path, only_folder_path=only_folder_path)
                job_state["returncode"] = 0 if result.get("ok") else 1
                return

            actual_mode = "write" if mode in {"scrape", "artist-online-write"} else mode
            args = [sys.executable, str(APP_DIR / "scrape_artist_images.py"), "--config", str(CONFIG_PATH), "--report", str(REPORT_PATH)]
            only_artist = (target or {}).get("artist") if target else None
            if actual_mode == "dry-run":
                args.append("--dry-run")
            if overwrite:
                args.append("--overwrite")
            if only_artist:
                args.extend(["--artist", str(only_artist)])
            job_state["command"] = args
            if auto_configure:
                append_job_log("[INFO] auto-configure: enabled (successful scrape will export to configured Navidrome artist image folder)")
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                append_job_log(line)
            proc.wait()
            job_state["returncode"] = proc.returncode
            if proc.returncode == 0 and auto_configure:
                export_dir = resolve_export_dir(get_config())
                if export_dir:
                    append_job_log(f"[INFO] auto-configure complete: Navidrome export dir updated -> {export_dir}")
                else:
                    append_job_log("[WARN] auto-configure skipped: navidrome_export_dir is not configured")
        except Exception as exc:
            append_job_log(f"[ERROR] {exc}")
            job_state["returncode"] = 1
        finally:
            job_state["running"] = False
            job_state["finished_at"] = int(time.time())

    threading.Thread(target=worker, daemon=True).start()
    return True


def merge_scan_stats_into_report_state(data: dict, scan_stats: Optional[dict]) -> dict:
    if not scan_stats:
        return data
    merged = dict(data)
    merged_stats = dict(merged.get("stats", {}))
    merged_stats.update({
        "artists": scan_stats.get("artists", merged_stats.get("artists", 0)),
        "success_artists": scan_stats.get("success_artists", merged_stats.get("success_artists", 0)),
        "failed_artists": scan_stats.get("failed_artists", merged_stats.get("failed_artists", 0)),
        "last_run_success_artists": scan_stats.get("last_run_success_artists", merged_stats.get("last_run_success_artists", 0)),
    })
    merged["stats"] = merged_stats
    return merged


@app.route("/")
def index():
    page = request.args.get("page", "music-library")
    if page not in {"artist-images", "music-library", "settings"}:
        page = "artist-images"

    current_filter = request.args.get("filter", "all")
    scan = request.args.get("scan")
    scan_error = request.args.get("scan_error", "")

    if page == "settings":
        config = get_config()
        music_root = resolve_music_root(config)
        extensions = set(s.lower() for s in config.get("audio_extensions", []))
        skip_dirs = list(config.get("skip_dirs", []))
        songs = collect_song_detail_rows(music_root, extensions, skip_dirs, limit=300)
        resolved_paths = {
            "config_path": str(CONFIG_PATH),
            "music_root": str(resolve_music_root(config)),
            "cache_dir": str(resolve_cache_dir(config)),
            "navidrome_export_dir": str(resolve_export_dir(config)) if resolve_export_dir(config) else "",
        }
        return render_template(
            "index.html",
            page=page,
            data={"config": config, "resolved_paths": resolved_paths, "songs": songs, "selected_song": songs[0] if songs else None},
            current_filter=current_filter,
            asset_version=ASSET_VERSION,
            page_updated_at=PAGE_UPDATED_AT,
            scan_error=scan_error,
            scan_mode=False,
            lyrics_scan_mode=False,
            album_art_scan_mode=False,
            auto_lyrics_scan_wait=False,
            auto_home_bootstrap_wait=False,
            auto_artist_scrape_wait=False,
        )

    if page == "music-library":
        live_scan = False
        view = request.args.get("view", "home")
        lyrics_scan = view == "lyrics"
        album_art_scan = view == "album-art"
        deep_media_scan = False
        auto_home_bootstrap_wait = False
        home_bootstrap_done = request.args.get("home_bootstrap_done") == "1"
        if not lyrics_scan and not album_art_scan and not home_bootstrap_done:
            auto_home_bootstrap_wait = bool(ensure_home_bootstrap_started() or home_bootstrap_state.get("running") or home_bootstrap_state.get("done"))
        try:
            data = build_library_state(scan_live=live_scan, lyrics_scan_only=deep_media_scan)
        except Exception as exc:
            scan_error = str(exc)
            data = build_library_state(scan_live=False, lyrics_scan_only=False)
        return render_template(
            "index.html",
            page=page,
            data=data,
            current_filter=current_filter,
            asset_version=ASSET_VERSION,
            page_updated_at=PAGE_UPDATED_AT,
            scan_error=scan_error,
            scan_mode=live_scan,
            lyrics_scan_mode=lyrics_scan,
            album_art_scan_mode=album_art_scan,
            auto_lyrics_scan_wait=False,
            auto_home_bootstrap_wait=auto_home_bootstrap_wait,
        )

    # 构建报告数据
    data = build_report_state()

    # 自动触发扫描计数任务（如果缓存过期且任务未运行）
    scan_cache = load_artist_scan_count_cache()
    cache_stale = is_artist_scan_cache_stale(scan_cache)
    data["artist_scan_count_updated_at"] = scan_cache.get("updated_at", "") if scan_cache else ""
    data["artist_scan_count_cache_stale"] = cache_stale
    if cache_stale and not job_state["running"]:
        # 启动后台线程执行扫描任务
        import threading
        def run_scan_bg():
            try:
                result = run_artist_scan_count_job()
                job_state["last_scan_count_result"] = result
                job_state["last_scan_count_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            except Exception as e:
                job_state["last_scan_count_error"] = str(e)
        threading.Thread(target=run_scan_bg, daemon=True).start()

    if scan_cache:
        data = merge_scan_stats_into_report_state(data, scan_cache.get("stats", {}))
    stats = data.get("stats", {})
    raw_report_artists = data.get("artists", {}) or {}
    report_artists = raw_report_artists if isinstance(raw_report_artists, dict) else {}
    resolved_names = {
        artist
        for artist, report_item in report_artists.items()
        if isinstance(report_item, dict) and (report_item.get("ok") is True or report_item.get("navidrome_exported"))
    }
    failed_names = [
        artist
        for artist, report_item in report_artists.items()
        if isinstance(report_item, dict) and report_item.get("ok") is False
    ]
    cached_pending_names = scan_cache.get("pending_artist_names", []) if scan_cache else []
    pending_artist_names = []
    seen_pending_names = set()
    for name in [*cached_pending_names, *failed_names]:
        if name in resolved_names or name in seen_pending_names:
            continue
        seen_pending_names.add(name)
        pending_artist_names.append(name)
    pending_artist_names.sort()
    total_artists = int(stats.get("artists") or 0)
    success_artists = int(stats.get("success_artists") or 0)
    failed_artists = int(stats.get("failed_artists") or 0)
    pending_from_stats = max(total_artists - success_artists - failed_artists, 0)
    data["pending_artists"] = len(pending_artist_names) if pending_artist_names else int(stats.get("pending_artists") or pending_from_stats)
    data["pending_artist_names"] = pending_artist_names
    pending_artist_rows = []
    if data["pending_artist_names"]:
        pending_artist_rows = [
            {"artist": name, "folder_count": None, "from_failed": name in failed_names}
            for name in data["pending_artist_names"]
        ]
    elif failed_names:
        pending_artist_rows = [
            {"artist": name, "folder_count": None, "from_failed": True}
            for name in failed_names
        ]
        data["pending_artist_names"] = failed_names
        data["pending_artists"] = len(failed_names)
    elif int(stats.get("failed_artists") or 0) > 0:
        raw_report = get_report()
        raw_failed_names = []
        raw_artists = raw_report.get("artists") or {}
        if isinstance(raw_artists, dict):
            raw_failed_names = [
                name for name, item in raw_artists.items()
                if isinstance(item, dict) and item.get("ok") is False
            ]
        if raw_failed_names:
            raw_failed_names = sorted(dict.fromkeys(raw_failed_names))
            pending_artist_rows = [
                {"artist": name, "folder_count": None, "from_failed": True}
                for name in raw_failed_names
            ]
            data["pending_artist_names"] = raw_failed_names
            data["pending_artists"] = len(raw_failed_names)
    data["pending_artist_rows"] = pending_artist_rows
    return render_template(
        "index.html",
        page=page,
        data=data,
        current_filter=current_filter,
        asset_version=ASSET_VERSION,
        page_updated_at=PAGE_UPDATED_AT,
        scan_error=scan_error,
        scan_mode=(scan == "1"),
        lyrics_scan_mode=False,
        album_art_scan_mode=False,
        auto_lyrics_scan_wait=False,
        auto_home_bootstrap_wait=False,
        auto_artist_scrape_wait=False,
    )


def build_scan_count_state() -> dict:
    config = get_config()
    music_root = resolve_music_root(config)
    extensions = set(s.lower() for s in config.get("audio_extensions", []))
    skip_dirs = list(config.get("skip_dirs", []))
    artist_aliases = build_artist_alias_map(config)
    album_folders: List[AlbumFolder] = collect_album_folders(music_root, extensions, skip_dirs, alias_map=artist_aliases, config=config)
    report = get_report()
    raw_report_artists = report.get("artists", {}) or {}
    report_artists = raw_report_artists if isinstance(raw_report_artists, dict) else {}

    grouped: Dict[str, List[AlbumFolder]] = {}
    for item in album_folders:
        grouped.setdefault(item.artist, []).append(item)

    success_artist_count = 0
    failed_artist_count = 0
    pending_artist_names: List[str] = []
    for artist, folders in grouped.items():
        report_item = report_artists.get(artist, {})
        exported_files = report_item.get("navidrome_exported", []) or []
        has_any_image = False
        for folder in folders:
            for ext in IMAGE_EXTENSIONS:
                candidate = folder.path / f"artist{ext}"
                if candidate.exists():
                    has_any_image = True
                    break
            if has_any_image:
                break
        status = "success" if (has_any_image or exported_files) else ("failed" if report_item.get("ok") is False else "missing")
        if status == "success":
            success_artist_count += 1
        elif status == "failed":
            failed_artist_count += 1
        else:
            pending_artist_names.append(artist)

    pending_artist_names.sort()
    return {
        "ok": True,
        "stats": {
            "artists": len(grouped),
            "success_artists": success_artist_count,
            "failed_artists": failed_artist_count,
            "pending_artists": len(pending_artist_names),
            "last_run_success_artists": report.get("stats", {}).get("resolved", 0),
        },
        "pending_artist_names": pending_artist_names,
    }


@app.route("/api/state")
def api_state():
    return jsonify(build_library_state())


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    config = get_config()
    if request.method == "GET":
        return jsonify({"ok": True, "config": config})
    
    # POST - 保存配置
    try:
        new_config = request.get_json()
        if not new_config or not isinstance(new_config, dict):
            return jsonify({"ok": False, "error": "invalid config format"}), 400
        
        # 验证必要字段
        if "music_root" not in new_config:
            return jsonify({"ok": False, "error": "music_root is required"}), 400
        
        # 备份原配置
        backup_path = CONFIG_PATH.parent / f"config.backup.{int(time.time())}.json"
        import shutil
        shutil.copy2(CONFIG_PATH, backup_path)
        
        # 保存新配置
        write_json_file(CONFIG_PATH, new_config)
        
        # 重新加载配置
        clear_audio_metadata_flags_cache()
        
        return jsonify({"ok": True, "message": "配置已保存", "backup": str(backup_path.name)})
    except Exception as e:
        return jsonify({"ok": False, "error": f"保存失败: {str(e)}"}), 500


@app.route("/api/song-cover")
def api_song_cover():
    audio_path_raw = str(request.args.get("path") or "").strip()
    if not audio_path_raw:
        abort(400)
    audio_path = Path(audio_path_raw)
    if not audio_path.exists() or not audio_path.is_file():
        abort(404)

    folder = audio_path.parent
    for candidate in [
        folder / "cover.jpg",
        folder / "cover.png",
        folder / "folder.jpg",
        folder / "folder.png",
        folder / "Cover.jpg",
        folder / "Cover.png",
        folder / "Folder.jpg",
        folder / "Folder.png",
    ]:
        if candidate.exists() and candidate.is_file():
            return send_file(candidate)

    try:
        audio = MutagenFile(audio_path)
        if audio is not None:
            pictures = getattr(audio, "pictures", None)
            if pictures:
                pic = pictures[0]
                return send_file(BytesIO(pic.data), mimetype=getattr(pic, "mime", None) or "image/jpeg")
            tags = getattr(audio, "tags", None)
            if tags:
                for key in tags.keys():
                    upper = str(key).upper()
                    if "APIC" in upper:
                        value = tags.get(key)
                        data = getattr(value, "data", None)
                        if data:
                            return send_file(BytesIO(data), mimetype=getattr(value, "mime", None) or "image/jpeg")
                    if "COVR" in upper:
                        value = tags.get(key)
                        if value:
                            blob = value[0]
                            data = bytes(blob)
                            return send_file(BytesIO(data), mimetype="image/jpeg")
    except Exception:
        pass
    abort(404)


def write_selected_lyric_candidate(audio_path_str: str, candidate_token: str, overwrite: bool = False) -> dict:
    audio_path = Path(audio_path_str).resolve()
    payload = lyrics_candidate_store.get(candidate_token)
    if not payload:
        return {"ok": False, "error": "candidate-not-found"}
    if str(audio_path) != str(payload.get("audio_path") or ""):
        return {"ok": False, "error": "candidate-audio-mismatch"}
    lyrics = str(payload.get("lyrics") or "").strip()
    if not lyrics:
        return {"ok": False, "error": "candidate-empty-lyrics"}
    ok, reason = write_lyric_to_audio_file(audio_path, lyrics, overwrite=overwrite)
    if ok:
        clear_audio_metadata_flags_cache()
        remove_song_from_lyrics_scan_cache(audio_path)
        append_job_log(f"[WRITE] selected {payload.get('source')} lyrics -> {audio_path}")
        return {
            "ok": True,
            "written": True,
            "source": payload.get("source"),
            "title": payload.get("title"),
            "artists": payload.get("artists") or [],
            "album": payload.get("album") or "",
        }
    return {"ok": False, "error": reason, "source": payload.get("source")}


@app.route("/api/scan-count")
def api_scan_count():
    data = load_artist_scan_count_cache()
    if not data:
        data = {
            "ok": True,
            "stats": {
                "artists": 0,
                "success_artists": 0,
                "failed_artists": 0,
                "pending_artists": 0,
                "last_run_success_artists": 0,
            },
            "pending_artist_names": [],
            "updated_at": None,
        }
    data["stale"] = is_artist_scan_cache_stale(data)
    data["cache_max_age_seconds"] = ARTIST_SCAN_COUNT_CACHE_MAX_AGE_SECONDS
    data["cache_age_seconds"] = get_artist_scan_cache_age_seconds(data)
    return jsonify(data)


@app.route("/api/job", methods=["GET", "POST"])
def api_job():
    if request.method == "GET":
        return jsonify({**job_state, "home_bootstrap": dict(home_bootstrap_state)})
    payload = request.get_json(silent=True) or request.form or {}
    mode = payload.get("mode", "dry-run")
    overwrite = str(payload.get("overwrite", "false")).lower() in {"1", "true", "yes", "on"}
    raw_target = payload.get("target")
    target = raw_target if isinstance(raw_target, dict) else None
    if target is None and isinstance(raw_target, str):
        try:
            parsed_target = json.loads(raw_target)
            if isinstance(parsed_target, dict):
                target = parsed_target
        except Exception:
            target = None
    if mode == "lyrics-candidate-write":
        target = target or {}
        audio_path = str(target.get("audio_path") or "").strip()
        candidate_token = str(target.get("candidate_token") or "").strip()
        if not audio_path or not candidate_token:
            return jsonify({"ok": False, "error": "missing-audio-or-candidate"}), 400
        result = write_selected_lyric_candidate(audio_path, candidate_token, overwrite=overwrite)
        return jsonify(result), (200 if result.get("ok") else 400)
    if mode not in {"dry-run", "write", "scrape", "artist-online-write", "artist-scan-count", "lyrics-scan", "album-art-scan", "album-art-write", "album-art-write-overwrite", "lyrics-write", "lyrics-write-overwrite", "lyrics-online-write", "lyrics-online-write-overwrite", "album-art-online-write", "album-art-online-write-overwrite"}:
        return jsonify({"ok": False, "error": "invalid mode"}), 400
    started = run_job(mode=mode, overwrite=overwrite, target=target)
    if not started:
        return jsonify({"ok": False, "error": "job already running", "job": job_state}), 409
    return jsonify({"ok": True, "job": job_state})


@app.route("/preview")
def preview():
    raw_path = request.args.get("path")
    if not raw_path:
        abort(400)
    file_path = Path(unquote(raw_path))
    config = get_config()
    allowed_roots = [resolve_music_root(config), resolve_cache_dir(config)]
    export_dir = resolve_export_dir(config)
    if export_dir:
        allowed_roots.append(export_dir)
    if not allowed_file(file_path, allowed_roots) or not file_path.exists() or not file_path.is_file():
        abort(404)
    return send_file(file_path)


@app.route("/health")
def health():
    return jsonify({"ok": True, "running": job_state["running"]})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
