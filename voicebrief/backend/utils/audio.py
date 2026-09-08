# -*- coding: utf-8 -*-
"""오디오 파일 유틸 — 포맷 정규화 및 길이 측정.

브라우저 MediaRecorder는 대부분 `audio/webm;codecs=opus`(크롬/안드로이드) 또는
`audio/mp4`(iOS 사파리)로 녹음한다. CLOVA Speech는 webm을 받지 않으므로
ffmpeg으로 mp3(모노 16kHz)로 변환한다. ffmpeg이 없으면 원본을 그대로 시도한다.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional, Tuple

from ..config import settings

# CLOVA Speech가 직접 수용하는 확장자
SUPPORTED_EXT = {".mp3", ".aac", ".ac3", ".ogg", ".flac", ".wav", ".m4a", ".mp4"}
NEEDS_CONVERT_EXT = {".webm", ".weba", ".opus", ".3gp", ".amr", ".caf"}


def has_ffmpeg() -> bool:
    return shutil.which(settings.ffmpeg_bin) is not None


async def _run(cmd: list[str], timeout: int = 600) -> Tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("오디오 처리 시간이 초과되었습니다.")
    return proc.returncode or 0, stdout, stderr


async def probe_duration_ms(path: Path) -> int:
    """ffprobe로 길이(ms)를 구한다. 실패하면 0."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0
    code, stdout, _ = await _run(
        [
            ffprobe, "-v", "quiet", "-print_format", "json",
            "-show_format", str(path),
        ],
        timeout=60,
    )
    if code != 0:
        return 0
    try:
        info = json.loads(stdout.decode("utf-8", "replace"))
        return int(float(info["format"]["duration"]) * 1000)
    except (ValueError, KeyError, TypeError):
        return 0


async def ensure_supported_audio(path: Path) -> Tuple[Path, Optional[str]]:
    """STT에 보낼 수 있는 파일 경로를 반환.

    Returns: (사용할 경로, 경고 메시지 or None)
    """
    ext = path.suffix.lower()
    if ext in SUPPORTED_EXT:
        return path, None

    if not has_ffmpeg():
        return path, (
            f"'{ext}' 포맷이며 ffmpeg이 설치되어 있지 않아 원본 그대로 STT에 전달합니다. "
            "실패 시 ffmpeg을 설치하세요."
        )

    target = path.with_suffix(".mp3")
    code, _, stderr = await _run(
        [
            settings.ffmpeg_bin, "-y", "-i", str(path),
            "-vn",                 # 비디오 트랙 제거
            "-ac", "1",            # 모노 (화자 분리 정확도에 유리)
            "-ar", "16000",        # 16kHz
            "-b:a", "64k",
            str(target),
        ],
        timeout=900,
    )
    if code != 0 or not target.exists():
        detail = stderr.decode("utf-8", "replace")[-500:]
        raise RuntimeError(f"오디오 변환(ffmpeg) 실패: {detail}")
    return target, None


def human_duration(ms: int) -> str:
    if ms <= 0:
        return "미상"
    total = ms // 1000
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    if h:
        return f"{h}시간 {m}분 {s}초"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"
