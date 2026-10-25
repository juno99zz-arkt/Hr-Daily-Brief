# VoiceBrief — Render(또는 다른 컨테이너 호스팅) 배포용 이미지.
#
# 브라우저 녹음(webm/opus)을 CLOVA Speech가 받는 포맷(mp3)으로 바꾸려면
# ffmpeg이 필요한데, Render의 기본 Python 런타임에는 없다.
# 그래서 apt로 직접 설치하는 Docker 배포를 쓴다.

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render는 PORT 환경변수로 리슨할 포트를 알려준다. 로컬 docker run 시엔 기본 8000.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
