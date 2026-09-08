# -*- coding: utf-8 -*-
"""API / 파이프라인 데이터 모델."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """클라이언트 진행 표시기와 1:1 대응되는 상태값."""

    QUEUED = "queued"            # 업로드 완료, 대기 중
    TRANSCRIBING = "transcribing"  # 텍스트 변환 중
    SUMMARIZING = "summarizing"    # AI 요약 중
    DELIVERING = "delivering"      # 전송 중
    DONE = "done"                  # 전송 완료
    FAILED = "failed"


STATUS_LABEL = {
    JobStatus.QUEUED: "업로드 완료 · 대기 중",
    JobStatus.TRANSCRIBING: "텍스트 변환 중",
    JobStatus.SUMMARIZING: "AI 요약 중",
    JobStatus.DELIVERING: "전송 중",
    JobStatus.DONE: "전송 완료",
    JobStatus.FAILED: "실패",
}


class Category(str, Enum):
    MEETING = "meeting"      # 회의
    INTERVIEW = "interview"  # 면담/상담
    LECTURE = "lecture"      # 강연/세미나
    MEMO = "memo"            # 개인 메모


CATEGORY_LABEL = {
    Category.MEETING: "회의",
    Category.INTERVIEW: "면담/상담",
    Category.LECTURE: "강연/세미나",
    Category.MEMO: "개인 메모",
}


# ── STT 결과 ────────────────────────────────────────────────────────────────

class Segment(BaseModel):
    start_ms: int = 0
    end_ms: int = 0
    speaker: str = "화자1"
    text: str = ""

    @property
    def timestamp(self) -> str:
        total = self.start_ms // 1000
        return f"{total // 60:02d}:{total % 60:02d}"


class Transcript(BaseModel):
    full_text: str = ""
    segments: List[Segment] = Field(default_factory=list)
    speakers: List[str] = Field(default_factory=list)
    duration_ms: int = 0
    engine: str = "clova"

    def to_dialogue(self, with_timestamp: bool = True) -> str:
        """LLM 입력용 화자 분리 스크립트."""
        if not self.segments:
            return self.full_text
        lines = []
        for seg in self.segments:
            prefix = f"[{seg.timestamp}] " if with_timestamp else ""
            lines.append(f"{prefix}{seg.speaker}: {seg.text}".strip())
        return "\n".join(lines)


# ── LLM 요약 결과 ───────────────────────────────────────────────────────────

class ActionItem(BaseModel):
    task: str
    owner: Optional[str] = None   # 원문에 없으면 None
    due: Optional[str] = None     # 원문에 없으면 None
    evidence: Optional[str] = None  # 근거 원문 인용


class Section(BaseModel):
    heading: str
    bullets: List[str] = Field(default_factory=list)


class Summary(BaseModel):
    category: Category = Category.MEMO
    category_reason: str = ""
    title: str = "제목 미상"
    one_liner: str = ""
    keywords: List[str] = Field(default_factory=list)
    participants: List[str] = Field(default_factory=list)
    sections: List[Section] = Field(default_factory=list)
    decisions: List[str] = Field(default_factory=list)
    action_items: List[ActionItem] = Field(default_factory=list)
    open_questions: List[str] = Field(default_factory=list)
    unclear_points: List[str] = Field(default_factory=list)

    @property
    def category_label(self) -> str:
        return CATEGORY_LABEL.get(self.category, "기타")


# ── 전송 결과 ───────────────────────────────────────────────────────────────

class DeliveryResult(BaseModel):
    channel: str
    ok: bool
    detail: str = ""


# ── 작업(Job) ───────────────────────────────────────────────────────────────

class Job(BaseModel):
    id: str
    status: JobStatus = JobStatus.QUEUED
    status_label: str = STATUS_LABEL[JobStatus.QUEUED]
    created_at: str = ""
    updated_at: str = ""
    filename: str = ""
    audio_path: Optional[str] = None
    duration_ms: int = 0
    note: str = ""              # 사용자가 남긴 메모/힌트
    error: Optional[str] = None
    transcript: Optional[Transcript] = None
    summary: Optional[Summary] = None
    markdown: str = ""
    deliveries: List[DeliveryResult] = Field(default_factory=list)

    def brief(self) -> dict:
        """목록 뷰용 경량 표현."""
        return {
            "id": self.id,
            "status": self.status,
            "status_label": self.status_label,
            "created_at": self.created_at,
            "title": self.summary.title if self.summary else self.filename,
            "category": self.summary.category if self.summary else None,
            "category_label": self.summary.category_label if self.summary else None,
            "one_liner": self.summary.one_liner if self.summary else "",
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class ProcessResponse(BaseModel):
    job_id: str
    status: JobStatus
    status_label: str
