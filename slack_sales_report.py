import os
import json
import requests
from datetime import datetime, timedelta
import pytz

# ─────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────
SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
CHANNEL_ID = "C07191ZU8BS"          # #4-mkt-monitoring-bot-결제
MY_USER_ID = os.environ["SLACK_USER_ID"]  # 리포트 받을 본인 DM용 user_id

# 이모지 → 담당자 매핑
EMOJI_TO_PERSON = {
    "goraniitsme": "채원",
    "gorani_itsme": "채원",
    "goraniitsme1": "채원",
    "purple_heart": "항우",
    "meow_party": "진솔",
    "kirby_dance": "다현",
    "쾅": "승연",
}

KST = pytz.timezone("Asia/Seoul")

# ─────────────────────────────────────────────
# 어제 날짜 문자열 반환 (결제 날짜 필드 필터링용)
# ─────────────────────────────────────────────
def get_yesterday_date_str():
    now_kst = datetime.now(KST)
    yesterday = now_kst - timedelta(days=1)
    return yesterday.strftime("%Y.%m.%d")  # e.g. "2026.04.30"

# ─────────────────────────────────────────────
# Slack 채널 메시지 수집 (cursor 기반 전체 순회)
# oldest/latest 타임스탬프 없이 cursor로만 페이지네이션
# → 자정 경계값 누락 문제 없음
# ─────────────────────────────────────────────
def fetch_all_messages_for_date(target_date_str):
    """
    cursor로 채널 전체를 순회하면서
    메시지 내 '결제 날짜: {target_date_str}' 텍스트가 있는 것만 수집.
    target_date_str: "2026.04.30" 형식
    이틀 전 날짜가 나오기 시작하면 순회 중단 (과거로 갈수록 오래된 데이터).
    """
    # 타겟 날짜 하루 전 날짜 (이 날짜가 보이면 중단)
    target_dt = datetime.strptime(target_date_str, "%Y.%m.%d")
    stop_date_str = (target_dt - timedelta(days=1)).strftime("%Y.%m.%d")

    matched = []
    cursor = None

    while True:
        params = {"channel": CHANNEL_ID, "limit": 200}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            "https://slack.com/api/conversations.history",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
            params=params,
        ).json()
        if not resp.get("ok"):
            raise RuntimeError(f"Slack API error: {resp.get('error')}")

        messages = resp.get("messages", [])
        stop = False
        for msg in messages:
            text = msg.get("text", "")
            if f"결제 날짜: {target_date_str}" in text:
                matched.append(msg)
            # 타겟보다 하루 이상 오래된 날짜가 나오면 중단
            elif f"결제 날짜: {stop_date_str}" in text:
                stop = True
                break

        meta = resp.get("response_metadata", {})
        cursor = meta.get("next_cursor")
        if stop or not cursor:
            break

    return matched

# ─────────────────────────────────────────────
# 메시지 파싱
# ─────────────────────────────────────────────
def parse_message(msg):
    text = msg.get("text", "")
    if "플러스 결제" not in text:
        return None

    # 고객명
    customer = ""
    for line in text.splitlines():
        if "고객 정보:" in line:
            customer = line.split("고객 정보:")[-1].strip()
            break

    # 결제액
    amount_raw = ""
    for line in text.splitlines():
        if "결제액:" in line:
            amount_raw = line.split("결제액:")[-1].strip()
            break

    # 구매 상품
    product = ""
    for line in text.splitlines():
        if "구매 상품:" in line:
            product = line.split("구매 상품:")[-1].strip()
            break

    # 이모지 reactions
    reactions = msg.get("reactions", [])
    emoji_names = [r["name"] for r in reactions]

    # agite 외 추가 이모지 → 담당자 판별
    sales_person = None
    for name in emoji_names:
        normalized = name.replace("-", "").replace("_", "").lower()
        for key, person in EMOJI_TO_PERSON.items():
            if key.replace("_", "").lower() == normalized:
                sales_person = person
                break
        if sales_person:
            break

    return {
        "customer": customer,
        "product": product,
        "amount_raw": amount_raw,
        "sales_person": sales_person,  # None이면 자연결제
        "ts": msg.get("ts"),
    }

# ─────────────────────────────────────────────
# 결제액 파싱 (원화 / 달러 모두 처리)
# ─────────────────────────────────────────────
def parse_amount(amount_raw):
    """returns (amount_float, currency)"""
    raw = amount_raw.replace(",", "").strip()
    if raw.startswith("$"):
        try:
            return float(raw[1:]), "USD"
        except ValueError:
            return 0, "USD"
    else:
        try:
            return float(raw.replace("원", "")), "KRW"
        except ValueError:
            return 0, "KRW"

# ─────────────────────────────────────────────
# 리포트 텍스트 생성
# ─────────────────────────────────────────────
def build_report(date_str, records):
    sales = [r for r in records if r["sales_person"]]
    natural = [r for r in records if not r["sales_person"]]

    # 담당자별 집계
    person_stats = {}
    for r in sales:
        p = r["sales_person"]
        amt, cur = parse_amount(r["amount_raw"])
        if p not in person_stats:
            person_stats[p] = {"count": 0, "krw": 0, "usd": 0}
        person_stats[p]["count"] += 1
        if cur == "KRW":
            person_stats[p]["krw"] += amt
        else:
            person_stats[p]["usd"] += amt

    # 전체 합계
    total_krw_sales = sum(v["krw"] for v in person_stats.values())
    total_usd_sales = sum(v["usd"] for v in person_stats.values())

    nat_krw = sum(parse_amount(r["amount_raw"])[0] for r in natural if parse_amount(r["amount_raw"])[1] == "KRW")
    nat_usd = sum(parse_amount(r["amount_raw"])[0] for r in natural if parse_amount(r["amount_raw"])[1] == "USD")

    total_krw = total_krw_sales + nat_krw
    total_usd = total_usd_sales + nat_usd

    total_count = len(records)
    sales_count = len(sales)
    nat_count = len(natural)

    krw_ratio = (total_krw_sales / total_krw * 100) if total_krw > 0 else 0

    lines = []
    lines.append(f"📊 *{date_str} 일별 결제 리포트*")
    lines.append("")
    lines.append(f"*전체 결제*: {total_count}건")
    lines.append(f"  • 원화: {total_krw:,.0f}원  |  달러: ${total_usd:,.2f}")
    lines.append("")
    lines.append(f"*🟣 세일즈 결제*: {sales_count}건 (원화 기준 {krw_ratio:.1f}%)")
    lines.append(f"  • 원화: {total_krw_sales:,.0f}원  |  달러: ${total_usd_sales:,.2f}")
    lines.append("")

    for person, stat in sorted(person_stats.items(), key=lambda x: -x[1]["krw"]):
        usd_str = f"  + ${stat['usd']:,.2f}" if stat["usd"] > 0 else ""
        lines.append(f"  ▸ *{person}*: {stat['count']}건 / {stat['krw']:,.0f}원{usd_str}")
        # 고객 목록
        for r in sales:
            if r["sales_person"] == person:
                lines.append(f"       - {r['customer']}  {r['amount_raw']}")

    lines.append("")
    lines.append(f"*⚪ 자연결제*: {nat_count}건")
    lines.append(f"  • 원화: {nat_krw:,.0f}원  |  달러: ${nat_usd:,.2f}")

    return "\n".join(lines)

# ─────────────────────────────────────────────
# Slack DM 전송
# ─────────────────────────────────────────────
def send_dm(user_id, text):
    # DM 채널 열기
    resp = requests.post(
        "https://slack.com/api/conversations.open",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        json={"users": user_id},
    ).json()
    if not resp.get("ok"):
        raise RuntimeError(f"conversations.open error: {resp.get('error')}")
    dm_channel = resp["channel"]["id"]

    # 메시지 전송
    resp2 = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
        json={"channel": dm_channel, "text": text, "mrkdwn": True},
    ).json()
    if not resp2.get("ok"):
        raise RuntimeError(f"chat.postMessage error: {resp2.get('error')}")
    print("✅ DM 전송 완료")

# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────
def main():
    date_str = get_yesterday_date_str()  # e.g. "2026.04.30"
    report_date = datetime.strptime(date_str, "%Y.%m.%d").strftime("%Y-%m-%d")
    print(f"📅 집계 날짜: {date_str} (결제 날짜 필드 기준)")

    messages = fetch_all_messages_for_date(date_str)
    print(f"📨 매칭된 메시지: {len(messages)}개")

    records = [r for r in (parse_message(m) for m in messages) if r]
    print(f"📦 파싱된 결제 건수: {len(records)}개")

    report = build_report(report_date, records)
    print("\n" + report)

    send_dm(MY_USER_ID, report)

if __name__ == "__main__":
    main()
