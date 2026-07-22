"""Stage 3: fetch — 감사/검토보고서 원본파일 다운로드 (데이터 우선의 핵심).

멱등: 이미 받은 ZIP은 건너뛴다. 레이트리밋/재시도는 클라이언트가 처리.
"""
from __future__ import annotations
import csv
from config import settings
from src.clients.opendart import OpenDartClient


def run(limit: int | None = None):
    settings.ensure_dirs()
    disc = settings.meta_dir / "disclosures.csv"
    rows = list(csv.DictReader(disc.open(encoding="utf-8-sig")))
    if limit:
        rows = rows[:limit]
    print(f"[fetch] 대상 보고서 {len(rows)}건")

    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout,
                          max_retry=settings.http_max_retry)

    ok, failures = 0, []
    for i, r in enumerate(rows, 1):
        dest = settings.raw_dir / r["corp_code"]
        try:
            dart.download_document(r["rcept_no"], dest)
            ok += 1
        except Exception as e:               # noqa
            failures.append({"rcept_no": r["rcept_no"], "corp_name": r.get("corp_name", ""),
                             "report_nm": r.get("report_nm", ""), "reason": str(e)[:200]})
            print(f"  [fail] {r['rcept_no']} {e}")
        if i % 200 == 0:
            print(f"  …{i}/{len(rows)} (ok={ok}, fail={len(failures)})")

    if failures:                             # Dead-letter: 수동 검수 대상
        fp = settings.meta_dir / "fetch_failures.csv"
        with fp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(failures[0].keys()))
            w.writeheader(); w.writerows(failures)
        print(f"[fetch] 실패 {len(failures)}건 → {fp} (Dead-letter)")
    print(f"[fetch] 완료 ok={ok}, fail={len(failures)} → {settings.raw_dir}")
    return ok, len(failures)
