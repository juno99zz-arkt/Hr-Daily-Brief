# -*- coding: utf-8 -*-
"""이메일 발송 모듈 (SMTP / Resend 선택).

SMTP는 blocking이라 asyncio.to_thread로 감싸 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import asyncio
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import List, Optional

import httpx

from ..config import settings
from ..schemas import DeliveryResult

RESEND_URL = "https://api.resend.com/emails"


def safe_filename(title: str) -> str:
    """제목을 첨부파일명으로 쓸 수 있게 정리한다."""
    keep = [c if c.isalnum() or c in " -_()[]" else "_" for c in title]
    return "".join(keep).strip()[:60] or "voicebrief"


def _send_smtp(
    subject: str,
    html_body: str,
    recipients: List[str],
    attachment_name: Optional[str],
    attachment_bytes: Optional[bytes],
) -> str:
    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("VoiceBrief", "utf-8")), settings.sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)

    if attachment_name and attachment_bytes:
        part = MIMEBase("text", "markdown")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", attachment_name),
        )
        msg.attach(part)

    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=60)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
    try:
        server.ehlo()
        if settings.smtp_use_tls and settings.smtp_port != 465:
            server.starttls()
            server.ehlo()
        server.login(settings.smtp_user, settings.smtp_pass)
        server.sendmail(settings.sender, recipients, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - 종료 실패는 무시
            pass
    return f"SMTP 발송 완료 → {', '.join(recipients)}"


async def _send_resend(
    subject: str,
    html_body: str,
    recipients: List[str],
    attachment_name: Optional[str],
    attachment_bytes: Optional[bytes],
) -> str:
    import base64

    payload = {
        "from": settings.sender or "VoiceBrief <onboarding@resend.dev>",
        "to": recipients,
        "subject": subject,
        "html": html_body,
    }
    if attachment_name and attachment_bytes:
        payload["attachments"] = [
            {
                "filename": attachment_name,
                "content": base64.b64encode(attachment_bytes).decode("ascii"),
            }
        ]
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(RESEND_URL, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend 오류 {resp.status_code}: {resp.text[:300]}")
    return f"Resend 발송 완료 → {', '.join(recipients)}"


async def send_report(
    subject: str,
    html_body: str,
    markdown_body: str,
    title: str = "voicebrief",
    recipients: Optional[List[str]] = None,
) -> DeliveryResult:
    to = recipients or settings.mail_to
    if not to:
        return DeliveryResult(channel="email", ok=False, detail="수신자(MAIL_TO) 미설정")
    if not settings.mail_ready:
        return DeliveryResult(channel="email", ok=False, detail="메일 설정 미완료")

    attach_name = f"{safe_filename(title)}.md"
    attach_bytes = markdown_body.encode("utf-8")

    try:
        if settings.mail_provider == "resend":
            detail = await _send_resend(subject, html_body, to, attach_name, attach_bytes)
        else:
            detail = await asyncio.to_thread(
                _send_smtp, subject, html_body, to, attach_name, attach_bytes
            )
        return DeliveryResult(channel="email", ok=True, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 전송 실패가 파이프라인을 중단시키면 안 됨
        return DeliveryResult(channel="email", ok=False, detail=f"{type(exc).__name__}: {exc}")
