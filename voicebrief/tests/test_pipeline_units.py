# -*- coding: utf-8 -*-
"""네트워크 없이 검증 가능한 순수 로직 테스트.

실행:  cd voicebrief && python -m pytest tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.schemas import Category, Segment, Transcript  # noqa: E402
from backend.services.llm import clamp_transcript, extract_json, to_summary  # noqa: E402
from backend.services.renderer import (  # noqa: E402
    render_email_html,
    render_markdown,
    render_messenger_text,
)
from backend.services.stt_clova import parse_clova_response  # noqa: E402
from backend.services.telegram import _split  # noqa: E402

# ── CLOVA 응답 파싱 ─────────────────────────────────────────────────────────

CLOVA_SAMPLE = {
    "result": "COMPLETED",
    "message": "Succeeded",
    "text": "다음 주 월요일까지 초안 부탁드립니다. 네, 제가 준비하겠습니다.",
    "segments": [
        {
            "start": 0, "end": 4200,
            "text": "다음 주 월요일까지 초안 부탁드립니다.",
            "speaker": {"label": "1", "name": "A"},
        },
        {
            "start": 4300, "end": 7100,
            "text": "네, 제가 준비하겠습니다.",
            "speaker": {"label": "2", "name": "B"},
        },
        {"start": 7200, "end": 7300, "text": "  ", "speaker": {"label": "1"}},  # 빈 세그먼트
    ],
}


def test_parse_clova_response():
    tr = parse_clova_response(CLOVA_SAMPLE)
    assert len(tr.segments) == 2, "빈 세그먼트는 제외되어야 함"
    assert tr.speakers == ["화자1", "화자2"]
    assert tr.duration_ms == 7100
    assert "월요일" in tr.full_text


def test_to_dialogue_has_timestamps():
    tr = parse_clova_response(CLOVA_SAMPLE)
    dialogue = tr.to_dialogue()
    assert dialogue.startswith("[00:00] 화자1:")
    assert "[00:04] 화자2:" in dialogue


def test_parse_clova_failure_raises():
    import pytest

    from backend.services.stt_clova import SttError

    with pytest.raises(SttError):
        parse_clova_response({"result": "FAILED", "message": "quota exceeded"})


# ── LLM 출력 파싱 ───────────────────────────────────────────────────────────

def test_extract_json_handles_code_fence():
    raw = '```json\n{"category": "meeting", "title": "주간 회의"}\n```'
    assert extract_json(raw)["title"] == "주간 회의"


def test_extract_json_handles_prose_wrapper():
    raw = '네, 요약 결과입니다.\n{"category": "memo", "title": "메모"}\n감사합니다.'
    assert extract_json(raw)["category"] == "memo"


def test_to_summary_normalizes_unknown_values():
    data = {
        "category": "meeting",
        "title": "제품 로드맵 회의",
        "one_liner": "3분기 로드맵을 확정했다.",
        "keywords": ["로드맵", "3분기"],
        "sections": [{"heading": "결정 사항", "bullets": ["9월 출시 확정"]}],
        "decisions": ["9월 출시 확정"],
        "action_items": [
            {"task": "초안 작성", "owner": "화자2", "due": "다음 주 월요일"},
            {"task": "예산 확인", "owner": "미상", "due": "null"},  # → None 정규화
        ],
    }
    summary = to_summary(data)
    assert summary.category is Category.MEETING
    assert summary.action_items[0].owner == "화자2"
    assert summary.action_items[1].owner is None, "'미상'은 None으로 정규화되어야 함"
    assert summary.action_items[1].due is None, "'null' 문자열은 None이어야 함"


def test_to_summary_falls_back_on_bad_category():
    summary = to_summary({"category": "workshop", "title": "x"})
    assert summary.category is Category.MEMO, "알 수 없는 분류는 memo로 폴백"


def test_clamp_transcript_keeps_head_and_tail():
    text = "가" * 1000
    out = clamp_transcript(text, limit=200)
    assert len(out) < len(text)
    assert "중략" in out


# ── 렌더링 ──────────────────────────────────────────────────────────────────

def _sample_summary():
    return to_summary(
        {
            "category": "meeting",
            "category_reason": "3인이 안건을 논의하고 결정함",
            "title": "제품 로드맵 회의",
            "one_liner": "3분기 로드맵을 확정하고 담당자를 배정했다.",
            "keywords": ["로드맵", "출시"],
            "participants": ["화자1(팀장)", "화자2(미상)"],
            "sections": [
                {"heading": "핵심 아젠다", "bullets": ["3분기 출시 일정 검토"]},
                {"heading": "결정 사항", "bullets": ["9월 출시 확정"]},
            ],
            "decisions": ["9월 출시 확정"],
            "action_items": [
                {"task": "초안 작성", "owner": "화자2", "due": "다음 주 월요일",
                 "evidence": "다음 주 월요일까지 초안 부탁드립니다."}
            ],
            "unclear_points": ["07:12 구간 인식 불명"],
        }
    )


def test_render_markdown_contains_all_sections():
    tr = parse_clova_response(CLOVA_SAMPLE)
    md = render_markdown(_sample_summary(), transcript=tr, recorded_at="2026-09-08 14:00")
    assert md.startswith("# 제품 로드맵 회의")
    assert "**유형**: 회의" in md
    assert "## 핵심 아젠다" in md
    assert "| 초안 작성 | 화자2 | 다음 주 월요일 |" in md
    assert "인식 불확실 구간" in md
    assert "## 전체 스크립트" in md


def test_render_markdown_escapes_pipe_in_table():
    summary = to_summary({"category": "meeting", "title": "t",
                          "action_items": [{"task": "A | B 비교"}]})
    md = render_markdown(summary, include_transcript=False)
    assert "A / B 비교" in md, "표를 깨뜨리는 파이프는 치환되어야 함"


def test_render_email_html_escapes_user_content():
    summary = to_summary({"category": "memo", "title": "<script>alert(1)</script>"})
    html = render_email_html(summary)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_messenger_text_is_short_and_tagged():
    text = render_messenger_text(_sample_summary())
    assert text.startswith("<b>[회의] 제품 로드맵 회의</b>")
    assert "액션 아이템" in text
    assert len(text) <= 3500


# ── 텔레그램 분할 ───────────────────────────────────────────────────────────

def test_split_respects_limit():
    text = "\n".join(f"라인 {i}" for i in range(2000))
    chunks = _split(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks).count("라인") == 2000


def test_split_handles_single_long_line():
    chunks = _split("가" * 5000, limit=1000)
    assert len(chunks) == 5
    assert all(len(c) <= 1000 for c in chunks)


# ── 프롬프트 ────────────────────────────────────────────────────────────────

def test_system_prompt_enforces_json_and_facts():
    from backend.prompts import SYSTEM_PROMPT, build_user_prompt

    assert "JSON 객체 하나만" in SYSTEM_PROMPT
    assert "추측 금지" in SYSTEM_PROMPT
    assert "meeting" in SYSTEM_PROMPT and "lecture" in SYSTEM_PROMPT

    prompt = build_user_prompt("[00:00] 화자1: 안녕하세요", note="")
    assert "<스크립트>" in prompt
    assert "없음" in prompt  # 메모가 비면 '없음'


def test_transcript_empty_dialogue_is_safe():
    tr = Transcript(full_text="", segments=[])
    assert tr.to_dialogue() == ""
    tr2 = Transcript(segments=[Segment(start_ms=65000, text="테스트", speaker="화자1")])
    assert "[01:05]" in tr2.to_dialogue()
