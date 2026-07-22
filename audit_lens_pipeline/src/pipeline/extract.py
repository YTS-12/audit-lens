"""Fact Store 추출(Layer 2 적재): 감사보고서·핵심 주석 청크 → Claude(Sonnet, tool-use)
→ 표준 Fact 12종 → PostgreSQL `facts`.

설계 §5.2: "모두 알려줘" 류 스크리닝/집계는 전수검사라 벡터 top-k로는 누락 → 수집 시점에
사실을 미리 추출해 구조화 테이블에 적재하고 SQL로 전수·정확 처리한다.

특성: 재개가능(이미 추출한 rcept 건너뜀) · 보고서 단위 멱등 교체 · 인용 기계검증 · dead-letter.
대상: 연차 감사보고서(period_type=연차, assurance=감사). 분기/반기 검토는 제외.
"""
from __future__ import annotations
import csv
import json
import logging
import time
from pathlib import Path

from config import settings
from src.clients.claude import ClaudeClient
from src.clients.postgres import PostgresStore

log = logging.getLogger(__name__)

# 추출 입력으로 보낼 섹션 선별 키워드(프롬프트 크기 제어)
_AUDIT_KEYS = ("감사의견", "핵심감사사항", "계속기업", "내부회계관리",
               "감사보수", "회계감사인", "독립된 감사인", "강조사항")
_NOTE_KEYS = ("회계정책", "회계추정", "추정", "유형자산", "감가상각", "내용연수",
              "특수관계자", "재고자산", "수익", "우발", "소송", "충당부채", "내부회계")
_SCALE = {"원": 1, "천원": 1000, "백만원": 1_000_000,
          "억원": 100_000_000, "십억원": 1_000_000_000}


def _norm(s: str) -> str:
    return "".join((s or "").split())


OPINION_TYPES = ("감사의견_유형", "내부회계관리제도_검토의견")


def normalize_opinion(raw: str) -> str:
    """감사·내부회계 검토의견 자유서술 → 통제어휘 5종(적정/비적정/한정/의견거절/미기재).
    감사기준(KSA 700/705)·내부회계 검토 관행 기반. 스크리닝 정밀화의 근본 해결."""
    v = (raw or "").strip()
    if not v or v in ("-", "—", "–"):
        return "미기재"
    if any(k in v for k in ["표기 없음", "칸은 미기재", "칸 미기재", "기재 없음(대시"]):
        return "미기재"
    # 결함/취약점/지적사항 → 비적정 (단 '없음/미발견/미비점 없음'은 결함 아님)
    if any(k in v for k in ["부적정", "비적정", "의견변형", "중요한 취약점 존재",
                            "중요한 취약점 발견", "중요한 취약점 2건", "중요한 취약점 3건",
                            "중요한 취약점이 발견", "취약점 2건 발견", "지적사항 있음",
                            "지적사항:", "통제절차 미비로", "미비로 불법"]):
        if not any(k in v for k in ["비적정 사항 미발견", "취약점 없", "취약점이 없", "지적사항 없"]):
            return "비적정"
    if any(k in v for k in ["의견거절", "의견표명 불가", "표명하지 아니", "의견 표명하지",
                            "의견표명하지", "의견을 표명하지"]):
        return "의견거절"
    if "한정" in v:
        return "한정"
    _clean = ["지적사항 없", "취약점 없", "취약점이 없", "이상 없", "이상없",
              "발견되지 아니", "발견되지아니", "효과적", "적정", "준수 운영", "미발견"]
    _absent = ["미기재", "미실시", "미대상", "비대상", "기재 없음", "기재없음", "미확인",
               "미해당", "시행하고 있지 않", "감사/검토 미", "검토 미실시", "이미지",
               "불명확", "대시 표기", "표 상 '-'"]
    if any(k in v for k in _absent) and not any(k in v for k in _clean):
        return "미기재"
    if any(k in v for k in _clean):
        return "적정"
    return "미기재"


def _parsed_path(corp_code: str, rcept_no: str) -> Path:
    return (settings.data_dir / settings.pipeline_version / "parsed"
            / corp_code / f"{rcept_no}.jsonl")


def _load_chunks(corp_code: str, rcept_no: str) -> list[dict]:
    p = _parsed_path(corp_code, rcept_no)
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _is_audit(c: dict) -> bool:
    return (c.get("doc_type") == "audit"
            or any(k in (c.get("section_path") or "") for k in _AUDIT_KEYS))


def _select(chunks: list[dict], max_chunks: int = 30, max_chars: int = 22000) -> list[dict]:
    """감사 관련 + 핵심 주석 청크만 선별."""
    audit = [c for c in chunks if _is_audit(c)]
    notes = [c for c in chunks
             if not _is_audit(c)
             and any(k in (c.get("section_path") or "") for k in _NOTE_KEYS)]
    sel, total = [], 0
    for c in audit + notes:
        t = c.get("text") or ""
        if not t:
            continue
        sel.append(c)
        total += len(t)
        if len(sel) >= max_chunks or total >= max_chars:
            break
    return sel


def _value_krw(value_raw, unit_scale, currency):
    if value_raw is None:
        return None
    if currency and str(currency).upper() not in ("KRW", "원", ""):
        return None  # 외화는 FX 환산을 후속 과제로(여기선 정규값 보류)
    try:
        return float(value_raw) * _SCALE.get(unit_scale or "원", 1)
    except (TypeError, ValueError):
        return None


def _header(r: dict) -> str:
    is_cfs = (r.get("is_consolidated") == "True")
    return (f"기업: {r.get('corp_name')} ({r.get('corp_code')}) / 사업연도 "
            f"{r.get('fiscal_year')} / {'연결(CFS)' if is_cfs else '별도(OFS)'} 기준")


def _build_rows(facts: list[dict], sel: list[dict], r: dict) -> list[dict]:
    """추출 Fact + 선별 청크 → facts 테이블 행(인용 기계검증 포함)."""
    rc, rcept, fy = r.get("corp_code"), r.get("rcept_no"), r.get("fiscal_year")
    is_cfs = (r.get("is_consolidated") == "True")
    built = []
    for fct in facts:
        if not isinstance(fct, dict) or not fct.get("fact_type"):
            continue
        ft = fct.get("fact_type")
        detail = fct.get("detail") if isinstance(fct.get("detail"), dict) else {}
        if ft in OPINION_TYPES and detail.get("opinion"):    # opinion 통제어휘 정규화(원본 보존)
            detail = {**detail, "opinion_raw": detail["opinion"],
                      "opinion": normalize_opinion(detail["opinion"])}
        q = fct.get("evidence_quote") or ""
        nq = _norm(q)
        src = next((c for c in sel if nq and nq in _norm(c.get("text") or "")), None)
        verified = bool(src) and len(nq) >= 6
        vraw = fct.get("value_raw")
        built.append({
            "corp_code": rc,
            "fiscal_year": int(fy) if str(fy).isdigit() else None,
            "fact_type": ft,
            "detail": detail,
            "value_raw": vraw,
            "unit_scale": fct.get("unit_scale"),
            "currency": fct.get("currency"),
            "value_krw": _value_krw(vraw, fct.get("unit_scale"), fct.get("currency")),
            "is_consolidated": is_cfs,
            "evidence_text": q,
            "section_path": (src or {}).get("section_path") or fct.get("section_ref") or "",
            "rcept_no": rcept,
            "dcm_no": r.get("dcm_no"),
            "dart_url": r.get("dart_url"),
            # 인용 기계검증(quote가 근거에 실재)만으로 판정 — 모델 자기신뢰는 배제(지표=기계검증).
            "confidence": "ok" if verified else "review",
            "run_id": settings.pipeline_version,
        })
    return built


def _read_disclosures() -> list[dict]:
    p = settings.meta_dir / "disclosures.csv"
    with p.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _sector_map() -> dict:
    p = settings.meta_dir / "universe.csv"
    m = {}
    if p.exists():
        with p.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                m[r.get("corp_code")] = r.get("krx_sector", "")
    return m


def _filter_reports(limit, sector, corp, latest_only, year=None) -> list[dict]:
    rows = [r for r in _read_disclosures()
            if r.get("period_type") == "연차" and r.get("assurance") == "감사"]
    if year:
        yrs = {str(y) for y in (year if isinstance(year, (list, tuple, set)) else [year])}
        rows = [r for r in rows if str(r.get("fiscal_year")) in yrs]
    if corp:
        rows = [r for r in rows
                if corp in (r.get("corp_code", ""), r.get("corp_name", ""))]
    if sector:
        sm = _sector_map()
        key = sector.replace("업", "")
        rows = [r for r in rows if key in (sm.get(r.get("corp_code"), "") or "")]
    if latest_only:
        best: dict = {}
        for r in rows:
            cc, fy = r.get("corp_code"), int(r.get("fiscal_year") or 0)
            if cc not in best or fy > int(best[cc].get("fiscal_year") or 0):
                best[cc] = r
        rows = list(best.values())
    rows.sort(key=lambda r: (r.get("corp_name", ""), r.get("fiscal_year", "")))
    return rows


def run(limit=None, sector=None, corp=None, latest_only=False, force=False, year=None):
    claude = ClaudeClient()
    pg = PostgresStore()
    if not pg.ping():
        raise SystemExit("PostgreSQL 연결 실패 — 컨테이너(audit-postgres) 확인")
    pg.ensure_ready()

    rows = _filter_reports(limit, sector, corp, latest_only, year)
    done = set() if force else pg.extracted_rcepts()
    todo = [r for r in rows if r.get("rcept_no") not in done]
    if limit:
        todo = todo[:limit]
    log.info("추출 대상 %d개 보고서 (연차감사 전체 %d · 이미완료 %d)",
             len(todo), len(rows), len(done))

    dead = settings.meta_dir / "extract_failures.csv"
    n_facts = ok = err = 0
    for i, r in enumerate(todo, 1):
        rc, rcept = r.get("corp_code"), r.get("rcept_no")
        try:
            sel = _select(_load_chunks(rc, rcept))
            if not sel:
                raise ValueError("선별 섹션 없음(파싱 청크 부재)")
            facts = claude.extract_facts(_header(r), sel)
            built = _build_rows(facts, sel, r)
            pg.replace_facts(rcept, built)
            n_facts += len(built)
            ok += 1
            if i % 25 == 0 or i == len(todo):
                log.info("…%d/%d 보고서 · 누적 Fact %d (ok=%d err=%d)",
                         i, len(todo), n_facts, ok, err)
        except Exception as e:  # noqa: BLE001
            err += 1
            with dead.open("a", encoding="utf-8") as f:
                f.write(f"{rcept},{rc},{type(e).__name__},{str(e)[:120]}\n")
            log.warning("추출 실패 %s/%s: %s", rc, rcept, e)

    log.info("=== 추출 완료: 보고서 ok=%d err=%d · Fact %d건 (PG 누적 %d) ===",
             ok, err, n_facts, pg.count_facts())
    print(f"[extract] ok={ok} err={err} facts_added={n_facts} total={pg.count_facts()}")


def run_batch(limit=None, sector=None, corp=None, latest_only=True, force=False,
              poll_interval: int = 30, year=None):
    """Batch API(50% 할인·비동기): 요청 일괄 제출 → 폴링 → 결과 검증·적재.
    장시간 폴링하므로 분리 프로세스(Start-Process)로 띄우는 것을 권장."""
    claude = ClaudeClient()
    pg = PostgresStore()
    if not pg.ping():
        raise SystemExit("PostgreSQL 연결 실패 — 컨테이너(audit-postgres) 확인")
    pg.ensure_ready()

    rows = _filter_reports(limit, sector, corp, latest_only, year)
    done = set() if force else pg.extracted_rcepts()
    todo = [r for r in rows if r.get("rcept_no") not in done]
    if limit:
        todo = todo[:limit]

    meta = {r["rcept_no"]: r for r in todo}
    requests, no_sel = [], 0
    for r in todo:
        sel = _select(_load_chunks(r["corp_code"], r["rcept_no"]))
        if not sel:
            no_sel += 1
            continue
        requests.append({"custom_id": r["rcept_no"],
                         "params": claude.extract_batch_params(_header(r), sel)})
    log.info("Batch 대상 %d건 (선별없음 %d · 이미완료 %d)", len(requests), no_sel, len(done))
    if not requests:
        print("[extract-batch] 제출할 요청 없음")
        return

    batch = claude.submit_batch(requests)
    bid = batch.id
    (settings.meta_dir / "extract_batch_state.json").write_text(
        json.dumps({"batch_id": bid, "n": len(requests),
                    "submitted": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
        encoding="utf-8")
    log.info("Batch 제출 완료: id=%s (요청 %d) → 폴링 시작", bid, len(requests))

    while True:
        b = claude.retrieve_batch(bid)
        log.info("Batch 상태=%s counts=%s", b.processing_status,
                 getattr(b, "request_counts", None))
        if b.processing_status == "ended":
            break
        time.sleep(poll_interval)

    dead = settings.meta_dir / "extract_failures.csv"
    n_facts = ok = err = 0
    for res in claude.batch_results(bid):
        rcept = res.custom_id
        r = meta.get(rcept) or {"corp_code": None, "rcept_no": rcept}
        try:
            if res.result.type != "succeeded":
                raise ValueError(f"batch result={res.result.type}")
            facts = claude.facts_from_message(res.result.message)
            sel = _select(_load_chunks(r.get("corp_code"), rcept))  # 검증용 재로드
            built = _build_rows(facts, sel, r)
            pg.replace_facts(rcept, built)
            n_facts += len(built)
            ok += 1
        except Exception as e:  # noqa: BLE001
            err += 1
            with dead.open("a", encoding="utf-8") as f:
                f.write(f"{rcept},{r.get('corp_code')},{type(e).__name__},{str(e)[:120]}\n")
            log.warning("결과 처리 실패 %s: %s", rcept, e)
    total = pg.count_facts()
    log.info("=== Batch 적재 완료: ok=%d err=%d · PG 누적 Fact %d ===", ok, err, total)
    print(f"[extract-batch] ok={ok} err={err} total_facts={total}")
