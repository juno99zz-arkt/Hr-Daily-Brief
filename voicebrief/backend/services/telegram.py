# -*- coding: utf-8 -*-
"""Telegram Bot API 발송 모듈.

- parse_mode=HTML 사용 (MarkdownV2는 이스케이프 규칙이 까다로워 실무에서 잘 깨짐)
- 4096자 제한을 고려해 자동 분할
- 마크다운 전문(全文)은 파일로도 보낼 수 있다(sendDocument)
"""

from __future__ import annotations

import io
from typing import List

import httpx

from ..config import settings
from ..schemas import DeliveryResult

TELEGRAM_LIMIT = 4096


def _split(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            # 한 줄이 limit을 넘는 극단적 경우
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


async def send_message(text: str, chat_id: str | None = None) -> DeliveryResult:
    if not settings.telegram_ready:
        return DeliveryResult(
            channel="telegram", ok=False, detail="TELEGRAM_BOT_TOKEN/CHAT_ID 미설정"
        )
    target = chat_id or settings.telegram_chat_id
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in _split(text):
                resp = await client.post(
                    _api("sendMessage"),
                    json={
                        "chat_id": target,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    # HTML 파싱 오류면 태그 없이 재시도
                    plain = chunk.replace("<b>", "").replace("</b>", "")
                    retry = await client.post(
                        _api("sendMessage"),
                        json={"chat_id": target, "text": plain},
                    )
                    if retry.status_code >= 400:
                        raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return DeliveryResult(channel="telegram", ok=True, detail=f"chat_id={target} 발송 완료")
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(channel="telegram", ok=False, detail=f"{type(exc).__name__}: {exc}")


async def send_document(
    filename: str, content: str, caption: str = "", chat_id: str | None = None
) -> DeliveryResult:
    if not settings.telegram_ready:
        return DeliveryResult(channel="telegram_file", ok=False, detail="설정 미완료")
    target = chat_id or settings.telegram_chat_id
    try:
        files = {"document": (filename, io.BytesIO(content.encode("utf-8")), "text/markdown")}
        data = {"chat_id": target, "caption": caption[:1000]}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(_api("sendDocument"), data=data, files=files)
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return DeliveryResult(channel="telegram_file", ok=True, detail=f"{filename} 전송 완료")
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(
            channel="telegram_file", ok=False, detail=f"{type(exc).__name__}: {exc}"
        )


async def get_chat_id_hint() -> str:
    """CHAT_ID를 모를 때: 봇에게 아무 메시지나 보낸 뒤 이 함수를 호출하면 id를 알려준다."""
    if not settings.telegram_bot_token:
        return "TELEGRAM_BOT_TOKEN이 없습니다."
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_api("getUpdates"))
    if resp.status_code >= 400:
        return f"오류 {resp.status_code}: {resp.text[:200]}"
    updates = resp.json().get("result", [])
    ids = []
    for item in updates:
        chat = (item.get("message") or item.get("channel_post") or {}).get("chat") or {}
        if chat.get("id") and chat["id"] not in ids:
            ids.append(chat["id"])
    if not ids:
        return "최근 메시지가 없습니다. 텔레그램에서 봇에게 /start 를 보낸 뒤 다시 호출하세요."
    return f"발견된 chat_id: {ids}"
