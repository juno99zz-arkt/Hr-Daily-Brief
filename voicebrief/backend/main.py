# -*- coding: utf-8 -*-
"""FastAPI 엔트리포인트.

실행:
    cd voicebrief && uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
정적 PWA(web/)를 같은 서버에서 서빙하므로 폰 브라우저로 바로 접속해 쓸 수 있다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .api.routes import router
from .config import WEB_DIR, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("voicebrief")


@asynccontextmanager
async def lifespan(app: FastAPI):
    fixed = await storage.mark_stale_as_failed()
    if fixed:
        log.warning("중단된 작업 %d건을 실패로 정리했습니다.", fixed)

    health = settings.health()
    log.info("서비스 상태: %s", health)
    missing = [k for k, v in health.items() if v is False]
    if missing:
        log.warning("미설정 서비스: %s (.env 확인)", ", ".join(missing))
    yield


app = FastAPI(
    title="VoiceBrief API",
    description="녹음 → STT(CLOVA) → LLM 구조화 요약 → 메일/텔레그램 발송 파이프라인",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):  # noqa: ANN001
    log.exception("처리되지 않은 오류: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"서버 오류: {type(exc).__name__}"},
    )


# ── 정적 클라이언트(PWA) ────────────────────────────────────────────────────
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # 서비스워커는 스코프 문제로 루트 경로에서 서빙해야 한다.
        return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(WEB_DIR / "manifest.webmanifest")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
