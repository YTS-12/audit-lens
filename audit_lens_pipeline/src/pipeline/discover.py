"""Stage 2: discover — 대상 보고서 rcept_no 수집.

- 최근 3개 사업연도 '사업보고서'(연차 감사)
- 가장 최근 1개 '반기/분기보고서'(검토)
예외(3년 미만 신규상장 등)는 '보유한 연도만' 자동 수집된다.
"""
from __future__ import annotations
import csv
import re
import random
from collections import defaultdict
from datetime import date
from config import settings
from src.clients.opendart import OpenDartClient

YEAR_RE = re.compile(r"\((\d{4})\.\d{2}\)")  # "사업보고서 (2024.12)"


def _classify(report_nm: str):
    nm = report_nm.replace(" ", "")
    if "사업보고서" in nm:
        return "연차", "감사"
    if "반기보고서" in nm:
        return "반기", "검토"
    if "분기보고서" in nm:
        return "분기", "검토"
    return None, None


def _stratified(companies: list[dict], n: int) -> list[dict]:
    """krx_sector로 층화해 N사를 고르게 뽑는다(섹터 라운드로빈, 고정 시드=재현성).
    26개 업종에 최대한 분산 → 포맷 다양성(금융·지주·외화 등) 확보."""
    buckets: dict = defaultdict(list)
    for c in companies:
        buckets[c.get("krx_sector") or "(공란)"].append(c)
    rng = random.Random(20260624)           # 고정 시드(재현성)
    for v in buckets.values():
        rng.shuffle(v)
    order = sorted(buckets)
    pools = {s: iter(buckets[s]) for s in order}
    picked: list = []
    while len(picked) < n and pools:
        for s in order:
            if s not in pools:
                continue
            try:
                picked.append(next(pools[s]))
            except StopIteration:
                del pools[s]
            if len(picked) >= n:
                break
    return picked


def run(resolve_dcm: bool = True, limit: int | None = None,
        sector: str | None = None, sample: int | None = None):
    settings.ensure_dirs()
    uni = settings.meta_dir / "universe.csv"
    companies = list(csv.DictReader(uni.open(encoding="utf-8-sig")))
    if sector:                              # 특정 업종만
        companies = [c for c in companies if c.get("krx_sector") == sector]
        print(f"[discover] 업종 '{sector}' 필터 → {len(companies)}사")
    if sample:                              # 업종 층화 표본 N사(설계용)
        companies = _stratified(companies, sample)
        from collections import Counter as _C
        dist = _C(c.get("krx_sector") or "(공란)" for c in companies)
        print(f"[discover] 층화 표본 {len(companies)}사 · {len(dist)}개 업종 분산")
    elif limit:                             # 앞에서 N사만
        companies = companies[:limit]
    print(f"[discover] 대상 {len(companies)}사 (연결 감사보고서 기준)")

    dart = OpenDartClient(settings.opendart_api_key,
                          rate_per_sec=settings.opendart_rate_per_sec,
                          timeout=settings.http_timeout,
                          max_retry=settings.http_max_retry)

    today = date.fromisoformat(settings.as_of_date)
    bgn = f"{today.year - settings.years_back - 1}0101"  # 여유 1년
    end = today.strftime("%Y%m%d")

    out_rows = []
    for i, c in enumerate(companies, 1):
        annuals, reviews = [], []
        for item in dart.list_disclosures(c["corp_code"], bgn, end, pblntf_ty="A"):
            period, assurance = _classify(item.get("report_nm", ""))
            if not period:
                continue
            m = YEAR_RE.search(item.get("report_nm", ""))
            fy = int(m.group(1)) if m else None
            row = {
                "rcept_no": item["rcept_no"], "corp_code": c["corp_code"],
                "corp_name": c["corp_name"], "report_nm": item["report_nm"],
                "fiscal_year": fy, "period_type": period, "assurance": assurance,
                "rcept_dt": item.get("rcept_dt", ""),
                "dcm_no": "",            # 연결(없으면 별도) 문서 번호(아래 해소)
                "is_consolidated": "",   # 연결(True)/별도(False) — 해소 시 채움
                "fs_basis": "CFS",       # 수집 기준(연결 우선)
                "dart_url": dart.deep_link(item["rcept_no"]),
            }
            (annuals if period == "연차" else reviews).append(row)

        # 사업연도별 1건만 — 같은 FY에 원본·정정이 섞이면 정리한다.
        # [첨부정정]은 document.xml이 없으므로(014) 제외하고, 그 외 최신(rcept_no)을 택한다.
        def _pick(cands):
            primary = [v for v in cands
                       if "[첨부정정]" not in v["report_nm"].replace(" ", "")]
            return max(primary or cands, key=lambda r: r["rcept_no"])

        by_year: dict = {}
        for r in annuals:
            if r["fiscal_year"] is not None:
                by_year.setdefault(r["fiscal_year"], []).append(r)
        keep = [_pick(by_year[fy])
                for fy in sorted(by_year, reverse=True)[:settings.years_back]]
        # 가장 최근 분/반기 1건(역시 [첨부정정] 회피)
        if reviews:
            keep.append(_pick(reviews))

        # 보고서 유형별 '연결'(없으면 별도) 문서 dcmNo 해소 → 딥링크 완성
        if resolve_dcm:
            for r in keep:
                dcm, is_cons, _ = dart.resolve_report_dcm(r["rcept_no"], r["assurance"])
                if dcm:
                    r["dcm_no"] = dcm
                    r["is_consolidated"] = is_cons
                    r["fs_basis"] = "CFS" if is_cons else "OFS"
                    r["dart_url"] = dart.deep_link(r["rcept_no"], dcm)

        out_rows.extend(keep)
        if i % 100 == 0:
            print(f"  …{i}/{len(companies)}사 처리")

    out = settings.meta_dir / "disclosures.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"[discover] 저장 {out} · 보고서 {len(out_rows)}건")
    return out
