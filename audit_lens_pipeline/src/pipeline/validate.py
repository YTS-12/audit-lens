"""Stage 8: validate — 골든셋으로 질의 엔진 회귀 측정(설계 §품질·운영).

골든셋(eval_queries, reviewed_by='claude-code')은 Claude Code가 Fact Store를
직접 열람해 만든 정답지. 각 질의를 엔진에 돌려 채점한다:
- 스크리닝(exact): recall(놓친 기업)·precision(오탐)
- 단일기업(single): 대상 기업 적중 여부
- 개방형(route): 벡터 경로 + 비어있지 않음
- 공통: 라우팅 정확도 · 인용 기계검증율

⚠️ QueryEngine이 Embedder를 로드하므로 sentence_transformers가 torch보다 먼저 import됨.
"""
from __future__ import annotations
import json
import logging

from config import settings
from src.clients.postgres import PostgresStore
from src.pipeline.query import QueryEngine, _universe_rows

log = logging.getLogger(__name__)


_KIND2MODE = {"screening": "exact", "single": "single", "open": "route"}


def _load_golden(limit=None, kind=None) -> list[dict]:
    pg = PostgresStore()
    with pg.conn.cursor() as cur:
        cur.execute("SELECT query_text, expected FROM eval_queries "
                    "WHERE reviewed_by='claude-code' ORDER BY eval_id")
        rows = cur.fetchall()
    out = []
    for q, exp in rows:
        d = exp if isinstance(exp, dict) else json.loads(exp)
        d["q"] = q
        out.append(d)
    if kind:                                   # 층화: 바뀐 부류만 부분 검증(비용↓)
        m = _KIND2MODE.get(kind, kind)
        out = [d for d in out if d.get("mode") == m or d.get("category") == kind]
    return out[:limit] if limit else out


def run(limit=None, fast=False, kind=None):
    golden = _load_golden(limit, kind)
    log.info("골든셋 %d문항 로드 · 엔진 준비 (fast=%s kind=%s)", len(golden), fast, kind)
    eng = QueryEngine(cache_understand=True)   # understand 캐시 → 반복 검증 비용 0
    name2code = {}
    for r in _universe_rows():
        name2code.setdefault(r.get("corp_name"), r["corp_code"])

    results = []
    for i, g in enumerate(golden, 1):
        # 방향 B(전수 게이트) 정합: 스크리닝 채점은 '전수 능력' 측정이므로
        # UI 빠른조회와 동일하게 전수 힌트로 실행(평시 서빙은 온디맨드 우선 그대로)
        hints = {"exhaustive": True} if g.get("mode") == "exact" else None
        res = eng.run(g["q"], fast=fast, hints=hints)
        items = res.get("items") or []
        found = set()
        for it in items:
            c = it.get("corp_code") or name2code.get(it.get("corp_name"))
            if c:
                found.add(c)
        exp = set(g.get("expected_corps") or [])
        path_ok = res.get("path") == g.get("path")
        e = {"q": g["q"], "category": g["category"], "mode": g["mode"],
             "exp_path": g.get("path"), "got_path": res.get("path"), "path_ok": path_ok,
             "n_exp": len(exp), "n_found": len(found), "n_items": len(items),
             "verified": res.get("verified_count", 0)}
        if g["mode"] == "exact":
            inter = found & exp
            e["recall"] = round(len(inter) / len(exp), 3) if exp else None
            e["precision"] = round(len(inter) / len(found), 3) if found else None
            e["missed"] = len(exp - found)
            e["extra"] = len(found - exp)
        elif g["mode"] == "single":
            e["hit"] = bool(exp & found)
        elif g["mode"] == "route":
            e["route_hit"] = bool(path_ok and len(items) > 0)
        results.append(e)
        log.info("[%d/%d] %s | path %s→%s | exp=%d found=%d",
                 i, len(golden), g["category"], g.get("path"), res.get("path"),
                 len(exp), len(found))

    summary = _aggregate(results)
    summary["mode"] = "fast(no-synth)" if fast else "full"
    out = {"summary": summary, "results": results}
    p = settings.data_dir / settings.pipeline_version / "validation_report.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("리포트 저장: %s", p)
    _print_summary(summary)
    return summary


def _aggregate(results: list[dict]) -> dict:
    scr = [r for r in results if r["mode"] == "exact"]
    sng = [r for r in results if r["mode"] == "single"]
    opn = [r for r in results if r["mode"] == "route"]
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    tot_items = sum(r["n_items"] for r in results)
    tot_ver = sum(r["verified"] for r in results)
    return {
        "n_questions": len(results),
        "routing_accuracy": round(sum(1 for r in results if r["path_ok"]) / len(results), 3),
        "screening": {"n": len(scr),
                      "recall_avg": avg([r.get("recall") for r in scr]),
                      "precision_avg": avg([r.get("precision") for r in scr])},
        "single_company": {"n": len(sng),
                           "hit_rate": round(sum(1 for r in sng if r.get("hit")) / len(sng), 3) if sng else None},
        "open_vector": {"n": len(opn),
                        "route_hit_rate": round(sum(1 for r in opn if r.get("route_hit")) / len(opn), 3) if opn else None},
        "citation_verified_pct": round(100 * tot_ver / tot_items, 1) if tot_items else 0,
        "total_items": tot_items, "total_verified": tot_ver,
    }


def _print_summary(s: dict):
    print("\n" + "=" * 60)
    print(f"[validate] 골든셋 {s['n_questions']}문항")
    print(f"  라우팅 정확도        : {s['routing_accuracy']*100:.1f}%")
    print(f"  스크리닝 recall/prec : {s['screening']['recall_avg']} / {s['screening']['precision_avg']}  (n={s['screening']['n']})")
    print(f"  단일기업 적중률      : {s['single_company']['hit_rate']}  (n={s['single_company']['n']})")
    print(f"  개방형 경로적중       : {s['open_vector']['route_hit_rate']}  (n={s['open_vector']['n']})")
    print(f"  인용 기계검증율      : {s['citation_verified_pct']}%  ({s['total_verified']}/{s['total_items']})")
    if str(s.get("mode", "")).startswith("fast"):
        print("  ※ fast(합성 생략): 개방형 인용검증율은 무의미 — 라우팅·recall만 유효")
    print("=" * 60)
