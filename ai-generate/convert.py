from pathlib import Path
from typing import Optional, List, Tuple
import subprocess
import re
import sys
import io
from difflib import SequenceMatcher

# Configure UTF-8 encoding for Windows console
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7, use alternative method
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ================= 配置 =================

ROOT_DIR = Path(__file__).parent   # 根目录 - 使用脚本所在目录
DRY_RUN = False                 # True = 不实际修改文件
ENABLE_SPLIT = True            # 是否切轨
ENABLE_DELETE = True           # 是否删除整轨

# ================= SACD 配置 =================

SACD_EXTRACT = Path(r"F:\soft\sacd_extract\sacd_extract.exe")
ENABLE_SACD = True

SACD_MODE = "stereo"     # stereo / multichannel / both
SACD_PCM_RATE = 88200    # 88200 or 176400
SACD_PCM_FMT = "s32"     # s32 recommended

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

# ================= 工具函数 =================

def safe_print(*args, **kwargs):
    """安全打印函数，处理Unicode编码问题"""
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

def run(cmd: List[str]):
    try:
        print("[CMD]", " ".join(cmd))
    except UnicodeEncodeError:
        # Fallback if console can't handle Unicode
        print("[CMD]", " ".join(cmd).encode('ascii', 'replace').decode('ascii'))
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

def fix_cue_encoding(cue: Path) -> bool:
    text, enc = read_cue_with_encoding(cue)
    if text is None:
        print(f"[FAIL] Cannot decode {cue}")
        return False

    if enc.startswith("utf-8"):
        print(f"[OK] UTF-8 already: {cue}")
        return True

    print(f"[FIX] {cue} ({enc} → utf-8)")
    if not DRY_RUN:
        # Normalize line endings to Unix style and write
        normalized_text = text.replace('\r\n', '\n').replace('\r', '\n')
        cue.write_text(normalized_text, encoding="utf-8")
    return True

def extract_audio_from_cue(cue: Path) -> Optional[Path]:
    text = cue.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        m = FILE_RE.match(line.strip())
        if m:
            return cue.parent / m.group(1)
    return None

def parse_cue_file(cue: Path) -> Optional[List[dict]]:
    """
    解析 CUE 文件，返回轨道信息列表
    每个轨道包含: track_num, title, performer, start_time, end_time
    时间格式: 秒（浮点数）
    """
    text = cue.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    
    tracks = []
    current_track = None
    current_performer = None
    
    for line in lines:
        line_stripped = line.strip()
        
        # 提取 PERFORMER（可能在 TRACK 之前）
        m = PERFORMER_RE.match(line)
        if m:
            current_performer = m.group(1)
        
        # 提取 TRACK
        m = TRACK_RE.match(line)
        if m:
            # 保存上一个轨道
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
        
        # 提取 TITLE
        if current_track:
            m = TITLE_RE.match(line)
            if m:
                current_track['title'] = m.group(1)
            
            # 提取 PERFORMER（在 TRACK 内部）
            m = PERFORMER_RE.match(line)
            if m:
                current_track['performer'] = m.group(1)
            
            # 提取 INDEX（开始时间，优先使用 INDEX 01）
            m = INDEX_RE.match(line)
            if m:
                index_num = int(m.group(1))
                minutes = int(m.group(2))
                seconds = int(m.group(3))
                frames = int(m.group(4))
                # CUE 格式：MM:SS:FF，FF 是帧（1秒=75帧）
                total_seconds = minutes * 60 + seconds + frames / 75.0
                # 只使用 INDEX 01 作为开始时间（INDEX 00 是预间隙）
                if index_num == 1:
                    current_track['start_time'] = total_seconds
    
    # 添加最后一个轨道
    if current_track:
        tracks.append(current_track)
    
    # 计算每个轨道的结束时间（下一个轨道的开始时间）
    for i in range(len(tracks)):
        if i < len(tracks) - 1:
            tracks[i]['end_time'] = tracks[i + 1]['start_time']
    
    return tracks if tracks else None

def cue_time_to_seconds(time_str: str) -> float:
    """将 CUE 时间格式 (MM:SS:FF) 转换为秒"""
    parts = time_str.split(':')
    if len(parts) == 3:
        minutes = int(parts[0])
        seconds = int(parts[1])
        frames = int(parts[2])
        return minutes * 60 + seconds + frames / 75.0
    return 0.0

def calculate_similarity(str1: str, str2: str) -> float:
    """计算两个字符串的相似度，返回 0.0 到 1.0 之间的值"""
    return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

def sanitize_filename(filename: str) -> str:
    """清理文件名，移除非法字符并处理Unicode字符"""
    # 移除Windows非法字符
    illegal_chars = r'[<>:"/\\|?*]'
    safe = re.sub(illegal_chars, '_', filename)
    
    # 尝试编码为ASCII，如果不能编码则使用Unicode转义或移除
    try:
        # 先尝试直接编码（如果全是ASCII）
        safe.encode('ascii')
        return safe
    except UnicodeEncodeError:
        # 如果有非ASCII字符，尝试使用UTF-8编码的十六进制表示
        # 或者简单地保留Unicode字符（现代Windows支持UTF-8文件名）
        # 为了兼容性，我们保留Unicode但确保没有非法字符
        return safe

def get_audio_files(folder: Path) -> List[Path]:
    """获取文件夹中所有的音频文件"""
    audio_files = []
    for ext in ['*.flac', '*.wav', '*.ape', '*.wv']:
        audio_files.extend(folder.glob(ext))
    return audio_files

def analyze_cue_audio_mapping(cues: List[Path], folder: Path) -> Tuple[dict, dict]:
    """
    分析 CUE 文件和音频文件的映射关系
    返回:
    - cue_to_audio: {cue_path: audio_path} 每个 CUE 对应的音频文件
    - audio_to_cues: {audio_path: [cue_paths]} 每个音频文件对应的 CUE 文件列表
    """
    cue_to_audio = {}
    audio_to_cues = {}
    
    for cue in cues:
        audio_from_cue = extract_audio_from_cue(cue)
        if audio_from_cue and audio_from_cue.exists():
            audio_path = audio_from_cue.resolve()
            cue_to_audio[cue] = audio_path
            if audio_path not in audio_to_cues:
                audio_to_cues[audio_path] = []
            audio_to_cues[audio_path].append(cue)
    
    return cue_to_audio, audio_to_cues

def split_audio(cue: Path, audio: Path, output_dir: Optional[Path] = None) -> bool:
    """
    切分音频文件
    output_dir: 输出目录，如果为 None 则输出到 cue 所在目录
    """
    print(f"[SPLIT] {audio}")
    
    # 解析 CUE 文件
    tracks = parse_cue_file(cue)
    if not tracks:
        print(f"[FAIL] Failed to parse CUE file: {cue}")
        return False
    
    # 使用相对路径，相对于 CUE 文件所在目录
    cue_dir = cue.parent
    if output_dir is None:
        output_dir = cue_dir
    else:
        # 确保输出目录存在
        if not DRY_RUN:
            output_dir.mkdir(parents=True, exist_ok=True)
    
    import os
    original_cwd = os.getcwd()
    
    try:
        os.chdir(cue_dir)
        audio_path = str(audio.relative_to(cue_dir))
        
        # 为每个轨道切分
        for i, track in enumerate(tracks):
            if track['start_time'] is None:
                print(f"[SKIP] Track {track['track_num']} has no start time")
                continue
            
            # 生成输出文件名
            track_num_str = f"{track['track_num']:02d}"
            title = track['title'] or f"Track {track['track_num']}"
            # 清理文件名中的非法字符
            safe_title = sanitize_filename(title)
            output_filename = f"{track_num_str} - {safe_title}.flac"
            
            if output_dir != cue_dir:
                output_path = output_dir / output_filename
                output_path_str = str(output_path.relative_to(cue_dir))
            else:
                output_path_str = output_filename
            
            # 构建 ffmpeg 命令
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-i", audio_path,
                "-ss", str(track['start_time']),
            ]
            
            # 如果有结束时间，添加 -to 参数
            if track['end_time'] is not None:
                duration = track['end_time'] - track['start_time']
                cmd.extend(["-t", str(duration)])
            
            # 添加元数据和编码参数
            cmd.extend([
                "-vn",
                "-c:a", "flac",
                "-compression_level", "8",
            ])
            
            # 添加元数据
            if track['title']:
                cmd.extend(["-metadata", f"title={track['title']}"])
            if track['performer']:
                cmd.extend(["-metadata", f"artist={track['performer']}"])
            cmd.extend(["-metadata", f"track={track['track_num']}"])
            
            cmd.append(output_path_str)
            
            safe_print(f"[TRACK] {track_num_str}: {title}")
            try:
                run(cmd)
            except subprocess.CalledProcessError as e:
                print(f"[FAIL] Failed to split track {track['track_num']}: {e}")
                return False
        
        return True
    finally:
        os.chdir(original_cwd)

def verify_split(folder: Path) -> bool:
    """验证切分后的文件是否存在"""
    tracks = list(folder.glob("01*.flac"))
    if not tracks:
        # 也检查子文件夹
        for subdir in folder.iterdir():
            if subdir.is_dir():
                tracks = list(subdir.glob("01*.flac"))
                if tracks:
                    return True
        print(f"[FAIL] No split tracks found in {folder}")
        return False
    return True

def delete_audio(audio: Path):
    print(f"[DEL] {audio}")
    if not DRY_RUN:
        audio.unlink()

# ================= 主流程 =================

def process_cue(cue: Path, output_dir: Optional[Path] = None):
    """
    处理单个 CUE 文件
    output_dir: 输出目录，如果为 None 则输出到 cue 所在目录
    """
    print(f"\n=== Processing {cue} ===")

    # ① 修复编码
    if not fix_cue_encoding(cue):
        return

    # ② 提取整轨文件
    audio = extract_audio_from_cue(cue)
    if not audio or not audio.exists():
        print(f"[MISS] Audio file not found for {cue}")
        return

    # ③ 切轨
    if ENABLE_SPLIT:
        if not split_audio(cue, audio, output_dir):
            return

    # ④ 校验
    check_dir = output_dir if output_dir else cue.parent
    if not verify_split(check_dir):
        return

    # ⑤ 删除整轨
    if ENABLE_DELETE:
        delete_audio(audio)

# ================= SACD 处理 =================

def sacd_iso_to_dsf_inplace(iso: Path) -> List[Path]:
    """
    将 SACD ISO 提取为分轨 DSF
    兼容 sacd_extract 的所有目录结构：
      iso.parent/
        album_name/
          stereo/*.dsf
          multichannel/*.dsf
    最终将所有 DSF 扁平化移动到 iso.parent
    """
    base_dir = iso.parent

    cmd = [
        str(SACD_EXTRACT),
        "-i", str(iso),
        "-s",       # stereo
        "-p",       # split tracks
        "-2",       # DSF
        "-o", str(base_dir),
    ]

    run(cmd)

    # 递归查找所有 DSF（排除 base_dir 本身已有的）
    dsf_files = list(base_dir.rglob("*.dsf"))
    if not dsf_files:
        raise RuntimeError("No DSF files generated")

    final_dsf_files: List[Path] = []

    for dsf in sorted(dsf_files):
        target = base_dir / dsf.name
        if dsf.resolve() == target.resolve():
            # 已经在目标位置
            final_dsf_files.append(target)
            continue

        print(f"[MOVE] {dsf} → {target}")
        if not DRY_RUN:
            target.unlink(missing_ok=True)
            dsf.replace(target)
        final_dsf_files.append(target)

    # 清理空目录（自底向上）
    for path in sorted(base_dir.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
                print(f"[DEL] {path}")
            except OSError:
                pass

    return final_dsf_files

def dsf_to_flac_inplace(dsf_files: List[Path]):
    for dsf in dsf_files:
        flac = dsf.with_suffix(".flac")

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-i", str(dsf),
            "-af", f"aformat=sample_fmts={SACD_PCM_FMT}:sample_rates={SACD_PCM_RATE}",
            "-c:a", "flac",
            "-compression_level", "8",
            str(flac),
        ]

        safe_print(f"[SACD] {dsf.name} → {flac.name}")
        run(cmd)

def process_sacd_iso_inplace(iso: Path):
    print(f"\n=== Processing SACD ISO (in-place): {iso.name} ===")

    dsf_files = sacd_iso_to_dsf_inplace(iso)
    dsf_to_flac_inplace(dsf_files)

    if ENABLE_DELETE:
        print(f"[DEL] {iso.name}")
        if not DRY_RUN:
            iso.unlink()

        for dsf in dsf_files:
            print(f"[DEL] {dsf.name}")
            if not DRY_RUN:
                dsf.unlink()

def main():
    album_dirs = [d for d in ROOT_DIR.iterdir() if d.is_dir()]
    print(f"Found {len(album_dirs)} top-level folders\n")

    for album in album_dirs:
        print(f"\n######## Folder: {album} ########")
        
        # ===== SACD ISO 处理 =====
        # ===== SACD ISO 优先处理 =====
        if ENABLE_SACD:
            # 递归查找所有子文件夹中的 ISO 文件
            isos = list(album.rglob("*.iso"))
            if isos:
                print(f"[SACD] Found {len(isos)} ISO file(s) in {album.name}, processing all")
                for iso in sorted(isos):
                    try:
                        process_sacd_iso_inplace(iso)
                    except Exception as e:
                        print(f"[ERROR] SACD {iso}: {e}", file=sys.stderr)
                continue   # ⭐ 关键：有 ISO 就不走 CUE 逻辑
        
        # 按子文件夹分组处理
        subfolders = {}
        for cue in album.rglob("*.cue"):
            folder = cue.parent
            if folder not in subfolders:
                subfolders[folder] = []
            subfolders[folder].append(cue)
        
        if not subfolders:
            print("No cue files, skip")
            continue
        
        for folder, cues in subfolders.items():
            print(f"\n--- Processing folder: {folder.name} ---")
            
            # 获取音频文件
            audio_files = get_audio_files(folder)
            
            # 情况1: 只有一个 CUE 和一个音频文件，且对应
            if len(cues) == 1 and len(audio_files) == 1:
                cue = cues[0]
                audio_from_cue = extract_audio_from_cue(cue)
                if audio_from_cue and audio_from_cue.exists() and audio_from_cue.resolve() == audio_files[0].resolve():
                    print(f"[CASE 1] Single CUE and audio file match, processing directly")
                    try:
                        process_cue(cue)
                    except Exception as e:
                        print(f"[ERROR] {cue}: {e}", file=sys.stderr)
                    continue
            
            # 情况2: 有多个 CUE 文件
            if len(cues) > 1:
                # 分析映射关系
                cue_to_audio, audio_to_cues = analyze_cue_audio_mapping(cues, folder)
                
                # 情况2.2: 检查是否有音频文件对应多个 CUE
                conflicting_audios = {audio: cue_list for audio, cue_list in audio_to_cues.items() if len(cue_list) > 1}
                
                if conflicting_audios:
                    print(f"[CASE 2.2] Found {len(conflicting_audios)} audio file(s) with multiple CUE files")
                    # 对于每个冲突的音频文件，选择最相似的 CUE
                    cues_to_process = []
                    cues_to_skip = set()
                    
                    for audio, cue_list in conflicting_audios.items():
                        print(f"[CONFLICT] Audio: {audio.name} has {len(cue_list)} CUE files")
                        best_cue = None
                        best_score = 0.0
                        
                        for cue in cue_list:
                            cue_stem = cue.stem.lower()
                            audio_stem = audio.stem.lower()
                            score = calculate_similarity(cue_stem, audio_stem)
                            print(f"  {cue.name}: score={score:.3f}")
                            
                            if score > best_score:
                                best_score = score
                                best_cue = cue
                        
                        if best_cue:
                            print(f"[SELECT] Chose {best_cue.name} (score={best_score:.3f})")
                            cues_to_process.append(best_cue)
                            # 标记其他 CUE 为跳过
                            for cue in cue_list:
                                if cue != best_cue:
                                    cues_to_skip.add(cue)
                    
                    # 处理选中的 CUE（情况2.1：每个 CUE 对应不同音频文件）
                    for cue in cues:
                        if cue in cues_to_skip:
                            continue
                        if cue not in cues_to_process and cue in cue_to_audio:
                            # 这个 CUE 有对应的音频文件，且没有冲突
                            cues_to_process.append(cue)
                    
                    # 如果多个 CUE 需要处理，创建子文件夹
                    if len(cues_to_process) > 1:
                        print(f"[CASE 2.1] Processing {len(cues_to_process)} CUE files, creating subfolders")
                        for cue in cues_to_process:
                            audio = cue_to_audio.get(cue)
                            if audio:
                                # 创建以 CUE 文件名命名的子文件夹
                                subfolder = folder / cue.stem
                                print(f"[SUBFOLDER] Creating subfolder: {subfolder.name}")
                                try:
                                    process_cue(cue, subfolder)
                                except Exception as e:
                                    print(f"[ERROR] {cue}: {e}", file=sys.stderr)
                    elif len(cues_to_process) == 1:
                        # 只有一个 CUE 需要处理，直接处理
                        for cue in cues_to_process:
                            try:
                                process_cue(cue)
                            except Exception as e:
                                print(f"[ERROR] {cue}: {e}", file=sys.stderr)
                else:
                    # 情况2.1: 每个 CUE 对应不同的音频文件
                    print(f"[CASE 2.1] Each CUE file has its own audio file, processing all")
                    if len(cue_to_audio) > 1:
                        # 多个 CUE，创建子文件夹
                        for cue, audio in cue_to_audio.items():
                            subfolder = folder / cue.stem
                            print(f"[SUBFOLDER] Creating subfolder: {subfolder.name}")
                            try:
                                process_cue(cue, subfolder)
                            except Exception as e:
                                print(f"[ERROR] {cue}: {e}", file=sys.stderr)
                    else:
                        # 只有一个 CUE 有对应音频，直接处理
                        for cue, audio in cue_to_audio.items():
                            try:
                                process_cue(cue)
                            except Exception as e:
                                print(f"[ERROR] {cue}: {e}", file=sys.stderr)
            else:
                # 其他情况：单个 CUE 文件
                for cue in cues:
                    try:
                        process_cue(cue)
                    except Exception as e:
                        print(f"[ERROR] {cue}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
