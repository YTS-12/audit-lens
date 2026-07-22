"""Stage 1: refresh_master — 코스피 전 종목 유니버스 스냅샷 구축.

KRX(코스피 명단+업종) × DART(corp_code, 기업개황)를 종목코드로 조인하고
예외 규칙을 적용해 as-of 스냅샷 CSV를 만든다.
"""
from __future__ import annotations
import csv
from pathlib import Path
from config import settings
from src.clients.opendart import OpenDartClient
from src.clients.krx import (KrxClient, DummyKrxClient,
                             load_sector_csv, find_latest_sector_csv)


def _krx_client():
    if settings.krx_api_key:
        return KrxClient(settings.krx_api_key, timeout=settings.http_timeout,
                         max_retry=settings.http_max_retry)
    print("[warn] KRX_API_KEY 없음 → DummyKrxClient로 흐름만 검증")
    return DummyKrxClient()


def run(enrich: bool = False, sector_csv: str | None = None):
    settings.ensure_dirs()
    base_date = settings.as_of_date.replace("-", "")  # YYYYMMDD

    krx = _krx_client()
    listing = krx.kospi_listing(base_date)
    print(f"[universe] KRX 코스피 종목 {len(listing)}건")

    # KRX 업종분류(data.krx.co.kr 수동 다운로드 CSV) → 종목코드→업종명
    if sector_csv is None:
        sector_csv = find_latest_sector_csv(Path(__file__).resolve().parents[2] / "inputs")
    sectors = load_sector_csv(sector_csv) if sector_csv else {}
    if sectors:
        print(f"[universe] KRX 업종 CSV: {sector_csv} ({len(sectors)}종목)")
    else:
        print("[universe] KRX 업종 CSV 없음 → krx_sector 공란 "
              "(inputs/krx_sector_*.csv 권장)")

    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout,
                          max_retry=settings.http_max_retry)
    code_map = {c["stock_code"]: c for c in dart.corp_codes() if c["stock_code"]}
    print(f"[universe] DART 상장사 corp_code {len(code_map)}건")

    rows, missing = [], 0
    for item in listing:
        sc = item["stock_code"].zfill(6)
        d = code_map.get(sc) or code_map.get(item["stock_code"])
        if not d:
            missing += 1
            continue
        rec = {
            "corp_code": d["corp_code"], "stock_code": sc,
            "corp_name": d["corp_name"], "market": "KOSPI",
            "krx_sector": sectors.get(sc, {}).get("krx_sector", "")
                          or item.get("krx_sector", ""),
            "induty_code": "", "industry_name": "",   # Phase 0에서 확정
            "fiscal_month": "", "listed_flag": "normal",
            "as_of_date": settings.as_of_date,
        }
        # 예외 규칙·업종코드는 기업개황 호출이 필요(호출량 큼) → enrich 옵션
        if enrich:
            try:
                co = dart.company(d["corp_code"])
                if co.get("status") == "000":
                    rec["induty_code"] = co.get("induty_code", "")
                    rec["fiscal_month"] = co.get("acc_mt", "")
                    # corp_cls Y가 아니면(코스피 아님) 예외 표시
                    if co.get("corp_cls") and co["corp_cls"] != "Y":
                        rec["listed_flag"] = f"non_kospi:{co['corp_cls']}"
            except Exception as e:           # noqa
                rec["listed_flag"] = "enrich_failed"
        rows.append(rec)

    out = settings.meta_dir / "universe.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    sector_filled = sum(1 for r in rows if r["krx_sector"])
    no_sector = [r["stock_code"] for r in rows if not r["krx_sector"]]
    print(f"[universe] 저장 {out} · {len(rows)}사 (DART 미매칭 {missing}건, "
          f"krx_sector 채움 {sector_filled}/{len(rows)})")
    if no_sector:
        print(f"  · 업종 미매칭 {len(no_sector)}사 예: {no_sector[:10]}")
    print("  ※ 예외 규칙(신규상장 3년 미만·상폐·합병·사명변경·결산월)은 "
          "discover 단계에서 보유 연도 기준으로 자동 반영됨")
    return out
