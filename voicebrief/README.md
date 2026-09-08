# VoiceBrief — 개인용 녹음 정리 파이프라인

녹음 → **CLOVA Speech STT(화자분리)** → **LLM 유형별 구조화 요약** → **이메일 + 텔레그램 자동 발송**

폰 브라우저에서 녹음 버튼 하나 누르고 끝내면, 몇 분 뒤 메일함과 텔레그램에 정리된 보고서가 도착합니다.

---

## 1. 아키텍처

```
┌─────────────────┐   ①오디오 업로드      ┌──────────────────────────────┐
│  클라이언트      │  POST /api/          │        FastAPI 백엔드         │
│  · 웹 PWA        │ ───recordings/process─▶│                              │
│    (MediaRecorder)│                      │  api/routes.py (컨트롤러)     │
│  · Expo 앱(옵션) │  ②상태 폴링           │      │                        │
│                 │ ◀──GET /{job_id}──────│      ▼                        │
└─────────────────┘                       │  pipeline.py (오케스트레이터) │
                                          │   │                           │
                                          │   ├─▶ services/stt_clova.py ──┼──▶ CLOVA Speech API
                                          │   │      (화자분리 + 타임스탬프)│
                                          │   ├─▶ services/llm.py ────────┼──▶ Claude / GPT-4o
                                          │   │      (분류 + 구조화 요약)  │
                                          │   ├─▶ services/renderer.py    │
                                          │   │      (MD / HTML / 메신저)  │
                                          │   ├─▶ services/mailer.py ─────┼──▶ SMTP / Resend
                                          │   ├─▶ services/telegram.py ───┼──▶ Telegram Bot API
                                          │   └─▶ services/kakao.py ──────┼──▶ 카카오 나에게 보내기
                                          │                               │
                                          │  storage.py (JSON 파일 영속화) │
                                          └──────────────────────────────┘
```

### 설계 포인트

| 결정 | 이유 |
|---|---|
| **업로드 즉시 202 반환 + 백그라운드 처리** | 1시간짜리 녹음은 STT만 몇 분 걸린다. HTTP를 붙들면 모바일에서 타임아웃 난다. 클라이언트는 `job_id`로 폴링해 진행 단계를 표시한다. |
| **LLM은 JSON만 출력, 마크다운은 코드가 렌더링** | 서식까지 LLM에 맡기면 출력이 흔들리고 검증이 불가능하다. LLM은 '내용', 코드는 '형식'을 책임진다. |
| **발송 실패가 파이프라인을 죽이지 않음** | 메일이 실패해도 텔레그램은 가야 하고, 요약 결과는 남아야 한다. 각 채널은 `DeliveryResult`로 성패를 개별 보고하고 `/resend`로 재시도한다. |
| **저장소는 JSON 파일** | 개인용이라 DB가 과하다. 규모가 커지면 `storage.py`만 SQLite로 교체하면 된다. |
| **오디오 자동 변환** | 브라우저는 webm/opus로 녹음하는데 CLOVA는 이를 받지 않는다. ffmpeg으로 mono 16kHz mp3 변환 후 전송한다. |

---

## 2. 디렉터리 구조

```
voicebrief/
├── .env.example              # 환경 변수 템플릿
├── requirements.txt
├── README.md
│
├── backend/
│   ├── main.py               # FastAPI 앱 · 정적 PWA 서빙 · 예외 핸들러
│   ├── config.py             # .env 로딩, 서비스별 준비 상태 점검
│   ├── schemas.py            # Job / Transcript / Summary / DeliveryResult 모델
│   ├── prompts.py            # ★ 시스템 프롬프트 · 유형별 템플릿 · 출력 스키마
│   ├── pipeline.py           # ★ STT→LLM→렌더링→발송 오케스트레이션
│   ├── storage.py            # Job 영속화 (JSON 파일)
│   ├── api/routes.py         # ★ 업로드 컨트롤러 및 조회 API
│   ├── services/
│   │   ├── stt_clova.py      # ★ CLOVA Speech 연동
│   │   ├── llm.py            # ★ Claude/GPT 호출 + JSON 방어 파싱
│   │   ├── renderer.py       # Summary → Markdown / HTML / 메신저 텍스트
│   │   ├── mailer.py         # SMTP / Resend
│   │   ├── telegram.py       # Telegram Bot API (분할 전송, 파일 첨부)
│   │   └── kakao.py          # 카카오톡 나에게 보내기 (선택)
│   └── utils/audio.py        # ffmpeg 포맷 변환 · 길이 측정
│
├── web/                      # 모바일 웹 PWA (빌드 불필요)
│   ├── index.html · app.js · styles.css
│   └── manifest.webmanifest · sw.js
│
├── mobile-expo/App.tsx       # React Native(Expo) 참고 구현
│
├── tests/
│   ├── test_pipeline_units.py  # 파싱·렌더링·분할 단위 테스트
│   └── test_e2e_mock.py        # STT/LLM 모킹 전체 파이프라인 E2E
│
└── data/                     # 런타임 생성 (git 제외)
    ├── audio/                # 업로드된 원본
    └── records/              # 작업 결과 JSON
```

---

## 3. 빠른 시작

### 3-1. 설치

```bash
cd voicebrief
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# webm→mp3 변환용 (브라우저 녹음을 쓰려면 사실상 필수)
brew install ffmpeg          # macOS
sudo apt install -y ffmpeg   # Ubuntu/Debian
```

### 3-2. 환경 변수

```bash
cp .env.example .env
# .env 를 열어 키를 채웁니다
```

| 변수 | 발급처 |
|---|---|
| `CLOVA_SPEECH_API_KEY`, `CLOVA_SPEECH_INVOKE_URL` | [NCP 콘솔](https://console.ncloud.com) → AI Services → **CLOVA Speech** → 도메인 생성 → Secret Key / Invoke URL |
| `ANTHROPIC_API_KEY` (또는 `OPENAI_API_KEY`) | [console.anthropic.com](https://console.anthropic.com) / [platform.openai.com](https://platform.openai.com) |
| `SMTP_USER`, `SMTP_PASS` | Gmail은 [앱 비밀번호](https://myaccount.google.com/apppasswords) 16자리 (일반 비번 아님) |
| `TELEGRAM_BOT_TOKEN` | 텔레그램에서 **@BotFather** → `/newbot` |
| `TELEGRAM_CHAT_ID` | 봇에게 `/start` 보낸 뒤 → `curl localhost:8000/api/config/telegram-chat-id` |

`MAIL_TO`에는 이미 `junoh.jung@samsung.com, juno99zz@naver.com`이 기본값으로 들어 있습니다.

### 3-3. 실행

```bash
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

- 브라우저: http://localhost:8000
- API 문서(Swagger): http://localhost:8000/docs
- 설정 점검: http://localhost:8000/api/health → `{"stt":true,"llm":true,...}` 확인

### 3-4. 폰에서 쓰기

> ⚠️ 브라우저 마이크는 **HTTPS 또는 localhost**에서만 열립니다. LAN IP(`http://192.168...`)로 접속하면 녹음 버튼이 동작하지 않습니다.

가장 간단한 방법은 터널링입니다.

```bash
# 방법 A: Cloudflare Tunnel (설치 불필요, 무료)
cloudflared tunnel --url http://localhost:8000

# 방법 B: ngrok
ngrok http 8000
```

출력된 `https://...` 주소를 폰에서 열고 **홈 화면에 추가**하면 앱처럼 쓸 수 있습니다.
공개 주소로 노출되므로 `.env`에 `API_TOKEN`을 설정하고, 브라우저 콘솔에서 한 번 등록하세요.

```js
localStorage.setItem('vb_token', '설정한_API_TOKEN');
```

---

## 4. API

| 메서드 | 경로 | 설명 |
|---|---|---|
| `POST` | `/api/recordings/process` | 오디오 업로드 (multipart: `file`, `note`) → `202 {job_id}` |
| `GET` | `/api/recordings/{job_id}` | 작업 상세 + 진행 상태 (폴링용) |
| `GET` | `/api/recordings?limit=30` | 최근 요약 목록 |
| `GET` | `/api/recordings/{job_id}/markdown` | 마크다운 보고서 원문 |
| `POST` | `/api/recordings/{job_id}/resend` | 발송 실패 재시도 |
| `DELETE` | `/api/recordings/{job_id}` | 기록 삭제 |
| `GET` | `/api/health` | 서비스별 설정 상태 |
| `GET` | `/api/config/telegram-chat-id` | chat_id 조회 도우미 |

`status` 값은 그대로 UI 진행 단계와 대응됩니다:
`queued` → `transcribing` → `summarizing` → `delivering` → `done` / `failed`

### curl 테스트

```bash
curl -F "file=@sample.m4a" -F "note=김철수, 피플팀" \
     http://localhost:8000/api/recordings/process
# → {"job_id":"20260908-140233-a1b2c3", ...}

curl http://localhost:8000/api/recordings/20260908-140233-a1b2c3 | jq '.status, .summary.title'
```

---

## 5. LLM 프롬프트 설계 (`backend/prompts.py`)

환각 억제를 위해 다음을 강제합니다.

1. **사실 기반** — 스크립트에 없는 이름·숫자·날짜·결론 생성 금지
2. **추측 금지** — 확인 불가 항목은 `null` / 빈 배열
3. **외부 지식 차단** — 모델이 아는 일반 상식으로 내용 보완 금지
4. **담당자·기한** — 원문에 명시적으로 지목된 경우에만 채움 (문맥 짐작 금지)
5. **오인식 보존** — 뜻이 통하지 않는 구간은 고쳐 쓰지 말고 `unclear_points`에 기록
6. **인용 정확성** — `evidence`에 원문 문장 그대로
7. **JSON 단독 출력** — Claude는 assistant prefill(`{`), GPT는 `response_format=json_object`, `temperature=0`

유형별 섹션 구성:

| 분류 | 섹션 |
|---|---|
| `meeting` 회의 | 핵심 아젠다 / 논의 내용 / 결정 사항 / 액션 아이템(담당자·기한) |
| `interview` 면담·상담 | 주요 논의점 / 화자별 입장 및 요구사항 / 후속 조치 |
| `lecture` 강연·세미나 | 핵심 주제 / 핵심 개념 요약 / 주요 인사이트 |
| `memo` 개인 메모 | 메모 요지 / 할 일 / 기억할 포인트 |

모델이 규칙을 어겨도 `llm.py`의 `extract_json` + `to_summary`가 코드펜스 제거, 부분 추출, `"미상"/"null"` → `None` 정규화로 방어합니다.

---

## 6. 테스트

```bash
pip install pytest
python -m pytest tests -q
```

- `test_pipeline_units.py` — CLOVA 응답 파싱, LLM JSON 방어 파싱, 마크다운/HTML 렌더링(XSS 이스케이프 포함), 텔레그램 4096자 분할
- `test_e2e_mock.py` — STT·LLM·발송을 모킹해 **업로드부터 발송까지 전체 흐름** 검증 + 실패 처리 + 토큰 인증

외부 API 키 없이 전부 통과해야 합니다 (현재 19개 통과).

---

## 7. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 녹음 버튼을 눌러도 반응이 없음 | HTTPS가 아님. localhost로 열거나 터널링 사용 |
| `오디오 변환(ffmpeg) 실패` | ffmpeg 미설치. 설치하거나 iOS(m4a 녹음)로 테스트 |
| `CLOVA Speech 오류 401` | Secret Key 또는 Invoke URL 오타. 콘솔의 도메인 상세에서 재확인 |
| STT는 되는데 요약이 실패 | `/api/health`로 `llm: true` 확인. 토큰 한도 초과면 `LLM_MAX_TOKENS` 조정 |
| Gmail SMTP 인증 실패 | 일반 비밀번호 대신 **앱 비밀번호** 사용, 2단계 인증 필요 |
| 텔레그램만 안 감 | `/api/config/telegram-chat-id`로 chat_id 재확인. 봇에게 먼저 `/start` 필요 |
| 긴 녹음이 타임아웃 | `CLOVA_TIMEOUT_SEC` 증가. 1시간 이상은 파일을 나누는 편이 안전 |

---

## 8. 다음 단계 (선택)

- **카카오톡**: `KAKAO_ENABLED=1` + access token 설정. 토큰 유효기간이 6시간이라 `kakao.refresh_access_token()` 훅으로 갱신 필요
- **Notion/Slack 연동**: `pipeline._deliver()`에 채널 함수 하나만 추가하면 됨
- **화자 이름 매핑**: `note`에 "화자1=정준호, 화자2=김철수"를 적고 프롬프트에서 치환하도록 확장
- **보관 정책**: `KEEP_AUDIO=0`으로 처리 후 원본 자동 삭제
