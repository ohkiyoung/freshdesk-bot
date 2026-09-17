import os
import re
import json
import base64
import requests
import threading
import time as time_module
from flask import Flask, request, jsonify

app = Flask(__name__)

GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FRESHDESK_API_KEY  = os.environ.get("FRESHDESK_API_KEY", "")
FRESHDESK_DOMAIN   = os.environ.get("FRESHDESK_DOMAIN", "bestcare")

PRIORITY_KO = {"1": "낮음", "2": "중간", "3": "높음", "4": "긴급"}
STATUS_KO   = {"2": "접수", "3": "대기중", "4": "해결됨", "5": "완료"}


def log(msg):
    print(msg, flush=True)


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def strip_email_quotes(text):
    if not text:
        return ""
    patterns = [r'On .+wrote:', r'From:.+Sent:', r'-----Original Message-----']
    lines = text.split('\n')
    clean_lines = []
    for line in lines:
        is_quote = False
        for pattern in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                is_quote = True
                break
        if is_quote:
            break
        clean_lines.append(line)
    result = '\n'.join(clean_lines).strip()
    return result if result else text


def freshdesk_headers():
    credentials = base64.b64encode(f"{FRESHDESK_API_KEY}:X".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def get_latest_comment(ticket_id):
    try:
        all_conversations = []
        page = 1
        while True:
            res = requests.get(
                f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2/tickets/{ticket_id}/conversations?page={page}&per_page=100",
                headers=freshdesk_headers(),
                timeout=15,
            )
            if res.status_code != 200:
                log(f"[Freshdesk API] 오류: {res.status_code}")
                break
            data = res.json()
            if not data:
                break
            all_conversations.extend(data)
            if len(data) < 100:
                break
            page += 1

        public = [c for c in all_conversations if not c.get("private", False)]
        if not public:
            log("[Freshdesk API] 공개 댓글 없음")
            return ""

        latest = sorted(public, key=lambda x: x.get("created_at", ""), reverse=True)[0]
        body = strip_email_quotes(strip_html(latest.get("body", "")))
        log(f"[Freshdesk API] 최신 댓글: {body[:100]}")
        return body
    except Exception as e:
        log(f"[Freshdesk API] 예외: {e}")
        return ""


def search_freshdesk(query):
    try:
        res = requests.get(
            f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2/tickets?per_page=100&order_by=created_at&order_type=desc",
            headers=freshdesk_headers(),
            timeout=15,
        )
        if res.status_code != 200:
            log(f"[검색] 티켓 조회 오류: {res.status_code}")
            return []

        all_tickets = res.json()
        keywords = [w.lower() for w in query.strip().split()]

        matched = []
        for t in all_tickets:
            subject = (t.get("subject", "") or "").lower()
            desc = strip_html(t.get("description_text", t.get("description", "")) or "").lower()
            text = subject + " " + desc
            if any(kw in text for kw in keywords):
                matched.append(t)
            if len(matched) >= 5:
                break

        log(f"[검색] '{query}' → {len(matched)}건 매칭")
        return matched
    except Exception as e:
        log(f"[검색] 예외: {e}")
        return []


def call_groq(prompt, temperature=0.3):
    models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound-mini"]
    for model in models:
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_API_KEY}"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature},
                timeout=20,
            )
            if res.status_code == 200:
                return res.json()["choices"][0]["message"]["content"].strip()
            else:
                log(f"[Groq] {model} 실패: {res.status_code}")
        except Exception as e:
            log(f"[Groq] {model} 오류: {e}")
    return None


def extract_search_keywords(question):
    prompt = (
        "아래 질문을 Freshdesk 티켓 검색에 적합한 영어 키워드로 변환해주세요.\n\n"
        f"질문: {question}\n\n"
        "규칙:\n"
        "- 영어 키워드만 반환 (한국어 금지)\n"
        "- 핵심 기술 용어 위주로 3~5개 단어\n"
        "- 따옴표나 다른 텍스트 없이 키워드만 반환\n"
        "예시: 마취 관련 오류 -> anesthesia exception error\n"
        "예시: 결제 컬럼 안보임 -> payment column visibility\n"
        "예시: ADT 메시지 안감 -> ADT message not sent\n"
    )
    result = call_groq(prompt, temperature=0.1)
    if result:
        log(f"[검색키워드] '{question}' -> '{result}'")
        return result
    return question


def answer_question(question, tickets):
    if not tickets:
        return "관련 티켓을 찾지 못했어요. 다른 키워드로 검색해보세요."

    ticket_summaries = ""
    for t in tickets[:5]:
        desc = strip_html(t.get("description_text", t.get("description", "")))[:300]
        ticket_summaries += (
            f"\n티켓 #{t.get('id')}: {t.get('subject', '')}\n"
            f"내용: {desc}\n"
            f"상태: {STATUS_KO.get(str(t.get('status', '')), str(t.get('status', '')))}\n"
            f"URL: https://{FRESHDESK_DOMAIN}.freshdesk.com/a/tickets/{t.get('id')}\n---"
        )

    prompt = (
        f"아래 Freshdesk 티켓들을 참고해서 질문에 한국어로 답변해주세요.\n\n"
        f"질문: {question}\n\n"
        f"관련 티켓들:{ticket_summaries}\n\n"
        "규칙:\n"
        "- 티켓에 있는 내용만 기반으로 답변. 없는 내용 추가 금지.\n"
        "- 관련 티켓 번호와 링크 포함\n"
        "- 간결하고 명확하게\n"
        "- 격식체 사용 금지\n"
    )
    result = call_groq(prompt)
    return result if result else "답변 생성에 실패했어요. 잠시 후 다시 시도해주세요."


def summarize_ticket(subject, description, priority, status, latest_comment=""):
    # 413 오류 방지 길이 제한
    description = description[:2000] if description else ""
    latest_comment = latest_comment[:1500] if latest_comment else ""

    prompt = (
        f"아래 Freshdesk 티켓을 분석해서 한국어로 요약하세요.\n\n"
        f"Subject: {subject}\n"
        f"Description (티켓 전체 내용): {description}\n"
        f"Latest Comment (최신 댓글): {latest_comment if latest_comment else '없음'}\n"
        f"Priority: {PRIORITY_KO.get(str(priority), priority)}\n"
        f"Status: {STATUS_KO.get(str(status), status)}\n\n"
        "[문체 규칙]\n"
        "- 격식체 절대 사용 금지\n"
        "- 모든 문장은 '~됨', '~임', '~함' 형태로 끝낼 것\n"
        "- '~할 수 있어야 함', '~해 주세요', '~요청드립니다' 같은 요청형 표현 금지\n"
        "- 상황을 객관적으로 서술하는 방식으로 작성\n"
        "- 중요한 세부 내용(수치, 날짜, 시스템명, 서버명, DB정보, MRN 등) 반드시 포함\n\n"
        "[항목별 작성 기준]\n"
        "- 핵심문의: Description 기반 문제 상황을 불렛(•)으로 3~5개. 발생한 상황과 영향을 객관적으로 서술.\n"
        "- 요청사항: Latest Comment 내용만 기반으로 요약. Description 내용 절대 가져오지 말 것.\n"
        "  * 최신 댓글이 'reminder', 'any update', 'follow up', 'kind reminder' 등 단순 재촉 메시지면 '이전 요청에 대한 단순 리마인더. 추가 정보 없음.' 으로 요약.\n"
        "  * 최신 댓글이 짧고 단순하면 억지로 늘리지 말고 그대로 짧게 요약.\n"
        "  * 기술적 내용(서버 목록, DB 접속 정보, 설정값 등)이 있으면 반드시 포함.\n"
        "  * 없는 내용 절대 추가 금지.\n\n"
        "[핵심문의 예시]\n"
        "좋은 예: • MRN 145197, 검체 번호 S-260002482 체크인 실수로 취소됨\n"
        "        • 취소 후 주문 정보로 돌아가 재체크인 시도했으나 불가능한 상태\n"
        "나쁜 예: • 취소 상태를 해제하거나 재체크인 가능하도록 해 주세요\n\n"
        "[요청사항 예시]\n"
        "리마인더인 경우: • 이전 요청에 대한 단순 리마인더. 추가 정보 없음.\n"
        "기술적 내용인 경우: • Staging/Clone 서버 목록(Stage-DB, HL7, WAS 등)과 DB 접속 정보(EXASTG) 상세히 전달하며 지원 요청함.\n\n"
        "JSON 형식으로만 응답. 코드블록 없이:\n"
        '{"제목": "한국어로 간결하게", "핵심문의": "• 항목1\\n• 항목2\\n• 항목3", '
        '"요청사항": "• 항목1\\n• 항목2", "긴급도": "높음 또는 중간 또는 낮음", '
        '"담당부서": "개발팀 또는 운영팀 또는 기획팀"}'
    )

    log("[Groq] 요약 시도 중...")
    text = call_groq(prompt)
    if not text:
        log("[Groq] 모든 모델 실패")
        return None
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        log("[Groq] 요약 성공!")
        return result
    except Exception as e:
        log(f"[Groq] JSON 파싱 오류: {e}")
        return None


def build_message_with_summary(summary, ticket_id, ticket_url, subject="", company=""):
    urgency_emoji = {"높음": "🔴", "중간": "🟡", "낮음": "🟢"}.get(summary.get("긴급도", ""), "⚪")
    dept_emoji    = {"개발팀": "💻", "운영팀": "🔧", "기획팀": "📋"}.get(summary.get("담당부서", ""), "📌")
    lines = [
        f"[{company}] 🐶" if company else "🐶",
        f"Ticket No : #{ticket_id}",
        f"Title : {subject}" if subject else None,
        f"({summary.get('제목', '-')})",
        "",
        "💬 티켓 내용",
        summary.get("핵심문의", "-"),
        "",
        "✅ 최신 답변",
        summary.get("요청사항", "-"),
        "",
        f"{urgency_emoji} 긴급도: {summary.get('긴급도', '-')}",
        f"{dept_emoji} 담당: {summary.get('담당부서', '-')}",
    ]
    lines = [l for l in lines if l is not None]
    if ticket_url:
        lines += ["", f"🔗 {ticket_url}"]
    return "\n".join(lines)


def build_message_without_summary(subject, description, ticket_id, ticket_url, priority, status, company=""):
    lines = [
        f"[{company}] 🐶" if company else "🐶",
        f"Ticket No : #{ticket_id}",
        f"Title : {subject}" if subject else None,
        "",
        "📝 내용",
        f"{description[:300]}{'...' if len(description or '') > 300 else ''}",
        "",
        f"⚡ 우선순위: {PRIORITY_KO.get(str(priority), priority)}",
        f"📊 상태: {STATUS_KO.get(str(status), status)}",
    ]
    lines = [l for l in lines if l is not None]
    if ticket_url:
        lines += ["", f"🔗 {ticket_url}"]
    return "\n".join(lines)


def build_create_message(summary, ticket_id, ticket_url, subject="", company=""):
    urgency_emoji = {"높음": "🔴", "중간": "🟡", "낮음": "🟢"}.get(summary.get("긴급도", ""), "⚪")
    dept_emoji    = {"개발팀": "💻", "운영팀": "🔧", "기획팀": "📋"}.get(summary.get("담당부서", ""), "📌")
    lines = [
        f"🆕 신규 티켓",
        f"[{company}] 🐶" if company else "🐶",
        f"Ticket No : #{ticket_id}",
        f"Title : {subject}" if subject else None,
        f"({summary.get('제목', '-')})",
        "",
        "💬 문의 내용",
        summary.get("핵심문의", "-"),
        "",
        f"{urgency_emoji} 긴급도: {summary.get('긴급도', '-')}",
        f"{dept_emoji} 담당: {summary.get('담당부서', '-')}",
    ]
    lines = [l for l in lines if l is not None]
    if ticket_url:
        lines += ["", f"🔗 {ticket_url}"]
    return "\n".join(lines)


def summarize_new_ticket(subject, description, priority, status):
    description = description[:2000] if description else ""
    prompt = (
        f"아래 신규 Freshdesk 티켓을 분석해서 한국어로 요약하세요.\n\n"
        f"Subject: {subject}\n"
        f"Description: {description}\n"
        f"Priority: {PRIORITY_KO.get(str(priority), priority)}\n"
        f"Status: {STATUS_KO.get(str(status), status)}\n\n"
        "[문체 규칙]\n"
        "- 격식체 절대 사용 금지\n"
        "- 모든 문장은 '~됨', '~임', '~함' 형태로 끝낼 것\n"
        "- 단문으로 끊어서 작성\n"
        "- 중요한 세부 내용(수치, 날짜, 시스템명) 반드시 포함\n\n"
        "[작성 기준]\n"
        "- 핵심문의: Description 기반 문제 상황을 불렛(•)으로 3~5개. 없는 내용 추가 금지.\n\n"
        "[불렛 예시]\n"
        "• Jubail - Nephrology 코드 그룹 반영됨. 담당 의사 정상 표시됨.\n"
        "• Yanbu - 동일 그룹 없어 대체 사용했으나 환자 목록 미반영됨.\n\n"
        "JSON 형식으로만 응답. 코드블록 없이:\n"
        '{"제목": "한국어로 간결하게", "핵심문의": "• 항목1\\n• 항목2\\n• 항목3", '
        '"긴급도": "높음 또는 중간 또는 낮음", "담당부서": "개발팀 또는 운영팀 또는 기획팀"}'
    )
    log("[Groq] 신규 티켓 요약 중...")
    text = call_groq(prompt)
    if not text:
        return None
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log(f"[Groq] JSON 파싱 오류: {e}")
        return None


def send_telegram(message):
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
        timeout=10,
    )
    return res.json().get("ok", False)


def send_telegram_to(chat_id, message):
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=10,
    )
    return res.json().get("ok", False)


@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    try:
        data = request.json or {}
        message = data.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        if not text or not chat_id:
            return jsonify({"status": "skip"}), 200

        log(f"[텔레그램] 메시지 수신: {text[:50]}")
        log(f"[텔레그램] 원문: {text}")

        if text.lower().startswith("/search"):
            query = re.sub(r'^/search(@\w+)?\s*', '', text, flags=re.IGNORECASE).strip()
        elif text.startswith("?"):
            query = text[1:].strip()
        else:
            return jsonify({"status": "skip"}), 200

        if not query:
            send_telegram_to(chat_id, "검색어를 입력해주세요. 예: /search Payment 컬럼 문제")
            return jsonify({"status": "ok"}), 200

        send_telegram_to(chat_id, "🔍 검색 중...")
        keywords = extract_search_keywords(query)
        tickets = search_freshdesk(keywords)
        answer = answer_question(query, tickets)
        send_telegram_to(chat_id, f"🐶 검색 결과\n\n{answer}")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log(f"[텔레그램] 오류: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/setup/telegram", methods=["GET"])
def setup_telegram_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if domain:
        webhook_url = f"https://{domain}/webhook/telegram"
    else:
        webhook_url = f"https://{request.host}/webhook/telegram"
    log(f"[텔레그램] Webhook 등록 시도: {webhook_url}")
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        json={"url": webhook_url},
        timeout=10,
    )
    result = res.json()
    log(f"[텔레그램] Webhook 등록 결과: {result}")
    return jsonify({"webhook_url": webhook_url, "result": result})


@app.route("/webhook/freshdesk", methods=["POST"])
def freshdesk_webhook():
    log("=== 웹훅 요청 수신 ===")
    try:
        data = request.json or {}
        fw = data.get("freshdesk_webhook", data)

        ticket_id   = str(fw.get("ticket_id", ""))
        subject     = fw.get("ticket_subject", fw.get("subject", ""))
        description = fw.get("ticket_description", fw.get("description_text", ""))
        priority    = str(fw.get("ticket_priority", fw.get("priority", "2")))
        status      = str(fw.get("ticket_status", fw.get("status", "2")))
        ticket_url  = fw.get("ticket_url", "")
        company     = fw.get("ticket_company", "")

        description = strip_html(description)

        if ticket_id and FRESHDESK_API_KEY:
            latest_comment = get_latest_comment(ticket_id)
        else:
            latest_comment = strip_email_quotes(strip_html(fw.get("ticket_latest_comment", "")))

        log(f"티켓 ID: {ticket_id}, 제목: {subject[:50]}")
        log(f"최신 댓글: {latest_comment[:100] if latest_comment else '없음'}")

        if not subject:
            return jsonify({"status": "skip", "reason": "no subject"}), 200

        summary = summarize_ticket(subject, description, priority, status, latest_comment)

        if summary:
            message = build_message_with_summary(summary, ticket_id, ticket_url, subject, company)
            mode = "ai_summary"
        else:
            message = build_message_without_summary(subject, description, ticket_id, ticket_url, priority, status, company)
            mode = "raw"

        log(f"텔레그램 발송 모드: {mode}")
        ok = send_telegram(message)
        log(f"텔레그램 발송 결과: {'성공' if ok else '실패'}")
        return jsonify({"status": "ok" if ok else "telegram_failed", "mode": mode}), 200

    except Exception as e:
        log(f"오류 발생: {e}")
        try:
            send_telegram(f"⚠️ 티켓 처리 오류\n{str(e)[:200]}")
        except Exception:
            pass
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/webhook/freshdesk/create", methods=["POST"])
def freshdesk_create_webhook():
    log("=== 신규 티켓 생성 웹훅 수신 ===")
    try:
        data = request.json or {}
        fw = data.get("freshdesk_webhook", data)

        ticket_id   = str(fw.get("ticket_id", ""))
        subject     = fw.get("ticket_subject", fw.get("subject", ""))
        description = fw.get("ticket_description", fw.get("description_text", ""))
        priority    = str(fw.get("ticket_priority", fw.get("priority", "2")))
        status      = str(fw.get("ticket_status", fw.get("status", "2")))
        ticket_url  = fw.get("ticket_url", "")
        company     = fw.get("ticket_company", "")

        description = strip_html(description)

        log(f"신규 티켓 ID: {ticket_id}, 제목: {subject[:50]}")

        if not subject:
            return jsonify({"status": "skip", "reason": "no subject"}), 200

        summary = summarize_new_ticket(subject, description, priority, status)

        if summary:
            message = build_create_message(summary, ticket_id, ticket_url, subject, company)
            mode = "ai_summary"
        else:
            message = build_message_without_summary(subject, description, ticket_id, ticket_url, priority, status, company)
            mode = "raw"

        log(f"신규 티켓 발송 모드: {mode}")
        ok = send_telegram(message)
        log(f"텔레그램 발송 결과: {'성공' if ok else '실패'}")
        return jsonify({"status": "ok" if ok else "telegram_failed", "mode": mode}), 200

    except Exception as e:
        log(f"오류 발생: {e}")
        try:
            send_telegram(f"⚠️ 신규 티켓 처리 오류\n{str(e)[:200]}")
        except Exception:
            pass
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "freshdesk-telegram-bot"}), 200


# 서버 자체 핑 - Render 무료 플랜 잠들기 방지
def self_ping():
    time_module.sleep(60)
    while True:
        try:
            requests.get("https://freshdesk-bot-s1fa.onrender.com/health", timeout=10)
            log("[핑] 서버 유지 성공")
        except Exception as e:
            log(f"[핑] 실패: {e}")
        time_module.sleep(840)

threading.Thread(target=self_ping, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
