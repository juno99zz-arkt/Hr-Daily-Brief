# -*- coding: utf-8 -*-
"""LLM 분류·요약 서비스.

Anthropic(Claude)과 OpenAI(GPT-4o)를 모두 지원하며 LLM_PROVIDER로 선택한다.
SDK 대신 httpx로 직접 호출해 의존성 버전 충돌을 피한다.

핵심 전략
- system 프롬프트로 사실 기반/JSON-only를 강제
- Anthropic: assistant 메시지 prefill("{")로 사족 출력을 원천 차단
- OpenAI: response_format={"type":"json_object"} 로 JSON 강제
- 그래도 깨진 출력이 올 수 있으므로 `extract_json`으로 방어적 파싱
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

import httpx

from ..config import settings
from ..prompts import ASSISTANT_PREFILL, SYSTEM_PROMPT, build_user_prompt
from ..schemas import ActionItem, Category, Section, Summary, Transcript
from ..utils.audio import human_duration

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# 매우 긴 회의도 한 번에 넣기 위한 상한(문자). 초과 시 앞/뒤를 보존하고 중간을 잘라낸다.
MAX_TRANSCRIPT_CHARS = 120_000


class LlmError(RuntimeError):
    pass


# ── 유틸 ────────────────────────────────────────────────────────────────────

def clamp_transcript(text: str, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.35):]
    return f"{head}\n\n…(중략: 길이 제한으로 일부 구간 생략)…\n\n{tail}"


def extract_json(raw: str) -> Dict[str, Any]:
    """모델 응답에서 JSON 객체를 최대한 안전하게 뽑아낸다."""
    text = (raw or "").strip()
    if not text:
        raise LlmError("LLM이 빈 응답을 반환했습니다.")

    # 코드펜스 제거
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM JSON 파싱 실패: {exc}") from exc
    raise LlmError(f"LLM 응답에서 JSON을 찾지 못했습니다: {text[:200]}")


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            out.append(json.dumps(item, ensure_ascii=False))
    return out


def _clean_optional(value: Any) -> str | None:
    """'null', '미상', '없음' 같은 값은 None으로 정규화(환각 방지)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "-", "미상", "없음", "미정", "미언급"):
        return None
    return text


def to_summary(data: Dict[str, Any]) -> Summary:
    """LLM JSON → Summary 모델 (누락/이상값 방어)."""
    raw_category = str(data.get("category") or "").strip().lower()
    try:
        category = Category(raw_category)
    except ValueError:
        category = Category.MEMO

    sections = []
    for item in data.get("sections") or []:
        if not isinstance(item, dict):
            continue
        heading = str(item.get("heading") or "").strip()
        bullets = _as_str_list(item.get("bullets"))
        if heading:
            sections.append(Section(heading=heading, bullets=bullets or ["언급 없음"]))

    actions = []
    for item in data.get("action_items") or []:
        if isinstance(item, str):
            actions.append(ActionItem(task=item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        task = str(item.get("task") or "").strip()
        if not task:
            continue
        actions.append(
            ActionItem(
                task=task,
                owner=_clean_optional(item.get("owner")),
                due=_clean_optional(item.get("due")),
                evidence=_clean_optional(item.get("evidence")),
            )
        )

    return Summary(
        category=category,
        category_reason=str(data.get("category_reason") or "").strip(),
        title=str(data.get("title") or "").strip() or "제목 미상",
        one_liner=str(data.get("one_liner") or "").strip(),
        keywords=_as_str_list(data.get("keywords"))[:10],
        participants=_as_str_list(data.get("participants")),
        sections=sections,
        decisions=_as_str_list(data.get("decisions")),
        action_items=actions,
        open_questions=_as_str_list(data.get("open_questions")),
        unclear_points=_as_str_list(data.get("unclear_points")),
    )


# ── 프로바이더별 호출 ───────────────────────────────────────────────────────

async def _call_anthropic(user_prompt: str) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": settings.anthropic_model,
        "max_tokens": settings.llm_max_tokens,
        "temperature": 0,  # 사실 기반 요약이므로 창의성 제거
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": ASSISTANT_PREFILL},  # JSON 시작 강제
        ],
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_sec) as client:
        resp = await client.post(ANTHROPIC_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        raise LlmError(f"Anthropic 오류 {resp.status_code}: {resp.text[:400]}")

    payload = resp.json()
    parts = [blk.get("text", "") for blk in payload.get("content", []) if blk.get("type") == "text"]
    text = "".join(parts)
    # prefill로 넣은 '{'는 응답에 포함되지 않으므로 다시 붙인다.
    return ASSISTANT_PREFILL + text if not text.lstrip().startswith("{") else text


async def _call_openai(user_prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.openai_model,
        "temperature": 0,
        "max_tokens": settings.llm_max_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_sec) as client:
        resp = await client.post(OPENAI_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        raise LlmError(f"OpenAI 오류 {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LlmError(f"OpenAI 응답 형식 오류: {str(payload)[:300]}") from exc


# ── 공개 API ────────────────────────────────────────────────────────────────

async def summarize(transcript: Transcript, recorded_at: str = "미상", note: str = "") -> Summary:
    if not settings.llm_ready:
        raise LlmError(
            f"LLM 설정이 없습니다. ({settings.llm_provider}) API 키를 .env에 설정하세요."
        )

    dialogue = clamp_transcript(transcript.to_dialogue())
    user_prompt = build_user_prompt(
        transcript=dialogue,
        recorded_at=recorded_at,
        duration=human_duration(transcript.duration_ms),
        speaker_count=len(transcript.speakers),
        note=note,
    )

    if settings.llm_provider == "openai":
        raw = await _call_openai(user_prompt)
    else:
        raw = await _call_anthropic(user_prompt)

    return to_summary(extract_json(raw))
