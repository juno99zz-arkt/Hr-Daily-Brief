# -*- coding: utf-8 -*-
"""카카오톡 '나에게 보내기' (메모 API) 연동 인터페이스.

엔드포인트: POST https://kapi.kakao.com/v2/api/talk/memo/default/send
- Authorization: Bearer {ACCESS_TOKEN}  (talk_message 동의항목 필요)
- template_object(JSON 문자열)를 form-urlencoded로 전송
- text 템플릿은 200자 제한

주의: 카카오 access token은 유효기간이 짧다(기본 6시간).
장기 운용 시 refresh token으로 갱신하는 로직이 필요하므로 아래에 훅을 남겨둔다.
기본값은 비활성(KAKAO_ENABLED=0)이며, 텔레그램을 1차 채널로 쓰는 것을 권장한다.
"""

from __future__ import annotations

import json
import re

import httpx

from ..config import settings
from ..schemas import DeliveryResult

MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
TEXT_LIMIT = 200


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def send_memo(text: str, link_url: str = "") -> DeliveryResult:
    if not settings.kakao_ready:
        return DeliveryResult(
            channel="kakao", ok=False, detail="KAKAO 비활성 또는 ACCESS_TOKEN 미설정"
        )

    plain = _strip_html(text).strip()
    if len(plain) > TEXT_LIMIT:
        plain = plain[: TEXT_LIMIT - 3] + "..."

    template = {
        "object_type": "text",
        "text": plain,
        "link": {"web_url": link_url, "mobile_web_url": link_url} if link_url else {},
    }
    if link_url:
        template["button_title"] = "전체 보기"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                MEMO_URL,
                headers={"Authorization": f"Bearer {settings.kakao_access_token}"},
                data={"template_object": json.dumps(template, ensure_ascii=False)},
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        return DeliveryResult(channel="kakao", ok=True, detail="나에게 보내기 발송 완료")
    except Exception as exc:  # noqa: BLE001
        return DeliveryResult(channel="kakao", ok=False, detail=f"{type(exc).__name__}: {exc}")


async def refresh_access_token(refresh_token: str, rest_api_key: str) -> str:
    """access token 갱신 훅. 반환된 토큰을 KAKAO_ACCESS_TOKEN에 반영해 사용한다."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": rest_api_key,
                "refresh_token": refresh_token,
            },
        )
    resp.raise_for_status()
    return resp.json().get("access_token", "")
