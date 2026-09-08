# -*- coding: utf-8 -*-
"""REST API 라우터 — 오디오 업로드 컨트롤러 및 조회 엔드포인트."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
)
from fastapi.responses import PlainTextResponse

from .. import pipeline, storage
from ..config import AUDIO_DIR, settings
from ..schemas import Job, JobStatus, ProcessResponse
from ..services import telegram

log = logging.getLogger("voicebrief.api")
router = APIRouter(prefix="/api", tags=["recordings"])

ALLOWED_EXT = {
    ".m4a", ".mp3", ".wav", ".webm", ".weba", ".ogg", ".opus",
    ".mp4", ".aac", ".flac", ".3gp", ".caf", ".amr",
}
CHUNK = 1024 * 1024  # 1MB


async def verify_token(authorization: Optional[str] = Header(None)) -> None:
    """API_TOKEN이 설정된 경우에만 Bearer 인증을 요구한다."""
    if not settings.api_token:
        return
    expected = f"Bearer {settings.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="인증 실패")


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in ALLOWED_EXT else ".webm"


@router.post("/recordings/process", response_model=ProcessResponse, status_code=202)
async def process_recording(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="녹음 오디오 (m4a/mp3/wav/webm)"),
    note: str = Form("", description="사용자 메모 / 고유명사 힌트(쉼표 구분)"),
    _: None = Depends(verify_token),
) -> ProcessResponse:
    """녹음 파일을 받아 즉시 202를 반환하고, 파이프라인은 백그라운드에서 실행한다.

    클라이언트는 job_id로 `GET /api/recordings/{job_id}`를 폴링해 진행 상태를 표시한다.
    """
    if not file.filename and not file.content_type:
        raise HTTPException(status_code=400, detail="오디오 파일이 없습니다.")

    job_id = f"{storage.now_kst().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    suffix = _safe_suffix(file.filename or "")
    dest = AUDIO_DIR / f"{job_id}{suffix}"

    # 스트리밍 저장 (대용량 녹음이 메모리를 잡아먹지 않도록)
    limit = settings.max_upload_mb * 1024 * 1024
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"파일이 너무 큽니다. (최대 {settings.max_upload_mb}MB)",
                    )
                out.write(chunk)
    finally:
        await file.close()

    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="빈 파일입니다. 녹음을 다시 시도하세요.")

    job = Job(
        id=job_id,
        filename=file.filename or dest.name,
        audio_path=str(dest),
        note=note or "",
        status=JobStatus.QUEUED,
    )
    await storage.create(job)
    log.info("업로드 수신: %s (%.1fMB)", job_id, size / 1024 / 1024)

    background.add_task(pipeline.run, job_id)
    return ProcessResponse(job_id=job_id, status=job.status, status_label=job.status_label)


@router.get("/recordings")
async def list_recordings(limit: int = 30, _: None = Depends(verify_token)):
    """최근 요약 기록 목록."""
    return {"items": await storage.list_recent(limit=limit)}


@router.get("/recordings/{job_id}")
async def get_recording(job_id: str, _: None = Depends(verify_token)):
    """작업 상세 + 진행 상태 (프론트 폴링용)."""
    job = await storage.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return job


@router.get("/recordings/{job_id}/markdown", response_class=PlainTextResponse)
async def get_markdown(job_id: str, _: None = Depends(verify_token)):
    job = await storage.get(job_id)
    if not job or not job.markdown:
        raise HTTPException(status_code=404, detail="아직 보고서가 준비되지 않았습니다.")
    return job.markdown


@router.post("/recordings/{job_id}/resend")
async def resend(job_id: str, _: None = Depends(verify_token)):
    """전송이 실패한 작업 재발송."""
    try:
        results = await pipeline.deliver_again(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"deliveries": [r.model_dump() for r in results]}


@router.delete("/recordings/{job_id}")
async def delete_recording(job_id: str, _: None = Depends(verify_token)):
    ok = await storage.delete(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return {"deleted": job_id}


@router.get("/config/telegram-chat-id")
async def telegram_chat_id(_: None = Depends(verify_token)):
    """TELEGRAM_CHAT_ID를 모를 때 봇에 /start 후 호출하면 알려준다."""
    return {"hint": await telegram.get_chat_id_hint()}


@router.get("/health")
async def health():
    return {"status": "ok", "services": settings.health()}
