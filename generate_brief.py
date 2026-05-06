#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""삼성디스플레이 HR 피플팀 | 데일리 뉴스 브리핑 자동 생성기"""

import sys, os, json, subprocess, feedparser, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta, date as _date
from pathlib import Path
from urllib.parse import quote

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

OUTPUT_DIR = Path(__file__).parent / "public"
OUTPUT_DIR.mkdir(exist_ok=True)

CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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
        "queries": ["고용노동부", "근로기준법", "노동법 개정",
                    "노동 판례", "중대재해처벌법", "고용 해고", "노동시장 동향"],
    },
    {
        "id": "c4", "eyebrow": "Safety & Mental Health", "badge": "Safety",
        "title": "삼성 마음건강 · 산재",
        "sub": "산재·직업병·정신건강·마음건강",
        "priority": "삼성디스플레이 > 삼성전자 > 관계사 > 업계 전반",
        "queries": ["삼성 산재", "삼성 직업병", "직장 정신건강",
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
                    "BOE 디스플레이", "CSOT 중국 패널", "디스플레이 시장"],
    },
    {
        "id": "c8", "eyebrow": "Korea Hot News", "badge": "Hot News",
        "title": "대한민국 핫뉴스",
        "sub": "실시간 인기검색어·정치·경제·사회·연예·스포츠·가십",
        "priority": "실시간 화제성·언급량·관심도 높은 순 (분야 무관)",
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

def fetch_news(queries, max_per_query=20):
    today = datetime.now()
    # KST 기준 전일 워킹데이 자정 → UTC 변환 (KST = UTC+9)
    cutoff_kst = last_working_day(today)
    cutoff_utc = cutoff_kst - timedelta(hours=9)

    articles, seen = [], set()
    for query in queries:
        url = f"https://news.google.com/rss/search?q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(url)
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
                    pub_str = ""

                seen.add(title)
                source = raw.rsplit(" - ", 1)[-1].strip() if " - " in raw else ""
                articles.append({
                    "title":     title,
                    "link":      entry.get("link", ""),
                    "published": pub_str,
                    "summary":   entry.get("summary", "")[:400],
                    "source":    source,
                })
        except Exception as e:
            print(f"  RSS 오류 ({query}): {e}")

    articles.sort(key=lambda x: x.get("published", ""), reverse=True)
    return articles

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


def generate_category(category, articles):
    articles = articles[:30]
    article_text = json.dumps(articles, ensure_ascii=False, indent=2)

    prompt = f"""당신은 삼성디스플레이 HR 피플팀의 시니어 뉴스 에디터입니다.

## 카테고리
- 이름: {category['title']}
- 설명: {category['sub']}
- 우선순위: {category['priority']}

## 수집 기사
{article_text}

## 선별 기준
1. 최소 1건, 최대 4건 선별 (기사가 없거나 부적합하면 1건 미만도 가능)
2. 우선순위 준수: {category['priority']}
3. 동일 사건 중복 기사 → 1건만 유지
4. 광고·홍보·보도자료성 기사 제외 (대체 기사 없으면 가장 유익한 것 포함)
5. highlight: 가장 중요한 1건만 true

## 출력 형식 (JSON만 출력, 설명·주석 금지)
{{
  "articles": [
    {{
      "headline": "헤드라인 (핵심 수치·팩트 포함, 50자 내외)",
      "summary": "3~4문장. 기사에 나온 수치·사실·발언 중심. 해석·시사점 없이 사실만.",
      "accent_line": "→ 이 기사가 HR 피플팀에 갖는 핵심 의미 한 줄",
      "source": "언론사명",
      "link": "기사 URL",
      "date": "YYYY.MM.DD",
      "tag": "핵심태그(8자이내)",
      "highlight": false
    }}
  ]
}}"""

    for attempt in range(2):
        try:
            if attempt == 1:
                # 재시도: 상위 5개만, 단순 지시
                top5 = json.dumps(articles[:5], ensure_ascii=False, indent=2)
                prompt = f"""다음 기사 중 가장 중요한 1~3개를 JSON만 출력하세요.
기사: {top5}
형식: {{"articles": [{{"headline":"제목","summary":"3문장 사실 요약","accent_line":"→ 핵심 의미","source":"언론사","link":"URL","date":"YYYY.MM.DD","tag":"태그","highlight":false}}]}}"""

            resp = CLIENT.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                messages=[{"role": "user", "content": prompt}],
            )
            result = _extract_json(resp.content[0].text.strip())
            if result and result.get("articles"):
                return result
        except Exception as e:
            print(f"  Claude API 오류 (시도 {attempt+1}): {e}")

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
            items_html += f"""
      <a class="news-item" href="{art.get('link','#')}" target="_blank" rel="noopener">
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


def render_html(all_data, session_label="morning"):
    today = datetime.now()
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
<style>{CSS}</style>
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

{render_filter_bar()}

<div class="cards-grid">
{cards_html}
</div>

<div class="page-footer">
  <span class="footer-note">※ {generated} 생성 · 수집기간: {period}<br>내부 정보 공유 목적으로만 활용</span>
  <span class="footer-tag">Samsung Display · HR People Team</span>
</div>

{FILTER_JS}
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
    session_str = "오후" if session_label == "afternoon" else "아침"
    subject = f"[피플팀 데일리 브리핑] {today.strftime('%Y.%m.%d')} ({day_str}) {session_str}"
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
      <div style="font-size:13px;color:rgba(255,255,255,0.6);">{today.strftime('%Y년 %m월 %d일')} ({day_str}) · {session_str} 브리핑 · 수집기간: {period}</div>
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
        attach_name = f"피플팀_데일리브리핑_{today.strftime('%Y%m%d')}_{session_str}.html"
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

    today = datetime.now()
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

    all_data = []
    for cat in CATEGORIES:
        print(f"[{cat['title']}]")
        # c8 핫뉴스는 쿼리가 많으므로 쿼리당 수집량을 줄여 속도 확보
        mpq = 10 if cat["id"] == "c8" else 20
        articles = fetch_news(cat["queries"], max_per_query=mpq)
        print(f"  수집: {len(articles)}건 → Claude 분석 중...")
        try:
            data = generate_category(cat, articles)
            cnt  = len(data.get("articles", []))
            print(f"  선별: {cnt}건 완료\n")
        except Exception as e:
            print(f"  오류: {e}\n")
            data = {"articles": []}
        all_data.append(data)

    html = render_html(all_data, session)
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
