"""Stage 4 (Phase 0): analyze — 약 500 표본 실분석 → 설계 입력 산출.

산출물
- embedding_spec.json     : 단위 표기 패턴, 섹션/표 구조, 엣지케이스 → 파서·임베딩 입력
- industry_validation.json: KRX 업종 vs DART 업종 분포·표본 대조 → 산업 라벨 기준 결정

원칙: 임베딩은 추측이 아니라 이 분석 결과로 설계한다(설계서 §1.4·§3.5).
"""
from __future__ import annotations
import csv
import io
import json
import random
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from lxml import etree
from config import settings

UNIT_RE = re.compile(r"\(\s*단위\s*[:：]\s*([^\)]{1,20})\)")
NEG_PATTERNS = {"(123)": r"\(\d[\d,]*\)", "△": r"△\d", "-": r"-\d"}
SECTION_HINTS = ["감사의견", "핵심감사사항", "계속기업", "재무제표에 대한 주석",
                 "유형자산", "리스", "수익", "특수관계자"]


def _extract_text(zip_path: Path) -> str:
    try:
        zf = zipfile.ZipFile(zip_path)
        xmls = [n for n in zf.namelist() if n.lower().endswith((".xml", ".html"))]
        if not xmls:
            return ""
        raw = zf.read(max(xmls, key=lambda n: zf.getinfo(n).file_size))
        parser = etree.XMLParser(recover=True, huge_tree=True)
        root = etree.fromstring(raw, parser=parser)
        return " ".join(root.itertext())
    except Exception:
        return ""


def _stratified_sample(disclosures, sectors_by_corp, n):
    annual = [d for d in disclosures if d["period_type"] == "연차"]
    buckets = defaultdict(list)
    for d in annual:
        buckets[sectors_by_corp.get(d["corp_code"], "미상")].append(d)
    if not annual:
        return []
    per = max(1, n // max(1, len(buckets)))
    sample = []
    for sec, items in buckets.items():
        random.shuffle(items)
        sample.extend(items[:per])
    random.shuffle(sample)
    return sample[:n]


def run(deep: int = 0):
    settings.ensure_dirs()
    uni = list(csv.DictReader((settings.meta_dir / "universe.csv").open(encoding="utf-8-sig")))
    disc = list(csv.DictReader((settings.meta_dir / "disclosures.csv").open(encoding="utf-8-sig")))
    sectors = {c["corp_code"]: c.get("krx_sector", "미상") for c in uni}

    sample = _stratified_sample(disc, sectors, settings.phase0_sample_size)
    print(f"[phase0] 표본 {len(sample)}건 분석 시작")

    unit_counter, neg_counter = Counter(), Counter()
    section_counter = Counter()
    read_ok, read_fail = 0, 0
    edge = []

    for d in sample:
        zp = settings.raw_dir / d["corp_code"] / f"{d['rcept_no']}.zip"
        if not zp.exists():
            continue
        text = _extract_text(zp)
        if not text:
            read_fail += 1
            edge.append({"rcept_no": d["rcept_no"], "issue": "텍스트 추출 실패(이미지표 의심→비전 폴백 대상)"})
            continue
        read_ok += 1
        for u in UNIT_RE.findall(text):
            unit_counter[u.strip()] += 1
        for label, pat in NEG_PATTERNS.items():
            if re.search(pat, text):
                neg_counter[label] += 1
        for h in SECTION_HINTS:
            if h in text:
                section_counter[h] += 1

    total_read = read_ok + read_fail
    parse_fail_rate = round(read_fail / total_read, 4) if total_read else None

    embedding_spec = {
        "sample_size": len(sample),
        "read_ok": read_ok, "read_fail": read_fail,
        "parse_failure_rate": parse_fail_rate,        # 품질지표
        "unit_patterns": unit_counter.most_common(),  # 예: [("백만원", 412), ...]
        "negative_number_styles": neg_counter.most_common(),
        "section_coverage": section_counter.most_common(),
        "edge_cases_sample": edge[:30],
        "recommendation": {
            "unit_strategy": "XBRL unitRef 우선 + 위 표기패턴 정규식으로 표 단위 전파",
            "vision_fallback_if": "텍스트 추출 실패 또는 표 셀 0 → 페이지 이미지화 후 비전 분석",
            "chunking": "섹션/주석 경계 우선 + 단위·통화 컨텍스트 헤더 주입",
        },
    }
    (settings.phase0_dir / "embedding_spec.json").write_text(
        json.dumps(embedding_spec, ensure_ascii=False, indent=2), encoding="utf-8")

    # 산업 분류 검증(KRX 업종 분포; induty_code는 enrich 시 채워짐)
    krx_dist = Counter(c.get("krx_sector", "미상") for c in uni)
    induty_filled = sum(1 for c in uni if c.get("induty_code"))
    industry_validation = {
        "krx_sector_distribution": krx_dist.most_common(),
        "n_companies": len(uni),
        "induty_code_filled": induty_filled,
        "note": "induty_code가 비어있으면 build_universe --enrich 로 채운 뒤 재실행. "
                "표본의 '사업의 내용'과 KRX·DART 업종을 대조해 라벨 기준을 결정한다.",
        "todo": "KRX vs DART 불일치율 산출 + 지주회사 등 예외목록 작성(개발자+Claude Code 공동 검토)",
    }
    (settings.phase0_dir / "industry_validation.json").write_text(
        json.dumps(industry_validation, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[phase0] parse_failure_rate={parse_fail_rate} · 단위패턴 {len(unit_counter)}종")
    print(f"[phase0] 산출물 → {settings.phase0_dir}")

    if deep:
        _deep_with_claude(sample[:deep])
    return embedding_spec


def _deep_with_claude(sample):
    """소수 표본을 Claude로 심층 분석해 파서 규칙 초안을 받는다(설계 수정 입력)."""
    if not settings.anthropic_api_key:
        print("[phase0] ANTHROPIC_API_KEY 없음 → deep 분석 건너뜀")
        return
    from anthropic import Anthropic
    client = Anthropic(api_key=settings.anthropic_api_key)
    notes = []
    for d in sample:
        zp = settings.raw_dir / d["corp_code"] / f"{d['rcept_no']}.zip"
        text = _extract_text(zp)[:120_000]
        if not text:
            continue
        msg = client.messages.create(
            model=settings.claude_model_workhorse, max_tokens=1500,
            messages=[{"role": "user", "content":
                "다음은 감사보고서 원문 텍스트다. (1)단위·통화 표기 위치와 형식, "
                "(2)재무제표 표 구조, (3)주석 번호 체계, (4)임베딩 시 주의할 엣지케이스를 "
                f"JSON으로 요약하라.\n\n{text}"}])
        notes.append({"rcept_no": d["rcept_no"],
                      "analysis": msg.content[0].text if msg.content else ""})
    (settings.phase0_dir / "deep_format_notes.json").write_text(
        json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[phase0] Claude 심층분석 {len(notes)}건 → deep_format_notes.json")
