# -*- coding: utf-8 -*-
"""Summary → 마크다운 / HTML 메일 / 메신저 텍스트 렌더링.

서식은 LLM이 아니라 코드가 결정론적으로 만든다.
(LLM은 '내용'만 책임지고 '형식'은 코드가 보장 → 출력 흔들림 제거)
"""

from __future__ import annotations

import html
from typing import List

from ..schemas import Summary, Transcript
from ..utils.audio import human_duration


def _bullets(items: List[str], empty: str = "_해당 없음_") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def render_markdown(
    summary: Summary,
    transcript: Transcript | None = None,
    recorded_at: str = "",
    include_transcript: bool = True,
) -> str:
    """이메일 본문/저장용 마크다운 보고서."""
    lines: List[str] = []
    lines.append(f"# {summary.title}")
    lines.append("")

    meta = [f"**유형**: {summary.category_label}"]
    if recorded_at:
        meta.append(f"**녹음 일시**: {recorded_at}")
    if transcript:
        meta.append(f"**길이**: {human_duration(transcript.duration_ms)}")
        if transcript.speakers:
            meta.append(f"**화자**: {len(transcript.speakers)}명")
    lines.append(" · ".join(meta))
    lines.append("")

    if summary.one_liner:
        lines.append(f"> {summary.one_liner}")
        lines.append("")

    if summary.keywords:
        lines.append("`" + "` `".join(summary.keywords) + "`")
        lines.append("")

    if summary.participants:
        lines.append("## 참석자 / 화자")
        lines.append(_bullets(summary.participants))
        lines.append("")

    for section in summary.sections:
        lines.append(f"## {section.heading}")
        lines.append(_bullets(section.bullets))
        lines.append("")

    # 액션 아이템은 표로(담당자·기한 가독성)
    if summary.action_items:
        lines.append("## 액션 아이템")
        lines.append("| # | 할 일 | 담당자 | 기한 |")
        lines.append("|---|-------|--------|------|")
        for idx, item in enumerate(summary.action_items, 1):
            task = item.task.replace("|", "/")
            owner = item.owner or "미지정"
            due = item.due or "미정"
            lines.append(f"| {idx} | {task} | {owner} | {due} |")
        lines.append("")

    if summary.decisions and not any(s.heading == "결정 사항" for s in summary.sections):
        lines.append("## 결정 사항")
        lines.append(_bullets(summary.decisions))
        lines.append("")

    if summary.open_questions:
        lines.append("## 미결 사항 / 추가 확인 필요")
        lines.append(_bullets(summary.open_questions))
        lines.append("")

    if summary.unclear_points:
        lines.append("## ⚠️ 인식 불확실 구간")
        lines.append("_아래 항목은 음성 인식 품질 문제로 원문 확인이 필요합니다._")
        lines.append("")
        lines.append(_bullets(summary.unclear_points))
        lines.append("")

    if include_transcript and transcript and transcript.segments:
        lines.append("---")
        lines.append("")
        lines.append("## 전체 스크립트")
        lines.append("")
        for seg in transcript.segments:
            lines.append(f"**[{seg.timestamp}] {seg.speaker}** {seg.text}")
            lines.append("")

    if summary.category_reason:
        lines.append("---")
        lines.append(f"<sub>분류 근거: {summary.category_reason} · VoiceBrief 자동 생성</sub>")

    return "\n".join(lines).strip() + "\n"


def render_email_html(
    summary: Summary,
    transcript: Transcript | None = None,
    recorded_at: str = "",
) -> str:
    """모바일 메일앱에서 읽기 좋은 간단한 인라인 스타일 HTML."""
    esc = html.escape

    def ul(items: List[str]) -> str:
        if not items:
            return '<p style="color:#888;margin:4px 0 12px">해당 없음</p>'
        lis = "".join(
            f'<li style="margin:4px 0;line-height:1.6">{esc(i)}</li>' for i in items
        )
        return f'<ul style="margin:4px 0 14px;padding-left:20px">{lis}</ul>'

    parts: List[str] = []
    parts.append(
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Malgun Gothic\','
        'sans-serif;max-width:680px;margin:0 auto;padding:20px;color:#1a1a1a">'
    )
    parts.append(
        f'<div style="display:inline-block;background:#2563eb;color:#fff;'
        f'padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600">'
        f"{esc(summary.category_label)}</div>"
    )
    parts.append(f'<h1 style="font-size:22px;margin:12px 0 6px">{esc(summary.title)}</h1>')

    meta = []
    if recorded_at:
        meta.append(esc(recorded_at))
    if transcript:
        meta.append(human_duration(transcript.duration_ms))
        if transcript.speakers:
            meta.append(f"화자 {len(transcript.speakers)}명")
    if meta:
        parts.append(
            f'<p style="color:#666;font-size:13px;margin:0 0 16px">{" · ".join(meta)}</p>'
        )

    if summary.one_liner:
        parts.append(
            f'<blockquote style="margin:0 0 18px;padding:12px 16px;background:#f1f5f9;'
            f'border-left:4px solid #2563eb;border-radius:4px;font-size:15px">'
            f"{esc(summary.one_liner)}</blockquote>"
        )

    if summary.keywords:
        chips = "".join(
            f'<span style="display:inline-block;background:#eef2ff;color:#4338ca;'
            f'padding:3px 9px;border-radius:10px;font-size:12px;margin:0 5px 5px 0">'
            f"#{esc(k)}</span>"
            for k in summary.keywords
        )
        parts.append(f'<div style="margin-bottom:16px">{chips}</div>')

    if summary.participants:
        parts.append('<h2 style="font-size:16px;margin:18px 0 6px">참석자 / 화자</h2>')
        parts.append(ul(summary.participants))

    for section in summary.sections:
        parts.append(
            f'<h2 style="font-size:16px;margin:18px 0 6px;padding-bottom:5px;'
            f'border-bottom:1px solid #e5e7eb">{esc(section.heading)}</h2>'
        )
        parts.append(ul(section.bullets))

    if summary.action_items:
        parts.append('<h2 style="font-size:16px;margin:18px 0 8px">액션 아이템</h2>')
        rows = "".join(
            f'<tr>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">{esc(i.task)}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;white-space:nowrap">'
            f'{esc(i.owner or "미지정")}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;white-space:nowrap">'
            f'{esc(i.due or "미정")}</td>'
            f"</tr>"
            for i in summary.action_items
        )
        parts.append(
            '<table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:14px">'
            '<thead><tr style="background:#f8fafc">'
            '<th style="text-align:left;padding:8px">할 일</th>'
            '<th style="text-align:left;padding:8px">담당자</th>'
            '<th style="text-align:left;padding:8px">기한</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )

    if summary.open_questions:
        parts.append('<h2 style="font-size:16px;margin:18px 0 6px">미결 사항</h2>')
        parts.append(ul(summary.open_questions))

    if summary.unclear_points:
        parts.append(
            '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;'
            'padding:12px;margin:16px 0">'
            '<strong style="font-size:14px">⚠️ 인식 불확실 구간</strong>'
            f"{ul(summary.unclear_points)}</div>"
        )

    parts.append(
        '<p style="color:#9ca3af;font-size:12px;margin-top:24px;'
        'border-top:1px solid #eee;padding-top:12px">'
        "전체 스크립트는 첨부된 Markdown 파일을 확인하세요. · VoiceBrief 자동 생성</p>"
    )
    parts.append("</div>")
    return "".join(parts)


def render_messenger_text(summary: Summary, max_len: int = 3500) -> str:
    """텔레그램/카카오용 핵심 요약 (HTML parse_mode 기준)."""
    esc = html.escape
    lines: List[str] = []
    lines.append(f"<b>[{esc(summary.category_label)}] {esc(summary.title)}</b>")
    if summary.one_liner:
        lines.append(f"\n{esc(summary.one_liner)}")

    # 유형에 따라 가장 중요한 섹션만 골라 보낸다(메신저는 짧게).
    priority = ["결정 사항", "액션 아이템", "핵심 아젠다", "주요 논의점", "후속 조치",
                "핵심 주제", "주요 인사이트", "메모 요지", "할 일"]
    picked = 0
    for name in priority:
        for section in summary.sections:
            if section.heading == name and section.bullets and section.bullets != ["언급 없음"]:
                lines.append(f"\n<b>· {esc(name)}</b>")
                for bullet in section.bullets[:4]:
                    lines.append(f"  - {esc(bullet)}")
                picked += 1
                break
        if picked >= 2:
            break

    if summary.action_items:
        lines.append("\n<b>· 액션 아이템</b>")
        for item in summary.action_items[:5]:
            tail = []
            if item.owner:
                tail.append(item.owner)
            if item.due:
                tail.append(item.due)
            suffix = f" ({esc(' / '.join(tail))})" if tail else ""
            lines.append(f"  - {esc(item.task)}{suffix}")

    if summary.keywords:
        lines.append("\n" + " ".join(f"#{esc(k.replace(' ', '_'))}" for k in summary.keywords[:5]))

    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[: max_len - 20] + "\n…(이하 생략, 메일 참조)"
    return text
