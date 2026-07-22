"""Stage: financials — OpenDART 전체재무제표(fnlttSinglAcntAll) → 핵심계정 → PostgreSQL.
계산형/순위 질의("영업이익 상위 10", "부채비율 200% 초과")를 SQL로 전수·정확 처리.
재개가능(이미 적재한 corp×year 건너뜀) · 연결(CFS) 우선, 없으면 별도(OFS) 폴백.
"""
from __future__ import annotations
import csv
import logging

from config import settings
from src.clients.opendart import OpenDartClient
from src.clients.postgres import PostgresStore

log = logging.getLogger(__name__)

# 표준 지표 ← account_nm 변형 후보(앞에서 먼저 매칭) — v2: 6종 → 17종(+현금흐름)
KEY_ACCOUNTS = {
    "자산총계": ["자산총계"],
    "부채총계": ["부채총계"],
    "자본총계": ["자본총계", "자본총계(결손금)"],
    "유동자산": ["유동자산", "I. 유동자산"],
    "비유동자산": ["비유동자산", "II. 비유동자산"],
    "유동부채": ["유동부채", "I. 유동부채"],
    "비유동부채": ["비유동부채", "II. 비유동부채"],
    "현금및현금성자산": ["현금및현금성자산", "현금 및 현금성자산", "현금및현금등가물"],
    "매출액": ["매출액", "수익(매출액)", "영업수익", "매출", "I. 매출액", "매출액(수익)"],
    "매출원가": ["매출원가", "영업비용"],
    "매출총이익": ["매출총이익", "매출총이익(손실)"],
    "판매비와관리비": ["판매비와관리비", "판매비와 관리비", "판매관리비"],
    "영업이익": ["영업이익", "영업이익(손실)", "영업손익"],
    "당기순이익": ["당기순이익", "당기순이익(손실)", "당기순손익", "분기순이익", "반기순이익"],
    "영업활동현금흐름": ["영업활동현금흐름", "영업활동으로 인한 현금흐름", "영업활동으로부터의 현금흐름",
                  "영업활동 현금흐름", "I. 영업활동현금흐름", "영업활동으로인한현금흐름"],
    "투자활동현금흐름": ["투자활동현금흐름", "투자활동으로 인한 현금흐름", "투자활동으로부터의 현금흐름",
                  "투자활동 현금흐름", "투자활동으로인한현금흐름"],
    "재무활동현금흐름": ["재무활동현금흐름", "재무활동으로 인한 현금흐름", "재무활동으로부터의 현금흐름",
                  "재무활동 현금흐름", "재무활동으로인한현금흐름"],
}


def _amount(accounts: list[dict], names: list[str]):
    for a in accounts:
        if (a.get("account_nm") or "").strip() in names:
            v = str(a.get("thstrm_amount") or "").replace(",", "").strip()
            try:
                return float(v)
            except ValueError:
                continue
    return None


def _extract(resp: dict) -> dict:
    accounts = resp.get("list") or []
    return {m: _amount(accounts, names) for m, names in KEY_ACCOUNTS.items()}


def _ensure_table(pg: PostgresStore) -> None:
    with pg.conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS financials(
            corp_code   text NOT NULL,
            fiscal_year int  NOT NULL,
            is_consolidated boolean NOT NULL,
            metric      text NOT NULL,
            amount      numeric,
            PRIMARY KEY (corp_code, fiscal_year, is_consolidated, metric))""")
        # v2: 전체 계정 원시 적재(향후 계정 단위 조회·온디맨드 수치 근거)
        cur.execute("""CREATE TABLE IF NOT EXISTS financial_items(
            corp_code   text NOT NULL,
            fiscal_year int  NOT NULL,
            is_consolidated boolean NOT NULL,
            sj_div      text,           -- BS/IS/CIS/CF/SCE
            account_id  text,
            account_nm  text,
            amount      numeric,
            ord         int)""")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_fitem_corp ON financial_items(corp_code, fiscal_year)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_fitem_acct ON financial_items(account_nm)")


def _universe():
    p = settings.meta_dir / "universe.csv"
    return list(csv.DictReader(p.open(encoding="utf-8-sig")))


def _done(pg: PostgresStore) -> set:
    # v2 재개 기준 = financial_items(구버전 6지표만 있는 회차는 재수집해 확장 적재)
    with pg.conn.cursor() as cur:
        cur.execute("SELECT DISTINCT corp_code, fiscal_year FROM financial_items")
        return {(r[0], int(r[1])) for r in cur.fetchall()}


def run(years=None, limit=None, force=False):
    years = years or [2025, 2024, 2023]
    pg = PostgresStore()
    if not pg.ping():
        raise SystemExit("PostgreSQL 연결 실패")
    pg.ensure_ready(); _ensure_table(pg)
    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout, max_retry=settings.http_max_retry)
    done = set() if force else _done(pg)
    uni = _universe()
    if limit:
        uni = uni[:limit]
    ok = miss = 0
    for i, r in enumerate(uni, 1):
        cc = r.get("corp_code")
        for y in years:
            if (cc, y) in done:
                continue
            is_cfs = True
            try:
                resp = dart.financials(cc, y, fs_div="CFS")
                if resp.get("status") != "000" or not resp.get("list"):
                    resp = dart.financials(cc, y, fs_div="OFS"); is_cfs = False
                if resp.get("status") != "000":
                    miss += 1; continue
                vals = _extract(resp)
                rows = [(cc, y, is_cfs, m, v) for m, v in vals.items() if v is not None]
                if not rows:
                    miss += 1; continue
                # v2: 전체 계정 원시행도 함께 적재
                items = []
                for a in (resp.get("list") or []):
                    raw = str(a.get("thstrm_amount") or "").replace(",", "").strip()
                    try:
                        amt = float(raw) if raw not in ("", "-") else None
                    except ValueError:
                        amt = None
                    try:
                        ordv = int(a.get("ord") or 0)
                    except ValueError:
                        ordv = 0
                    items.append((cc, y, is_cfs, (a.get("sj_div") or "")[:8],
                                  (a.get("account_id") or "")[:200],
                                  (a.get("account_nm") or "").strip()[:200], amt, ordv))
                with pg.conn.cursor() as cur:
                    cur.execute("DELETE FROM financials WHERE corp_code=%s AND fiscal_year=%s", (cc, y))
                    cur.executemany(
                        "INSERT INTO financials(corp_code,fiscal_year,is_consolidated,metric,amount) "
                        "VALUES (%s,%s,%s,%s,%s)", rows)
                    cur.execute("DELETE FROM financial_items WHERE corp_code=%s AND fiscal_year=%s", (cc, y))
                    if items:
                        cur.executemany(
                            "INSERT INTO financial_items(corp_code,fiscal_year,is_consolidated,"
                            "sj_div,account_id,account_nm,amount,ord) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            items)
                ok += 1
            except Exception as e:  # noqa: BLE001
                miss += 1
                log.warning("financials 실패 %s/%s: %s", cc, y, e)
        if i % 100 == 0:
            log.info("…%d/%d사 (적재 %d · 미확보 %d)", i, len(uni), ok, miss)
    with pg.conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT corp_code) FROM financials")
        tot = cur.fetchone()
    log.info("=== financials 완료: 적재 %d · 미확보 %d · 총행 %d · 기업 %d ===",
             ok, miss, tot[0], tot[1])
    print(f"[financials] ok={ok} miss={miss} rows={tot[0]} corps={tot[1]}")
