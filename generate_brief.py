#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""삼성디스플레이 HR 피플팀 | 데일리 뉴스 브리핑 자동 생성기"""

import sys, os, re, html, json, subprocess, feedparser, smtplib, socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta, date as _date, timezone
from pathlib import Path
from urllib.parse import quote
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import urllib.request, urllib.error

socket.setdefaulttimeout(15)  # RSS 요청 타임아웃 15초

KST = timezone(timedelta(hours=9))
def now_kst():
    """현재 KST 시각 반환 (GitHub Actions UTC 환경 대응)"""
    return datetime.now(KST).replace(tzinfo=None)

try:
    import holidays
except ImportError:
    holidays = None

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic 패키지가 없습니다. pip install anthropic")
    sys.exit(1)

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ── 설정 로드 ─────────────────────────────────────────────────────────────────

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _l in _env.read_text("utf-8").splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            _k, _v = _l.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().replace('﻿', ''))

def _env_clean(key, default=""):
    return os.environ.get(key, default).replace('﻿', '').replace('\r', '').strip()

IS_CI = os.environ.get("GITHUB_ACTIONS") == "true"
ANTHROPIC_API_KEY = _env_clean("ANTHROPIC_API_KEY")
EMAIL_SENDER      = _env_clean("EMAIL_SENDER")
EMAIL_PASSWORD    = _env_clean("EMAIL_PASSWORD").replace(" ", "")
EMAIL_RECIPIENT   = _env_clean("EMAIL_RECIPIENT")
NAVER_CLIENT_ID     = _env_clean("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = _env_clean("NAVER_CLIENT_SECRET")

OUTPUT_DIR = Path(__file__).parent / "public"
OUTPUT_DIR.mkdir(exist_ok=True)

SEEN_FILE = OUTPUT_DIR / "seen_titles.json"
SEEN_DAYS = 180  # Google RSS 구기사 재색인은 수개월 뒤에도 발생 (예: 9/7 기사가 10/23 재등장)
ARCHIVE_DIR = OUTPUT_DIR / "archive"
_SRC_TITLE_RE = re.compile(r'data-src-title="([^"]*)"')

def _norm_title(t):
    """따옴표·띄어쓰기·문장부호 차이를 무시한 비교용 제목"""
    return re.sub(r"[\W_]+", "", t).lower()

def load_seen_titles(today):
    """최근 SEEN_DAYS일간 발행된 기사 원문 제목 로드 (seen_titles.json + 날짜별 아카이브)"""
    titles = set()
    cutoff = today - timedelta(days=SEEN_DAYS)
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text("utf-8"))
            cutoff_str = cutoff.strftime("%Y.%m.%d")
            titles |= {e["title"] for e in data.get("entries", []) if e.get("date", "") >= cutoff_str}
        except Exception as e:
            print(f"  seen_titles 로드 오류: {e}")
    # 아카이브는 실제 발행 기록이라 seen_titles.json이 유실·단축돼도 이력을 복원할 수 있음
    archive_cutoff = cutoff.strftime("%Y-%m-%d")
    for f in ARCHIVE_DIR.glob("*.html"):
        if f.stem >= archive_cutoff:
            titles |= {html.unescape(t) for t in _SRC_TITLE_RE.findall(f.read_text("utf-8"))}
    return titles

def save_seen_titles(titles, today):
    """선별된 원문 제목을 seen_titles.json에 누적 저장 (SEEN_DAYS 초과 항목 자동 제거)"""
    existing = []
    if SEEN_FILE.exists():
        try:
            existing = json.loads(SEEN_FILE.read_text("utf-8")).get("entries", [])
        except Exception:
            pass
    today_str = today.strftime("%Y.%m.%d")
    cutoff = (today - timedelta(days=SEEN_DAYS)).strftime("%Y.%m.%d")
    before = len(existing)
    existing = [e for e in existing if e.get("date", "") >= cutoff]
    seen_set = {e["title"] for e in existing}
    for t in titles:
        if t not in seen_set:
            existing.append({"title": t, "date": today_str})
    SEEN_FILE.write_text(json.dumps({"entries": existing}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  seen_titles 업데이트: 기존 {before}건 → 총 {len(existing)}건 저장")

BLACKLIST_FILE = OUTPUT_DIR / "blacklist.json"

def load_blacklist():
    """관리자가 삭제 지정한 원문 제목 목록 로드 (만료 없이 영구 차단)"""
    if not BLACKLIST_FILE.exists():
        return set()
    try:
        return set(json.loads(BLACKLIST_FILE.read_text("utf-8")).get("titles", []))
    except Exception as e:
        print(f"  blacklist 로드 오류: {e}")
        return set()

CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# 모델 설정 (되돌리려면 MODEL만 "claude-haiku-4-5"로 변경)
MODEL  = "claude-haiku-5-5"
EFFORT = "medium"   # low / medium / high — thinking 깊이. medium이 기본값

# 토큰 사용량 집계 (병렬 실행이므로 lock 필요)
_USAGE = {"input": 0, "output": 0, "calls": 0}
_USAGE_LOCK = Lock()
# 100K 토큰 이하 프롬프트 기준 단가 ($/1M tokens)
_PRICE = {"claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.00, 5.00)}

def _record_usage(usage):
    with _USAGE_LOCK:
        _USAGE["input"]  += usage.input_tokens
        _USAGE["output"] += usage.output_tokens
        _USAGE["calls"]  += 1

def print_usage_summary():
    """실행 1회분 토큰·비용 요약 (모델 비교용)"""
    inp, out, calls = _USAGE["input"], _USAGE["output"], _USAGE["calls"]
    if not calls:
        return
    pin, pout = _PRICE.get(MODEL, (0, 0))
    cost = inp / 1_000_000 * pin + out / 1_000_000 * pout
    print(f"\n  [토큰] {MODEL} (effort={EFFORT}) · 호출 {calls}회")
    print(f"    입력 {inp:,} / 출력 {out:,} 토큰")
    print(f"    1회 실행 비용 ${cost:.4f} · 월 22일 환산 ${cost*22:.2f}")

# ── 카테고리 정의 ──────────────────────────────────────────────────────────────

CATEGORIES = [
    {
        "id": "c1", "eyebrow": "Samsung Business", "badge": "Business",
        "title": "삼성 비즈니스",
        "sub": "삼성디스플레이·삼성전자 산업·제품·실적·투자 동향",
        "priority": "삼성디스플레이 직접 관련 > 삼성전자 > 관계사",
        "queries": ["삼성디스플레이", "삼성전자 실적", "삼성전자 반도체",
                    "삼성 HBM", "삼성 OLED", "삼성 파운드리", "삼성 투자 수주"],
    },
    {
        "id": "c2", "eyebrow": "Samsung Labor Relations", "badge": "Labor",
        "title": "삼성 노사관계 이슈",
        "sub": "노조·교섭·파업·임금·성과급·쟁의 동향",
        "priority": "삼성디스플레이 > 삼성전자 > 관계사 > 경쟁사",
        "queries": ["삼성디스플레이 노조", "삼성전자 노조", "삼성 파업",
                    "삼성 성과급", "삼성 임금 교섭", "삼성 쟁의"],
    },
    {
        "id": "c3", "eyebrow": "Korea Labor Trend", "badge": "KR Labor",
        "title": "대한민국 노동 관련",
        "sub": "노동시장·노동부 정책·법령·판례·고용·해고",
        "priority": "노동부 정책/법령 > 판례/행정해석 > 노동시장 동향",
        "queries": ["고용노동부", "근로기준법", "중대재해처벌법",
                    "최저임금", "주52시간", "비정규직", "임금체불",
                    "노동조합 파업", "해고 노동", "고용보험 실업급여",
                    "근로시간 단축", "노동부 정책"],
    },
    {
        "id": "c4", "eyebrow": "Safety & Mental Health", "badge": "Safety",
        "title": "삼성 마음건강 · 산재",
        "sub": "산재·직업병·정신건강·마음건강",
        "priority": "삼성디스플레이 > 삼성전자 > 관계사 > 업계 전반",
        "queries": ["반올림 삼성디스플레이", "반올림", "삼성 산재", "삼성 직업병", "직장 정신건강",
                    "직장 번아웃", "산업재해", "중대재해", "직장인 마음건강"],
    },
    {
        "id": "c5", "eyebrow": "Global AI Trend", "badge": "AI Trend",
        "title": "최신 AI 이슈",
        "sub": "생성형AI·에이전트·피지컬AI·HR AI·규제·딥페이크",
        "priority": "HR·업무 관련 AI > 규제·법 > 기술 트렌드",
        "queries": ["인공지능", "생성형AI", "AI 에이전트", "ChatGPT",
                    "AI 규제", "딥페이크", "AI 업무자동화", "OpenAI"],
    },
    {
        "id": "c6", "eyebrow": "Global Big Tech", "badge": "Big Tech",
        "title": "글로벌 빅테크 뉴스",
        "sub": "미국 빅테크·반도체·AI기업·CapEx·데이터센터",
        "priority": "반도체 공급망 > 빅테크 실적/투자 > 글로벌 동향",
        "queries": ["엔비디아", "TSMC", "마이크로소프트 AI", "구글 메타 실적",
                    "데이터센터 투자", "반도체 공급망", "빅테크 실적"],
    },
    {
        "id": "c7", "eyebrow": "Display Industry", "badge": "Display",
        "title": "디스플레이 기술/산업/경쟁사",
        "sub": "OLED·MicroLED·기술동향·LGD·BOE·CSOT 등 경쟁사",
        "priority": "삼성디스플레이 기술 > 업계 기술동향 > 경쟁사(LGD·BOE·CSOT)",
        "queries": ["OLED 기술", "MicroLED", "LG디스플레이",
                    "BOE 디스플레이", "CSOT 중국 패널", "디스플레이 시장",
                    "QD-OLED", "AMOLED", "플렉시블 디스플레이", "디스플레이 패널"],
    },
    {
        "id": "c8", "eyebrow": "Korea Hot News", "badge": "Hot News",
        "title": "대한민국 핫뉴스",
        "sub": "실시간 인기검색어·정치·경제·사회·연예·스포츠·가십",
        "priority": "실시간 화제성 기준, 분야별 분산 (정치 최대 1건)",
        "queries": [
            # 정치·정부
            "대통령", "국회", "여당 야당", "총리 장관",
            # 경제·시장
            "코스피 코스닥", "환율 달러", "금리 한국은행", "물가 인플레이션",
            # 부동산·생활
            "아파트 부동산", "전세 월세", "청약",
            # 사회·사건사고
            "사건 사고 한국", "화재 사고", "검찰 경찰 수사", "법원 판결",
            # 연예·문화
            "연예인 화제", "드라마 시청률", "K팝 아이돌", "영화 흥행",
            # 스포츠
            "야구 한국시리즈", "축구 국가대표", "골프 한국선수",
            # 트렌드·SNS 화제
            "오늘 화제", "SNS 유행", "논란", "핫이슈 한국",
            # 국제·외교
            "한미 관계", "한일 관계", "북한",
            # 날씨·재난
            "날씨 한국", "재난 한국",
        ],
    },
]

# ── 한국 공휴일 ───────────────────────────────────────────────────────────────

def _get_kr_holidays(year):
    """한국 공휴일 + 근로자의 날 반환"""
    if holidays:
        kr = holidays.Korea(years=year)
    else:
        kr = {}
    result = dict(kr)
    result[_date(year, 5, 1)] = "근로자의 날"
    return result

def is_holiday(d: _date) -> bool:
    h = _get_kr_holidays(d.year)
    return d in h or d.weekday() >= 5

def last_working_day(ref: datetime) -> datetime:
    """ref 기준 직전 워킹데이 00:00 반환"""
    d = ref.date() - timedelta(days=1)
    while is_holiday(d):
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, 0, 0, 0)

# ── 뉴스 수집 ──────────────────────────────────────────────────────────────────

_RSS_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

def _do_fetch(queries, cutoff_utc, max_per_query, when_days):
    """when_days: Google 검색의 when:Nd 연산자 기간.
    Google RSS의 published 필드는 구기사 재색인 시 현재 날짜로 위조되지만,
    when: 연산자는 Google 색인의 실제 게재일을 기준으로 동작하므로 이쪽이 신뢰 가능."""
    articles, seen = [], set()
    for query in queries:
        q = f"{query} when:{when_days}d"
        url = f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(url, agent=_RSS_AGENT)
            for entry in feed.entries[:max_per_query]:
                raw   = entry.get("title", "")
                title = raw.rsplit(" - ", 1)[0].strip() if " - " in raw else raw
                if title in seen:
                    continue
                parsed_time = entry.get("published_parsed")
                if parsed_time:
                    pub_dt = datetime(*parsed_time[:6])  # UTC
                    if pub_dt < cutoff_utc:
                        continue
                    pub_str = (pub_dt + timedelta(hours=9)).strftime("%Y.%m.%d")
                else:
                    continue  # 날짜 없는 기사는 구기사 가능성 → 제외
                seen.add(title)
                source = raw.rsplit(" - ", 1)[-1].strip() if " - " in raw else ""
                articles.append({
                    "title":     title,
                    "link":      entry.get("link", ""),
                    "published": pub_str,
                    "source":    source,
                    "query":     query,
                })
        except Exception as e:
            print(f"  RSS 오류 ({query}): {e}")
    return articles

def fetch_news(queries, today, max_per_query=20):
    # 월요일만 금요일까지 소급, 나머지는 어제 자정으로 제한 (이틀 이상 된 기사 차단)
    if today.weekday() == 0:  # 월요일: 금요일(전 워킹데이) 기사 포함
        cutoff_kst = last_working_day(today)
    else:
        cutoff_kst = datetime(today.year, today.month, today.day) - timedelta(days=1)

    # 내일이 공휴일/주말이면 뉴스 부족 가능 → 컷오프 1일 추가 확장
    tomorrow = today.date() + timedelta(days=1)
    if is_holiday(tomorrow):
        cutoff_kst -= timedelta(days=1)
        print(f"  (연휴 전날: 컷오프 1일 확장 → {cutoff_kst.strftime('%Y.%m.%d')})")

    cutoff_utc = cutoff_kst - timedelta(hours=9)
    when_days  = max(1, (today.date() - cutoff_kst.date()).days)

    articles = _do_fetch(queries, cutoff_utc, max_per_query, when_days)

    # 수집 3건 미만이면 3일 전으로 확장 재시도 (Google RSS 차단/지연 대응)
    if len(articles) < 3:
        fallback_cutoff = cutoff_utc - timedelta(days=3)
        articles = _do_fetch(queries, fallback_cutoff, max_per_query, when_days + 3)
        if articles:
            print(f"  (컷오프 3일 확장 재시도 → {len(articles)}건)")

    articles.sort(key=lambda x: x.get("published", ""), reverse=True)
    return articles

# ── 최종 선별 기사 원문 검증 ───────────────────────────────────────────────────
# AI는 RSS 제목만 보고 요약하므로 제목에 회사명이 없으면 카테고리 맥락으로 회사를 짐작하고(예: LGD 기사를
# 삼성디스플레이로 요약), RSS 날짜는 구글 재색인 시 갱신돼 구기사를 못 거른다.
# 최종 선별 기사만 네이버 뉴스 검색으로 원문을 찾아 실제 발행일·본문 발췌를 확보한 뒤
#   1) 구기사 제외  2) 본문 근거로 AI 검수·재작성  3) 카테고리 불일치 제외
#   4) 헤드라인·태그에 원문 근거 없는 주요 회사명이 남으면 제외 → 빈자리는 남은 후보로 재선별
# 원문을 못 찾으면(네이버 키 없음 포함) 제목만 근거로 2)~4)를 적용한다.

STALE_MARGIN_DAYS = 3  # 수집 기준일보다 이만큼 더 오래된 원문만 차단 (재게재 구기사는 수주~수개월 차이)
_HTTP_TIMEOUT = 8
_PUB_DATE_RES = [
    re.compile(r'<meta[^>]+property=["\']article:published_time["\'][^>]*content=["\'](\d{4})-(\d{2})-(\d{2})'),
    re.compile(r'<meta[^>]+content=["\'](\d{4})-(\d{2})-(\d{2})[^"\']*["\'][^>]*property=["\']article:published_time["\']'),
    re.compile(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})'),
]
_OG_DESC_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*content="([^"]*)"'),
    re.compile(r'<meta[^>]+content="([^"]*)"[^>]*(?:property|name)=["\'](?:og:description|description)["\']'),
]
_CHARSET_RE = re.compile(rb'charset=["\']?([\w-]+)', re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# 헤드라인·태그 검사 대상 회사 → 같은 회사를 가리키는 표기 (_norm_title 적용 후 비교)
_COMPANY_ALIASES = {
    "삼성디스플레이": ["삼성디스플레이", "삼성d", "삼성디스", "삼성디플", "sdc", "삼성lg디스플레이", "삼성lgd"],
    "삼성전자":       ["삼성전자", "삼전", "ds부문", "dx부문"],
    "LG디스플레이":   ["lg디스플레이", "lgd", "lg디스", "lgdisplay"],
    "SK하이닉스":     ["sk하이닉스", "하이닉스", "삼전닉스"],
    "BOE":            ["boe"],
}

def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": _RSS_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        raw = r.read(500_000)
        m = _CHARSET_RE.search(r.headers.get("Content-Type", "").encode()) or _CHARSET_RE.search(raw[:3000])
    charset = m.group(1).decode() if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")

def _clean_text(s):
    return html.unescape(_TAG_RE.sub("", s or "")).strip()

def _extract_pub_date(page):
    for rx in _PUB_DATE_RES:
        m = rx.search(page)
        if m:
            return _date(*map(int, m.groups()))
    return None

def _naver_search(q):
    """네이버 뉴스 검색 원본 응답. 검색 API는 2026.7 NAVER API HUB(네이버 클라우드)로 이관됨 →
    HUB 키 방식으로 먼저 호출하고, 인증 거부(401/403)면 기존 개발자센터 키 방식으로 재시도"""
    params = f"query={q}&display=10&start=1&sort=sim"
    try:
        return _http_get(f"https://naverapihub.apigw.ntruss.com/search/v1/news?{params}&format=json",
                         {"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET})
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            raise
    return _http_get(f"https://openapi.naver.com/v1/search/news.json?{params}",
                     {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET})

def _naver_find(title):
    """네이버 뉴스 검색으로 같은 제목의 기사 → {url, date, desc} (못 찾으면 None)"""
    title = re.sub(r"\s+-\s+[^-]{1,15}$", "", title)   # 끝에 붙은 " - 언론사명" 제거
    body = _naver_search(quote(re.sub(r"^\[[^\]]*\]\s*", "", title)[:80]))
    target = _norm_title(title)
    best, best_score = None, 0.0
    for it in json.loads(body).get("items", []):
        cand = _norm_title(_clean_text(it.get("title", "")))
        if not cand:
            continue
        score = 1.0 if cand == target else SequenceMatcher(None, cand, target).ratio()
        if score > best_score:
            best, best_score = it, score
    if not best or best_score < 0.85:
        return None
    pub = parsedate_to_datetime(best["pubDate"]).astimezone(KST).date() if best.get("pubDate") else None
    return {"url": best.get("originallink") or best.get("link"), "date": pub,
            "desc": _clean_text(best.get("description", ""))}

def _article_info(art):
    """원문 정보 {date, excerpt, note}. note는 원문을 못 찾은 사유 (찾았으면 None)"""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return {"date": None, "excerpt": "", "note": "네이버 API 키 없음"}
    title = art.get("src_title") or art.get("headline", "")
    try:
        found = _naver_find(title)
    except Exception as e:
        return {"date": None, "excerpt": "", "note": f"네이버 검색 실패 {type(e).__name__}: {e}"[:80]}
    if not found:
        return {"date": None, "excerpt": "", "note": "네이버에서 같은 기사 못 찾음"}
    excerpt, date = found["desc"], found["date"]
    try:
        page = _http_get(found["url"])
        og = next((m.group(1) for rx in _OG_DESC_RES for m in [rx.search(page)] if m), "")
        og = _clean_text(og)
        if og and og[:40] not in excerpt:
            excerpt = f"{og} {excerpt}".strip()
        date = date or _extract_pub_date(page)
    except Exception:
        pass   # 원문 페이지 접속 실패 시 네이버 발췌만 사용
    return {"date": date, "excerpt": excerpt[:500], "note": None}

def _unsupported_companies(art, support):
    """헤드라인·태그에 있지만 원문 제목·발췌에는 없는 주요 회사"""
    shown = _norm_title(f"{art.get('tag', '')} {art.get('headline', '')}")
    basis = _norm_title(support)
    return [co for co, al in _COMPANY_ALIASES.items()
            if any(a in shown for a in al) and not any(a in basis for a in al)]

def _verify_articles(cat, arts, infos):
    """원문 제목·발췌를 근거로 AI가 초안을 검수·재작성. 반환: (수정된 기사 목록, 카테고리 불일치 link 집합)"""
    if not arts:
        return arts, set()
    items = [{"no": i,
              "원문 제목": a.get("src_title") or a.get("headline", ""),
              "원문 발췌": infos.get(a.get("link"), {}).get("excerpt", ""),
              "초안": {k: a.get(k, "") for k in ("tag", "headline", "summary", "accent_line")}}
             for i, a in enumerate(arts, 1)]
    result = _ask_json(f"""당신은 삼성디스플레이 HR 피플팀 뉴스 브리핑의 팩트체커입니다.
카테고리: {cat['title']} — {cat['sub']}

아래 초안은 AI가 원문 제목만 보고 작성한 것입니다. 각 기사의 '원문 제목'과 '원문 발췌'(비어 있을 수 있음)를 근거로 초안을 검수해 고쳐 쓰세요.

규칙:
1. 회사명·인물명·직함·수치는 원문 제목이나 원문 발췌에 나온 것만 쓰세요. 근거 없이 주체를 삼성디스플레이·삼성전자 등으로 단정한 부분은 근거 있는 주체로 바로잡거나, 근거가 없으면 주체를 빼고 쓰세요.
2. 원문 발췌가 있으면 summary를 발췌 내용에 맞게 2~3문장으로 다시 쓰세요. 발췌에 없는 사실은 지어내지 마세요.
3. 원문 발췌가 비어 있으면 제목으로 확인되는 사실만 남기세요.
4. headline은 50자 내외, tag는 8자 이내, accent_line은 "→ "로 시작하는 HR 피플팀 관점 한 줄. 표현 방식은 초안을 따르세요.
5. 기사의 실제 주체·주제가 카테고리 설명과 맞지 않으면 fits_category를 false로 하세요. (예: '삼성 비즈니스'에 들어온 LG디스플레이 기사)

기사:
{json.dumps(items, ensure_ascii=False, indent=2)}

출력 (JSON만): {{"articles": [{{"no": 기사번호, "fits_category": true, "tag": "", "headline": "", "summary": "", "accent_line": ""}}]}}""")
    if not result:
        print(f"  [{cat['title']}] 원문 검수 AI 응답 없음 → 초안 유지")
        return arts, set()
    off = set()
    for r in result.get("articles", []):
        no = r.get("no")
        if not (isinstance(no, int) and 1 <= no <= len(arts)):
            continue
        art = arts[no - 1]
        for k in ("tag", "headline", "summary", "accent_line"):
            if isinstance(r.get(k), str) and r[k].strip():
                art[k] = r[k].strip()
        if r.get("fits_category") is False and cat["id"] != "c8":
            off.add(art.get("link"))
    return arts, off

def select_and_verify(cat, articles, today):
    """Claude 선별 → 원문 검증(구기사·회사명·카테고리) → 걸린 기사 제외 후 재선별 (최대 2회)"""
    threshold = last_working_day(today).date() - timedelta(days=STALE_MARGIN_DAYS)
    infos, excluded, stale_titles = {}, set(), []
    data = generate_category(cat, articles, today)
    for attempt in range(3):
        arts = data.get("articles", [])
        for a in arts:
            if a.get("link") and a["link"] not in infos:
                infos[a["link"]] = _article_info(a)
        bad = {}
        for a in arts:
            d = infos.get(a.get("link"), {}).get("date")
            if d and d < threshold:
                bad[a["link"]] = f"구기사 (원문 {d.strftime('%Y.%m.%d')})"
                stale_titles.append(a.get("src_title") or a.get("headline", ""))
        arts, off = _verify_articles(cat, [a for a in arts if a.get("link") not in bad], infos)
        for a in arts:
            title = a.get("src_title") or a.get("headline", "")
            if a.get("link") in off:
                bad[a["link"]] = "카테고리 불일치"
            elif cos := _unsupported_companies(a, title + " " + infos.get(a.get("link"), {}).get("excerpt", "")):
                bad[a["link"]] = f"원문 근거 없는 회사명: {', '.join(cos)}"
        found = sum(1 for a in data.get("articles", []) if not infos.get(a.get("link"), {}).get("note"))
        notes = {i["note"] for i in infos.values() if i.get("note")}
        print(f"  [{cat['title']}] 원문 확인 {found}/{len(data.get('articles', []))}건"
              + (f" (미확인 사유: {', '.join(sorted(notes))})" if notes else ""))
        data["articles"] = [a for a in arts if a.get("link") not in bad]
        if not bad:
            break
        for link, why in bad.items():
            t = next((x["title"] for x in articles if x.get("link") == link), link)
            print(f"  [{cat['title']}] 제외: {t[:40]} — {why}")
        excluded |= set(bad)
        articles = [a for a in articles if a.get("link") not in excluded]
        if attempt == 2 or not articles:
            break
        print(f"[{cat['title']}] {len(bad)}건 제외 후 재선별")
        data = generate_category(cat, articles, today)
    # 구기사는 기억해 다음 실행에서 바로 거름 (카테고리 불일치·회사명 오류는 다른 날 다른 카테고리에서 정상일 수 있어 제외)
    data["_source_titles"] = data.get("_source_titles", []) + stale_titles
    return data

# ── 카테고리 간 중복 정리 ─────────────────────────────────────────────────────
# 카테고리별 선별이 독립 병렬로 돌아 같은 기사가 여러 곳에 실림(주로 삼성 비즈니스↔디스플레이).
# 가장 맞는 1곳에만 남기고, 기사를 내준 카테고리는 남은 후보로 다시 채워 전체 건수를 유지한다.

def _art_key(art):
    return _norm_title(art.get("src_title") or art.get("headline", ""))

def _ask_json(prompt):
    """단발성 Claude 호출 → JSON dict (실패 시 None)"""
    try:
        resp = CLIENT.messages.create(
            model=MODEL, max_tokens=16000, output_config={"effort": EFFORT},
            messages=[{"role": "user", "content": prompt}],
        )
        _record_usage(resp.usage)
        if resp.stop_reason == "refusal":
            return None
        return _extract_json("".join(b.text for b in resp.content if b.type == "text").strip())
    except Exception as e:
        print(f"  Claude API 오류: {e}")
        return None

def _pick_owners(dups, titles):
    """dups: 기사키→후보 카테고리 id 목록. 기사키→남길 카테고리 id (AI 판정, 실패 시 CATEGORIES 순서상 앞선 곳)"""
    cats = {c["id"]: c for c in CATEGORIES}
    keys = list(dups)
    cat_lines = "\n".join(f"- {c['id']} {c['title']}: {c['sub']}" for c in CATEGORIES)
    items = json.dumps([{"no": i, "title": titles[k], "candidates": dups[k]} for i, k in enumerate(keys, 1)],
                       ensure_ascii=False, indent=2)
    result = _ask_json(f"""같은 기사가 여러 카테고리에 동시에 선별됐습니다.
각 기사를 내용상 가장 잘 맞는 카테고리 1곳에 배정하세요. 반드시 그 기사의 candidates 중에서만 고르세요.
기사의 주체·핵심 주제가 카테고리 설명과 가장 직접적으로 맞는 곳을 고르고,
삼성 관련 카테고리는 기사의 주체가 삼성일 때만 고르세요.

## 카테고리
{cat_lines}

## 중복 기사
{items}

출력 (JSON만): {{"assign": [{{"no": 기사번호, "category": "카테고리id"}}]}}""") or {}
    picked = {}
    for a in result.get("assign", []):
        no = a.get("no")
        if isinstance(no, int) and 1 <= no <= len(keys) and a.get("category") in dups[keys[no - 1]]:
            picked[keys[no - 1]] = a["category"]
    order = [c["id"] for c in CATEGORIES]
    for k in keys:
        if k not in picked:
            picked[k] = min(dups[k], key=order.index)
            print(f"  [카테고리 중복] AI 배정 실패 → 기본 순서 적용: {titles[k][:40]}")
        print(f"  [카테고리 중복] {titles[k][:40]} → {cats[picked[k]]['title']}에만 표시")
    return picked

def dedupe_across_categories(results, pools, today):
    """results: 카테고리id→선별 결과, pools: 카테고리id→선별 전 후보 기사 목록 (results를 직접 수정)"""
    cats = {c["id"]: c for c in CATEGORIES}
    order = [c["id"] for c in CATEGORIES]
    for rnd in range(3):
        where, titles = {}, {}
        for cid in order:
            for art in results.get(cid, {}).get("articles", []):
                k = _art_key(art)
                if k:
                    where.setdefault(k, [])
                    if cid not in where[k]:
                        where[k].append(cid)
                    titles.setdefault(k, art.get("src_title") or art.get("headline", ""))
        dups = {k: v for k, v in where.items() if len(v) > 1}
        if not dups:
            return
        owners = _pick_owners(dups, titles)
        removed = {}   # 카테고리id → 다른 곳에 넘긴 기사키
        for k, cids in dups.items():
            for cid in cids:
                if cid != owners[k]:
                    removed.setdefault(cid, set()).add(k)

        def _trimmed(cid):
            return [a for a in results[cid]["articles"] if _art_key(a) not in removed[cid]]

        if rnd == 2:   # 재선별을 반복해도 겹치면 더 채우지 않고 빼기만 함
            for cid in removed:
                results[cid]["articles"] = _trimmed(cid)
            return

        def _refill(cid):
            # 다른 카테고리에 실린(또는 배정된) 기사는 후보에서 제외
            taken = {k for k, v in where.items() if owners.get(k, v[0]) != cid}
            pool = [a for a in pools.get(cid, []) if _norm_title(a["title"]) not in taken]
            fallback = _trimmed(cid)
            try:
                data = select_and_verify(cats[cid], pool, today) if pool else {"articles": []}
            except Exception as e:
                print(f"[{cats[cid]['title']}] 중복 제외 후 재선별 오류: {e}")
                data = {"articles": []}
            if len(data.get("articles", [])) < len(fallback):
                # 재선별이 오히려 적게 골랐으면 기존 선별에서 중복만 뺀 쪽을 유지
                data = {**results[cid], "articles": fallback}
            print(f"[{cats[cid]['title']}] 중복 제외 후 재선별: {len(fallback)}건 → {len(data['articles'])}건")
            return cid, data

        with ThreadPoolExecutor(max_workers=4) as pool_exec:
            for cid, data in pool_exec.map(_refill, list(removed)):
                results[cid] = data

# ── Claude API ─────────────────────────────────────────────────────────────────

def _extract_json(text):
    import re
    if "```" in text:
        text = text.split("```", 1)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    last = text.rfind('},')
    if last > 0:
        try:
            return json.loads(text[:last+1] + ']}')
        except json.JSONDecodeError:
            pass
    return None


def _pick_diverse(cands, n=4):
    """핫뉴스: 화제성 순 후보에서 분야가 겹치지 않게 n건 선택 (부족분은 정치 외 분야로 채움)"""
    picked, fields = [], set()
    for a in cands:
        if len(picked) < n and a.get("field") not in fields:
            picked.append(a)
            fields.add(a.get("field"))
    for a in cands:
        if len(picked) < n and a not in picked and a.get("field") != "정치":
            picked.append(a)
    return picked


def _round_robin_by_query(articles, n):
    """핫뉴스: 검색어별로 번갈아 뽑아 n건 구성 (최신순으로 자르면 정치 기사로 쏠림)"""
    buckets = {}
    for a in articles:  # 이미 최신순 정렬됨
        buckets.setdefault(a.get("query"), []).append(a)
    out = []
    while len(out) < n and any(buckets.values()):
        for q in buckets:
            if buckets[q] and len(out) < n:
                out.append(buckets[q].pop(0))
    return out


def generate_category(category, articles, today=None):
    if not articles:
        return {"articles": []}
    if category["id"] == "c8":
        articles = _round_robin_by_query(articles, 40)
    else:
        articles = articles[:30]
    today_str = today.strftime("%Y.%m.%d") if today else now_kst().strftime("%Y.%m.%d")

    # 인덱스 맵 생성 (Claude에게 link 대신 idx 번호만 출력하게 함)
    link_map = {}
    indexed = []
    for i, a in enumerate(articles, 1):
        link_map[i] = a.get("link", "")
        indexed.append({
            "idx":       i,
            "title":     a.get("title", ""),
            "date":      a.get("published", ""),
            "source":    a.get("source", ""),
        })
    article_text = json.dumps(indexed, ensure_ascii=False, indent=2)

    # c8(핫뉴스)는 분야 무관 화제 기사 → HR 관점 강제 제거, 단순 요약 지시
    is_hot = category["id"] == "c8"

    if is_hot:
        prompt = f"""오늘 날짜: {today_str}
다음은 대한민국 최신 뉴스 기사 목록입니다.
아래 6개 분야마다 오늘 가장 화제인 기사를 1건씩 골라(최대 6건), 화제성 높은 순으로 정렬해 JSON만 출력하세요.
해당 분야 기사가 목록에 없을 때만 그 분야를 생략하세요. 각 기사의 field는 반드시 아래 중 하나로 지정하세요.
- field 목록: 정치 / 경제 / 사회 / 연예 / 스포츠 / 국제
- 대통령·장관·국회·정당·정부 인사가 주체이거나 발언한 기사는 주제가 문화·경제여도 무조건 "정치"
date가 오늘과 2일 이상 차이나는 오래된 기사는 제외하세요.
요약 작성 시 헤드라인의 핵심 사실을 서술한 뒤, 이 사안의 배경·맥락을 자연스럽게 덧붙여 2~3문장의 완결된 문단으로 작성하세요.
단, 헤드라인에 없는 구체적 수치·인용문은 지어내지 마세요. 인물명·기업명 등 고유명사는 원문 그대로만 쓰고, 원문에 없는 이름은 절대 추가하지 마세요.

기사 목록:
{article_text}

출력 형식 (JSON만, 설명 금지):
{{"articles": [
  {{
    "idx": 기사번호(정수),
    "field": "정치|경제|사회|연예|스포츠|국제 중 하나",
    "headline": "헤드라인 (50자 이내)",
    "summary": "2~3문장. 헤드라인의 핵심 사실 + 배경·맥락 설명.",
    "accent_line": "→ 핵심 요점 한 줄",
    "source": "언론사명",
    "date": "YYYY.MM.DD",
    "tag": "태그(6자이내)",
    "highlight": false
  }}
]}}"""
    else:
        prompt = f"""당신은 삼성디스플레이 HR 피플팀의 시니어 뉴스 에디터입니다.
오늘 날짜: {today_str}

## 카테고리
- 이름: {category['title']}
- 설명: {category['sub']}
- 우선순위: {category['priority']}

## 수집 기사
{article_text}

## 선별 기준
1. 기본 목표는 4건 선별. 관련 기사가 충분하면 반드시 4건을 채울 것. 최소 2건 이상.
2. 우선순위 준수: {category['priority']}
3. 동일 사건 중복 기사 → 1건만 유지
4. 광고·홍보·보도자료성 기사 제외 (대체 기사 없으면 가장 유익한 것 포함)
5. highlight: 가장 중요한 1건만 true
6. 오늘 날짜({today_str}) 기준 2일 이상 지난 기사는 선별하지 말 것.
7. 요약은 헤드라인의 핵심 사실을 서술한 뒤, 이 사안이 왜 중요한지·어떤 맥락인지를 자연스럽게 덧붙여 2~3문장의 완결된 문단으로 작성할 것.
   단, 헤드라인에 없는 구체적 수치·인용문은 지어내지 말 것. 인물명·기업명 등 고유명사는 원문 그대로만 쓰고, 원문에 없는 이름은 절대 추가하지 말 것.

## 출력 형식 (JSON만 출력, 설명·주석 금지)
{{
  "articles": [
    {{
      "idx": 기사번호(정수),
      "headline": "헤드라인 (핵심 수치·팩트 포함, 50자 내외)",
      "summary": "2~3문장. 헤드라인의 핵심 사실 + 왜 중요한지/배경 맥락. 원문에 없는 구체적 수치·인용문·인물명은 금지.",
      "accent_line": "→ 이 기사가 HR 피플팀에 갖는 핵심 의미 한 줄",
      "source": "언론사명",
      "date": "YYYY.MM.DD",
      "tag": "핵심태그(8자이내)",
      "highlight": false
    }}
  ]
}}"""

    idx_to_title = {item["idx"]: item["title"] for item in indexed}

    def _apply_links(result):
        """Claude가 출력한 idx로 실제 link를 매핑, 원문 제목도 기록 (관리자 삭제 기능용)"""
        source_titles = []
        for art in result.get("articles", []):
            idx = art.pop("idx", None)
            art["link"] = link_map.get(idx, "")
            src_title = idx_to_title.get(idx, "")
            art["src_title"] = src_title
            if src_title:
                source_titles.append(src_title)
        result["_source_titles"] = source_titles
        return result

    for attempt in range(2):
        try:
            if attempt == 1:
                # 재시도: 상위 5개만, 단순 지시
                top5 = json.dumps(indexed[:5], ensure_ascii=False, indent=2)
                prompt = f"""다음 기사 중 가장 중요한 1~3개를 JSON만 출력하세요.
기사: {top5}
형식: {{"articles": [{{"idx":기사번호,"headline":"제목","summary":"2~3문장 요약(핵심사실+배경)","accent_line":"→ 핵심 요점","source":"언론사","date":"YYYY.MM.DD","tag":"태그","highlight":false}}]}}"""

            resp = CLIENT.messages.create(
                model=MODEL,
                max_tokens=16000,          # thinking이 함께 쓰므로 여유 확보
                output_config={"effort": EFFORT},
                messages=[{"role": "user", "content": prompt}],
            )
            _record_usage(resp.usage)

            # Haiku 5.5는 thinking이 기본 ON → 첫 블록이 thinking일 수 있음.
            # 위치가 아닌 type으로 text 블록을 찾아야 한다.
            if resp.stop_reason == "refusal":
                cat_name = getattr(resp.stop_details, "category", None)
                print(f"  ⚠ 안전 분류기 거절 (category={cat_name}) — 재시도")
                continue
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                print(f"  ⚠ max_tokens 도달 (출력 {resp.usage.output_tokens}토큰) — JSON이 잘렸을 수 있음")

            result = _extract_json(text)
            if result and result.get("articles"):
                if is_hot:
                    print(f"  핫뉴스 후보 분야: {[a.get('field') for a in result['articles']]}")
                    result["articles"] = _pick_diverse(result["articles"])
                return _apply_links(result)
        except Exception as e:
            print(f"  Claude API 오류 (시도 {attempt+1}): {e}")

    # 2번 시도 후에도 0건 → 상위 2건 직접 변환 (Claude 거부 대응)
    if articles:
        print(f"  Claude 선별 실패 → 상위 2건 직접 포함")
        fallback = []
        for a in articles[:2]:
            fallback.append({
                "headline": a.get("title", "")[:50],
                "summary":  a.get("summary", "") or a.get("title", ""),
                "accent_line": "→ 화제 기사",
                "source":   a.get("source", ""),
                "link":     a.get("link", ""),
                "date":     a.get("published", ""),
                "tag":      "핫이슈",
                "highlight": False,
            })
        return {"articles": fallback}

    return {"articles": []}

# ── HTML 렌더링 ────────────────────────────────────────────────────────────────

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700;900&family=Bebas+Neue&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Noto Sans KR',sans-serif;background:#EDEAE3;min-height:100vh;padding:48px 32px 80px;}
.page-header{max-width:960px;margin:0 auto 28px;display:flex;align-items:flex-end;justify-content:space-between;padding-bottom:18px;border-bottom:3px solid #1A1A1A;}
.page-label{font-size:12px;font-weight:700;letter-spacing:2.5px;text-transform:uppercase;color:#999;margin-bottom:7px;}
.page-title{font-size:34px;font-weight:900;color:#1A1A1A;}
.page-date{font-size:15px;font-weight:500;color:#555;text-align:right;}
.page-period{font-size:12px;color:#AAA;margin-top:4px;text-align:right;}
.session-badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:1px;padding:4px 12px;border-radius:20px;margin-top:6px;}
.session-morning{background:#FFF3CD;color:#856404;}
.session-afternoon{background:#D1ECF1;color:#0C5460;}

.install-banner{display:none;max-width:960px;margin:0 auto 20px;background:#1A1A1A;color:#EDEAE3;border-radius:14px;padding:14px 20px;align-items:center;justify-content:space-between;gap:16px;font-size:13px;line-height:1.5;flex-wrap:wrap;}
.install-banner.show{display:flex;}
.install-banner b{font-weight:700;}
.install-btn{flex-shrink:0;background:#EDEAE3;color:#1A1A1A;border:none;font-size:12px;font-weight:700;padding:8px 16px;border-radius:20px;cursor:pointer;}
.install-close{flex-shrink:0;background:transparent;color:#999;border:none;font-size:18px;cursor:pointer;line-height:1;padding:0 4px;}

.filter-bar{max-width:960px;margin:0 auto 32px;background:#fff;border-radius:16px;padding:18px 24px;box-shadow:0 2px 12px rgba(0,0,0,0.06);}
.filter-label{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#AAA;margin-bottom:12px;}
.filter-checks{display:flex;flex-wrap:wrap;gap:10px;}
.filter-checks label{display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;}
.filter-checks input[type=checkbox]{width:16px;height:16px;cursor:pointer;accent-color:#1A1A1A;}
.filter-checks .chip{font-size:12px;font-weight:700;padding:4px 12px;border-radius:20px;letter-spacing:0.3px;}
.select-all{font-size:12px;font-weight:700;color:#888;background:#F0EEE9;border:none;padding:5px 14px;border-radius:20px;cursor:pointer;transition:background 0.15s;}
.select-all:hover{background:#E5E2DC;color:#444;}

.cards-grid{max-width:960px;margin:0 auto;display:flex;flex-direction:column;gap:32px;}
.card{background:#fff;border-radius:20px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);transition:opacity 0.2s;}
.card.hidden{display:none;}
.card-header{padding:28px 36px 24px;display:flex;align-items:flex-start;justify-content:space-between;gap:16px;}
.header-eyebrow{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,0.5);margin-bottom:7px;}
.header-title{font-size:28px;font-weight:900;color:#fff;line-height:1.2;}
.header-sub{font-size:13.5px;color:rgba(255,255,255,0.5);margin-top:6px;line-height:1.5;}
.badge{flex-shrink:0;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;padding:5px 14px;border-radius:24px;margin-top:4px;white-space:nowrap;}
.card-body{padding:20px 32px 8px;}
.news-list{display:flex;flex-direction:column;gap:14px;}
.news-item{display:flex;flex-direction:row;gap:20px;align-items:flex-start;padding:20px 22px;border-radius:14px;background:#F7F6F2;text-decoration:none;transition:background 0.15s,transform 0.12s;}
.news-item:hover{background:#EEECEA;transform:translateY(-2px);}
.news-num{font-family:'Bebas Neue',sans-serif;font-size:44px;line-height:1;flex-shrink:0;min-width:36px;margin-top:2px;}
.news-content{flex:1;min-width:0;}
.news-tag-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.tag{display:inline-block;font-size:11.5px;font-weight:700;padding:3px 10px;border-radius:5px;}
.source-badge{font-size:11.5px;color:#BBB;}
.news-date{font-size:11px;color:#CCC;font-weight:500;background:#F0EEE9;padding:2px 7px;border-radius:4px;}
.news-headline{font-size:16px;font-weight:700;color:#1A1A1A;line-height:1.5;margin-bottom:9px;}
.news-summary{font-size:13.5px;color:#555;line-height:1.8;margin-bottom:10px;}
.accent-line{font-size:13px;color:#555;line-height:1.6;padding-left:12px;border-left:3px solid;font-weight:600;}
.card-footer{padding:14px 36px 18px;border-top:2px solid;display:flex;align-items:center;justify-content:space-between;margin-top:12px;}
.footer-label{font-size:12px;color:#CCC;font-weight:500;letter-spacing:0.8px;}
.footer-date{font-size:13px;font-weight:700;}

.empty-state{padding:32px;text-align:center;color:#BBB;font-size:14px;}

/* 카드 색상 */
.c1 .card-header{background:linear-gradient(135deg,#0A2342,#0D3260);}
.c1 .badge{background:rgba(29,158,117,.25);color:#6EEFC7;border:1px solid rgba(29,158,117,.4);}
.c1 .news-num,.c1 .accent-line{color:#1D9E75;}.c1 .tag{background:#E1F5EE;color:#085041;}
.c1 .accent-line{border-color:#1D9E75;}.c1 .card-footer{border-color:#E1F5EE;}.c1 .footer-date{color:#1D9E75;}

.c2 .card-header{background:linear-gradient(135deg,#2D0A0A,#4A1515);}
.c2 .badge{background:rgba(226,75,74,.25);color:#FF9898;border:1px solid rgba(226,75,74,.4);}
.c2 .news-num{color:#E24B4A;}.c2 .tag{background:#FCEBEB;color:#A32D2D;}
.c2 .accent-line{border-color:#E24B4A;}.c2 .card-footer{border-color:#FCEBEB;}.c2 .footer-date{color:#A32D2D;}

.c3 .card-header{background:linear-gradient(135deg,#2C1800,#4A2D00);}
.c3 .badge{background:rgba(186,117,23,.3);color:#FAC775;border:1px solid rgba(186,117,23,.4);}
.c3 .news-num{color:#BA7517;}.c3 .tag{background:#FAEEDA;color:#633806;}
.c3 .accent-line{border-color:#BA7517;}.c3 .card-footer{border-color:#FAEEDA;}.c3 .footer-date{color:#BA7517;}

.c4 .card-header{background:linear-gradient(135deg,#0F2E1A,#17472A);}
.c4 .badge{background:rgba(99,153,34,.25);color:#AADF6A;border:1px solid rgba(99,153,34,.4);}
.c4 .news-num{color:#639922;}.c4 .tag{background:#EAF3DE;color:#3B6D11;}
.c4 .accent-line{border-color:#639922;}.c4 .card-footer{border-color:#EAF3DE;}.c4 .footer-date{color:#639922;}

.c5 .card-header{background:linear-gradient(135deg,#0C1E3B,#162E58);}
.c5 .badge{background:rgba(55,138,221,.25);color:#7EC3FF;border:1px solid rgba(55,138,221,.4);}
.c5 .news-num{color:#378ADD;}.c5 .tag{background:#E6F1FB;color:#0C447C;}
.c5 .accent-line{border-color:#378ADD;}.c5 .card-footer{border-color:#E6F1FB;}.c5 .footer-date{color:#185FA5;}

.c6 .card-header{background:linear-gradient(135deg,#1A0A30,#2E1555);}
.c6 .badge{background:rgba(127,119,221,.3);color:#CECBF6;border:1px solid rgba(127,119,221,.4);}
.c6 .news-num{color:#7F77DD;}.c6 .tag{background:#EEEDFE;color:#3C3489;}
.c6 .accent-line{border-color:#7F77DD;}.c6 .card-footer{border-color:#EEEDFE;}.c6 .footer-date{color:#534AB7;}

.c7 .card-header{background:linear-gradient(135deg,#003366,#005599);}
.c7 .badge{background:rgba(0,120,220,.3);color:#7ECFFF;border:1px solid rgba(0,120,220,.4);}
.c7 .news-num{color:#0078DC;}.c7 .tag{background:#E0F2FF;color:#004C8C;}
.c7 .accent-line{border-color:#0078DC;}.c7 .card-footer{border-color:#E0F2FF;}.c7 .footer-date{color:#0057B3;}
.c8 .card-header{background:linear-gradient(135deg,#1A1A2E,#16213E);}
.c8 .badge{background:rgba(229,57,53,.25);color:#FF8A80;border:1px solid rgba(229,57,53,.4);}
.c8 .news-num{color:#E53935;}.c8 .tag{background:#FEEBEE;color:#B71C1C;}
.c8 .accent-line{border-color:#E53935;}.c8 .card-footer{border-color:#FEEBEE;}.c8 .footer-date{color:#C62828;}

/* 체크박스 칩 */
.chip-c1{background:#E1F5EE;color:#085041;}.chip-c2{background:#FCEBEB;color:#A32D2D;}
.chip-c3{background:#FAEEDA;color:#633806;}.chip-c4{background:#EAF3DE;color:#3B6D11;}
.chip-c5{background:#E6F1FB;color:#0C447C;}.chip-c6{background:#EEEDFE;color:#3C3489;}
.chip-c7{background:#E0F2FF;color:#004C8C;}.chip-c8{background:#FEEBEE;color:#B71C1C;}
.page-footer{max-width:960px;margin:36px auto 0;display:flex;align-items:center;justify-content:space-between;padding-top:16px;border-top:1px solid #CCC;}
.footer-note{font-size:12px;color:#AAA;line-height:1.6;}
.footer-tag{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#BBB;}

@media(max-width:640px){
  body{padding:20px 14px 60px;}
  .page-header{flex-direction:column;align-items:flex-start;gap:10px;}
  .page-title{font-size:26px;}
  .filter-bar{padding:14px 16px;}
  .card-header{padding:20px 20px 18px;}
  .header-title{font-size:22px;}
  .card-body{padding:14px 14px 6px;}
  .news-item{padding:16px 14px;gap:14px;}
  .news-num{font-size:34px;}
  .card-footer{padding:12px 20px 14px;}
}
"""

FILTER_JS = """
<script>
(function(){
  var checks = document.querySelectorAll('.filter-checks input[type=checkbox]');
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem('briefFilter')); } catch(e){}

  function applyFilter(){
    var state = {};
    checks.forEach(function(cb){
      state[cb.dataset.card] = cb.checked;
      var card = document.getElementById(cb.dataset.card);
      if(card) card.classList.toggle('hidden', !cb.checked);
    });
    try { localStorage.setItem('briefFilter', JSON.stringify(state)); } catch(e){}
  }

  if(saved){
    checks.forEach(function(cb){
      if(saved[cb.dataset.card] !== undefined) cb.checked = saved[cb.dataset.card];
    });
  }
  applyFilter();

  checks.forEach(function(cb){ cb.addEventListener('change', applyFilter); });

  var btn = document.getElementById('selectAll');
  if(btn) btn.addEventListener('click', function(){
    var allOn = Array.from(checks).every(function(c){ return c.checked; });
    checks.forEach(function(cb){ cb.checked = !allOn; });
    btn.textContent = allOn ? '전체 선택' : '전체 해제';
    applyFilter();
    setTimeout(function(){ btn.textContent = allOn ? '전체 해제' : '전체 선택'; }, 1000);
  });
})();
</script>
"""

INSTALL_HTML = """
<div class="install-banner" id="installBanner">
  <span id="installMsg"><b>홈 화면에 앱으로 추가</b>하면 알림 아이콘처럼 바로 열어볼 수 있어요.</span>
  <button class="install-btn" id="installBtn" style="display:none;">설치하기</button>
  <button class="install-close" id="installClose" aria-label="닫기">×</button>
</div>"""

INSTALL_JS = """
<script>
(function(){
  var banner = document.getElementById('installBanner');
  var msg = document.getElementById('installMsg');
  var btn = document.getElementById('installBtn');
  var closeBtn = document.getElementById('installClose');
  if (!banner) return;

  var isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  var dismissed = false;
  try { dismissed = localStorage.getItem('installBannerDismissed') === '1'; } catch(e){}
  if (isStandalone || dismissed) return;

  var ua = window.navigator.userAgent;
  var isIOS = /iPad|iPhone|iPod/.test(ua) && !window.MSStream;
  var deferredPrompt = null;

  window.addEventListener('beforeinstallprompt', function(e){
    e.preventDefault();
    deferredPrompt = e;
    btn.style.display = 'inline-block';
    banner.classList.add('show');
  });

  if (isIOS) {
    msg.innerHTML = '<b>홈 화면에 앱으로 추가</b>하려면 하단 공유 버튼(⬆️) → \\'홈 화면에 추가\\'를 눌러주세요.';
    banner.classList.add('show');
  }

  btn.addEventListener('click', function(){
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    deferredPrompt.userChoice.finally(function(){
      deferredPrompt = null;
      banner.classList.remove('show');
    });
  });

  closeBtn.addEventListener('click', function(){
    banner.classList.remove('show');
    try { localStorage.setItem('installBannerDismissed', '1'); } catch(e){}
  });
})();
</script>
"""


def render_filter_bar():
    checks = ""
    for cat in CATEGORIES:
        cid = cat["id"]
        checks += f'\n    <label><input type="checkbox" data-card="{cid}" checked><span class="chip chip-{cid}">{cat["title"]}</span></label>'
    return f"""
<div class="filter-bar">
  <div class="filter-label">카테고리 선택</div>
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
    <button class="select-all" id="selectAll">전체 해제</button>
    <div class="filter-checks">{checks}
    </div>
  </div>
</div>"""


def render_card(category, data, period):
    cid = category["id"]
    arts = data.get("articles", [])

    if not arts:
        items_html = '<div class="empty-state">수집된 기사가 없습니다.</div>'
    else:
        items_html = ""
        for i, art in enumerate(arts, 1):
            summary = art.get("summary", "").replace("\n", " ")
            src_title_attr = art.get("src_title", "").replace('"', "&quot;")
            items_html += f"""
      <a class="news-item" href="{art.get('link','#')}" target="_blank" rel="noopener" data-src-title="{src_title_attr}">
        <div class="news-num">{i:02d}</div>
        <div class="news-content">
          <div class="news-tag-row">
            <span class="tag">{art.get('tag','')}</span>
            <span class="source-badge">{art.get('source','')} ↗</span>
            <span class="news-date">{art.get('date','')}</span>
          </div>
          <div class="news-headline">{art.get('headline','')}</div>
          <div class="news-summary">{summary}</div>
          <div class="accent-line">{art.get('accent_line','')}</div>
        </div>
      </a>"""

    return f"""
<div class="card {cid}" id="{cid}">
  <div class="card-header">
    <div>
      <div class="header-eyebrow">{category['eyebrow']}</div>
      <div class="header-title">{category['title']}</div>
      <div class="header-sub">{category['sub']}</div>
    </div>
    <span class="badge">{category['badge']}</span>
  </div>
  <div class="card-body"><div class="news-list">{items_html}
  </div></div>
  <div class="card-footer">
    <span class="footer-label">Samsung Display · HR People Team</span>
    <span class="footer-date">{period}</span>
  </div>
</div>"""


def render_html(all_data, today, session_label="morning"):
    days  = ["월","화","수","목","금","토","일"]
    period_start = last_working_day(today)
    period = f"{period_start.strftime('%Y.%m.%d')} – {today.strftime('%m.%d')}"
    date_display = f"{today.year}년 {today.month}월 {today.day}일 ({days[today.weekday()]})"

    if session_label == "afternoon":
        session_html = '<span class="session-badge session-afternoon">오후 브리핑 (17:00)</span>'
        session_title = "오후 브리핑"
    else:
        session_html = '<span class="session-badge session-morning">아침 브리핑 (07:00)</span>'
        session_title = "아침 브리핑"

    cards_html = "".join(render_card(cat, data, period) for cat, data in zip(CATEGORIES, all_data))
    generated = today.strftime('%Y.%m.%d %H:%M')

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="description" content="삼성디스플레이 HR 피플팀 데일리 뉴스 브리핑 {today.strftime('%Y.%m.%d')}">
<title>피플팀 데일리 브리핑 | {today.strftime('%Y.%m.%d')} {session_title}</title>
<link rel="manifest" href="/Hr-Daily-Brief/manifest.json">
<meta name="theme-color" content="#1A1A1A">
<link rel="icon" href="/Hr-Daily-Brief/icons/favicon-32.png" sizes="32x32">
<link rel="icon" href="/Hr-Daily-Brief/icons/favicon-16.png" sizes="16x16">
<link rel="apple-touch-icon" href="/Hr-Daily-Brief/icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="데일리 브리핑">
<style>{CSS}</style>
<script>
if ('serviceWorker' in navigator) {{
  window.addEventListener('load', () => {{
    navigator.serviceWorker.register('/Hr-Daily-Brief/sw.js', {{ scope: '/Hr-Daily-Brief/' }}).catch(() => {{}});
  }});
}}
</script>
</head>
<body>

<div class="page-header">
  <div>
    <div class="page-label">Samsung Display · HR People Team Daily</div>
    <div class="page-title">피플팀 데일리 뉴스 브리핑</div>
    {session_html}
  </div>
  <div>
    <div class="page-date">{date_display}</div>
    <div class="page-period">수집 기간 : {period}</div>
    <div class="page-period" style="color:#BBB;font-size:11px;">생성: {generated}</div>
  </div>
</div>

{INSTALL_HTML}

{render_filter_bar()}

<div class="cards-grid">
{cards_html}
</div>

<div class="page-footer">
  <span class="footer-note">※ {generated} 생성 · 수집기간: {period}<br>내부 정보 공유 목적으로만 활용</span>
  <span class="footer-tag">Samsung Display · HR People Team</span>
</div>

{FILTER_JS}
{INSTALL_JS}
</body>
</html>"""


# ── 이메일 발송 ───────────────────────────────────────────────────────────────

def send_email(all_data, today, session_label="morning"):
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECIPIENT:
        print("  이메일 설정 없음, 건너뜀")
        return

    days = ["월","화","수","목","금","토","일"]
    day_str = days[today.weekday()]
    period_start = last_working_day(today)
    period = f"{period_start.strftime('%Y.%m.%d')} – {today.strftime('%m.%d')}"
    session_str = "오후" if session_label == "afternoon" else ""
    subject = f"[피플팀 데일리 브리핑] {today.strftime('%Y.%m.%d')} ({day_str})"
    url = "https://juno99zz-arkt.github.io/Hr-Daily-Brief/"

    rows_html = ""
    for cat, data in zip(CATEGORIES, all_data):
        arts = data.get("articles", [])
        if not arts:
            continue
        first = arts[0]
        rows_html += f"""
        <tr>
          <td style="padding:12px 16px;border-bottom:1px solid #eee;">
            <div style="font-size:11px;color:#999;font-weight:700;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px;">{cat['eyebrow']}</div>
            <div style="font-size:13px;font-weight:700;color:#1A1A1A;line-height:1.5;">{first.get('headline','')}</div>
            <div style="font-size:12px;color:#888;margin-top:4px;">{first.get('source','')} · {first.get('date','')}</div>
          </td>
        </tr>"""

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#EDEAE3;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
  <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">
    <tr><td style="background:linear-gradient(135deg,#0A2342,#0D3260);padding:32px 36px;">
      <div style="font-size:11px;color:rgba(255,255,255,0.5);font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Samsung Display · HR People Team</div>
      <div style="font-size:24px;font-weight:900;color:#fff;margin-bottom:6px;">피플팀 데일리 뉴스 브리핑</div>
      <div style="font-size:13px;color:rgba(255,255,255,0.6);">{today.strftime('%Y년 %m월 %d일')} ({day_str}) · 수집기간: {period}</div>
    </td></tr>
    <tr><td style="padding:24px 36px 12px;">
      <a href="{url}" style="display:inline-block;background:#1A1A1A;color:#fff;text-decoration:none;padding:12px 28px;border-radius:8px;font-size:14px;font-weight:700;">전체 브리핑 보기 →</a>
    </td></tr>
    <tr><td style="padding:8px 36px 16px;">
      <div style="font-size:11px;font-weight:700;color:#AAA;letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;">Today's Headlines</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:8px;overflow:hidden;">
        {rows_html}
      </table>
    </td></tr>
    <tr><td style="padding:12px 36px 28px;">
      <div style="font-size:11px;color:#BBB;">※ 내부 정보 공유 목적으로만 활용 · Samsung Display HR People Team</div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""

    recipients = [r.strip() for r in EMAIL_RECIPIENT.split(",") if r.strip()]
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    html_file = OUTPUT_DIR / "index.html"
    if html_file.exists():
        attach_name = f"피플팀_데일리브리핑_{today.strftime('%Y%m%d')}.html"
        part = MIMEBase("application", "octet-stream")
        part.set_payload(html_file.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=attach_name)
        msg.attach(part)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        print(f"  이메일 발송 완료 → {', '.join(recipients)}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"  이메일 발송 오류 (인증 실패): {e}")
        print("  ⚠ Gmail 앱 비밀번호가 만료됐습니다. GitHub Secrets의 EMAIL_PASSWORD를 갱신하세요.")
        sys.exit(1)  # 워크플로 실패 → GitHub 이메일 알림
    except Exception as e:
        print(f"  이메일 발송 오류: {e}")


# ── Git 배포 (로컬 전용) ───────────────────────────────────────────────────────

def git_deploy(today):
    if IS_CI:
        print("  CI 환경: git 배포는 워크플로가 처리합니다.")
        return
    repo = str(OUTPUT_DIR)
    ts   = today.strftime("%Y-%m-%d %H:%M")
    try:
        subprocess.run(["git", "add", "index.html"], cwd=repo, check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
        if result.returncode == 0:
            print("  변경 없음, 커밋 생략")
            return
        subprocess.run(["git", "commit", "-m", f"브리핑 업데이트: {ts}"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "origin", "gh-pages"], cwd=repo, check=True, capture_output=True)
        print("  배포 완료 → https://juno99zz-arkt.github.io/Hr-Daily-Brief/")
    except subprocess.CalledProcessError as e:
        print(f"  배포 오류: {e}")


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["morning","afternoon"], default=None)
    parser.add_argument("--force",   action="store_true", help="공휴일에도 강제 실행")
    args = parser.parse_args()

    today = now_kst()
    hour  = today.hour

    # 세션 결정
    if args.session:
        session = args.session
    else:
        session = "afternoon" if hour >= 12 else "morning"

    print(f"\n{'='*55}")
    print(f"  삼성디스플레이 HR 데일리 브리핑 | {session.upper()}")
    print(f"  {today.strftime('%Y-%m-%d %H:%M:%S')} (KST)")
    print(f"{'='*55}\n")

    # 공휴일/주말 체크
    if not args.force and is_holiday(today.date()):
        print(f"  오늘({today.strftime('%Y-%m-%d')})은 공휴일/주말입니다. 실행 건너뜀.")
        print("  강제 실행: --force 옵션 사용\n")
        return

    # ── 카테고리별 병렬 처리 ──────────────────────────────────────────────────
    seen_titles = load_seen_titles(today)
    if seen_titles:
        print(f"  seen_titles 로드: {len(seen_titles)}건 (중복 차단 대상)\n")
    blacklist = load_blacklist()
    if blacklist:
        print(f"  blacklist 로드: {len(blacklist)}건 (관리자 영구 차단)\n")
    blocked = {_norm_title(t) for t in seen_titles | blacklist}
    credit_errors = 0

    def _process(cat):
        nonlocal credit_errors
        mpq = 10 if cat["id"] == "c8" else 20
        articles = fetch_news(cat["queries"], today, max_per_query=mpq)
        # 이미 선별된 적 있거나 관리자가 삭제한 기사 제목 제거
        if blocked:
            before = len(articles)
            articles = [a for a in articles if _norm_title(a["title"]) not in blocked]
            filtered = before - len(articles)
            if filtered:
                print(f"[{cat['title']}] 차단 필터: {filtered}건 제거 → {len(articles)}건 남음")
        if not articles:
            print(f"[{cat['title']}] 수집: 0건 → 건너뜀")
            return cat["id"], {"articles": []}, []
        print(f"[{cat['title']}] 수집: {len(articles)}건 → Claude 분석 중...")
        try:
            data = select_and_verify(cat, articles, today)
            cnt  = len(data.get("articles", []))
            print(f"[{cat['title']}] 선별: {cnt}건 완료")
            return cat["id"], data, articles
        except Exception as e:
            errmsg = str(e)
            if "credit balance is too low" in errmsg:
                credit_errors += 1
                print(f"[{cat['title']}] ⚠ 크레딧 부족")
            else:
                print(f"[{cat['title']}] 오류: {e}")
            return cat["id"], {"articles": []}, articles

    results, pools = {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_process, cat): cat for cat in CATEGORIES}
        for future in as_completed(futures):
            cid, data, cands = future.result()
            results[cid], pools[cid] = data, cands

    if not credit_errors:
        dedupe_across_categories(results, pools, today)

    all_data = [results[cat["id"]] for cat in CATEGORIES]
    print_usage_summary()

    # 크레딧 부족 감지 → 워크플로 실패로 GitHub 알림
    if credit_errors > 0:
        print(f"\n  ⚠ {credit_errors}개 카테고리 크레딧 부족으로 실패.")
        print("  Anthropic 콘솔에서 크레딧을 충전하세요: https://console.anthropic.com/settings/billing")
        sys.exit(1)

    # 선별된 원문 제목 수집 → seen_titles 저장
    source_titles = set()
    for data in all_data:
        for t in data.pop("_source_titles", []):
            source_titles.add(t)
    if source_titles:
        save_seen_titles(source_titles, today)

    html = render_html(all_data, today, session)
    out  = OUTPUT_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"  HTML 저장 완료 → {out}\n")

    print("  GitHub 배포 중...")
    git_deploy(today)

    print("  이메일 발송 중...")
    send_email(all_data, today, session)

    print(f"\n{'='*55}")
    print(f"  완료! {today.strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
