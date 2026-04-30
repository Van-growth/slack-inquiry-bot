import os
import hmac
import hashlib
import time
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import anthropic
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")  # #도입문의 채널 ID (선택)

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")

MENTION_SMALL = "<@D08K3KKKR36> <@D08HPVD794J>"  # 임직원 100명 미만
MENTION_LARGE = "<@D08GPQ50TPZ>"                  # 임직원 100명 이상 / fallback

# 중복 이벤트 방지 (Slack은 동일 이벤트를 재전송할 수 있음)
processed_events: set[str] = set()
processed_events_lock = threading.Lock()

last_processed: dict[str, float] = {}
last_processed_lock = threading.Lock()
DUPLICATE_THRESHOLD_SECONDS = 60

executor = ThreadPoolExecutor(max_workers=5)


# ──────────────────────────────────────────────────
# 슬랙 서명 검증
# ──────────────────────────────────────────────────
def verify_slack_signature(req) -> bool:
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False

    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    slack_sig = req.headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(expected, slack_sig)


# ──────────────────────────────────────────────────
# Step 1: Claude로 회사명·이메일 도메인 파싱
# ──────────────────────────────────────────────────
def parse_company_info(message_text: str) -> dict:
    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음 도입문의 메시지에서 회사명과 이메일 도메인을 추출해주세요.\n\n"
                    f"메시지:\n{message_text}\n\n"
                    "반드시 아래 JSON 형식으로만 응답하세요. 없는 항목은 null:\n"
                    '{"company_name": "회사명", "email_domain": "example.com"}'
                ),
            }
        ],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    # 마크다운 코드블록 제거 후 JSON 파싱
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[PARSE] JSON 파싱 실패, 기본값 반환. 원본: {text!r}", flush=True)
        return {"company_name": None, "email_domain": None}


# ──────────────────────────────────────────────────
# 네이버 검색 API 호출
# ──────────────────────────────────────────────────
def _strip_html(text: str) -> str:
    if not text:
        return ""
    try:
        cleaned = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    except Exception:
        cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def naver_search(endpoint: str, query: str, display: int) -> list[dict]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        print("[NAVER] 자격증명 없음, 검색 스킵", flush=True)
        return []
    url = f"https://openapi.naver.com/v1/search/{endpoint}.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": query, "display": display}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("items", []) or []
    except Exception as e:
        print(f"[NAVER] {endpoint} 검색 실패: {e}", flush=True)
        return []


def collect_naver_context(company_name: str) -> str:
    news_items = naver_search("news", company_name, 10)
    web_items = naver_search("webkr", company_name, 5)

    blocks: list[str] = []
    if news_items:
        blocks.append("[뉴스 검색 결과]")
        for i, it in enumerate(news_items, 1):
            title = _strip_html(it.get("title", ""))
            desc = _strip_html(it.get("description", ""))
            pub = it.get("pubDate", "")
            link = it.get("link", "")
            blocks.append(f"{i}. {title}")
            if pub:
                blocks.append(f"   날짜: {pub}")
            if desc:
                blocks.append(f"   요약: {desc}")
            if link:
                blocks.append(f"   링크: {link}")
    if web_items:
        blocks.append("")
        blocks.append("[웹문서 검색 결과]")
        for i, it in enumerate(web_items, 1):
            title = _strip_html(it.get("title", ""))
            desc = _strip_html(it.get("description", ""))
            link = it.get("link", "")
            blocks.append(f"{i}. {title}")
            if desc:
                blocks.append(f"   요약: {desc}")
            if link:
                blocks.append(f"   링크: {link}")
    return "\n".join(blocks).strip()


# ──────────────────────────────────────────────────
# Step 2: 네이버 검색 + Claude 요약으로 회사 리서치
# ──────────────────────────────────────────────────
def research_company(company_name, email_domain) -> str:
    if not company_name and not email_domain:
        return "회사 정보를 특정할 수 없어 리서치를 건너뜁니다."

    search_query = company_name or email_domain
    naver_context = collect_naver_context(search_query) if search_query else ""
    if not naver_context:
        naver_context = "(네이버 검색 결과 없음)"

    prompt = f"""You are a B2B SaaS sales research assistant for Spendit (expense management platform).

Your task is to conduct a structured pre-discovery research on a company before a sales discovery call.

CRITICAL INSTRUCTIONS:
- The following is the search context retrieved from Naver Search API (news + web). Base your answer ONLY on this context. Do NOT use training data, do NOT fabricate.
- Only include news and information from the last 12 months (2025~2026). Exclude older articles.
- If information cannot be confirmed from the search context, use "정보없음". Never guess.

ALL outputs MUST be written in Korean.
Do NOT use English except for company names, product names, proper nouns.

IMPORTANT: Your response must be a single, complete, valid JSON object. Do not truncate. Do not add any text before or after the JSON.

Return ONLY JSON with this structure:
{{
  "company_name": "",
  "summary": {{"founded_year": "", "ceo": "", "employee_count": "", "business": "", "recent_issue": ""}},
  "company_overview": {{"founding_background": "", "recent_developments": "", "organization": "", "employee_count": {{"value": "", "confidence": "확정|추정|정보없음"}}}},
  "business_area": {{"products_services": [], "industries": [], "customers": [], "competitors": []}},
  "financials": {{"revenue": "", "operating_profit": "", "confidence": "확정|추정|정보없음"}},
  "investment_stage": "",
  "recent_news": [{{"date": "", "title": "", "summary": "", "impact": ""}}],
  "spendit_insight": {{
    "likely_pain_points": ["페인포인트를 반드시 문자열로만 작성. 절대 객체 사용 금지. 예: '프로젝트별 비용 추적 어려움'"],
    "fit_hypothesis": ["세일즈 메시지를 반드시 문자열로만 작성. 절대 객체 사용 금지. 예: '프로젝트별 비용 실시간 관리로 손익 투명성 확보'"],
    "discovery_questions": ["질문을 반드시 문자열로만 작성. 절대 객체 사용 금지. 예: '현재 프로젝트별 비용은 어떻게 관리하시나요?'"]
  }},
  "unknowns": []
}}

Pain point categories to map:
1. 비용 가시성 부족
2. 수기 비용 처리 / 보고 프로세스
3. 경비 규정 및 컴플라이언스 문제
4. 회계/결산 비효율
5. 거래처 지급 및 정산 복잡성
6. 프로젝트별 비용 관리 어려움
7. 외근/현장 인력 비용 처리 문제

fit_hypothesis should be actionable sales messages like: '프로젝트별 비용 실시간 관리로 손익 투명성 확보', '현장 인력 경비 자동화로 수기 처리 제거'
discovery_questions should be specific, open-ended questions tied to the company's business model.
CRITICAL: likely_pain_points, fit_hypothesis, discovery_questions 는 반드시 문자열 배열(string array)이어야 함. 절대 객체 배열 사용 금지.

Company name: {company_name}, Email domain: {email_domain}

=== Naver Search Context ===
{naver_context}
=== End of Context ==="""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text_blocks = [b.text for b in response.content if b.type == "text"]
    raw = text_blocks[-1] if text_blocks else "회사 정보를 가져올 수 없습니다."
    start = raw.find("{")
    end = raw.rfind("}") + 1
    return raw[start:end] if start != -1 and end > start else raw


# ──────────────────────────────────────────────────
# Step 3: 도입문의 질문 템플릿 반환
# ──────────────────────────────────────────────────
def generate_questions(
    message_text, company_name, research_summary
) -> str:
    return """*안녕하세요! 간단히 현재 상황 이해를 위해 몇 가지 여쭤봐도 괜찮을까요? 🙂*

*1. 도입 배경*
• 이번에 지출관리 솔루션을 알아보시게 된 계기가 있을까요?
• 현재 가장 불편하거나 개선하고 싶은 부분은 어떤 점이세요?

*2. 회사 규모*
• 현재 임직원 수는 어느 정도 되실까요?

*3. 지출 관리 방식 (현황)*
• 비용 처리는 내부에서 하시나요, 아니면 세무사/외부 맡기고 계신가요?
• ERP나 그룹웨어(전자결재)는 어떤 걸 사용 중이세요?
• 현재 별도 비용관리 솔루션도 사용 중이실까요?

*4. 카드 사용 방식*
• 법인카드는 어떤 형태로 운영 중이세요? (개인별 / 공용 / 개인카드 후 청구 등)

*5. 세금계산서 & 지급 관리*
• 세금계산서 수집이나 처리도 자동화가 필요하신 상황이세요?
• 거래처 지급까지 같이 관리하길 원하시나요?

*6. 핵심 니즈 (Needs)*
• 이번 도입으로 꼭 해결하고 싶은 1~2가지 과제는 무엇일까요?

*7. 도입 시기 (Timeline)*
• 내부적으로 도입 목표 시점이 대략 언제쯤이실까요?

*8. 예산 (Budget)*
• 관련 예산은 이미 확보된 상태일까요, 아니면 검토 단계일까요?

*9. 검토 단계*
• 지금은 정보 탐색 단계이신지, 아니면 비교/선정 단계이신지도 궁금합니다.

*10. 의사결정 (Authority)*
• 이후 미팅에 의사결정자분도 함께 참여 가능하실까요?"""


# ──────────────────────────────────────────────────
# 리서치 JSON 파싱 (공용 헬퍼)
# ──────────────────────────────────────────────────
def _close_unclosed_json(js: str) -> str | None:
    depth_obj = 0
    depth_arr = 0
    in_str = False
    escape = False
    for ch in js:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_obj += 1
        elif ch == "}":
            depth_obj -= 1
        elif ch == "[":
            depth_arr += 1
        elif ch == "]":
            depth_arr -= 1
    if in_str or (depth_obj <= 0 and depth_arr <= 0):
        return None
    closing = ("]" * max(0, depth_arr)) + ("}" * max(0, depth_obj))
    return js + closing


def parse_research_json(raw: str) -> dict | None:
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    js = raw[start:end]

    no_trailing = re.sub(r",\s*([}\]])", r"\1", js)
    no_ctrl = re.sub(r"[\x00-\x1f]+", " ", no_trailing)

    for candidate in (js, js.replace("\n", " ").replace("\r", " "), no_trailing, no_ctrl):
        try:
            v = json.loads(candidate)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            continue

    try:
        import ast
        v = ast.literal_eval(no_ctrl)
        if isinstance(v, dict):
            return v
    except Exception:
        pass

    closed = _close_unclosed_json(no_ctrl)
    if closed:
        try:
            v = json.loads(closed)
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass

    return None


def _fallback_format(raw: str) -> str:
    def grab(field: str) -> str:
        m = re.search(rf'"{field}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', raw)
        if not m:
            return "정보없음"
        v = m.group(1).strip()
        return v if v and v != "정보없음" else "정보없음"

    cn = grab("company_name")
    lines = [
        f"*🔍 {cn if cn != '정보없음' else '회사'} 디스커버리콜 사전 리서치 (요약본)*",
        "",
        "_⚠️ 응답 일부 파싱에 실패하여 핵심 정보만 표시합니다._",
        "",
        "*📊 요약 테이블*",
        f"• *회사명:* {cn}",
        f"• *설립연도:* {grab('founded_year')}",
        f"• *대표이사:* {grab('ceo')}",
        f"• *임직원 수:* {grab('employee_count')}",
        f"• *사업:* {grab('business')}",
        f"• *매출:* {grab('revenue')}",
        f"• *최근 이슈:* {grab('recent_issue')}",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────
# 리서치 JSON → Slack 메시지 포맷 변환
# ──────────────────────────────────────────────────
def format_research_result(raw: str) -> str:
    d = parse_research_json(raw)
    if d is None:
        print("[FORMAT] JSON 파싱 실패, 폴백 포맷 사용", flush=True)
        return _fallback_format(raw)

    def val(v):
        return v if v and v != "정보없음" else "정보없음"

    s = d.get("summary", {})
    fin = d.get("financials", {})
    overview = d.get("company_overview", {})
    biz = d.get("business_area", {})
    insight = d.get("spendit_insight", {})

    lines = [
        f"*🔍 {val(d.get('company_name'))} 디스커버리콜 사전 리서치*",
        "",
        "*📊 요약 테이블*",
        f"• *회사명:* {val(d.get('company_name'))}",
        f"• *설립연도:* {val(s.get('founded_year'))}",
        f"• *대표이사:* {val(s.get('ceo'))}",
        f"• *임직원 수:* {val(s.get('employee_count'))}",
        f"• *사업:* {val(s.get('business'))}",
        f"• *매출:* {val(fin.get('revenue'))}",
        f"• *투자단계:* {val(d.get('investment_stage'))}",
        f"• *최근 이슈:* {val(s.get('recent_issue'))}",
    ]

    competitors = biz.get("competitors", [])
    if competitors:
        lines.append(f"• *경쟁사:* {', '.join(competitors)}")

    lines += ["", "---", "*🏢 1. 회사 개요*"]
    founding = val(overview.get("founding_background"))
    if founding != "정보없음":
        lines += [f"*설립 배경:* {founding}"]
    recent_dev = val(overview.get("recent_developments"))
    if recent_dev != "정보없음":
        lines += ["", f"*최근 동향:* {recent_dev}"]
    org = val(overview.get("organization"))
    if org != "정보없음":
        lines += ["", f"*조직 구조:* {org}"]

    lines += ["", "---", "*💼 2. 사업 영역*"]
    products = biz.get("products_services", [])
    if products:
        lines += ["*주력 서비스:*"] + [f"• {p}" for p in products]
    industries = biz.get("industries", [])
    customers = biz.get("customers", [])
    if industries or customers:
        lines += ["", "*주요 산업 / 고객:*"]
        lines += [f"• {i}" for i in industries]
        lines += [f"• {c}" for c in customers]

    lines += ["", "---", "*💰 3. 재무 현황*"]
    lines += [
        f"• *매출:* {val(fin.get('revenue'))}",
        f"• *영업이익:* {val(fin.get('operating_profit'))}",
        f"• *신뢰도:* {val(fin.get('confidence'))}",
    ]

    news_list = d.get("recent_news", [])[:3]
    if news_list:
        lines += ["", "---", "*📰 4. 최근 뉴스*"]
        for n in news_list:
            lines += [
                f"• *[{n.get('date', '')}] {n.get('title', '')}*",
                f"  {n.get('summary', '')}",
                f"  _→ 임팩트: {n.get('impact', '')}_",
            ]

    lines += ["", "---", "*🧠 5. Spendit 관점 인사이트*"]

    def to_str(item):
        if isinstance(item, dict):
            parts = []
            if item.get("category"):
                parts.append(f"*{item['category']}*")
            if item.get("detail"):
                parts.append(item["detail"])
            if item.get("message"):
                parts.append(item["message"])
            return ": ".join(parts) if parts else str(item)
        return str(item)

    pain_points = insight.get("likely_pain_points", [])
    if pain_points:
        lines += ["", "*🎯 핵심 Pain Point:*"]
        lines += [f"• {to_str(p)}" for p in pain_points]

    fit_hypothesis = insight.get("fit_hypothesis", [])
    if fit_hypothesis:
        lines += ["", "*💡 세일즈 포인트:*"]
        lines += [f"• {to_str(h)}" for h in fit_hypothesis]

    discovery_questions = insight.get("discovery_questions", [])
    if discovery_questions:
        lines += ["", "*❓ 추천 디스커버리 질문:*"]
        lines += [f"• {to_str(q)}" for q in discovery_questions]

    unknowns = d.get("unknowns", [])
    if unknowns:
        lines += ["", "---", "*⚠️ 확인 필요 항목:*"]
        lines += [f"• {u}" for u in unknowns]

    if fit_hypothesis:
        lines += ["", "---", f"*🔥 한줄 전략:* {fit_hypothesis[0]}"]

    return "\n".join(lines)


# ──────────────────────────────────────────────────
# 임직원 수 기반 멘션 결정
# ──────────────────────────────────────────────────
def _extract_employee_number(text: str) -> int | None:
    if not text:
        return None
    t = str(text)
    if "정보없음" in t and not re.search(r"\d", t):
        return None

    patterns = [
        r"(?:임직원|직원|구성원|employees?)\s*(?:수)?\s*(?:는|은|:)?\s*약?\s*([\d,]+)\s*(?:여)?\s*명?",
        r"약\s*([\d,]+)\s*(?:여)?\s*명",
        r"([\d,]+)\s*(?:여)?\s*명\s*(?:의|규모|수준|이상|미만|내외|안팎)",
        r"([\d,]+)\s*(?:여)?\s*명",
    ]
    for pat in patterns:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue

    nums = re.findall(r"\d[\d,]*", t)
    if nums:
        try:
            return max(int(n.replace(",", "")) for n in nums)
        except ValueError:
            return None
    return None


def pick_mention_by_employee_count(research_raw: str) -> str:
    try:
        d = parse_research_json(research_raw)
        if not d:
            print("[MENTION] JSON 파싱 실패, raw 텍스트 스캔", flush=True)
            count = _extract_employee_number(research_raw)
            if count is not None:
                print(f"[MENTION] raw 텍스트에서 추출: {count}", flush=True)
                return MENTION_SMALL if count < 100 else MENTION_LARGE
            return MENTION_LARGE

        primary_candidates: list = []
        s = d.get("summary") or {}
        primary_candidates.append(s.get("employee_count"))
        overview = d.get("company_overview") or {}
        ec = overview.get("employee_count")
        if isinstance(ec, dict):
            primary_candidates.append(ec.get("value"))
        else:
            primary_candidates.append(ec)

        for raw in primary_candidates:
            count = _extract_employee_number(raw)
            if count is not None:
                print(f"[MENTION] primary employee_count: {raw!r} → {count}", flush=True)
                return MENTION_SMALL if count < 100 else MENTION_LARGE

        secondary_texts: list[str] = []
        for key in ("business", "recent_issue"):
            v = s.get(key)
            if v:
                secondary_texts.append(str(v))
        for key in ("founding_background", "recent_developments", "organization"):
            v = overview.get(key)
            if v:
                secondary_texts.append(str(v))
        for n in (d.get("recent_news") or [])[:5]:
            if isinstance(n, dict):
                for key in ("summary", "title", "impact"):
                    v = n.get(key)
                    if v:
                        secondary_texts.append(str(v))
        for u in d.get("unknowns") or []:
            if u:
                secondary_texts.append(str(u))

        for txt in secondary_texts:
            if not re.search(r"임직원|직원|구성원|employees?|명", txt, re.IGNORECASE):
                continue
            count = _extract_employee_number(txt)
            if count is not None and 1 <= count <= 1_000_000:
                print(f"[MENTION] secondary 텍스트에서 추출: {txt[:80]!r} → {count}", flush=True)
                return MENTION_SMALL if count < 100 else MENTION_LARGE

        print("[MENTION] 모든 필드에서 임직원 수 추출 실패, fallback", flush=True)
        return MENTION_LARGE
    except Exception as e:
        print(f"[MENTION] 파싱 예외, fallback: {e}", flush=True)
        return MENTION_LARGE


# ──────────────────────────────────────────────────
# 전체 파이프라인 (백그라운드 스레드에서 실행)
# ──────────────────────────────────────────────────
def process_inquiry(channel_id: str, thread_ts: str, message_text: str):
    with last_processed_lock:
        now = time.time()
        last = last_processed.get(channel_id, 0)
        if now - last < DUPLICATE_THRESHOLD_SECONDS:
            print(f"[DUPLICATE] 중복 요청 무시: channel={channel_id}, 경과={now-last:.1f}초", flush=True)
            return
        last_processed[channel_id] = now

    def post(text: str):
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
            mrkdwn=True,
        )

    try:
        # 1. 회사 정보 파싱
        info = parse_company_info(message_text)
        company_name = info.get("company_name")
        email_domain = info.get("email_domain")

        parsed_summary = []
        if company_name:
            parsed_summary.append(f"• *회사명:* {company_name}")
        if email_domain:
            parsed_summary.append(f"• *이메일 도메인:* {email_domain}")
        parsed_text = "\n".join(parsed_summary) if parsed_summary else "• 회사 정보를 파싱하지 못했습니다."

        # 2. 회사 리서치
        post(f"🔍 *회사 리서치 중입니다...* 잠시만 기다려주세요.\n\n{parsed_text}")
        research = research_company(company_name, email_domain)
        post(format_research_result(research))

        # 3. 질문 생성
        questions = generate_questions(message_text, company_name, research)
        post(f"📋 *도입문의 미팅 질문 10개*\n\n{questions}")

        # 4. 임직원 수 기반 담당자 멘션
        mention = pick_mention_by_employee_count(research)
        post(
            f"{mention} 새로운 도입문의가 접수되었습니다! "
            "위 리서치 내용과 질문 목록을 참고해주세요 🙌"
        )

    except SlackApiError as e:
        print(f"[Slack API Error] {e.response['error']}")
    except Exception as e:
        print(f"[Process Error] {e}")
        try:
            post(f"⚠️ 처리 중 오류가 발생했습니다: `{e}`")
        except Exception:
            pass


# ──────────────────────────────────────────────────
# Slack Events 웹훅 엔드포인트
# ──────────────────────────────────────────────────
@app.route("/slack/events", methods=["POST"])
def slack_events():
    print(f"[EVENT] 요청 수신: {request.method} {request.path}", flush=True)
    data = request.json or {}

    # URL 검증 핸드셰이크 (서명 검증 전에 처리)
    if data.get("type") == "url_verification":
        print("[EVENT] URL verification challenge 처리", flush=True)
        return jsonify({"challenge": data["challenge"]})

    if not verify_slack_signature(request):
        print("[EVENT] 서명 검증 실패 - 403 반환", flush=True)
        return jsonify({"error": "Invalid signature"}), 403

    event_type = data.get("type")
    print(f"[EVENT] 이벤트 타입: {event_type}", flush=True)

    if event_type != "event_callback":
        return jsonify({"ok": True})

    event = data.get("event", {})
    event_id = data.get("event_id", "")
    print(f"[EVENT] event_id={event_id}, event.type={event.get('type')}, channel={event.get('channel')}, bot_id={event.get('bot_id')}, subtype={event.get('subtype')}", flush=True)

    # 중복 이벤트 스킵
    with processed_events_lock:
        if event_id in processed_events:
            print(f"[EVENT] 중복 이벤트 스킵: {event_id}", flush=True)
            return jsonify({"ok": True})
        processed_events.add(event_id)
        # 메모리 절약: 1000개 초과 시 오래된 항목 제거
        if len(processed_events) > 1000:
            processed_events.clear()

    # 일반 메시지만 처리 (봇 메시지, 수정, 삭제 제외)
    if event.get("type") != "message":
        print(f"[EVENT] 메시지 타입 아님, 스킵: {event.get('type')}", flush=True)
        return jsonify({"ok": True})
    if event.get("bot_id") or event.get("subtype"):
        print(f"[EVENT] 봇 메시지 또는 subtype 이벤트 스킵", flush=True)
        return jsonify({"ok": True})

    channel_id = event.get("channel", "")
    message_text = event.get("text", "").strip()
    ts = event.get("ts", "")

    # 특정 채널만 처리 (SLACK_CHANNEL_ID 미설정 시 모든 채널)
    if SLACK_CHANNEL_ID and channel_id != SLACK_CHANNEL_ID:
        print(f"[EVENT] 채널 불일치 스킵: 수신={channel_id}, 설정={SLACK_CHANNEL_ID}", flush=True)
        return jsonify({"ok": True})

    if not message_text:
        print("[EVENT] 메시지 텍스트 없음, 스킵", flush=True)
        return jsonify({"ok": True})

    print(f"[EVENT] 처리 시작: channel={channel_id}, ts={ts}, text={message_text[:50]!r}", flush=True)

    # 즉시 200 응답 후 백그라운드에서 처리
    executor.submit(process_inquiry, channel_id, ts, message_text)

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
