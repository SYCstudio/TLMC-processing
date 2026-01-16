#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import subprocess
from pathlib import Path
import shutil
import re
from difflib import SequenceMatcher
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from datetime import datetime
import hashlib

# ================= CONFIG =================
ROOT_DIR = Path("/data/disk14tb/TLMC/incoming")   # 源目录
OUTPUT_DIR = Path("/data/disk14tb/TLMC/library")  # 输出目录
DRY_RUN = False
ENABLE_SPLIT = True
ENABLE_DELETE = True
ENABLE_WAV_TO_FLAC = True

CANDIDATE_ENCODINGS = [
    "utf-8",
    "utf-8-sig",
    "windows-1252",
    "gbk",
    "gb2312",
    "big5",
    "shift_jis",
    "latin1",
]

FILE_RE = re.compile(r'^FILE\s+"(.+?)"\s+', re.IGNORECASE)
TRACK_RE = re.compile(r'^\s+TRACK\s+(\d+)\s+', re.IGNORECASE)
TITLE_RE = re.compile(r'^\s+TITLE\s+"(.+?)"', re.IGNORECASE)
PERFORMER_RE = re.compile(r'^\s+PERFORMER\s+"(.+?)"', re.IGNORECASE)
INDEX_RE = re.compile(r'^\s+INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)', re.IGNORECASE)

PROCESSING_FILE = ".processing"  # 用于记录已处理的 Album（rsync exclude）
LOG_FILE = ROOT_DIR / "processing.log"

# ================= LOGGING =================
def log(msg: str, level: str="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line)
    if not DRY_RUN:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

# ================= UTILS =================
def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        print(" ".join(str(a) for a in args))

def run(cmd):
    safe_print("[CMD]", " ".join(cmd))
    if not DRY_RUN:
        subprocess.run(cmd, check=True)

def read_cue_with_encoding(cue: Path):
    data = cue.read_bytes()
    for enc in CANDIDATE_ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return None, None

def fix_cue_encoding(cue: Path):
    text, enc = read_cue_with_encoding(cue)
    if text is None:
        log(f"Cannot decode {cue}", "ERROR")
        return False
    if enc.startswith("utf-8"):
        return True
    log(f"Fixing {cue} encoding ({enc} → utf-8)")
    if not DRY_RUN:
        normalized = text.replace("\r\n","\n").replace("\r","\n")
        cue.write_text(normalized, encoding="utf-8")
    return True

def parse_cue_file(cue: Path):
    text = cue.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    tracks = []
    current_track = None
    current_performer = None

    for line in lines:
        line = line.strip()
        m = PERFORMER_RE.match(line)
        if m:
            current_performer = m.group(1)
        m = TRACK_RE.match(line)
        if m:
            if current_track:
                tracks.append(current_track)
            track_num = int(m.group(1))
            current_track = {
                'track_num': track_num,
                'title': None,
                'performer': current_performer,
                'start_time': None,
                'end_time': None
            }
        if current_track:
            m = TITLE_RE.match(line)
            if m:
                current_track['title'] = m.group(1)
            m = PERFORMER_RE.match(line)
            if m:
                current_track['performer'] = m.group(1)
            m = INDEX_RE.match(line)
            if m and int(m.group(1)) == 1:
                minutes = int(m.group(2))
                seconds = int(m.group(3))
                frames = int(m.group(4))
                total_seconds = minutes*60 + seconds + frames/75.0
                current_track['start_time'] = total_seconds
    if current_track:
        tracks.append(current_track)
    for i in range(len(tracks)):
        if i < len(tracks)-1:
            tracks[i]['end_time'] = tracks[i+1]['start_time']
    return tracks

def extract_audio_from_cue(cue: Path):
    text = cue.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        m = FILE_RE.match(line.strip())
        if m:
            return cue.parent / m.group(1)
    return None

def sanitize_filename(name: str, max_bytes=200) -> str:
    # 替换非法字符
    safe = re.sub(r'[<>:"/\\|?*]', "_", name)
    # 转为 utf-8 bytes 检查长度
    enc = safe.encode("utf-8")
    if len(enc) > max_bytes:
        # 生成 8 位 sha1 摘要
        h = hashlib.sha1(enc).hexdigest()[:8]
        # 截断原始 safe 字符串
        while len(safe.encode("utf-8")) > max_bytes - len(h) - 1:
            safe = safe[:-1]
        safe = f"{safe}_{h}"
    return safe

def convert_wav_to_flac(wav: Path):
    try:
        flac = wav.with_suffix(".flac")
        cmd = ["ffmpeg","-y","-loglevel","error","-i",str(wav),"-c:a","flac","-compression_level","8",str(flac)]
        run(cmd)
        log(f"Converted WAV to FLAC: {wav} → {flac}")
        if not DRY_RUN and ENABLE_DELETE:
            wav.unlink()
        return flac
    except Exception as e:
        log(f"Failed to convert {wav}: {e}", "ERROR")
        return None

def split_audio(cue: Path, audio: Path, output_dir: Path, progress=None, task_id=None):
    try:
        tracks = parse_cue_file(cue)
        if not output_dir.exists() and not DRY_RUN:
            output_dir.mkdir(parents=True, exist_ok=True)
        for track in tracks:
            if track['start_time'] is None:
                continue
            track_num_str = f"{track['track_num']:02d}"
            title = track['title'] or f"Track {track['track_num']}"
            safe_title = sanitize_filename(title)
            output_path = output_dir / f"{track_num_str} - {safe_title}.flac"
            cmd = [
                "ffmpeg","-y","-loglevel","error","-i",str(audio),
                "-ss", str(track['start_time'])
            ]
            if track['end_time'] is not None:
                duration = track['end_time'] - track['start_time']
                cmd.extend(["-t", str(duration)])
            cmd.extend([
                "-vn","-c:a","flac","-compression_level","8"
            ])
            if track['title']:
                cmd.extend(["-metadata", f"title={track['title']}"])
            if track['performer']:
                cmd.extend(["-metadata", f"artist={track['performer']}"])
            cmd.extend(["-metadata", f"track={track['track_num']}", str(output_path)])
            run(cmd)
            log(f"Split track {track_num_str}: {title}")
            if progress:
                progress.update(task_id, advance=1)
        if ENABLE_DELETE:
            audio.unlink()
    except Exception as e:
        log(f"Failed to split {audio} using {cue}: {e}", "ERROR")

# ================= PROCESS ALBUM =================
def process_album(artist_dir: Path, album_dir: Path, output_root: Path, progress=None, album_task_id=None):
    album_name = album_dir.name
    try:
        cues = list(album_dir.glob("*.cue"))
        audios = list(album_dir.glob("*.flac")) + list(album_dir.glob("*.wav"))

        if not cues and not audios:
            log(f"No files to process in {album_dir}", "WARNING")
            return

        # 检查 .processing
        processing_file = output_root / PROCESSING_FILE
        processed_albums = []
        if processing_file.exists():
            processed_albums = processing_file.read_text().splitlines()
        rel_path = str(album_dir.relative_to(ROOT_DIR))
        if rel_path in processed_albums:
            log(f"Already processed {rel_path}, skipping")
            return

        multiple_cue = len(cues) > 1
        inner_total = 0
        for cue in cues:
            tracks = parse_cue_file(cue)
            inner_total += len(tracks) if ENABLE_SPLIT else 0
        if inner_total == 0:
            inner_total = 1

        # 内层进度条
        inner_progress = None
        inner_task_id = None
        if progress and inner_total > 0:
            from rich.progress import Progress
            inner_progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeRemainingColumn()
            )
            inner_progress.start()
            inner_task_id = inner_progress.add_task(f"Tracks in {album_name}", total=inner_total)

        # 处理 CUE
        for cue in cues:
            try:
                fix_cue_encoding(cue)
                audio = extract_audio_from_cue(cue)
                if audio is None or not audio.exists():
                    log(f"No matching audio for {cue}", "WARNING")
                    continue
                out_dir = album_dir
                if multiple_cue:
                    out_dir = album_dir / cue.stem
                split_audio(cue, audio, out_dir, progress=inner_progress, task_id=inner_task_id)
            except Exception as e:
                log(f"Failed processing cue {cue}: {e}", "ERROR")

        # 转换 WAV 文件
        for wav in album_dir.glob("*.wav"):
            convert_wav_to_flac(wav)

        if inner_progress:
            inner_progress.stop()

        # 一次性移动 Album
        dest_album_dir = output_root / artist_dir.name / album_name
        if not DRY_RUN:
            dest_album_dir.parent.mkdir(parents=True, exist_ok=True)
            if dest_album_dir.exists():
                shutil.rmtree(dest_album_dir)
            shutil.move(str(album_dir), str(dest_album_dir))
            log(f"Moved album {album_name} to library")

        # 更新 .processing
        if not DRY_RUN:
            with open(processing_file, "a", encoding="utf-8") as f:
                f.write(rel_path + "\n")

        if progress and album_task_id is not None:
            progress.update(album_task_id, advance=1)
    except Exception as e:
        log(f"Failed processing album {album_dir}: {e}", "ERROR")

# ================= MAIN =================
def main():
    artists = [d for d in ROOT_DIR.iterdir() if d.is_dir()]
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn()
    ) as progress:
        total_albums = sum(len(list(a.iterdir())) for a in artists)
        album_task_id = progress.add_task("Processing Albums", total=total_albums)
        for artist_dir in artists:
            for album_dir in artist_dir.iterdir():
                if album_dir.is_dir():
                    process_album(artist_dir, album_dir, OUTPUT_DIR, progress=progress, album_task_id=album_task_id)

if __name__ == "__main__":
    main()
