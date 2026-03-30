import os
import hmac
import hashlib
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import anthropic

app = Flask(__name__)

slack_client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
SLACK_USER_ID = os.environ["SLACK_USER_ID"]          # 박슬기 슬랙 유저 ID
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")  # #도입문의 채널 ID (선택)

# 중복 이벤트 방지 (Slack은 동일 이벤트를 재전송할 수 있음)
processed_events: set[str] = set()
processed_events_lock = threading.Lock()

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
        model="claude-opus-4-6",
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
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "company_name": {"type": ["string", "null"]},
                        "email_domain": {"type": ["string", "null"]},
                    },
                    "required": ["company_name", "email_domain"],
                    "additionalProperties": False,
                },
            }
        },
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ──────────────────────────────────────────────────
# Step 2: Claude + web_search로 회사 리서치
# ──────────────────────────────────────────────────
def research_company(company_name: str | None, email_domain: str | None) -> str:
    query = company_name or (email_domain.split(".")[-2] if email_domain else None)
    if not query:
        return "회사 정보를 특정할 수 없어 리서치를 건너뜁니다."

    prompt = (
        f"'{query}' 회사에 대해 조사해주세요. "
        "한국어로 다음 내용을 포함해 요약해주세요:\n"
        "1. 회사 소개 및 주요 사업\n"
        "2. 회사 규모 (직원 수, 매출 등)\n"
        "3. 주요 고객 또는 서비스 영역\n"
        "4. 최근 뉴스 또는 이슈\n"
        "5. 기술 스택 또는 주요 도구 (IT 기업인 경우)"
    )

    with anthropic_client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=4096,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        final = stream.get_final_message()

    return next(
        (b.text for b in final.content if b.type == "text"),
        "웹 검색 결과를 가져올 수 없습니다.",
    )


# ──────────────────────────────────────────────────
# Step 3: Claude로 도입문의 질문 10개 생성
# ──────────────────────────────────────────────────
def generate_questions(
    message_text: str, company_name: str | None, research_summary: str
) -> str:
    response = anthropic_client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": (
                    "B2B SaaS 영업 담당자로서, 고객사와의 첫 미팅 또는 디스커버리 콜을 위한 "
                    "도입문의 관련 질문 10개를 작성해주세요.\n\n"
                    f"[원본 도입문의]\n{message_text}\n\n"
                    f"[회사명] {company_name or '정보 없음'}\n\n"
                    f"[회사 리서치 요약]\n{research_summary}\n\n"
                    "위 정보를 바탕으로 맞춤형 질문 10개를 번호 매겨 한국어로 작성해주세요. "
                    "현재 Pain Point, 도입 목적, 의사결정 구조, 예산, 타임라인 등을 파악할 수 있는 질문을 포함해주세요."
                ),
            }
        ],
    )
    return next(b.text for b in response.content if b.type == "text")


# ──────────────────────────────────────────────────
# 전체 파이프라인 (백그라운드 스레드에서 실행)
# ──────────────────────────────────────────────────
def process_inquiry(channel_id: str, thread_ts: str, message_text: str):
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

        post(
            f"📊 *회사 리서치 결과*\n\n{parsed_text}\n\n{research}"
        )

        # 3. 질문 생성
        questions = generate_questions(message_text, company_name, research)
        post(f"📋 *도입문의 미팅 질문 10개*\n\n{questions}")

        # 4. 박슬기 멘션
        post(
            f"<@{SLACK_USER_ID}> 새로운 도입문의가 접수되었습니다! "
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
