"""
OpenDART 공시 -> Telegram 채널.
- keywords.txt 의 회사명을 corp_code로 매핑
- 핵심 공시 유형(정기/주요사항/발행/거래소)만 송출
- seen.json 으로 중복 방지
"""
import html
import io
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
KEYWORDS_FILE = ROOT / "keywords.txt"
SEEN_FILE = ROOT / "seen.json"
CORP_CODES_FILE = ROOT / "corp_codes.json"

MAX_SEEN = 3000
SEND_DELAY_SEC = 0.6
REQUEST_TIMEOUT = 30
LOOKBACK_DAYS = 1
CORP_CODES_REFRESH_DAYS = 7

# 핵심 공시 유형 코드 (DART 분류)
# A: 정기공시 (사업/반기/분기보고서)
# B: 주요사항보고 (유상증자/감자/합병/공급계약 등)
# C: 발행공시 (증권신고서)
# I: 거래소공시 (자기주식 취득/처분, 단일판매·공급계약 등)
CORE_PBLNTF_TY = ["A", "B", "C", "I"]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DART_API_KEY = os.environ.get("DART_API_KEY")

KST = timezone(timedelta(hours=9))


def load_keywords() -> list[str]:
    if not KEYWORDS_FILE.exists():
        print(f"keywords.txt 없음: {KEYWORDS_FILE}", file=sys.stderr)
        return []
    out = []
    for raw in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_seen(seen_list: list[str]) -> None:
    trimmed = seen_list[-MAX_SEEN:]
    SEEN_FILE.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def download_corp_codes() -> dict:
    """OpenDART 전체 회사 고유번호 다운로드. 상장사만 추림."""
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    r = requests.get(url, params={"crtfc_key": DART_API_KEY}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open("CORPCODE.xml") as f:
            tree = ET.parse(f)

    by_name: dict[str, str] = {}
    by_stock: dict[str, str] = {}
    for item in tree.getroot().findall("list"):
        corp_name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if not corp_name or not corp_code:
            continue
        # stock_code 가 비어있으면 비상장사 (제외)
        if not stock_code:
            continue
        by_name[corp_name] = corp_code
        by_stock[stock_code] = corp_code
    return {"by_name": by_name, "by_stock": by_stock}


def is_corp_codes_fresh() -> bool:
    if not CORP_CODES_FILE.exists():
        return False
    age_days = (time.time() - CORP_CODES_FILE.stat().st_mtime) / 86400
    return age_days < CORP_CODES_REFRESH_DAYS


def load_corp_codes() -> dict:
    if is_corp_codes_fresh():
        return json.loads(CORP_CODES_FILE.read_text(encoding="utf-8"))
    print("corp_codes 새로 다운로드 중...")
    mapping = download_corp_codes()
    CORP_CODES_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return mapping


def resolve_keyword(kw: str, corp_codes: dict) -> str | None:
    """키워드 -> corp_code. 회사명 정확 일치 또는 6자리 종목코드."""
    if kw.isdigit() and len(kw) == 6:
        return corp_codes["by_stock"].get(kw)
    return corp_codes["by_name"].get(kw)


def fetch_disclosures(corp_code: str, pblntf_ty: str) -> list[dict]:
    today = datetime.now(KST).date()
    bgn_de = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": pblntf_ty,
        "page_count": 100,
    }
    r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    status = data.get("status")
    if status == "013":  # 조회된 데이터 없음
        return []
    if status != "000":
        print(
            f"DART API 오류 [{corp_code}/{pblntf_ty}]: {data.get('message')}",
            file=sys.stderr,
        )
        return []
    return data.get("list", [])


def send_telegram(text: str) -> None:
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        api,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()


def format_message(item: dict) -> str:
    corp_name = html.escape(item.get("corp_name", ""))
    report_nm = html.escape(item.get("report_nm", "").strip())
    flr_nm = html.escape(item.get("flr_nm", ""))
    rcept_dt = item.get("rcept_dt", "")
    rcept_no = item.get("rcept_no", "")

    if len(rcept_dt) == 8:
        rcept_dt_fmt = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}"
    else:
        rcept_dt_fmt = rcept_dt

    link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

    return (
        f"<b>[{corp_name}]</b>\n"
        f"{report_nm}\n"
        f"제출: {flr_nm} | {rcept_dt_fmt}\n"
        f"{link}"
    )


def main() -> int:
    if not BOT_TOKEN or not CHAT_ID or not DART_API_KEY:
        print(
            "환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DART_API_KEY 필요",
            file=sys.stderr,
        )
        return 1

    keywords = load_keywords()
    if not keywords:
        print("키워드가 비어있음. keywords.txt 확인.", file=sys.stderr)
        return 1

    corp_codes = load_corp_codes()

    targets = []  # [(keyword, corp_code), ...]
    for kw in keywords:
        code = resolve_keyword(kw, corp_codes)
        if code:
            targets.append((kw, code))
        else:
            print(
                f"[WARN] '{kw}' DART 등록명/종목코드와 일치하는 상장사 없음",
                file=sys.stderr,
            )

    if not targets:
        print("매칭된 회사 없음.", file=sys.stderr)
        return 1

    is_first_run = not SEEN_FILE.exists()
    seen_order = load_seen()
    seen_set = set(seen_order)

    sent_count = 0
    discovered_count = 0

    for kw, corp_code in targets:
        for ty in CORE_PBLNTF_TY:
            try:
                items = fetch_disclosures(corp_code, ty)
            except Exception as ex:
                print(f"[{kw}/{ty}] fetch 실패: {ex}", file=sys.stderr)
                continue

            for item in items:
                rcept_no = item.get("rcept_no", "")
                if not rcept_no or rcept_no in seen_set:
                    continue

                discovered_count += 1

                if is_first_run:
                    seen_set.add(rcept_no)
                    seen_order.append(rcept_no)
                    continue

                try:
                    send_telegram(format_message(item))
                    seen_set.add(rcept_no)
                    seen_order.append(rcept_no)
                    sent_count += 1
                    time.sleep(SEND_DELAY_SEC)
                except Exception as ex:
                    print(f"송출 실패 [{kw}] {rcept_no}: {ex}", file=sys.stderr)

    save_seen(seen_order)

    if is_first_run:
        print(f"첫 실행: {discovered_count}건을 seen.json 에 시드. 다음 실행부터 송출.")
    else:
        print(f"발견 {discovered_count}건 / 송출 {sent_count}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
