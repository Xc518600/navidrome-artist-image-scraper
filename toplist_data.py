# -*- coding: utf-8 -*-
"""排行榜数据源：QQ 音乐 toplist + 网易云榜单歌单（直连公开接口，零新依赖）。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"

# QQ 榜单 topId（musicToplist.ToplistInfoServer GetDetail 实测可用）
QQ_TOPLISTS = [
    {"id": "qq_26", "top_id": 26, "name": "热歌榜", "source": "qq"},
    {"id": "qq_62", "top_id": 62, "name": "飙升榜", "source": "qq"},
    {"id": "qq_27", "top_id": 27, "name": "新歌榜", "source": "qq"},
    {"id": "qq_4", "top_id": 4, "name": "流行指数榜", "source": "qq"},
]

# 网易云榜单歌单 ID（官方 toplist/detail 实测确认）
NETEASE_TOPLISTS = [
    {"id": "ne_3778678", "playlist_id": 3778678, "name": "热歌榜", "source": "netease"},
    {"id": "ne_19723756", "playlist_id": 19723756, "name": "飙升榜", "source": "netease"},
    {"id": "ne_3779629", "playlist_id": 3779629, "name": "新歌榜", "source": "netease"},
]

_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 1800  # 榜单 30 分钟缓存


def list_toplists() -> List[Dict[str, Any]]:
    items = []
    for t in QQ_TOPLISTS:
        items.append({"id": t["id"], "name": t["name"], "source": "qq", "source_label": "QQ音乐"})
    for t in NETEASE_TOPLISTS:
        items.append({"id": t["id"], "name": t["name"], "source": "netease", "source_label": "网易云"})
    return items


def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    entry = _CACHE.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["songs"]
    return None


def _cache_set(key: str, songs: List[Dict[str, Any]]) -> None:
    if len(_CACHE) > 30:
        for k in list(_CACHE.keys())[:10]:
            _CACHE.pop(k, None)
    _CACHE[key] = {"ts": time.time(), "songs": songs}


def fetch_qq_toplist(top_id: int, limit: int = 50) -> Dict[str, Any]:
    """QQ toplist：period 用当前日期往前找最近的有效期（榜单每日更新）。"""
    songs = _cache_get(f"qq:{top_id}")
    if songs is not None:
        return {"ok": True, "songs": songs}

    import datetime
    now = datetime.datetime.now()
    last_err = ""
    for back in range(0, 6):
        period = (now - datetime.timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            resp = requests.post(
                "https://u.y.qq.com/cgi-bin/musicu.fcg",
                headers={"Referer": "https://y.qq.com/", "User-Agent": _UA,
                         "Content-Type": "application/json"},
                json={"comm": {"ct": 24, "cv": 0},
                      "req1": {"module": "musicToplist.ToplistInfoServer", "method": "GetDetail",
                               "param": {"topId": top_id, "offset": 0, "num": min(limit, 100),
                                         "period": period}}},
                timeout=15,
            )
            data = ((resp.json().get("req1") or {}).get("data") or {}).get("data") or {}
            raw = data.get("song") or []
            if raw:
                break
        except Exception as exc:
            last_err = str(exc)
            raw = []
    if not raw:
        return {"ok": False, "error": last_err or "qq-toplist-empty"}

    songs = []
    for item in raw[:limit]:
        title = str(item.get("title") or "").strip()
        singer = str(item.get("singerName") or "").strip()
        if not title:
            continue
        songs.append({
            "title": title,
            "artist": singer,
            "rank": item.get("rank"),
            "song_id": str(item.get("songId") or ""),
            "cover": str(item.get("cover") or ""),
            "keyword": f"{title} {singer}".strip(),
        })
    if not songs:
        return {"ok": False, "error": "qq-toplist-no-valid-songs"}
    _cache_set(f"qq:{top_id}", songs)
    return {"ok": True, "songs": songs}


def fetch_netease_toplist(playlist_id: int, limit: int = 50) -> Dict[str, Any]:
    songs = _cache_get(f"ne:{playlist_id}")
    if songs is not None:
        return {"ok": True, "songs": songs}
    try:
        resp = requests.get(
            f"https://music.163.com/api/playlist/detail?id={playlist_id}",
            headers={"Referer": "https://music.163.com", "User-Agent": _UA, "Cookie": "appver=2.9.7"},
            timeout=15,
        )
        result = (resp.json().get("result") or {})
        tracks = result.get("tracks") or []
    except Exception as exc:
        return {"ok": False, "error": f"netease-toplist-failed: {exc}"}
    if not tracks:
        return {"ok": False, "error": "netease-toplist-empty"}

    songs = []
    for idx, t in enumerate(tracks[:limit], start=1):
        title = str(t.get("name") or "").strip()
        artists = "/".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
        if not title:
            continue
        songs.append({
            "title": title,
            "artist": artists,
            "rank": idx,
            "song_id": str(t.get("id") or ""),
            "cover": str((t.get("album") or {}).get("picUrl") or ""),
            "keyword": f"{title} {artists}".strip(),
        })
    if not songs:
        return {"ok": False, "error": "netease-toplist-no-valid-songs"}
    _cache_set(f"ne:{playlist_id}", songs)
    return {"ok": True, "songs": songs}


def fetch_toplist(list_id: str, limit: int = 50) -> Dict[str, Any]:
    for t in QQ_TOPLISTS:
        if t["id"] == list_id:
            return fetch_qq_toplist(t["top_id"], limit)
    for t in NETEASE_TOPLISTS:
        if t["id"] == list_id:
            return fetch_netease_toplist(t["playlist_id"], limit)
    return {"ok": False, "error": "unknown-toplist"}
