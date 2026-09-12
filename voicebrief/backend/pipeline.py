# -*- coding: utf-8 -*-
"""녹음 처리 파이프라인 오케스트레이터.

업로드 → STT → LLM 요약 → 렌더링 → 발송(메일/텔레그램/카카오)
각 단계마다 Job 상태를 갱신해 클라이언트가 진행률을 폴링할 수 있게 한다.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from . import storage
from .config import settings
from .schemas import DeliveryResult, JobStatus
from .services import kakao, llm, mailer, renderer, stt_clova, telegram
from .utils.audio import ensure_supported_audio, human_duration, probe_duration_ms

log = logging.getLogger("voicebrief.pipeline")


async def run(job_id: str) -> None:
    """백그라운드 태스크 진입점. 예외를 밖으로 던지지 않는다."""
    job = await storage.get(job_id)
    if not job or not job.audio_path:
        log.error("작업을 찾을 수 없음: %s", job_id)
        return

    audio_path = Path(job.audio_path)
    try:
        # ── 1) 오디오 정규화 ────────────────────────────────────────────
        await storage.update(job_id, status=JobStatus.TRANSCRIBING)
        usable_path, warning = await ensure_supported_audio(audio_path)
        if warning:
            log.warning("[%s] %s", job_id, warning)

        duration_ms = await probe_duration_ms(usable_path)
        if duration_ms:
            await storage.update(job_id, duration_ms=duration_ms)

        # ── 2) STT ──────────────────────────────────────────────────────
        transcript = await stt_clova.transcribe(usable_path, note=job.note)
        if not transcript.duration_ms and duration_ms:
            transcript.duration_ms = duration_ms
        await storage.update(
            job_id,
            transcript=transcript,
            duration_ms=transcript.duration_ms or duration_ms,
            status=JobStatus.SUMMARIZING,
        )
        log.info("[%s] STT 완료: %d자 / 화자 %d명", job_id, len(transcript.full_text),
                 len(transcript.speakers))

        # ── 3) LLM 분류 + 구조화 요약 ──────────────────────────────────
        job = await storage.get(job_id)
        recorded_at = (job.created_at if job else "") or ""
        summary = await llm.summarize(transcript, recorded_at=recorded_at, note=job.note if job else "")

        markdown = renderer.render_markdown(
            summary, transcript=transcript, recorded_at=recorded_at
        )
        await storage.update(
            job_id, summary=summary, markdown=markdown, status=JobStatus.DELIVERING
        )
        log.info("[%s] 요약 완료: %s / %s", job_id, summary.category_label, summary.title)

        # ── 4) 발송 ─────────────────────────────────────────────────────
        deliveries = await _deliver(summary, transcript, markdown, recorded_at)
        await storage.update(job_id, deliveries=deliveries, status=JobStatus.DONE)
        log.info("[%s] 완료. 발송 결과: %s", job_id,
                 [(d.channel, d.ok) for d in deliveries])

        # ── 5) 정리 ─────────────────────────────────────────────────────
        if not settings.keep_audio:
            for path in {audio_path, usable_path}:
                path.unlink(missing_ok=True)

    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 Job에 기록하고 종료
        log.error("[%s] 파이프라인 실패: %s\n%s", job_id, exc, traceback.format_exc())
        await storage.update(
            job_id, status=JobStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
        )


async def _deliver(summary, transcript, markdown: str, recorded_at: str) -> list[DeliveryResult]:
    results: list[DeliveryResult] = []

    subject = f"[{summary.category_label}] {summary.title}"
    html_body = renderer.render_email_html(summary, transcript, recorded_at)
    results.append(
        await mailer.send_report(
            subject=subject,
            html_body=html_body,
            markdown_body=markdown,
            title=summary.title,
        )
    )

    messenger_text = renderer.render_messenger_text(summary)
    results.append(await telegram.send_message(messenger_text))

    # 전체 스크립트는 파일로 함께 전송(선택)
    if settings.telegram_ready:
        results.append(
            await telegram.send_document(
                filename=f"{mailer.safe_filename(summary.title)}.md",
                content=markdown,
                caption=f"{summary.title} — 전체 보고서",
            )
        )

    if settings.kakao_ready:
        results.append(await kakao.send_memo(f"[{summary.category_label}] {summary.title}\n{summary.one_liner}"))

    return results


async def deliver_again(job_id: str) -> list[DeliveryResult]:
    """이미 요약된 작업을 다시 발송(전송 실패 재시도용)."""
    job = await storage.get(job_id)
    if not job or not job.summary:
        raise ValueError("요약이 완료된 작업이 아닙니다.")
    results = await _deliver(job.summary, job.transcript, job.markdown, job.created_at)
    await storage.update(job_id, deliveries=results, status=JobStatus.DONE)
    return results


def summarize_status(job) -> str:
    """로그/디버그용 한 줄 요약."""
    return (
        f"{job.id} [{job.status_label}] {job.filename} "
        f"({human_duration(job.duration_ms)})"
    )
