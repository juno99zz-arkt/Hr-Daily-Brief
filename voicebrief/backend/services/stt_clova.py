# -*- coding: utf-8 -*-
"""Naver CLOVA Speech (Long Sentence) 연동 모듈.

엔드포인트: POST {INVOKE_URL}/recognizer/upload
- 헤더 `X-CLOVASPEECH-API-KEY` 에 Secret Key
- multipart/form-data 로 `media`(오디오) + `params`(JSON 문자열) 전송
- `completion: sync` 로 요청하면 인식이 끝날 때까지 응답을 붙들고 있다가 결과를 준다.

응답 예시(발췌):
{
  "result": "COMPLETED",
  "text": "전체 텍스트",
  "segments": [
    {"start": 0, "end": 4120, "text": "안녕하세요",
     "speaker": {"label": "1", "name": "A"}, "confidence": 0.98}
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import httpx

from ..config import settings
from ..schemas import Segment, Transcript

MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}


class SttError(RuntimeError):
    pass


def _build_params(note: str = "") -> dict:
    params: dict = {
        "language": settings.clova_language,
        "completion": "sync",
        "wordAlignment": True,
        "fullText": True,
        "resultToObs": False,
        "noiseFiltering": True,
        "format": "JSON",
    }
    if settings.clova_diarization:
        params["diarization"] = {
            "enable": True,
            "speakerCountMin": max(1, settings.clova_speaker_min),
            "speakerCountMax": max(settings.clova_speaker_min, settings.clova_speaker_max),
        }
    else:
        params["diarization"] = {"enable": False}

    # 사용자가 메모에 고유명사를 적어두면 인식 정확도가 올라간다(부스팅).
    boostings = [w.strip() for w in note.split(",") if 1 < len(w.strip()) <= 20]
    if boostings:
        params["boostings"] = [{"words": ",".join(boostings[:100])}]
    return params


def _speaker_name(raw: dict) -> str:
    speaker = raw.get("speaker") or {}
    name = (speaker.get("name") or "").strip()
    label = str(speaker.get("label") or "").strip()
    if name and name.lower() not in ("unknown", "none"):
        return name if name.startswith("화자") else f"화자{label or name}"
    return f"화자{label}" if label else "화자1"


def parse_clova_response(payload: dict) -> Transcript:
    """CLOVA 응답 → 내부 Transcript 모델. (네트워크 없이 단위 테스트 가능)"""
    result = (payload.get("result") or "").upper()
    if result and result not in ("COMPLETED", "SUCCEEDED"):
        raise SttError(f"CLOVA 인식 실패: result={result} message={payload.get('message')}")

    segments: list[Segment] = []
    for raw in payload.get("segments") or []:
        text = (raw.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start_ms=int(raw.get("start") or 0),
                end_ms=int(raw.get("end") or 0),
                speaker=_speaker_name(raw),
                text=text,
            )
        )

    full_text = (payload.get("text") or "").strip()
    if not full_text and segments:
        full_text = " ".join(seg.text for seg in segments)

    speakers: list[str] = []
    for seg in segments:
        if seg.speaker not in speakers:
            speakers.append(seg.speaker)

    duration = max((seg.end_ms for seg in segments), default=0)
    return Transcript(
        full_text=full_text,
        segments=segments,
        speakers=speakers,
        duration_ms=duration,
        engine="clova-speech",
    )


async def transcribe(audio_path: Path, note: str = "") -> Transcript:
    """오디오 파일을 CLOVA Speech로 전사한다."""
    if not settings.stt_ready:
        raise SttError(
            "CLOVA Speech 설정이 없습니다. CLOVA_SPEECH_API_KEY / CLOVA_SPEECH_INVOKE_URL 을 확인하세요."
        )
    if not audio_path.exists():
        raise SttError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

    url = settings.clova_invoke_url.rstrip("/") + "/recognizer/upload"
    headers = {
        "X-CLOVASPEECH-API-KEY": settings.clova_api_key,
        "Accept": "application/json",
    }
    mime = MIME_BY_EXT.get(audio_path.suffix.lower(), "application/octet-stream")
    params_json = json.dumps(_build_params(note), ensure_ascii=False)

    timeout = httpx.Timeout(settings.clova_timeout_sec, connect=30.0)
    with audio_path.open("rb") as fp:
        files = {
            "media": (audio_path.name, fp, mime),
            "params": (None, params_json, "application/json"),
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(url, headers=headers, files=files)
            except httpx.HTTPError as exc:
                raise SttError(f"CLOVA Speech 요청 실패: {exc}") from exc

    if resp.status_code >= 400:
        raise SttError(f"CLOVA Speech 오류 {resp.status_code}: {resp.text[:500]}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise SttError(f"CLOVA 응답 파싱 실패: {resp.text[:300]}") from exc

    transcript = parse_clova_response(payload)
    if not transcript.full_text:
        raise SttError("음성에서 텍스트를 추출하지 못했습니다. (무음이거나 인식 실패)")
    return transcript


async def check_connection() -> Optional[str]:
    """설정 점검용. 문제가 있으면 사유 문자열, 정상이면 None."""
    if not settings.stt_ready:
        return "CLOVA_SPEECH_API_KEY 또는 CLOVA_SPEECH_INVOKE_URL 미설정"
    return None
