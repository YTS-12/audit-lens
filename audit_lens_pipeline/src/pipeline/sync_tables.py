"""Stage 5c: sync-tables — 색인(OpenSearch) ↔ 표 스토어(doc_tables) 정합 동기화.

배경(2026-07): 로컬에서 만든 표 스토어를 서버에 전면 교체(swap)하면, 그 사이 서버 증분
파이프라인이 넣은 신규·정정 공시의 표가 사라지고(색인엔 있는데 표 없음), 반대로 정정으로
대체된 옛 공시의 표만 남는(표는 있는데 색인 없음) 어긋남이 생긴다. 표 매칭은 rcept_no
기준이라 어긋나면 그 보고서는 영원히 '공시 원본 표'를 못 쓴다.

이 모듈은 두 저장소의 rcept 집합을 맞춘다:
  ① 색인에만 있는 rcept → 원본 zip에서 표 추출·적재(서버 raw_dir 사용)
  ② 표에만 있는 rcept   → 이미 대체된 공시이므로 표 삭제(정리)
재실행 안전(멱등). 전면 교체 직후 반드시 1회 실행할 것.
"""
from __future__ import annotations
import csv
import logging

from config import settings

log = logging.getLogger(__name__)


def _indexed_rcepts() -> dict[str, str]:
    """색인된 rcept_no → corp_code (표 추출 시 zip 경로 확인용)."""
    from src.clients.opensearch_store import OpenSearchStore   # 지연 임포트
    store = OpenSearchStore()
    out: dict[str, str] = {}
    after = None
    while True:                                   # composite 페이징(수천 건)
        body = {"size": 0, "aggs": {"r": {"composite": {
            "size": 1000,
            "sources": [{"rc": {"terms": {"field": "rcept_no"}}},
                        {"cc": {"terms": {"field": "corp_code"}}}]}}}}
        if after:
            body["aggs"]["r"]["composite"]["after"] = after
        res = store.client.search(index=store.index, body=body)
        agg = res["aggregations"]["r"]
        for b in agg["buckets"]:
            out[b["key"]["rc"]] = b["key"]["cc"]
        after = agg.get("after_key")
        if not after or not agg["buckets"]:
            break
    return out


def _disclosure_meta() -> dict[str, dict]:
    fp = settings.meta_dir / "disclosures.csv"
    if not fp.exists():
        return {}
    return {r["rcept_no"]: r for r in csv.DictReader(fp.open(encoding="utf-8-sig"))}


def run(prune: bool = True, dry_run: bool = False):
    from src.clients.postgres import PostgresStore
    from src.pipeline.tables import extract_filing, extract_filing_extra

    pg = PostgresStore()
    pg.ensure_doc_tables()
    with pg.conn.cursor() as cur:
        cur.execute("SELECT DISTINCT rcept_no FROM doc_tables")
        stored = {r[0] for r in cur.fetchall()}
    indexed = _indexed_rcepts()
    meta = _disclosure_meta()

    missing = [rc for rc in indexed if rc not in stored]          # ① 표 없음 → 추출
    stale = sorted(stored - set(indexed)) if prune else []        # ② 색인 없음 → 정리
    print(f"[sync-tables] 색인 {len(indexed)} · 표 {len(stored)} "
          f"· 표없음 {len(missing)} · 색인없음 {len(stale)}")
    if dry_run:
        for rc in missing[:20]:
            print(f"  (표없음) {rc} {meta.get(rc, {}).get('corp_name', '')}")
        for rc in stale[:20]:
            print(f"  (색인없음) {rc}")
        return {"missing": len(missing), "stale": len(stale), "added": 0, "pruned": 0}

    added = n_tbl = fail = miss_zip = 0
    for rc in missing:
        cc = indexed[rc]
        zp = settings.raw_dir / cc / f"{rc}.zip"
        if not zp.exists():
            miss_zip += 1
            continue
        row = meta.get(rc) or {"corp_code": cc, "rcept_no": rc,
                               "fiscal_year": 0, "dart_url": ""}
        row = {**row, "corp_code": cc, "rcept_no": rc}
        try:
            recs = extract_filing(zp, row) + extract_filing_extra(zp, row)
            pg.replace_doc_tables(rc, recs)       # rcept 단위 멱등 교체
            added += 1
            n_tbl += len(recs)
        except Exception as e:                    # noqa: BLE001 — 보고서 단위 격리
            fail += 1
            log.warning("표 추출 실패 %s: %s", rc, e)

    pruned = 0
    if stale:
        with pg.conn.cursor() as cur:
            cur.execute("DELETE FROM doc_tables WHERE rcept_no = ANY(%s)", (stale,))
            pruned = cur.rowcount
    print(f"[sync-tables] 완료: 추가 필링 {added} · 표 {n_tbl} · zip없음 {miss_zip} "
          f"· 실패 {fail} · 정리 {pruned}행")
    return {"missing": len(missing), "stale": len(stale), "added": added,
            "tables": n_tbl, "pruned": pruned, "no_zip": miss_zip, "fail": fail}
