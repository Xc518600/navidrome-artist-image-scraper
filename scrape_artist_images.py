#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

AUDIO_EXTENSIONS_DEFAULT = {
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".ape", ".wma", ".dsf"
}
IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def safe_slug(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", value, flags=re.UNICODE)
    return value.strip("_") or "unknown"


def normalize_artist_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", " ", name)
    return name


def compact_artist_key(name: str) -> str:
    value = normalize_artist_name(name).casefold()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value, flags=re.UNICODE)
    return value


def artist_name_score(query: str, candidate: str, aliases: Optional[List[str]] = None) -> int:
    query_norm = normalize_artist_name(query)
    candidate_norm = normalize_artist_name(candidate)
    query_key = compact_artist_key(query_norm)
    candidate_key = compact_artist_key(candidate_norm)

    if not query_key or not candidate_key:
        return 0
    if query_key == candidate_key:
        return 100

    best = 0
    if len(query_key) >= 3 and (query_key in candidate_key or candidate_key in query_key):
        best = max(best, 72)

    for alias in aliases or []:
        alias_key = compact_artist_key(alias)
        if not alias_key:
            continue
        if alias_key == query_key:
            best = max(best, 98)
        elif len(alias_key) >= 3 and (query_key in alias_key or alias_key in query_key):
            best = max(best, 70)

    return best


def build_artist_alias_map(config: dict) -> Dict[str, str]:
    raw = config.get("artist_aliases", {}) or {}
    mapping: Dict[str, str] = {}
    for alias, canonical in raw.items():
        alias_norm = normalize_artist_name(str(alias))
        canonical_norm = normalize_artist_name(str(canonical))
        if alias_norm and canonical_norm:
            mapping[alias_norm] = canonical_norm
    return mapping


def canonicalize_artist_name(name: str, alias_map: Optional[Dict[str, str]] = None) -> str:
    normalized = normalize_artist_name(name)
    if not alias_map:
        return normalized
    return alias_map.get(normalized, normalized)


def split_artist_candidates(value: str) -> List[str]:
    if not value:
        return []
    raw = value.strip()
    parts = re.split(r"\s*(?:,|/|;|&|、|，| feat\.? | ft\.? | x | X | and )\s*", raw)
    parts = [normalize_artist_name(p) for p in parts if p and p.strip()]
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return seen or [normalize_artist_name(raw)]


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def build_navidrome_artist_aliases(config: dict) -> Dict[str, List[str]]:
    raw = config.get("navidrome_artist_aliases", {}) or {}
    result: Dict[str, List[str]] = {}
    for canonical, aliases in raw.items():
        canonical_norm = normalize_artist_name(str(canonical))
        if not canonical_norm:
            continue
        values = aliases if isinstance(aliases, list) else [aliases]
        cleaned = []
        for alias in values:
            alias_norm = normalize_artist_name(str(alias))
            if alias_norm and alias_norm not in cleaned and alias_norm != canonical_norm:
                cleaned.append(alias_norm)
        result[canonical_norm] = cleaned
    return result


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def guess_ext_from_url(url: str) -> Optional[str]:
    lower = url.lower().split("?")[0]
    for ext in IMAGE_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    return None


def guess_ext_from_content_type(content_type: str) -> Optional[str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    return mapping.get(ct)


def cleanup_existing_artist_images(folder: Path):
    for ext in IMAGE_EXTENSIONS:
        p = folder / f"artist{ext}"
        if p.exists():
            p.unlink()


def parse_artist_from_folder_name(folder: Path) -> Optional[str]:
    candidates = [folder]
    parent = folder.parent
    if parent and parent != folder:
        candidates.append(parent)

    patterns = [
        r"^(.+?)\s+-\s+.+$",
        r"^(.+?)\s*[—–-]\s*.+$",
    ]
    for candidate_folder in candidates:
        name = candidate_folder.name.strip()
        for pattern in patterns:
            m = re.match(pattern, name)
            if m:
                artist = normalize_artist_name(m.group(1))
                parts = split_artist_candidates(artist)
                if parts:
                    return parts[0]
    return None


@dataclass
class AlbumFolder:
    path: Path
    artist: str
    source_file: Path
    has_artist_image: bool
    mode: str = "album-folder"


@dataclass
class ResolveResult:
    artist: str
    ok: bool
    source: str = ""
    url: str = ""
    cache_file: Optional[Path] = None
    ext: str = ".jpg"
    error: str = ""


class ArtistImageResolver:
    def __init__(self, config: dict, cache_dir: Path):
        self.config = config
        self.cache_dir = cache_dir
        ensure_dir(cache_dir)
        self.timeout = int(config.get("request_timeout", 20))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.get("user_agent", "navidrome-artist-image-scraper/0.1")
        })
        self.providers = config.get("providers", ["override", "netease", "theaudiodb", "deezer"])
        self.overrides = config.get("overrides", {})
        self.artist_aliases = build_artist_alias_map(config)

    def resolve(self, artist: str) -> ResolveResult:
        artist = canonicalize_artist_name(artist, self.artist_aliases)
        cache_key = sha1_text(artist)
        existing = self._find_cached(artist, cache_key)
        if existing:
            return ResolveResult(artist=artist, ok=True, source="cache", cache_file=existing, ext=existing.suffix.lower())

        last_err = "no provider matched"
        for provider in self.providers:
            try:
                result = None
                if provider == "override":
                    result = self._from_override(artist)
                elif provider == "netease":
                    result = self._from_netease(artist)
                elif provider == "theaudiodb":
                    result = self._from_theaudiodb(artist)
                elif provider == "deezer":
                    result = self._from_deezer(artist)

                if result and result[0]:
                    source, image_bytes, original_url, ext = result
                    cache_file = self.cache_dir / f"{safe_slug(artist)}-{cache_key[:10]}{ext}"
                    cache_file.write_bytes(image_bytes)
                    return ResolveResult(
                        artist=artist,
                        ok=True,
                        source=source,
                        url=original_url,
                        cache_file=cache_file,
                        ext=ext,
                    )
            except Exception as exc:
                last_err = f"{provider}: {exc}"
                continue

        return ResolveResult(artist=artist, ok=False, error=last_err)

    def _find_cached(self, artist: str, cache_key: str) -> Optional[Path]:
        stem = f"{safe_slug(artist)}-{cache_key[:10]}"
        for ext in IMAGE_EXTENSIONS:
            p = self.cache_dir / f"{stem}{ext}"
            if p.exists() and p.stat().st_size > 0:
                return p
        return None

    def _fetch(self, url: str) -> Tuple[bytes, str]:
        r = self.session.get(url, timeout=self.timeout)
        r.raise_for_status()
        ext = guess_ext_from_content_type(r.headers.get("content-type", "")) or guess_ext_from_url(url) or ".jpg"
        return r.content, ext

    def _from_override(self, artist: str):
        item = self.overrides.get(artist)
        if not item:
            return None
        if isinstance(item, str):
            item = {"url": item}
        if item.get("file"):
            p = Path(item["file"]).expanduser()
            if not p.is_absolute():
                p = Path.cwd() / p
            ext = p.suffix.lower() if p.suffix.lower() in IMAGE_EXTENSIONS else ".jpg"
            return ("override:file", p.read_bytes(), str(p), ext)
        if item.get("url"):
            data, ext = self._fetch(item["url"])
            return ("override:url", data, item["url"], ext)
        return None

    def _from_netease(self, artist: str):
        urls = [
            "https://music.163.com/api/search/get/web",
            "https://music.163.com/api/cloudsearch/pc",
        ]
        params = {"s": artist, "type": "100", "limit": "5", "offset": "0"}
        headers = {"Referer": "https://music.163.com/"}
        best = None
        for api in urls:
            resp = self.session.get(api, params=params, headers=headers, timeout=self.timeout)
            data = resp.json()
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(result, dict):
                continue
            items = result.get("artists") or []
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = normalize_artist_name((item.get("name") or ""))
                pic = item.get("img1v1Url") or item.get("picUrl") or item.get("avatar")
                if not pic:
                    continue
                alias_values = []
                for key in ("alias", "alia", "transNames"):
                    value = item.get(key) or []
                    if isinstance(value, list):
                        alias_values.extend(value)
                    elif isinstance(value, str) and value.strip():
                        alias_values.append(value)
                trans = item.get("trans")
                if isinstance(trans, str) and trans.strip():
                    alias_values.append(trans)
                aliases = [normalize_artist_name(x) for x in alias_values if isinstance(x, str) and x.strip()]
                score = artist_name_score(artist, name, aliases=aliases)
                if best is None or score > best[0]:
                    best = (score, pic, name)
            if best and best[0] >= 90:
                break
        if not best or best[0] < 90:
            return None
        pic = best[1]
        image_bytes, ext = self._fetch(pic)
        return ("netease", image_bytes, pic, ext)

    def _from_theaudiodb(self, artist: str):
        url = f"https://theaudiodb.com/api/v1/json/2/search.php?s={quote(artist)}"
        data = self.session.get(url, timeout=self.timeout).json()
        artists = data.get("artists") or []
        if not artists:
            return None
        best = None
        for item in artists:
            name = (item.get("strArtist") or "").strip()
            thumb = item.get("strArtistThumb") or item.get("strArtistFanart") or item.get("strArtistLogo")
            if not thumb:
                continue
            aliases = [x for x in [item.get("strArtistAlternate"), item.get("strArtistSort")] if x]
            score = artist_name_score(artist, name, aliases=aliases)
            if best is None or score > best[0]:
                best = (score, thumb)
        if not best or best[0] < 90:
            return None
        thumb = best[1]
        image_bytes, ext = self._fetch(thumb)
        return ("theaudiodb", image_bytes, thumb, ext)

    def _from_deezer(self, artist: str):
        url = f"https://api.deezer.com/search/artist?q={quote(artist)}"
        data = self.session.get(url, timeout=self.timeout).json()
        items = data.get("data") or []
        if not items:
            return None
        best = None
        for item in items:
            name = (item.get("name") or "").strip()
            thumb = item.get("picture_xl") or item.get("picture_big") or item.get("picture_medium")
            if not thumb:
                continue
            aliases = []
            score = artist_name_score(artist, name, aliases=aliases)
            if best is None or score > best[0]:
                best = (score, thumb)
        if not best or best[0] < 90:
            return None
        thumb = best[1]
        image_bytes, ext = self._fetch(thumb)
        return ("deezer", image_bytes, thumb, ext)


def probe_tags(audio_path: Path) -> Dict[str, str]:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout)
    except Exception:
        return {}
    tags = (data.get("format") or {}).get("tags") or {}
    return {str(k).lower(): str(v).strip() for k, v in tags.items() if v is not None and str(v).strip()}


def detect_album_artist(audio_path: Path, folder: Path, alias_map: Optional[Dict[str, str]] = None, include_fallback: bool = True) -> Optional[str]:
    tags = probe_tags(audio_path)
    preferred_keys = ["album_artist", "albumartist", "album artist"]
    fallback_keys = ["artist", "author"]

    for key in preferred_keys:
        value = tags.get(key)
        if value:
            parts = split_artist_candidates(value)
            if parts:
                return canonicalize_artist_name(parts[0], alias_map)

    if include_fallback:
        for key in fallback_keys:
            value = tags.get(key)
            if value:
                parts = split_artist_candidates(value)
                if parts:
                    return canonicalize_artist_name(parts[0], alias_map)

    parsed = parse_artist_from_folder_name(folder)
    if parsed:
        return canonicalize_artist_name(parsed, alias_map)
    return None


def detect_folder_artist(audio_files: List[Path], folder: Path, alias_map: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[Path]]:
    preferred_counts: Dict[str, int] = {}
    preferred_source: Dict[str, Path] = {}
    fallback_counts: Dict[str, int] = {}
    fallback_source: Dict[str, Path] = {}
    parsed = parse_artist_from_folder_name(folder)
    parsed_canonical = canonicalize_artist_name(parsed, alias_map) if parsed else None

    for audio_path in sorted(audio_files):
        preferred_artist = detect_album_artist(audio_path, folder, alias_map=alias_map, include_fallback=False)
        if preferred_artist:
            preferred_counts[preferred_artist] = preferred_counts.get(preferred_artist, 0) + 1
            preferred_source.setdefault(preferred_artist, audio_path)
            continue

        fallback_artist = detect_album_artist(audio_path, folder, alias_map=alias_map, include_fallback=True)
        if fallback_artist:
            fallback_counts[fallback_artist] = fallback_counts.get(fallback_artist, 0) + 1
            fallback_source.setdefault(fallback_artist, audio_path)

    if parsed_canonical:
        if parsed_canonical in preferred_source:
            return parsed_canonical, preferred_source[parsed_canonical]
        if parsed_canonical in fallback_source:
            return parsed_canonical, fallback_source[parsed_canonical]
        return parsed_canonical, sorted(audio_files)[0] if audio_files else None

    if preferred_counts:
        total = sum(preferred_counts.values())
        best_artist, best_count = sorted(preferred_counts.items(), key=lambda item: (-item[1], item[0]))[0]
        dominance = best_count / total if total else 0
        if len(preferred_counts) == 1 or dominance >= 0.7:
            return best_artist, preferred_source[best_artist]

    if fallback_counts:
        total = sum(fallback_counts.values())
        best_artist, best_count = sorted(fallback_counts.items(), key=lambda item: (-item[1], item[0]))[0]
        dominance = best_count / total if total else 0
        if len(fallback_counts) == 1 or dominance >= 0.7:
            return best_artist, fallback_source[best_artist]

    return None, None


def collect_existing_artist_image(folder: Path) -> bool:
    return any((folder / f"artist{ext}").exists() for ext in IMAGE_EXTENSIONS)


def is_flat_mixed_music_dir(folder: Path, audio_files: List[Path], config: Optional[dict] = None) -> bool:
    if len(audio_files) < 8:
        return False
    flat_dir_names = set(config.get("flat_mixed_dir_names", ["自己下载"]) if config else ["自己下载"])
    if folder.name in flat_dir_names:
        return True
    subdirs = [p for p in folder.iterdir() if p.is_dir()]
    return len(subdirs) == 0 and folder.name == "自己下载"


def collect_flat_mixed_artist_groups(audio_files: List[Path], folder: Path, alias_map: Optional[Dict[str, str]] = None) -> List[AlbumFolder]:
    groups: Dict[str, Path] = {}
    counts: Dict[str, int] = {}
    for audio_path in sorted(audio_files):
        artist = detect_album_artist(audio_path, folder, alias_map=alias_map, include_fallback=True)
        if not artist:
            continue
        counts[artist] = counts.get(artist, 0) + 1
        groups.setdefault(artist, audio_path)

    results: List[AlbumFolder] = []
    for artist in sorted(groups):
        results.append(AlbumFolder(
            path=folder,
            artist=artist,
            source_file=groups[artist],
            has_artist_image=False,
            mode="flat-mixed",
        ))
    return results


def collect_album_folders(root: Path, extensions: set, skip_dirs: List[str], alias_map: Optional[Dict[str, str]] = None, config: Optional[dict] = None) -> List[AlbumFolder]:
    results: List[AlbumFolder] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        current = Path(current_root)
        audio_files = [current / f for f in filenames if (current / f).suffix.lower() in extensions]
        if not audio_files:
            continue

        if is_flat_mixed_music_dir(current, audio_files, config=config):
            results.extend(collect_flat_mixed_artist_groups(audio_files, current, alias_map=alias_map))
            continue

        try:
            artist, source_file = detect_folder_artist(audio_files, current, alias_map=alias_map)
        except Exception as exc:
            eprint(f"[WARN] tag read failed: {current}: {exc}")
            continue
        if not artist or not source_file:
            continue

        results.append(AlbumFolder(
            path=current,
            artist=artist,
            source_file=source_file,
            has_artist_image=collect_existing_artist_image(current),
            mode="album-folder",
        ))
    return results


def copy_cached_image(cache_file: Path, album_dir: Path, overwrite: bool, ext: str) -> Tuple[bool, str]:
    target = album_dir / f"artist{ext}"
    exists = collect_existing_artist_image(album_dir)
    if exists and not overwrite:
        return False, "exists"
    if overwrite:
        cleanup_existing_artist_images(album_dir)
    shutil.copy2(cache_file, target)
    return True, str(target)


def export_navidrome_artist_images(cache_file: Path, export_dir: Optional[Path], artist: str, aliases: List[str], ext: str) -> List[str]:
    if not export_dir:
        return []
    ensure_dir(export_dir)
    exported = []
    names = [normalize_artist_name(artist)] + [normalize_artist_name(a) for a in aliases]
    seen = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    for name in seen:
        target = export_dir / f"{name}{ext}"
        shutil.copy2(cache_file, target)
        exported.append(str(target))
    return exported


def make_report(summary: dict, output_path: Optional[Path]):
    if not output_path:
        return
    ensure_dir(output_path.parent)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Scrape artist images for Navidrome by writing artist.* into album folders.")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not write files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing artist image")
    parser.add_argument("--path", help="Limit scanning to a subpath")
    parser.add_argument("--artist", help="Limit scraping to a single artist name")
    parser.add_argument("--report", help="Write JSON report to file", default="./last-run-report.json")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_json(config_path)
    base_dir = config_path.parent

    music_root = Path(args.path or config["music_root"]).expanduser()
    if not music_root.is_absolute():
        music_root = (base_dir / music_root).resolve()
    else:
        music_root = music_root.resolve()

    cache_dir = Path(config.get("cache_dir", "./cache"))
    if not cache_dir.is_absolute():
        cache_dir = (base_dir / cache_dir).resolve()

    navidrome_export_dir = config.get("navidrome_export_dir")
    export_dir = Path(navidrome_export_dir).expanduser() if navidrome_export_dir else None
    if export_dir and not export_dir.is_absolute():
        export_dir = (base_dir / export_dir).resolve()

    extensions = set(s.lower() for s in config.get("audio_extensions", list(AUDIO_EXTENSIONS_DEFAULT)))
    skip_dirs = list(config.get("skip_dirs", []))
    artist_aliases = build_artist_alias_map(config)
    navidrome_artist_aliases = build_navidrome_artist_aliases(config)

    resolver = ArtistImageResolver(config=config, cache_dir=cache_dir)

    print(f"[INFO] music_root={music_root}")
    print(f"[INFO] cache_dir={cache_dir}")
    print(f"[INFO] navidrome_export_dir={export_dir}")
    print(f"[INFO] dry_run={args.dry_run} overwrite={args.overwrite}")

    album_folders = collect_album_folders(music_root, extensions, skip_dirs, alias_map=artist_aliases, config=config)
    print(f"[INFO] detected album folders: {len(album_folders)}")

    unique_artists: Dict[str, List[AlbumFolder]] = {}
    for item in album_folders:
        unique_artists.setdefault(item.artist, []).append(item)

    if args.artist:
        exact_artist = args.artist.strip()
        unique_artists = {artist: folders for artist, folders in unique_artists.items() if artist == exact_artist}
        print(f"[INFO] artist filter: {exact_artist}")

    print(f"[INFO] detected unique artists: {len(unique_artists)}")

    report = {
        "music_root": str(music_root),
        "dry_run": args.dry_run,
        "overwrite": args.overwrite,
        "artist": args.artist or "",
        "stats": {
            "album_folders": len(album_folders),
            "artists": len(unique_artists),
            "resolved": 0,
            "failed": 0,
            "written": 0,
            "skipped_existing": 0,
        },
        "artists": {},
    }

    for idx, (artist, folders) in enumerate(sorted(unique_artists.items()), start=1):
        print(f"\n[{idx}/{len(unique_artists)}] resolving: {artist} ({len(folders)} folders)")
        resolved = resolver.resolve(artist)
        item = {
            "ok": resolved.ok,
            "source": resolved.source,
            "url": resolved.url,
            "ext": resolved.ext,
            "error": resolved.error,
            "folders": [str(f.path) for f in folders],
            "folder_modes": {str(f.path): f.mode for f in folders},
            "written": [],
            "skipped": [],
            "navidrome_exported": [],
            "navidrome_aliases": navidrome_artist_aliases.get(artist, []),
        }

        if not resolved.ok or not resolved.cache_file:
            report["stats"]["failed"] += 1
            report["artists"][artist] = item
            print(f"  [MISS] {resolved.error}")
            continue

        report["stats"]["resolved"] += 1
        print(f"  [HIT] source={resolved.source} ext={resolved.ext}")

        aliases_for_export = navidrome_artist_aliases.get(artist, [])
        if args.dry_run:
            planned = []
            if export_dir:
                planned = [str(export_dir / f"{name}{resolved.ext}") for name in [artist] + aliases_for_export]
                item["navidrome_exported"].extend(planned)
                print(f"  [PLAN] navidrome exports={len(planned)}")
        else:
            exported = export_navidrome_artist_images(
                resolved.cache_file,
                export_dir=export_dir,
                artist=artist,
                aliases=aliases_for_export,
                ext=resolved.ext,
            )
            item["navidrome_exported"].extend(exported)
            if exported:
                print(f"  [EXPORT] navidrome files={len(exported)}")

        for folder in folders:
            target = folder.path / f"artist{resolved.ext}"
            if folder.mode == "flat-mixed":
                item["skipped"].append(str(target))
                print(f"    [SKIP] flat-mixed {folder.path} -> export only")
                continue
            if args.dry_run:
                if collect_existing_artist_image(folder.path) and not args.overwrite:
                    report["stats"]["skipped_existing"] += 1
                    item["skipped"].append(str(target))
                    print(f"    [SKIP] exists {folder.path}")
                else:
                    item["written"].append(str(target))
                    print(f"    [PLAN] write {target}")
                continue

            wrote, msg = copy_cached_image(resolved.cache_file, folder.path, overwrite=args.overwrite, ext=resolved.ext)
            if wrote:
                report["stats"]["written"] += 1
                item["written"].append(str(target))
                print(f"    [WRITE] {target}")
            else:
                report["stats"]["skipped_existing"] += 1
                item["skipped"].append(str(target))
                print(f"    [SKIP] {folder.path}")

        report["artists"][artist] = item
        time.sleep(0.2)

    report_path = Path(args.report)
    if not report_path.is_absolute():
        report_path = (base_dir / report_path).resolve()
    make_report(report, report_path)

    print("\n=== SUMMARY ===")
    for k, v in report["stats"].items():
        print(f"{k}: {v}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
