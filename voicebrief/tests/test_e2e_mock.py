# -*- coding: utf-8 -*-
"""STT/LLM/발송을 모킹한 전체 파이프라인 E2E 테스트.

외부 API 키 없이 업로드 → 전사 → 요약 → 렌더링 → 발송까지의 흐름을 검증한다.
실행:  cd voicebrief && python -m pytest tests/test_e2e_mock.py -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import pipeline, storage  # noqa: E402
from backend.main import app  # noqa: E402
from backend.schemas import DeliveryResult, Transcript  # noqa: E402
from backend.services import llm, mailer, stt_clova, telegram  # noqa: E402
from backend.services.stt_clova import parse_clova_response  # noqa: E402

FAKE_CLOVA = {
    "result": "COMPLETED",
    "text": "이번 분기 채용 계획을 확정합시다. 네, 다음 주 월요일까지 초안 만들겠습니다.",
    "segments": [
        {"start": 0, "end": 5000, "text": "이번 분기 채용 계획을 확정합시다.",
         "speaker": {"label": "1", "name": "A"}},
        {"start": 5100, "end": 11000, "text": "네, 다음 주 월요일까지 초안 만들겠습니다.",
         "speaker": {"label": "2", "name": "B"}},
    ],
}

FAKE_LLM_JSON = """{
  "category": "meeting",
  "category_reason": "채용 계획이라는 안건을 논의하고 담당자를 정함",
  "title": "분기 채용 계획 회의",
  "one_liner": "이번 분기 채용 계획을 확정하고 초안 작성을 화자2가 맡기로 했다.",
  "keywords": ["채용", "분기 계획"],
  "participants": ["화자1(미상)", "화자2(미상)"],
  "sections": [
    {"heading": "핵심 아젠다", "bullets": ["이번 분기 채용 계획 확정"]},
    {"heading": "논의 내용", "bullets": ["초안 작성 일정 논의"]},
    {"heading": "결정 사항", "bullets": ["채용 계획 초안을 다음 주 월요일까지 작성"]},
    {"heading": "액션 아이템", "bullets": ["화자2가 초안 작성"]}
  ],
  "decisions": ["채용 계획 초안을 다음 주 월요일까지 작성"],
  "action_items": [
    {"task": "채용 계획 초안 작성", "owner": "화자2", "due": "다음 주 월요일",
     "evidence": "네, 다음 주 월요일까지 초안 만들겠습니다."}
  ],
  "open_questions": [],
  "unclear_points": []
}"""


@pytest.fixture
def mocked(monkeypatch):
    """외부 호출을 모두 가짜로 교체."""
    sent: dict = {"email": None, "telegram": None}

    async def fake_transcribe(path, note=""):  # noqa: ANN001, ARG001
        return parse_clova_response(FAKE_CLOVA)

    async def fake_anthropic(prompt):  # noqa: ANN001, ARG001
        return FAKE_LLM_JSON

    async def fake_mail(subject, html_body, markdown_body, title="", recipients=None):  # noqa: ANN001, ARG001
        sent["email"] = {"subject": subject, "html": html_body, "md": markdown_body}
        return DeliveryResult(channel="email", ok=True, detail="mock")

    async def fake_tg(text, chat_id=None):  # noqa: ANN001, ARG001
        sent["telegram"] = text
        return DeliveryResult(channel="telegram", ok=True, detail="mock")

    monkeypatch.setattr(stt_clova, "transcribe", fake_transcribe)
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm, "_call_anthropic", fake_anthropic)
    monkeypatch.setattr(mailer, "send_report", fake_mail)
    monkeypatch.setattr(telegram, "send_message", fake_tg)
    # 파이프라인이 import한 심볼도 교체
    monkeypatch.setattr(pipeline.stt_clova, "transcribe", fake_transcribe)
    monkeypatch.setattr(pipeline.mailer, "send_report", fake_mail)
    monkeypatch.setattr(pipeline.telegram, "send_message", fake_tg)
    return sent


def test_full_pipeline(mocked):
    with TestClient(app) as client:
        resp = client.post(
            "/api/recordings/process",
            files={"file": ("meeting.m4a", b"\x00" * 8000, "audio/mp4")},
            data={"note": "채용, 피플팀"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job = None
        for _ in range(40):
            job = client.get(f"/api/recordings/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

        assert job["status"] == "done", job.get("error")
        assert job["status_label"] == "전송 완료"

        # 요약 결과
        summary = job["summary"]
        assert summary["category"] == "meeting"
        assert summary["title"] == "분기 채용 계획 회의"
        assert summary["action_items"][0]["owner"] == "화자2"

        # 전사 결과
        assert len(job["transcript"]["segments"]) == 2
        assert job["transcript"]["speakers"] == ["화자1", "화자2"]

        # 마크다운 보고서
        md = client.get(f"/api/recordings/{job_id}/markdown").text
        assert "# 분기 채용 계획 회의" in md
        assert "| 채용 계획 초안 작성 | 화자2 | 다음 주 월요일 |" in md

        # 발송 내용
        assert mocked["email"]["subject"] == "[회의] 분기 채용 계획 회의"
        assert "분기 채용 계획 회의" in mocked["email"]["html"]
        assert "<b>[회의] 분기 채용 계획 회의</b>" in mocked["telegram"]

        # 목록에 노출
        items = client.get("/api/recordings").json()["items"]
        assert any(i["id"] == job_id and i["category"] == "meeting" for i in items)

        # 정리
        assert client.delete(f"/api/recordings/{job_id}").status_code == 200


def test_llm_failure_marks_job_failed(monkeypatch):
    async def fake_transcribe(path, note=""):  # noqa: ANN001, ARG001
        return parse_clova_response(FAKE_CLOVA)

    async def boom(prompt):  # noqa: ANN001, ARG001
        raise llm.LlmError("rate limit")

    monkeypatch.setattr(pipeline.stt_clova, "transcribe", fake_transcribe)
    monkeypatch.setattr(llm.settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(llm, "_call_anthropic", boom)

    with TestClient(app) as client:
        job_id = client.post(
            "/api/recordings/process",
            files={"file": ("x.m4a", b"\x00" * 4000, "audio/mp4")},
        ).json()["job_id"]

        for _ in range(40):
            job = client.get(f"/api/recordings/{job_id}").json()
            if job["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

        assert job["status"] == "failed"
        assert "rate limit" in job["error"]
        # 실패해도 전사 결과는 남아 있어야 재시도가 가능하다
        assert job["transcript"]["full_text"]
        client.delete(f"/api/recordings/{job_id}")


def test_api_token_guard(monkeypatch):
    from backend.api import routes

    monkeypatch.setattr(routes.settings, "api_token", "s3cret")
    with TestClient(app) as client:
        assert client.get("/api/recordings").status_code == 401
        ok = client.get("/api/recordings", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        # health는 인증 없이 열려 있어야 모니터링이 가능
        assert client.get("/api/health").status_code == 200
