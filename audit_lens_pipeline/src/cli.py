"""감사렌즈 파이프라인 CLI (수동 트리거).

데이터 우선 권장 순서:
  python -m src.cli universe --enrich     # 코스피 유니버스(+업종코드)
  python -m src.cli discover              # 최근 3년 감사 + 최근 분/반기 검토
  python -m src.cli fetch                 # 원본 보고서 최우선 수집
  python -m src.cli phase0 --deep 5       # 약 500 표본 분석(+Claude 심층 5건)
  # ↑ 여기까지 결과로 설계 확정 후 ↓
  python -m src.cli parse / embed / extract / validate   # (구현 예정)
"""
from __future__ import annotations
import argparse
from config import settings


def _ensure_selfsigned(cert: str, key: str) -> None:
    """자기서명 인증서가 없으면 생성(openssl) — 전송 암호화용(브라우저 1회 경고 수락)."""
    import os
    import subprocess
    if os.path.exists(cert) and os.path.exists(key):
        return
    d = os.path.dirname(cert)
    if d:
        os.makedirs(d, exist_ok=True)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert, "-days", "825",
                    "-subj", "/CN=audit-lens"], check=True)
    print(f"[serve] 자기서명 인증서 생성: {cert}")


def main():
    p = argparse.ArgumentParser(prog="audit-lens")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("universe", help="코스피 유니버스 스냅샷")
    s.add_argument("--enrich", action="store_true", help="기업개황으로 업종코드·결산월 보강")

    s = sub.add_parser("discover", help="대상 보고서 rcept_no 수집(연결 기준 딥링크)")
    s.add_argument("--no-resolve-dcm", action="store_true",
                   help="연결 감사보고서 dcmNo 해소 생략(빠름, 딥링크는 rcpNo만)")
    s.add_argument("--limit", type=int, default=None, help="앞에서 N사만(파일럿)")
    s.add_argument("--sector", default=None, help="krx_sector 필터(예: 건설)")
    s.add_argument("--sample", type=int, default=None,
                   help="업종 층화 표본 N사(설계용, 26업종 분산)")

    s = sub.add_parser("resolve-dcm", help="빈 dcm_no 재해소(재개가능, 뷰어 스로틀)")
    s.add_argument("--force", action="store_true", help="이미 채운 dcm도 재해소")

    s = sub.add_parser("fetch", help="원본 보고서 다운로드")
    s.add_argument("--limit", type=int, default=None)

    s = sub.add_parser("phase0", help="약 500 표본 실분석")
    s.add_argument("--deep", type=int, default=0, help="Claude 심층분석 표본 수")

    s = sub.add_parser("parse", help="연결 기준 섹션-인지 청크 생성")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--corp", default=None, help="특정 corp_code/기업명만(테스트)")

    s = sub.add_parser("embed", help="청크 → BGE-M3(GPU) → OpenSearch 하이브리드 색인")
    s.add_argument("--limit", type=int, default=None, help="청크 N개만(테스트)")
    s.add_argument("--sector", default=None, help="krx_sector 필터(예: 건설)")
    s.add_argument("--corp", default=None, help="특정 corp_code만")
    s.add_argument("--recreate", action="store_true", help="인덱스 재생성")
    s.add_argument("--missing-only", action="store_true",
                   help="top-off: 이미 색인된 청크는 건너뛰고 빠진 것만")

    s = sub.add_parser("query", help="질의→하이브리드 검색→Claude 합성→인용 검증")
    s.add_argument("question", help="자연어 질문")
    s.add_argument("-k", type=int, default=12, help="근거 청크 수")

    s = sub.add_parser("serve", help="웹 UI + API 서버 (FastAPI)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    s = sub.add_parser("extract", help="감사보고서·주석 → Fact 12종 → PostgreSQL(스크리닝용)")
    s.add_argument("--limit", type=int, default=None, help="보고서 N개만(테스트)")
    s.add_argument("--sector", default=None, help="krx_sector 필터(예: 건설)")
    s.add_argument("--corp", default=None, help="특정 corp_code/기업명만")
    s.add_argument("--latest-only", action="store_true", help="기업별 최신 사업연도만")
    s.add_argument("--year", type=int, default=None,
                   help="특정 사업연도만(예: 2024). 과거연도 백필용 — 지정 시 latest-only 미강제")
    s.add_argument("--force", action="store_true", help="이미 추출한 보고서도 재추출")
    s.add_argument("--batch", action="store_true",
                   help="Anthropic Batch API로 일괄 추출(50%% 할인·비동기)")

    s = sub.add_parser("graph-parties",
                       help="특수관계자_거래 Fact → 상대방 지식그래프(엣지) 구축(§5.8 확장)")
    s.add_argument("--limit", type=int, default=None, help="Fact N건만(테스트)")
    s.add_argument("--force", action="store_true", help="이미 구축된 것도 전체 재구축")

    s = sub.add_parser("incremental",
                       help="무인 증분 파이프라인: 신규/정정 공시 → fetch→parse→embed→Fact (AWS 크론용)")
    s.add_argument("--days", type=int, default=7, help="며칠 전까지 조회(기본 7 — 겹침 안전)")
    s.add_argument("--dry-run", action="store_true", help="신규 목록만 출력(다운로드·색인 안 함)")
    s.add_argument("--skip-llm", action="store_true", help="LLM 추출·XBRL 생략(표 Fact·색인만)")

    s = sub.add_parser("extract-tables",
                       help="표 기반 Fact(감사_투입시간·비감사용역_계약) 규칙 추출(무API)")
    s.add_argument("--corp", default=None, help="특정 기업만(corp_code)")

    s = sub.add_parser("sync-tables",
                       help="색인↔표 스토어 정합 동기화(전면 교체 직후 필수): 표없음 추출 · 색인없음 정리")
    s.add_argument("--dry-run", action="store_true", help="차집합만 출력")
    s.add_argument("--keep-stale", action="store_true", help="색인 없는 표를 지우지 않음")

    s = sub.add_parser("validate", help="골든셋으로 질의 엔진 회귀 측정")
    s.add_argument("--limit", type=int, default=None, help="앞 N문항만(테스트)")
    s.add_argument("--fast", action="store_true",
                   help="합성(Sonnet) 생략·understand 캐시 → 라우팅/recall만 저렴하게 회귀")
    s.add_argument("--kind", default=None,
                   help="부류만 검증: screening|single|open (층화 부분검증)")

    s = sub.add_parser("evalcand",
                       help="반려(👎)→골든셋 후보 파이프라인: 수확·목록·상태변경(하네스)")
    s.add_argument("action", choices=["harvest", "list", "promote", "reject"],
                   help="harvest=반려 집계 적재 · list=후보 목록 · promote/reject=상태변경")
    s.add_argument("--id", type=int, default=None, help="promote/reject 대상 후보 id")
    s.add_argument("--min", type=int, default=1, help="harvest 최소 반려 수(기본 1)")
    s.add_argument("--status", default="new", help="list 필터: new|promoted|rejected|all")
    s.add_argument("--note", default="", help="promote/reject 메모")

    args = p.parse_args()
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    print(f"[run] version={settings.pipeline_version} as_of={settings.as_of_date} cmd={args.cmd}")

    if args.cmd == "universe":
        from src.pipeline import build_universe
        build_universe.run(enrich=args.enrich)
    elif args.cmd == "discover":
        from src.pipeline import discover
        discover.run(resolve_dcm=not args.no_resolve_dcm,
                     limit=args.limit, sector=args.sector, sample=args.sample)
    elif args.cmd == "resolve-dcm":
        from src.pipeline import resolve_dcm
        resolve_dcm.run(force=args.force)
    elif args.cmd == "fetch":
        from src.pipeline import fetch
        fetch.run(limit=args.limit)
    elif args.cmd == "phase0":
        from src.pipeline import phase0_analyze
        phase0_analyze.run(deep=args.deep)
    elif args.cmd == "parse":
        from src.pipeline import parse
        parse.run(limit=args.limit, corp=args.corp)
    elif args.cmd == "embed":
        from src.pipeline import embed
        embed.run(limit=args.limit, sector=args.sector, corp=args.corp,
                  recreate=args.recreate, missing_only=args.missing_only)
    elif args.cmd == "extract":
        from src.pipeline import extract
        if args.batch:
            extract.run_batch(limit=args.limit, sector=args.sector, corp=args.corp,
                              latest_only=args.latest_only or not (args.corp or args.sector or args.year),
                              force=args.force, year=args.year)
        else:
            extract.run(limit=args.limit, sector=args.sector, corp=args.corp,
                        latest_only=args.latest_only, force=args.force, year=args.year)
    elif args.cmd == "query":
        from src.pipeline import query
        query.cli_run(args.question, k=args.k)
    elif args.cmd == "graph-parties":
        from src.pipeline import graph_parties
        graph_parties.run(force=args.force, limit=args.limit)
    elif args.cmd == "incremental":
        from src.pipeline import incremental
        incremental.run(days_back=args.days, dry_run=args.dry_run, skip_llm=args.skip_llm)
    elif args.cmd == "extract-tables":
        from src.pipeline import extract_tables
        extract_tables.run(corps={args.corp} if args.corp else None)
    elif args.cmd == "sync-tables":
        from src.pipeline import sync_tables
        sync_tables.run(prune=not args.keep_stale, dry_run=args.dry_run)
    elif args.cmd == "validate":
        from src.pipeline import validate
        validate.run(limit=args.limit, fast=args.fast, kind=args.kind)
    elif args.cmd == "evalcand":
        from src.clients.postgres import PostgresStore
        pg = PostgresStore()
        if args.action == "harvest":
            n = pg.harvest_downvotes(min_downvotes=args.min)
            print(f"[evalcand] 반려 수확: {n}건 신규/갱신 (status='new')")
            print("  → 'evalcand list'로 검토 후 'evalcand promote --id N'")
        elif args.action == "list":
            rows = pg.list_eval_candidates(status=args.status)
            print(f"[evalcand] status={args.status} · {len(rows)}건")
            for r in rows:
                print(f"  #{r['id']} [{r['status']}] 👎{r['down_count']} | {r['question'][:60]}"
                      + (f"  (예: {r['sample_corp']})" if r['sample_corp'] else ""))
        elif args.action in ("promote", "reject"):
            if not args.id:
                print("  --id 필요 (evalcand list로 id 확인)"); return
            pg.set_eval_candidate_status(args.id, "promoted" if args.action == "promote"
                                         else "rejected", note=args.note)
            print(f"[evalcand] #{args.id} → {args.action}. "
                  + ("골든셋 편입은 정답집합 확정 후 eval_queries INSERT."
                     if args.action == "promote" else "기각 완료."))
    elif args.cmd == "serve":
        import uvicorn
        ssl_kw = {}
        cert, key = settings.ssl_certfile, settings.ssl_keyfile
        if cert and key:                            # TLS(자기서명): HTTPS로 서빙
            _ensure_selfsigned(cert, key)
            ssl_kw = {"ssl_certfile": cert, "ssl_keyfile": key}
            print(f"[serve] HTTPS(TLS) 활성 — https://{args.host}:{args.port}")
        uvicorn.run("src.api.server:app", host=args.host, port=args.port,
                    log_level="info", **ssl_kw)
    else:
        print(f"[todo] '{args.cmd}' 단계는 Phase 0 결과 확정 후 구현합니다 "
              "(파서 규칙·BGE-M3 임베딩·Fact 추출·골든셋 검증).")


if __name__ == "__main__":
    main()
