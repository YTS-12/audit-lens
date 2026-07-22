"""Stage 8: incremental — 무인 증분 파이프라인 (AWS 상시 운영·로컬 불필요).

매 실행(평일 아침 크론):
  ① 최근 N일 전(全)시장 정기공시 조회 → 유니버스 808사 + 사업/분기/반기보고서만
  ② 이미 색인된 rcept는 건너뜀(상태 저장소 = OpenSearch 자체 → 별도 상태파일 없음, 멱등)
  ③ 신규 보고서: dcm 해소 → fetch → parse(v1·v2) → embed(v2, missing_only)
     → (연차만) LLM Fact 추출 + 표 기반 Fact(무API) + 정형재무(XBRL)
  ④ 정정공시 교체: 같은 (corp, fiscal_year, period_type)의 옛 rcept 청크·Fact 삭제

설계 원칙: 실패 격리(보고서 단위 try — 한 건 실패가 전체를 막지 않음), 멱등(재실행 안전),
           [첨부정정] 회피(document.xml 014), 뷰어 스로틀(AWS IP 보호).
"""
from __future__ import annotations
# ⚠️ sentence_transformers(→torch)를 lxml(parse)보다 먼저 로드해야 한다 — 순서가 바뀌면
#    Windows/conda에서 결정적 segfault(embed.py 상단 경고와 동일 계열). embed가 ST를 최상단 로드.
from src.pipeline import embed  # noqa: F401,E402  (순서 의도적 — 제거 금지)

import csv
import logging
import re
from datetime import date, timedelta
from config import settings
from src.clients.opendart import OpenDartClient
from src.clients.opensearch_store import OpenSearchStore

log = logging.getLogger(__name__)

YEAR_RE = re.compile(r"\((\d{4})\.\d{2}\)")   # "사업보고서 (2025.12)"


def _classify(report_nm: str):
    nm = report_nm.replace(" ", "")
    if "사업보고서" in nm:
        return "연차", "감사"
    if "반기보고서" in nm:
        return "반기", "검토"
    if "분기보고서" in nm:
        return "분기", "검토"
    return None, None


def _universe() -> dict[str, dict]:
    p = settings.meta_dir / "universe.csv"
    return {r["corp_code"]: r for r in csv.DictReader(p.open(encoding="utf-8-sig"))}


def _known(store: OpenSearchStore, rcept_no: str) -> bool:
    r = store.client.count(index=store.index,
                           body={"query": {"term": {"rcept_no": rcept_no}}})
    return int(r.get("count", 0)) > 0


def _old_rcepts(store: OpenSearchStore, corp: str, fy, period: str, exclude: str) -> list[str]:
    """같은 (기업, 연도, 보고서유형)의 기존 rcept — 정정 교체 대상."""
    must = [{"term": {"corp_code": corp}}, {"term": {"period_type": period}}]
    if fy is not None:
        must.append({"term": {"fiscal_year": int(fy)}})
    r = store.client.search(index=store.index, body={
        "size": 0, "query": {"bool": {"must": must}},
        "aggs": {"r": {"terms": {"field": "rcept_no", "size": 10}}}})
    return [b["key"] for b in r["aggregations"]["r"]["buckets"] if b["key"] != exclude]


def _append_disclosure(row: dict):
    """서버에도 disclosures.csv 상태를 누적(parse·extract가 읽는 계약 파일)."""
    p = settings.meta_dir / "disclosures.csv"
    fields = ["rcept_no", "corp_code", "corp_name", "report_nm", "fiscal_year",
              "period_type", "assurance", "rcept_dt", "dcm_no", "is_consolidated",
              "fs_basis", "dart_url"]
    exists = p.exists()
    if exists:  # 같은 rcept 중복 append 방지
        with p.open(encoding="utf-8-sig") as f:
            if any(r.get("rcept_no") == row["rcept_no"] for r in csv.DictReader(f)):
                return
    with p.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def run(days_back: int = 7, dry_run: bool = False, skip_llm: bool = False):
    settings.ensure_dirs()
    uni = _universe()
    store = OpenSearchStore()                  # 기본 = 서빙 인덱스(audit_chunks_v2)
    if not store.ping():
        raise SystemExit("OpenSearch 연결 실패")
    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout, max_retry=settings.http_max_retry)

    end = date.today()
    bgn = end - timedelta(days=days_back)
    print(f"[incremental] 조회 {bgn:%Y%m%d}~{end:%Y%m%d} · 인덱스 {store.index}")

    # ① 전시장 정기공시 → 유니버스·유형 필터, [첨부정정] 회피
    cands = []
    for item in dart.list_disclosures(None, f"{bgn:%Y%m%d}", f"{end:%Y%m%d}", pblntf_ty="A"):
        cc = item.get("corp_code")
        if cc not in uni:
            continue
        nm = item.get("report_nm", "")
        period, assurance = _classify(nm)
        if not period or "[첨부정정]" in nm.replace(" ", ""):
            continue
        m = YEAR_RE.search(nm)
        cands.append({"rcept_no": item["rcept_no"], "corp_code": cc,
                      "corp_name": uni[cc]["corp_name"], "report_nm": nm,
                      "fiscal_year": int(m.group(1)) if m else None,
                      "period_type": period, "assurance": assurance,
                      "rcept_dt": item.get("rcept_dt", ""), "dcm_no": "",
                      "is_consolidated": "", "fs_basis": "CFS",
                      "dart_url": dart.deep_link(item["rcept_no"])})
    # ② 신규만
    todo = [r for r in cands if not _known(store, r["rcept_no"])]
    print(f"[incremental] 기간 내 대상공시 {len(cands)} · 신규 {len(todo)}")
    if dry_run or not todo:
        for r in todo:
            print(f"  (신규) {r['corp_name']} {r['report_nm']} rcept={r['rcept_no']}")
        return {"candidates": len(cands), "new": len(todo), "ok": 0, "fail": 0}

    from src.pipeline import parse as parse_v1          # 지연 임포트(경량 우선)
    from src.pipeline import parse_v2, extract_tables

    ok = fail = 0
    new_annual_years: set[int] = set()
    for r in todo:
        cc, rcept = r["corp_code"], r["rcept_no"]
        try:
            # dcm 해소(뷰어, 스로틀) → 딥링크 완성
            dcm, is_cons, _ = dart.resolve_report_dcm(rcept, r["assurance"])
            if dcm:
                r["dcm_no"], r["is_consolidated"] = dcm, is_cons
                r["fs_basis"] = "CFS" if is_cons else "OFS"
                r["dart_url"] = dart.deep_link(rcept, dcm)
            zp = dart.download_document(rcept, settings.raw_dir / cc)
            _append_disclosure(r)
            parse_v1.run(corp=cc)                       # 멱등(해당 기업 재파싱, 초 단위)
            parse_v2.run(corps={cc})
            embed.run(corp=cc, src_subdir="parsed_v2", missing_only=True)
            # 표 스토어(doc_tables) 적재 — 누락 시 이 보고서는 '공시 원본 표'를 못 씀
            # (2026-07 발견: 증분에 이 단계가 빠져 색인↔표 어긋남 발생 → sync-tables로 복구)
            from src.pipeline import tables as tbl_store
            from src.clients.postgres import PostgresStore as _PG
            try:
                _pg = _PG()
                _pg.ensure_doc_tables()
                _pg.replace_doc_tables(
                    rcept, tbl_store.extract_filing(zp, r) + tbl_store.extract_filing_extra(zp, r))
            except Exception as te:                     # noqa: BLE001 — 표는 부가기능(색인은 유지)
                log.warning("표 스토어 적재 실패 %s: %s", rcept, te)
            # 정정 교체: 새 rcept 색인 후 같은 (corp, fy, period) 옛 rcept 제거
            olds = _old_rcepts(store, cc, r["fiscal_year"], r["period_type"], rcept)
            for old in olds:
                store.client.delete_by_query(index=store.index, body={
                    "query": {"term": {"rcept_no": old}}}, params={"refresh": "true"})
                log.info("정정 교체: %s 옛 청크 삭제(rcept=%s)", r["corp_name"], old)
            if olds:                                    # 옛 Fact는 보관함으로 이동(정정 이력 보존) 후 삭제
                from src.clients.postgres import PostgresStore
                with PostgresStore().conn.cursor() as cur:
                    cur.execute("""CREATE TABLE IF NOT EXISTS facts_archive
                                   (LIKE facts INCLUDING DEFAULTS,
                                    archived_at timestamptz DEFAULT now(), superseded_by text)""")
                    cur.execute("INSERT INTO facts_archive SELECT *, now(), %s FROM facts "
                                "WHERE rcept_no = ANY(%s)", (rcept, olds))
                    cur.execute("DELETE FROM facts WHERE rcept_no = ANY(%s)", (olds,))
                    cur.execute("DELETE FROM doc_tables WHERE rcept_no = ANY(%s)", (olds,))
            if r["period_type"] == "연차":
                if not skip_llm:
                    from src.pipeline import extract    # Claude API
                    extract.run(corp=cc, year=r["fiscal_year"])
                extract_tables.run(corps={cc}, years={r["fiscal_year"]})
                if r["fiscal_year"]:
                    new_annual_years.add(int(r["fiscal_year"]))
            ok += 1
            print(f"  [ok] {r['corp_name']} {r['report_nm']}")
        except Exception as e:                          # noqa: BLE001 — 보고서 단위 격리
            fail += 1
            log.warning("증분 실패 %s/%s: %s", cc, rcept, e)
            print(f"  [fail] {r['corp_name']} {rcept}: {type(e).__name__} {str(e)[:120]}")

    if new_annual_years and not skip_llm:               # 새 연차 → 정형재무(XBRL)도 갱신
        try:
            from src.pipeline import financials
            financials.run(years=sorted(new_annual_years))
        except Exception as e:                          # noqa: BLE001
            log.warning("financials 갱신 실패(다음 실행 때 재시도): %s", e)

    if ok:                                              # 정정공시_이력 최신화(disclosures.csv 기반, 멱등)
        try:
            extract_tables.run_correction_history()
        except Exception as e:                          # noqa: BLE001
            log.warning("정정공시_이력 갱신 실패(다음 실행 재시도): %s", e)

    store.refresh()
    print(f"[incremental] 완료 ok={ok} fail={fail} · index count={store.count():,}")
    return {"candidates": len(cands), "new": len(todo), "ok": ok, "fail": fail}
