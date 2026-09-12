# -*- coding: utf-8 -*-
"""환경 변수 로딩 및 설정 객체.

.env 파일은 voicebrief/.env 를 우선 사용하고, 없으면 프로세스 환경 변수를 쓴다.
외부 의존성(python-dotenv)이 없어도 동작하도록 자체 파서를 포함한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

BASE_DIR = Path(__file__).resolve().parent.parent  # voicebrief/
DATA_DIR = BASE_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
RECORD_DIR = DATA_DIR / "records"
WEB_DIR = BASE_DIR / "web"


def _load_dotenv(path: Path) -> None:
    """아주 단순한 .env 파서 (KEY=VALUE, # 주석, 따옴표 제거)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 이미 환경에 있으면 환경 변수를 우선(운영 배포 시 주입값 존중)
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _env_list(key: str, default: str = "") -> List[str]:
    raw = _env(key, default)
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


@dataclass
class Settings:
    # ── 서버 ────────────────────────────────────────────────────────────────
    app_name: str = "VoiceBrief"
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    cors_origins: List[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS", "*"))
    max_upload_mb: int = field(default_factory=lambda: _env_int("MAX_UPLOAD_MB", 200))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN"))  # 비우면 인증 없음

    # ── STT (Naver CLOVA Speech) ───────────────────────────────────────────
    clova_api_key: str = field(default_factory=lambda: _env("CLOVA_SPEECH_API_KEY"))
    clova_invoke_url: str = field(default_factory=lambda: _env("CLOVA_SPEECH_INVOKE_URL"))
    clova_language: str = field(default_factory=lambda: _env("CLOVA_LANGUAGE", "ko-KR"))
    clova_diarization: bool = field(default_factory=lambda: _env_bool("CLOVA_DIARIZATION", True))
    clova_speaker_min: int = field(default_factory=lambda: _env_int("CLOVA_SPEAKER_MIN", 1))
    clova_speaker_max: int = field(default_factory=lambda: _env_int("CLOVA_SPEAKER_MAX", 6))
    clova_timeout_sec: int = field(default_factory=lambda: _env_int("CLOVA_TIMEOUT_SEC", 900))

    # ── LLM ────────────────────────────────────────────────────────────────
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "anthropic").lower())
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-sonnet-4-5"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o"))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 4000))
    llm_timeout_sec: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_SEC", 240))

    # ── 메일 ───────────────────────────────────────────────────────────────
    mail_provider: str = field(default_factory=lambda: _env("MAIL_PROVIDER", "smtp").lower())
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST", "smtp.gmail.com"))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_pass: str = field(default_factory=lambda: _env("SMTP_PASS"))
    smtp_use_tls: bool = field(default_factory=lambda: _env_bool("SMTP_USE_TLS", True))
    mail_from: str = field(default_factory=lambda: _env("MAIL_FROM"))
    mail_to: List[str] = field(default_factory=lambda: _env_list("MAIL_TO"))
    resend_api_key: str = field(default_factory=lambda: _env("RESEND_API_KEY"))

    # ── 메신저 ─────────────────────────────────────────────────────────────
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    kakao_access_token: str = field(default_factory=lambda: _env("KAKAO_ACCESS_TOKEN"))
    kakao_enabled: bool = field(default_factory=lambda: _env_bool("KAKAO_ENABLED", False))

    # ── 기타 ───────────────────────────────────────────────────────────────
    keep_audio: bool = field(default_factory=lambda: _env_bool("KEEP_AUDIO", True))
    ffmpeg_bin: str = field(default_factory=lambda: _env("FFMPEG_BIN", "ffmpeg"))

    # ── 유효성 헬퍼 ────────────────────────────────────────────────────────
    @property
    def stt_ready(self) -> bool:
        return bool(self.clova_api_key and self.clova_invoke_url)

    @property
    def llm_ready(self) -> bool:
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return bool(self.anthropic_api_key)

    @property
    def mail_ready(self) -> bool:
        if not self.mail_to:
            return False
        if self.mail_provider == "resend":
            return bool(self.resend_api_key)
        return bool(self.smtp_host and self.smtp_user and self.smtp_pass)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def kakao_ready(self) -> bool:
        return bool(self.kakao_enabled and self.kakao_access_token)

    @property
    def sender(self) -> str:
        return self.mail_from or self.smtp_user

    def health(self) -> dict:
        return {
            "stt": self.stt_ready,
            "llm": self.llm_ready,
            "llm_provider": self.llm_provider,
            "mail": self.mail_ready,
            "telegram": self.telegram_ready,
            "kakao": self.kakao_ready,
        }


settings = Settings()

for _dir in (DATA_DIR, AUDIO_DIR, RECORD_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
