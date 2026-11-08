# -*- coding: utf-8 -*-
"""작업(Job) 저장소.

개인용 앱이므로 DB 없이 JSON 파일로 저장한다(파일 1개 = 작업 1건).
메모리 캐시 + 파일 영속화 조합이라 서버 재시작 후에도 기록이 남는다.
나중에 규모가 커지면 이 모듈만 SQLite/Postgres로 교체하면 된다.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import RECORD_DIR
from .schemas import STATUS_LABEL, Job, JobStatus

KST = timezone(timedelta(hours=9))
_lock = asyncio.Lock()
_cache: Dict[str, Job] = {}


def now_kst() -> datetime:
    return datetime.now(KST)


def now_iso() -> str:
    return now_kst().isoformat(timespec="seconds")


def _path(job_id: str) -> Path:
    return RECORD_DIR / f"{job_id}.json"


def _persist(job: Job) -> None:
    _path(job.id).write_text(
        json.dumps(job.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def create(job: Job) -> Job:
    async with _lock:
        job.created_at = job.created_at or now_iso()
        job.updated_at = job.created_at
        _cache[job.id] = job
        _persist(job)
    return job


async def update(job_id: str, **fields) -> Optional[Job]:
    async with _lock:
        job = _cache.get(job_id) or _load_from_disk(job_id)
        if not job:
            return None
        for key, value in fields.items():
            setattr(job, key, value)
        if "status" in fields:
            job.status_label = STATUS_LABEL.get(job.status, job.status.value)
        job.updated_at = now_iso()
        _cache[job.id] = job
        _persist(job)
        return job


def _load_from_disk(job_id: str) -> Optional[Job]:
    path = _path(job_id)
    if not path.exists():
        return None
    try:
        job = Job.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 손상된 파일은 무시
        return None
    _cache[job.id] = job
    return job


async def get(job_id: str) -> Optional[Job]:
    async with _lock:
        return _cache.get(job_id) or _load_from_disk(job_id)


async def list_recent(limit: int = 30) -> List[dict]:
    async with _lock:
        files = sorted(
            RECORD_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:limit]
        jobs: List[Job] = []
        for path in files:
            job = _cache.get(path.stem) or _load_from_disk(path.stem)
            if job:
                jobs.append(job)
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [job.brief() for job in jobs]


async def delete(job_id: str) -> bool:
    async with _lock:
        _cache.pop(job_id, None)
        path = _path(job_id)
        if path.exists():
            path.unlink()
            return True
        return False


async def mark_stale_as_failed(max_age_minutes: int = 60) -> int:
    """서버가 처리 중 죽었을 때 남은 좀비 작업 정리 (기동 시 호출)."""
    running = {JobStatus.QUEUED, JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING, JobStatus.DELIVERING}
    cutoff = now_kst() - timedelta(minutes=max_age_minutes)
    fixed = 0
    for path in RECORD_DIR.glob("*.json"):
        job = _load_from_disk(path.stem)
        if not job or job.status not in running:
            continue
        try:
            updated = datetime.fromisoformat(job.updated_at)
        except (ValueError, TypeError):
            updated = cutoff - timedelta(minutes=1)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=KST)
        if updated < cutoff:
            job.status = JobStatus.FAILED
            job.status_label = STATUS_LABEL[JobStatus.FAILED]
            job.error = "서버 재시작으로 처리가 중단되었습니다."
            _persist(job)
            fixed += 1
    return fixed
