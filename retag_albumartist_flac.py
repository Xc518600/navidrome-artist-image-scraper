#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

OPS = [
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/周杰倫 - 不能說的·秘密 電影原聲帶 (2007) {88697167512 111001} [FLAC]'), '周杰伦'),
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/G.E.M. 邓紫棋 - I AM GLORIA - 2025 FLAC Multi File'), 'G.E.M. 邓紫棋'),
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/邓紫棋 - 桃花诺 2017 FLAC'), 'G.E.M. 邓紫棋'),
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/邓紫棋 - 极品试音天碟 2015 FLAC/CD1'), 'G.E.M. 邓紫棋'),
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/邓紫棋 - 极品试音天碟 2015 FLAC/CD2'), 'G.E.M. 邓紫棋'),
    (Path('/vol4/1000/硬盘4国语电影/音乐/音乐种子/Wu Bai (伍佰) & China Blue - 伍佰力(2004 Live 生命热力) (2004) { JingWen Records, 9787880456707, CD} [FLAC]'), '伍佰 & China Blue'),
]


def retag_flac(path: Path, album_artist: str) -> bool:
    tmp_dir = Path(tempfile.mkdtemp(prefix='retag-flac-', dir=str(path.parent)))
    tmp_out = tmp_dir / path.name
    cmd = [
        'ffmpeg', '-y', '-v', 'error',
        '-i', str(path),
        '-map', '0',
        '-map_metadata', '0',
        '-c', 'copy',
        '-metadata', f'album_artist={album_artist}',
        str(tmp_out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp_out.exists():
        print(f'[FAIL] {path}')
        if proc.stderr.strip():
            print(proc.stderr.strip())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    original_stat = path.stat()
    backup = path.with_suffix(path.suffix + '.bak-openclaw')
    if backup.exists():
        backup.unlink()
    path.rename(backup)
    try:
        shutil.move(str(tmp_out), str(path))
        path.chmod(original_stat.st_mode)
        shutil.copystat(backup, path)
        backup.unlink(missing_ok=True)
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        backup.rename(path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f'[OK] {path}')
    return True


def main():
    total = 0
    changed = 0
    for root, album_artist in OPS:
        for p in sorted(root.rglob('*.flac')):
            total += 1
            if retag_flac(p, album_artist):
                changed += 1
    print(f'changed={changed} total={total}')


if __name__ == '__main__':
    main()
