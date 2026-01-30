from __future__ import annotations
import os, shutil, re, subprocess, inspect, traceback, threading
from pathlib import Path
import hashlib
import cuetools as ct
from typing import Any, Dict, List, Tuple, Set
from datetime import datetime
from difflib import SequenceMatcher
from pprint import pprint
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn, Live, Text
import concurrent.futures

# ================= CONFIG =================
MAIN_DIR = Path(__file__).parent
ROOT_DIR = MAIN_DIR / "incoming"   # 源目录
OUTPUT_DIR = MAIN_DIR / "library"  # 输出目录
ERROR_DIR = MAIN_DIR / "error"    # 错误目录
DRY_RUN = False
ENABLE_SPLIT = True
ENABLE_DELETE = True
ENABLE_MULTI_THREAD = True
LOG_FILE = MAIN_DIR / "processing.log"
ERROR_LOG_FILE = MAIN_DIR / "error.log"
MOVED_ADDITIONAL_FILES_FILE = MAIN_DIR / "moved_additional_files.log"
PROCESSED_FILE = MAIN_DIR / ".processed"
ERROR_PROCESSED_FILE = MAIN_DIR / ".error_processed"
FFMPEG_INNER_THREADS = 4
THREAD_NUM = 8

if not ERROR_DIR.exists():
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
if not OUTPUT_DIR.exists():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
if not LOG_FILE.exists():
    LOG_FILE.touch()
if not ERROR_LOG_FILE.exists():
    ERROR_LOG_FILE.touch()
if not MOVED_ADDITIONAL_FILES_FILE.exists():
    MOVED_ADDITIONAL_FILES_FILE.touch()
if not ERROR_PROCESSED_FILE.exists():
    ERROR_PROCESSED_FILE.touch()

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

def main():
    initialize_files()
    # collect albums
    log(f"========== Collecting albums from {ROOT_DIR} ==========", "INFO")
    albums = collect_albums(ROOT_DIR)
    log(f"Found {len(albums)} albums", "INFO")
    log("================================================", "INFO")
    log(f"========== Processing {len(albums)} albums ==========", "INFO")
    process_albums(albums)
    log("================================================", "INFO")
    log(f"========== Cleanup additional files ==========", "INFO")
    cleanup_additional_files()
    log("================================================", "INFO")
    log("========== Processing completed ==========", "INFO")
    log("================================================", "INFO")


def archive_old_file(file_path, dst_dir: Path):
    """如果文件存在且有内容，则将其移动到日期目录下"""
    if file_path.exists() and file_path.stat().st_size > 0:
        # 移动文件
        try:
            shutil.move(str(file_path), str(dst_dir))
            return True
        except Exception as e:
            return False
    return False

def initialize_files():
    """初始化所有文件，归档旧文件"""
    # 需要归档的文件列表及其类型
    files_to_archive = [
        LOG_FILE, ERROR_LOG_FILE, MOVED_ADDITIONAL_FILES_FILE, ERROR_PROCESSED_FILE
    ]
    # 获取当前日期作为目录名
    today = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dir = MAIN_DIR / "log" / today
    if not dir.exists():
        dir.mkdir(parents=True, exist_ok=True)
    for file_path in files_to_archive:
        archive_old_file(file_path, dir)

# 预先收集所有专辑
def collect_albums(start_dir: Path) -> list[Path]:
    albums: list[Path] = []
    music_suffix = [".cue", ".flac", ".wav", ".mp3", ".ogg", ".ape", ".aac", ".wv"]
    def traverse(dir: Path, live: Live):
        nonlocal albums
        live.update(Text(f"Traversing: Albums collected: {len(albums)} at {dir}"))
        files = [f for f in dir.iterdir() if f.is_file()]
        if any(f.suffix in music_suffix for f in files):
            albums.append(dir)
        else:
            for subdir in dir.iterdir():
                if subdir.is_dir():
                    traverse(subdir, live)

    with Live(refresh_per_second=4) as live:  # 每秒刷新 4 次
        traverse(start_dir, live)
    return albums

def collect_raw_music_files(album: Path) -> list[Path]:
    files = [f for f in album.iterdir() if f.is_file()]
    raw_music_suffix = [".flac", ".wav", ".mp3", ".ogg", ".ape", ".aac", ".iso"]
    return [f for f in files if f.suffix in raw_music_suffix]

def process_albums(albums: list[Path]):
    encode_need_to_convert = [".wav", ".wv", ".ape", ".tta", ".alac"]
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn()
    ) as progress:
        total_albums = len(albums)
        album_task_id = progress.add_task("Processing Albums", total=total_albums)
        for album in albums:
            progress.update(album_task_id, advance=1)
            log(f"========== Processing {album} ==========", "INFO")
            # 1. convert album to desired format
            try:# try to process the album
                files = [f for f in album.iterdir() if f.is_file()]
                if any(f.suffix == ".cue" for f in files): # check if album has cue file
                    log(f"========== Processing {album} with cue file ==========", "INFO")
                    process_cues(album, progress)
                elif any(f.suffix == ".iso" for f in files): # check if album has iso file
                    log(f"========== Processing {album} with iso file ==========", "INFO")
                    raise NotImplementedError("ISO file is not supported yet")
                else:
                    log(f"========== Processing {album} with additional music files ==========", "INFO")
                    files_to_convert = [f for f in files if f.suffix in encode_need_to_convert]
                    if len(files_to_convert) > 0:
                        convert_musics_to_flac(files_to_convert, progress)
            except Exception as e:
                # 1.1 move the album to error directory
                log(f"========== Processing {album} failed ==========", "ERROR")
                log(f"Error: {e}", "ERROR")
                traceback_str = traceback.format_exc()
                log(f"Traceback: {traceback_str}", "ERROR")
                relative = album.relative_to(ROOT_DIR)
                error_path = ERROR_DIR / relative
                move_dir(album, error_path)
                with open(ERROR_PROCESSED_FILE, "a", encoding="utf-8") as f:
                    f.write(escape_rsync_pattern(str(relative)) + "/" + "\n")
                continue
            # 2. move the album to completed directory
            log(f"========== Processing {album} completed ==========", "INFO")
            relative = album.relative_to(ROOT_DIR)
            completed_path = OUTPUT_DIR / relative
            move_dir(album, completed_path)
            mark_as_processed(album)
            log(f"========== Moving {album} to completed directory completed==========", "INFO")

def cleanup_additional_files(): # there maybe some additional files. e.g. artist/album/disc1, artist/album/disc2, there are maybe some files in artist/album, we need to move them
    additional_files = []
    processed_dirs = set()
    def traverse(dir: Path):
        files = [f for f in dir.iterdir() if f.is_file()]
        if len(files) > 0:
            additional_files.extend(files)
            processed_dirs.add(dir)
        for subdir in dir.iterdir():
            if subdir.is_dir():
                traverse(subdir)
    traverse(ROOT_DIR)
    log(f"========== Cleaning up {len(additional_files)} additional files ==========", "INFO")
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn()
    ) as progress:
        total_files = len(additional_files)
        file_task_id = progress.add_task("Cleaning up additional files", total=total_files)
        for file in additional_files:
            progress.update(file_task_id, advance=1)
            relative = file.relative_to(ROOT_DIR)
            dst_dir = OUTPUT_DIR / relative.parent
            if not DRY_RUN:
                if not dst_dir.exists():
                    dst_dir.mkdir(parents=True, exist_ok=True)
                log(f"Move {file} to {dst_dir}", "INFO")
                dst_file = dst_dir / file.name
                shutil.move(file, dst_file)
                with open(MOVED_ADDITIONAL_FILES_FILE, "a", encoding="utf-8") as f:
                    f.write(str(file) + " -> " + str(dst_file) + "\n")
            else:
                log(f"Expected to move {file} to {dst_dir}", "INFO")
    for dir in processed_dirs:
        mark_as_processed(dir)

# ================= UTILS =================
def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # 如果无法编码，尝试使用ASCII替换
        safe_args = []
        for arg in args:
            if isinstance(arg, str):
                safe_args.append(arg.encode('ascii', 'replace').decode('ascii'))
            else:
                safe_args.append(str(arg).encode('ascii', 'replace').decode('ascii'))
        print(*safe_args, **kwargs)

def run_with_threads(cmds: list[list[str]], title: str, progress: Progress):
    if not ENABLE_MULTI_THREAD:
        for cmd in cmds:
            run(cmd)
        return
    try:
        task_id = progress.add_task(title, total=len(cmds))
        complete_count = 0
        lock = threading.Lock()
        def update_progress():
            nonlocal complete_count
            with lock:
                complete_count += 1
                progress.update(task_id, advance=1)
        def run_with_progress(cmd: list[str]):
            run(cmd)
            update_progress()
        with concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_NUM) as executor:
            futures = [executor.submit(run_with_progress, cmd) for cmd in cmds]
            concurrent.futures.wait(futures)
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    log(f"Error: {e}", "ERROR")
                    traceback_str = traceback.format_exc()
                    log(f"Traceback: {traceback_str}", "ERROR")
                    raise e
    finally:
        progress.remove_task(task_id)
        

def run(cmd):
    log("[CMD] " + " ".join(cmd), "INFO")
    if not DRY_RUN:
        subprocess.run(cmd, check=True)

def log(msg: str, level: str="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 如果是 ERROR 级别，获取调用者的行号信息
    if level == "ERROR":
        stack = inspect.stack()
        # stack[1] 是调用 log 函数的代码位置
        if len(stack) > 1:
            caller_frame = stack[1]
            filename = Path(caller_frame.filename).name
            lineno = caller_frame.lineno
            line = f"[{timestamp}] [{level}] [{filename}:{lineno}] {msg}"
        else:
            line = f"[{timestamp}] [{level}] {msg}"
    else:
        line = f"[{timestamp}] [{level}] {msg}"
    
    safe_print(line)
    if not DRY_RUN:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if level == "ERROR":
            with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")

def calculate_similarity(str1: str, str2: str) -> float:
    """计算两个字符串的相似度，返回 0.0 到 1.0 之间的值"""
    ratio = SequenceMatcher(None, str1.lower(), str2.lower()).ratio()
    # log(f"Calculating similarity between {str1} and {str2} ratio: {ratio}", "DEBUG")
    return ratio

def escape_rsync_pattern(pattern):
    """
    正确的 rsync 字面量转义函数
    避免重复替换问题
    """
    result = []
    
    for char in pattern:
        if char in ['*', '?', '[', ']']:
            result.append(f'[{char}]')
        else:
            result.append(char)
    
    return ''.join(result)

def mark_as_processed(album: Path):
    relative = album.relative_to(ROOT_DIR)
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(escape_rsync_pattern(str(relative)) + "/" + "\n")
    log(f"Marked {album} as processed", "INFO")

def move_dir(src_dir: Path, dst_dir: Path):
    if not dst_dir.exists():
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
    else:
        log(f"Destination directory {dst_dir} already exists", "ERROR")
        raise Exception(f"Destination directory {dst_dir} already exists")
    shutil.move(src_dir, dst_dir)
    log(f"Moved {src_dir} to {dst_dir}", "INFO")

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

# ================= CUE PROCESSING =================

def process_cues(album: Path, progress: Progress):
    cues = [f for f in album.iterdir() if f.suffix == ".cue"]
    for cue in cues:
        if fix_cue_encoding(cue) == False:
            log(f"Failed to fix cue encoding {cue}", "ERROR")
            raise Exception(f"Failed to fix cue encoding {cue}")
    raw_music_files = collect_raw_music_files(album)
    matched, files_need_to_delete = match_cue_and_raw_music_files(cues, raw_music_files)
    if len(matched) > 0 and ENABLE_SPLIT:
        if len(matched) == 1:
            raw_music = list(matched.keys())[0]
            album_title, album_performer, tracks = list(matched.values())[0]
            split_audio(album, raw_music, album_title, album_performer, tracks, album, progress)
        else:
            cue_task_id = progress.add_task("Splitting cues", total=len(matched))
            try:
                for raw_music, (album_title, album_performer, tracks) in matched.items():
                    progress.update(cue_task_id, advance=1)
                    split_audio(album, raw_music, album_title, album_performer, tracks, album / album_title, progress)
            finally:
                progress.remove_task(cue_task_id)
        for raw_music in matched.keys():
            files_need_to_delete.add(raw_music)
    if ENABLE_DELETE and not DRY_RUN:
        for file in files_need_to_delete:
            if file.exists():
                log(f"Deleting {file}", "INFO")
                file.unlink()

def match_cue_and_raw_music_files(cues: list[Path], raw_music_files: list[Path]) -> Tuple[Dict[Path, Tuple[str, str, List[Dict[str, Any]]]], Set[Path]]:
    files_need_to_delete: Set[Path] = set()
    # give each raw music its candidate cue file
    matched: Dict[Path, List[Tuple[str, str, List[Dict[str, Any]]]]] = dict()
    for raw_music in raw_music_files:
        matched[raw_music] = []
    
    # parsing the cue file, and match cue to raw music file
    for cue in cues:
        album_title, album_performer, tracks = parse_cue_file(cue)
        expected_raw_files: Set[Path] = set()
        for track in tracks:
            expected_raw_files.add(track['file'])
        if len(expected_raw_files) == len(tracks):
            # no need to split, the music files are already cut into tracks
            log(f"No need to split {cue}, all tracks are already cut into tracks", "INFO")
            continue
        if len(expected_raw_files) >1:
            # too difficult to split, need manual maintainance
            log(f"Too many cue raw music files for {cue}, need manual maintainance", "ERROR")
            raise Exception(f"Too many cue raw music files for {cue}")
        # check if the split file are already at place
        # for cue that have only one track and one raw file, the situation is filteded above
        expected_raw_file = list(expected_raw_files)[0]
        if all(any(calculate_similarity(track['title'], raw_music_file.name) >= 0.6
            or track['title'] in raw_music_file.name
            for raw_music_file in raw_music_files) for track in tracks):
            files_need_to_delete.add(expected_raw_file) # if true, the combined file need to be deleted
            log(f"No need to split {cue}, all tracks are already cut into tracks, but the combined file need to be deleted", "INFO")
            continue
        # choose the best raw music file for the cue
        best_score = 0.0
        best_raw_music = None
        for raw_music in raw_music_files:
            score = calculate_similarity(raw_music.name, expected_raw_file.name)
            if score > best_score:
                best_score = score
                best_raw_music = raw_music
        if best_raw_music is not None and best_score >= 0.8:
            matched[best_raw_music].append((album_title, album_performer, tracks))
            log(f"Matched {cue} to {best_raw_music}", "INFO")
    # delete the raw music files that are not matched in the matched dict
    matched = {raw_music: lst for raw_music, lst in matched.items() if len(lst) > 0}
    final_matched: Dict[Path, Tuple[str, str, List[Dict[str, Any]]]] = dict()
    for raw_music, lst in matched.items():
        if len(lst) == 1:
            final_matched[raw_music] = lst[0]
        else:
            # choose the best cue file for the raw music
            best_score = 0.0
            best_title = None
            best_performer = None
            for album_title, album_performer, tracks in lst:
                score = calculate_similarity(album_title, raw_music.name)
                if score > best_score:
                    best_score = score
                    best_title = album_title
                    best_performer = album_performer
            assert best_title is not None
            assert best_performer is not None
            final_matched[raw_music] = (best_title, best_performer, tracks)
    return final_matched, files_need_to_delete

def split_audio(album: Path, raw_audio: Path, album_title: str, album_performer: str, tracks: List[Dict[str, Any]], output_dir: Path, progress: Progress) -> None:
    ffmpeg_cmds = []
    original_cwe = os.getcwd()
    working_dir = album.parent
    try:
        os.chdir(working_dir)
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
                "ffmpeg",
                "-threads", str(FFMPEG_INNER_THREADS),
                "-y",
                "-loglevel", "error",
                "-i", str(raw_audio),
                "-ss", str(track['start_time']),
            ]
            if track['end_time'] is not None:
                duration = track['end_time'] - track['start_time']
                cmd.extend(["-t", str(duration)])
            cmd.extend([
                "-vn",
                "-c:a", "flac",
                "-compression_level", "8",
            ])
            if track['title']:
                cmd.extend(["-metadata", f"title={track['title']}"])
            if track['performer']:
                cmd.extend(["-metadata", f"artist={track['performer']}"])
            cmd.extend(["-metadata", f"track={track['track_num']}"])
            if album_title:
                cmd.extend(["-metadata", f"album={album_title}"])
            if album_performer:
                cmd.extend(["-metadata", f"album_artist={album_performer}"])
            cmd.append(str(output_path))
            ffmpeg_cmds.append(cmd)
        run_with_threads(ffmpeg_cmds, "Splitting tracks", progress)
    finally:
        os.chdir(original_cwe)

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
    if enc == "utf-8":
        return True
    log(f"Fixing {cue} encoding ({enc} → utf-8)")
    if not DRY_RUN:
        normalized = text.replace("\r\n","\n").replace("\r","\n")
        cue.write_text(normalized, encoding="utf-8")
    return True

def read_cue_text(cue: Path) -> str:
    text = cue.read_text(encoding="utf-8-sig")
    result = ""
    for line in text.splitlines():
        if "REM " in line:
            continue
        result += line + "\n"
    return result

def parse_cue_file(cue: Path)-> Tuple[str, str, List[Dict[str, Any]]]:
    text = read_cue_text(cue)
    album_title: str | None = None
    album_performer: str | None = None
    tracks: List[Dict[str, Any]] = []
    try:
        cue_data = ct.loads(text)
        if cue_data.title:
            album_title = cue_data.title
        if cue_data.performer:
            album_performer = cue_data.performer
        for track in cue_data.tracks:
            data = {
                'file' : track.file,
                'track_num' : track.track,
                'title' : track.title,
                'performer' : track.performer,
                'start_time' : track.index01.seconds,
                'end_time' : None
            }
            tracks.append(data)
        for i in range(len(tracks) - 1):
            tracks[i]['end_time'] = tracks[i + 1]['start_time']
    except Exception as e:
        log(f"Failed to parse {cue} with cue tools ({e!s}), fallback to manul parsering", "WARNING")
        FILE_RE = re.compile(r'^FILE\s+"(.+?)"\s+', re.IGNORECASE)
        TRACK_RE = re.compile(r'^TRACK\s+(\d+)\s+', re.IGNORECASE)
        TITLE_RE = re.compile(r'^TITLE\s+"(.+?)"', re.IGNORECASE)
        PERFORMER_RE = re.compile(r'^PERFORMER\s+"(.+?)"', re.IGNORECASE)
        INDEX_RE = re.compile(r'^INDEX\s+(\d+)\s+(\d+):(\d+):(\d+)', re.IGNORECASE)
        current_track = None
        current_performer = None
        current_file = None
        for line in text.splitlines():
            line = line.strip()
            m = PERFORMER_RE.match(line)
            if m:
                current_performer = m.group(1)
                if album_performer is None:
                    album_performer = current_performer
            m = FILE_RE.match(line)
            if m:
                current_file = Path(m.group(1))
            m = TRACK_RE.match(line)
            if m:
                if current_track:
                    tracks.append(current_track)
                track_num = int(m.group(1))
                current_track = {
                    'file': current_file,
                    'track_num': track_num,
                    'title': None,
                    'performer': current_performer,
                    'start_time': None,
                    'end_time': None
                }
            m = TITLE_RE.match(line)
            if m:
                if album_title is None:
                    album_title = m.group(1)
                if current_track is not None:
                    current_track['title'] = m.group(1)
            m = INDEX_RE.match(line)
            if m:
                if current_track is not None:
                    index_num = int(m.group(1))
                    minutes = int(m.group(2))
                    seconds = int(m.group(3))
                    frames = int(m.group(4))
                    total_seconds = minutes * 60 + seconds + frames / 75.0
                    current_track['start_time'] = total_seconds
        if current_track is not None:
            tracks.append(current_track)
        for i in range(len(tracks) - 1):
            tracks[i]['end_time'] = tracks[i + 1]['start_time']
    assert album_title is not None
    assert album_performer is not None
    return album_title, album_performer, tracks

# ================= wav to flac =================
def convert_musics_to_flac(files: List[Path], progress: Progress) -> None:
    cmds = []
    deleted_files = []
    for file in files:
        if file.with_suffix(".flac").exists():
            log(f"{file.with_suffix('.flac')} already exists", "WARNING")
            continue
        cmd = [
            "ffmpeg",
            "-threads", str(FFMPEG_INNER_THREADS),
            "-y",
            "-loglevel", "error",
            "-i", str(file),
            "-c:a", "flac",
            "-compression_level", "8",
            str(file.with_suffix(".flac")),
        ]
        cmds.append(cmd)
        deleted_files.append(file)
    if not DRY_RUN:
        run_with_threads(cmds, "Converting music files to flac", progress)
    if not DRY_RUN and ENABLE_DELETE:
        for file in deleted_files:
            file.unlink()

if __name__ == "__main__":
    main()