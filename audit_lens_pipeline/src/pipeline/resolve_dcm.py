"""Stage 2.5: resolve-dcm — disclosures.csv의 빈 dcm_no를 채운다 (재개가능).

DART 뷰어 스크레이핑(스로틀+백오프 내장)으로 보고서별 '연결'(없으면 별도) 문서
dcmNo를 해소해 딥링크를 완성한다. discover를 --no-resolve-dcm로 빠르게 돌린 뒤,
또는 뷰어 차단으로 일부가 비었을 때 이 단계로 재시도한다(이미 채운 행은 건너뜀).
전량(코스피 전 종목) 확장 시 discover와 분리해 차단 위험을 줄이는 핵심 단계.
"""
from __future__ import annotations
import csv
from config import settings
from src.clients.opendart import OpenDartClient


def run(force: bool = False):
    settings.ensure_dirs()
    path = settings.meta_dir / "disclosures.csv"
    if not path.exists():
        print("[resolve-dcm] disclosures.csv 없음 — 먼저 discover 실행"); return
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not rows:
        print("[resolve-dcm] disclosures.csv 비어있음"); return

    # is_consolidated 컬럼 보장(구버전 CSV 호환)
    fields = list(rows[0].keys())
    if "is_consolidated" not in fields:
        idx = fields.index("fs_basis") if "fs_basis" in fields else len(fields)
        fields.insert(idx, "is_consolidated")
    for r in rows:
        r.setdefault("is_consolidated", "")

    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout, max_retry=settings.http_max_retry)

    todo = [r for r in rows if force or not r.get("dcm_no")]
    print(f"[resolve-dcm] 대상 {len(todo)}/{len(rows)}건 "
          f"({'전체 재해소' if force else '빈 dcm만'})")

    cons = sep = fail = 0
    for i, r in enumerate(todo, 1):
        dcm, is_cons, _ = dart.resolve_report_dcm(r["rcept_no"], r.get("assurance", "감사"))
        if dcm:
            r["dcm_no"] = dcm
            r["is_consolidated"] = is_cons
            r["fs_basis"] = "CFS" if is_cons else "OFS"
            r["dart_url"] = dart.deep_link(r["rcept_no"], dcm)
            cons += int(bool(is_cons)); sep += int(not is_cons)
        else:
            fail += 1
        if i % 50 == 0:
            print(f"  …{i}/{len(todo)} (연결={cons}, 별도={sep}, 미해소={fail})")

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)

    filled = sum(1 for r in rows if r.get("dcm_no"))
    print(f"[resolve-dcm] 완료: 연결={cons} 별도={sep} 미해소={fail} | "
          f"전체 dcm 채움 {filled}/{len(rows)} → {path}")
    return cons, sep, fail
