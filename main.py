import os
import re
import json
import base64
import requests
import threading
import time as time_module
from datetime import datetime, timezone, timedelta
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


QUOTE_MARKERS = [
    r'\bFrom:\s.{1,150}?\bSent:',          # Outlook 스타일 헤더
    r'\bOn\s.{5,120}?\bwrote:',            # Gmail 스타일 헤더
    r'-{3,}\s*Original Message\s*-{3,}',
    r'보내는\s?사람\s*:',                    # 한국어 메일 헤더
]


def strip_email_quotes(text):
    """이메일 인용(이전 대화)을 잘라내고 최신 본문만 남김. 공백으로 합쳐진 텍스트도 처리."""
    if not text:
        return ""
    cut = len(text)
    for pattern in QUOTE_MARKERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m and m.start() > 0:
            cut = min(cut, m.start())
    result = text[:cut].strip()
    return result if result else text


def clean_body(html, limit=None):
    """Freshdesk HTML 본문 -> 인용 제거된 순수 텍스트"""
    if not html:
        return ""
    head = re.split(r'<div[^>]*freshdesk_quote|<blockquote', html, maxsplit=1, flags=re.IGNORECASE)[0]
    text = strip_email_quotes(strip_html(head))
    if not text.strip():
        text = strip_email_quotes(strip_html(html))
    return text[:limit] if limit else text


def make_ticket_url(ticket_id, fallback=""):
    """에이전트 화면 직행 링크 (이미 로그인된 브라우저면 바로 열림)"""
    if ticket_id:
        return f"https://{FRESHDESK_DOMAIN}.freshdesk.com/a/tickets/{ticket_id}"
    return fallback


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
        body = clean_body(latest.get("body", ""))
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
        '"담당부서": "개발팀 또는 운영팀 또는 기획팀", "담당파트": "진료/간호 또는 원무/보험 또는 진료지원"}'
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
    part_emoji    = {"진료/간호": "🩺", "원무/보험": "💳", "진료지원": "🔬"}.get(summary.get("담당파트", ""), "📂")
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
        f"{part_emoji} 파트: {summary.get('담당파트', '-')}",
    ]
    lines = [l for l in lines if l is not None]
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
        '"긴급도": "높음 또는 중간 또는 낮음", "담당부서": "개발팀 또는 운영팀 또는 기획팀", "담당파트": "진료/간호 또는 원무/보험 또는 진료지원"}'
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


def send_telegram(message, button_url=None):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    if button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "🔗 티켓 열기", "url": button_url}]]
        }
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=10,
    )
    ok = res.json().get("ok", False)
    if not ok and button_url:
        # 버튼 전송 실패 시 링크를 본문에 넣어 재시도 (알림 누락 방지)
        log(f"[텔레그램] 버튼 전송 실패, 텍스트 링크로 재시도: {res.text[:150]}")
        res = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"{message}\n\n🔗 {button_url}"},
            timeout=10,
        )
        ok = res.json().get("ok", False)
    return ok


def send_telegram_to(chat_id, message):
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": message},
        timeout=10,
    )
    return res.json().get("ok", False)



# ───────────── /timeline : 티켓 흐름 한눈에 보기 ─────────────
KST = timezone(timedelta(hours=9))
TIMELINE_STATUS = {2: "접수", 3: "대기중", 4: "해결됨", 5: "종료", 6: "고객 답변 대기", 7: "제3자 대기"}
MAX_TIMELINE_ENTRIES = 40


def is_allowed_chat(chat_id):
    """/timeline은 내부메모를 포함하므로 허용된 채팅에서만 동작"""
    allowed = {TELEGRAM_CHAT_ID.strip()} if TELEGRAM_CHAT_ID else set()
    extra = os.environ.get("ALLOWED_CHAT_IDS", "")
    allowed |= {x.strip() for x in extra.split(",") if x.strip()}
    return str(chat_id) in allowed


def parse_fd_time(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST)
    except Exception:
        return None


def fd_get(path, timeout=15):
    return requests.get(
        f"https://{FRESHDESK_DOMAIN}.freshdesk.com/api/v2/{path}",
        headers=freshdesk_headers(),
        timeout=timeout,
    )


def resolve_user_name(uid, agent_first, cache, budget):
    """user_id -> 이름. 실패하면 None (조회 횟수 제한)"""
    if not uid:
        return None
    if uid in cache:
        return cache[uid]
    name = None
    for kind in (["agents", "contacts"] if agent_first else ["contacts", "agents"]):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        try:
            r = fd_get(f"{kind}/{uid}", timeout=5)
            if r.status_code == 200:
                j = r.json()
                name = (j.get("contact") or {}).get("name") or j.get("name")
                if name:
                    break
        except Exception:
            pass
    cache[uid] = name
    return name


def fetch_conversations(ticket_id):
    items, page = [], 1
    while page <= 10:
        r = fd_get(f"tickets/{ticket_id}/conversations?page={page}&per_page=100")
        if r.status_code != 200:
            return None, r.status_code
        data = r.json()
        items.extend(data)
        if len(data) < 100:
            break
        page += 1
    return items, None


def build_timeline_entries(ticket, convs, cache, budget):
    entries = [{
        "time": parse_fd_time(ticket.get("created_at", "")),
        "side": "customer",
        "kind": "접수",
        "who": resolve_user_name(ticket.get("requester_id"), False, cache, budget) or "고객",
        "text": clean_body(ticket.get("description") or ticket.get("description_text", "")),
    }]
    for cv in sorted(convs, key=lambda x: x.get("created_at", "")):
        private = bool(cv.get("private"))
        incoming = bool(cv.get("incoming"))
        if private:
            side, kind, default_who = "note", "내부메모", "에이전트"
        elif incoming:
            side, kind, default_who = "customer", "고객 회신", "고객"
        else:
            side, kind, default_who = "agent", "에이전트 회신", "에이전트"
        who = resolve_user_name(cv.get("user_id"), side != "customer", cache, budget) or default_who
        entries.append({
            "time": parse_fd_time(cv.get("created_at", "")),
            "side": side, "kind": kind, "who": who,
            "text": clean_body(cv.get("body") or cv.get("body_text", "")),
        })
    return entries


def summarize_timeline(entries):
    """항목별 한 줄 요약 + 현재 상황. 실패하면 (None, None)"""
    n = len(entries)
    per = max(120, min(400, 6000 // n))  # 413 방지: 전체 입력 길이 제한
    convo = "\n".join(
        f"{i}. [{e['kind']}/{e['who']}] {e['text'][:per] or '(내용 없음)'}"
        for i, e in enumerate(entries, 1)
    )
    prompt = (
        f"아래는 Freshdesk 티켓의 시간순 대화 {n}건이야. 각 항목을 한국어 한 줄로 요약하고, 마지막에 현재 상황을 정리해.\n\n"
        "[규칙]\n"
        "- 항목당 한 줄, 70자 이내, '~함/~됨/~요청함/~제공함' 형태의 단문 (격식체 금지)\n"
        "- MRN, 환자번호, 서버명, 날짜, 수치 같은 핵심 정보는 유지\n"
        "- 인사말, 서명, 면책 문구는 무시\n"
        "- 'reminder', 'any update' 같은 단순 재촉은 '단순 리마인더(추가 정보 없음)'로 요약\n"
        "- 원문에 없는 내용 추가 금지\n"
        f"- lines 배열은 입력 순서 그대로 정확히 {n}개\n"
        "- current: 지금까지의 흐름과 현재 상황을 2문장 이내로\n\n"
        f"[대화]\n{convo}\n\n"
        'JSON으로만 응답 (코드블록 금지): {"lines": ["...", "..."], "current": "..."}'
    )
    text = call_groq(prompt)
    if not text:
        return None, None
    try:
        data = json.loads(text.replace("```json", "").replace("```", "").strip())
        lines = data.get("lines")
        if isinstance(lines, list) and len(lines) == n:
            return [str(x).strip() for x in lines], str(data.get("current", "")).strip()
        log(f"[타임라인] 요약 줄 수 불일치: {len(lines) if isinstance(lines, list) else 'N/A'} != {n}")
    except Exception as e:
        log(f"[타임라인] JSON 파싱 오류: {e}")
    return None, None


def ball_in_court(entries, status):
    public = [e for e in entries if e["side"] != "note"]
    if status in (4, 5):
        return "✅ 처리 완료 상태"
    last = public[-1]
    if last["side"] == "customer":
        msg = "🔔 우리 쪽 답변 필요 (고객이 마지막으로 회신)" if len(public) > 1 \
            else "🔔 우리 쪽 첫 답변 필요 (접수 후 회신 없음)"
    else:
        msg = "⏳ 고객 회신 대기 (우리가 마지막으로 회신)"
    t_last, t_pub = entries[-1]["time"], last["time"]
    if entries[-1]["side"] == "note" and t_last and t_pub and t_last > t_pub:
        msg += " · 이후 내부 논의 진행 중"
    return msg


def ago_text(t):
    if not t:
        return "알 수 없음"
    delta = datetime.now(KST) - t
    if delta.days >= 1:
        return f"{delta.days}일 전"
    hours = delta.seconds // 3600
    return f"{hours}시간 전" if hours >= 1 else "방금 전"


def send_long(chat_id, text, limit=3800):
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            send_telegram_to(chat_id, chunk.rstrip())
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        send_telegram_to(chat_id, chunk.rstrip())


def run_timeline(chat_id, ticket_id):
    try:
        r = fd_get(f"tickets/{ticket_id}")
        if r.status_code == 404:
            send_telegram_to(chat_id, f"티켓 #{ticket_id}을(를) 찾을 수 없어요. 번호를 확인해 주세요.")
            return
        if r.status_code != 200:
            send_telegram_to(chat_id, f"Freshdesk 조회에 실패했어요. (코드 {r.status_code})")
            return
        ticket = r.json()
        convs, err = fetch_conversations(ticket_id)
        if err:
            send_telegram_to(chat_id, f"대화 내역 조회에 실패했어요. (코드 {err})")
            return

        entries = build_timeline_entries(ticket, convs, {}, [10])
        total, omitted = len(entries), 0
        if total > MAX_TIMELINE_ENTRIES:
            omitted = total - MAX_TIMELINE_ENTRIES
            entries = entries[:1] + entries[-(MAX_TIMELINE_ENTRIES - 1):]

        summaries, current = summarize_timeline(entries)
        ai_ok = summaries is not None
        if not ai_ok:
            summaries = [(e["text"][:70] + ("…" if len(e["text"]) > 70 else "")) or "(내용 없음)" for e in entries]
            current = "AI 요약에 실패해 원문 일부를 표시했어요."

        icons = {"customer": "👤", "agent": "🛠", "note": "📝"}
        status = ticket.get("status")
        created = parse_fd_time(ticket.get("created_at", ""))
        head = [
            f"🕘 #{ticket_id} 타임라인",
            (ticket.get("subject") or "")[:150],
            f"상태: {TIMELINE_STATUS.get(status, f'상태코드 {status}')} · "
            f"접수 {created.strftime('%m/%d') if created else '-'} · 대화 {total}건",
            f"마지막 활동: {ago_text(entries[-1]['time'])}",
            ball_in_court(entries, status),
            "",
        ]
        body = []
        for i, (e, s) in enumerate(zip(entries, summaries)):
            t = e["time"].strftime("%m/%d %H:%M") if e["time"] else "--/-- --:--"
            body.append(f"{t} {icons[e['side']]} {e['who'][:16]} · {s}")
            if i == 0 and omitted:
                body.append(f"   … 중간 {omitted}건 생략 …")
        tail = ["", "📌 현재 상황", current or "-"]
        send_long(chat_id, "\n".join(head + body + tail))
    except Exception as ex:
        log(f"[타임라인] 오류: {ex}")
        try:
            send_telegram_to(chat_id, "타임라인 생성 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")
        except Exception:
            pass


def handle_timeline_command(chat_id, text):
    if not is_allowed_chat(chat_id):
        send_telegram_to(
            chat_id,
            "이 채팅에서는 /timeline을 사용할 수 없어요.\n"
            f"(chat id: {chat_id})\n관리자가 Render 환경변수 ALLOWED_CHAT_IDS에 이 값을 추가하면 사용할 수 있어요.",
        )
        return
    m = re.search(r'(\d{3,})', text)
    if not m:
        send_telegram_to(chat_id, "티켓 번호를 입력해 주세요. 예: /timeline 14564")
        return
    ticket_id = m.group(1)
    send_telegram_to(chat_id, f"⏳ #{ticket_id} 타임라인 정리 중...")
    threading.Thread(target=run_timeline, args=(chat_id, ticket_id), daemon=True).start()


@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    try:
        data = request.json or {}
        # 그룹/1:1은 "message", 채널은 "channel_post"로 들어옴
        message = data.get("message") or data.get("channel_post") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()

        if not text or not chat_id:
            return jsonify({"status": "skip"}), 200

        log(f"[텔레그램] 메시지 수신: {text[:50]}")
        log(f"[텔레그램] 원문: {text}")

        if re.match(r'^/timeline(@\w+)?(\s|$)', text, re.IGNORECASE):
            handle_timeline_command(chat_id, text)
            return jsonify({"status": "ok"}), 200

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
        ticket_url  = make_ticket_url(str(fw.get("ticket_id", "")), fw.get("ticket_url", ""))
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
        ok = send_telegram(message, ticket_url)
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
        ticket_url  = make_ticket_url(str(fw.get("ticket_id", "")), fw.get("ticket_url", ""))
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
        ok = send_telegram(message, ticket_url)
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


def ping_url():
    """자기 자신의 /health 주소: SELF_URL > Render 자동 환경변수 > 기존 주소 순"""
    base = os.environ.get("SELF_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://freshdesk-bot-s1fa.onrender.com"
    return base.rstrip("/") + "/health"


# 서버 자체 핑 - Render 무료 플랜 잠들기 방지
def self_ping():
    time_module.sleep(60)
    while True:
        try:
            requests.get(ping_url(), timeout=10)
            log("[핑] 서버 유지 성공")
        except Exception as e:
            log(f"[핑] 실패: {e}")
        time_module.sleep(840)

threading.Thread(target=self_ping, daemon=True).start()


# 서버 시작(배포 완료) 알림
STARTUP_LOCK = "/tmp/.freshdesk_bot_started"
REQUIRED_ENV = ["GROQ_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FRESHDESK_API_KEY", "FRESHDESK_DOMAIN"]


def notify_startup(delay=5):
    """컨테이너가 시작될 때 한 번만 텔레그램으로 알림 (워커만 재시작되면 건너뜀)"""
    try:
        time_module.sleep(delay)
        try:
            fd = os.open(STARTUP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            return
        except OSError:
            pass  # 잠금 파일을 못 만들어도 알림은 보냄

        commit = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]
        lines = [
            "🚀 봇 서버가 시작됐어요",
            f"버전: {commit}" if commit else None,
            f"시각: {datetime.now(KST).strftime('%m/%d %H:%M')} (KST)",
        ]
        missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            lines += ["", "⚠️ 설정되지 않은 환경변수: " + ", ".join(missing)]
        send_telegram("\n".join(l for l in lines if l is not None))
        log("[시작알림] 전송 완료")
    except Exception as e:
        log(f"[시작알림] 실패: {e}")


threading.Thread(target=notify_startup, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
