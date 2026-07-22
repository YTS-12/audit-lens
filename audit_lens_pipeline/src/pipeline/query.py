"""Stage 7: query — 질의이해 → 멀티쿼리(RAG-Fusion) → 하이브리드 검색
→ Claude 합성 → 인용 기계검증 → 근거 있는 답.

3경로 중 현재 **벡터/하이브리드 경로** 가동(Fact Store·온디맨드는 extract 구현 후 추가).
인용 검증: 합성이 인용한 quote가 실제 근거 청크 텍스트에 글자 단위로 존재하는지 대조,
실패하면 verified=false(환각 차단). 설계서 §5.4·§5.7.

⚠️ embed.Embedder를 import하므로 sentence_transformers가 torch보다 먼저 로드된다.
"""
from __future__ import annotations
import csv
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from config import settings
from src.clients.opensearch_store import OpenSearchStore
from src.clients.claude import ClaudeClient
from src.clients.ontology import Ontology
from src.pipeline.embed import Embedder, Reranker   # ST를 torch보다 먼저 import (segfault 회피)

log = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


# 완전성(전수) 신호 — 있으면 Fact Store 전수 스크리닝, 없으면 온디맨드(방향 B: 온디맨드 우선)
_EXHAUSTIVE_MARKERS = ("전부", "모두", "모든", "전체", "명단", "리스트", "몇 개", "몇개",
                       "개수", "빠짐없", "전 종목", "전종목", "다 알려", "다 정리", "각각",
                       "총 몇", "얼마나 많", "전 기업", "전기업")


def _is_exhaustive(question: str) -> bool:
    return bool(question) and any(m in question for m in _EXHAUSTIVE_MARKERS)


def _universe_rows():
    return list(csv.DictReader((settings.meta_dir / "universe.csv").open(encoding="utf-8-sig")))


def _build_filters(u: dict) -> dict:
    """질의이해 결과 → OpenSearch 메타 필터. 산업/기업은 corp_code로 환원."""
    f: dict = {}
    if u.get("is_consolidated") is not None:
        f["is_consolidated"] = bool(u["is_consolidated"])
    if u.get("fiscal_years"):
        f["fiscal_year"] = [int(y) for y in u["fiscal_years"] if str(y).isdigit()]
    if u.get("doc_types"):
        f["doc_type"] = u["doc_types"]
    # 보고서 유형: 검토(분기/반기)를 명시한 질의는 검토보고서 청크로 한정(설계서 §2.0 감사/검토 구분)
    if u.get("assurance") in ("감사", "검토"):
        f["assurance"] = u["assurance"]
    if u.get("report_period") in ("연차", "분기", "반기"):
        f["period_type"] = u["report_period"]
    wc = u.get("_wics_corps")              # WICS(UI) 명시 스코프 — 유사도 질의에도 적용
    if u.get("_similarity"):               # 유사도 질의: 회사/산업 필터 제거 → 순수 의미검색(참조기업은 검색어에 포함)
        if wc:
            f["corp_code"] = list(wc)
        return f
    rows = None
    if u.get("company"):
        rows = _universe_rows()
        codes = [r["corp_code"] for r in rows if u["company"] in (r.get("corp_name") or "")]
        if codes:
            f["corp_code"] = codes
    elif u.get("industry"):
        rows = _universe_rows()
        ind = u["industry"].replace("업", "").strip()        # "건설업"→"건설"
        codes = [r["corp_code"] for r in rows
                 if ind and (ind in (r.get("krx_sector") or "")
                             or (r.get("krx_sector") or "") in ind)]
        if codes:
            f["corp_code"] = codes
    if wc:                                 # WICS ∩ (회사/산업). 명시 선택이므로 교집합 적용.
        base = f.get("corp_code")
        f["corp_code"] = [c for c in base if c in wc] if base else list(wc)
    return f


def _corp_codes(u: dict) -> list | None:
    """산업/기업 → corp_code 목록(없으면 None=전체). WICS(UI 선택)가 있으면 교집합.
    업종은 온톨로지 정확 sector 우선(substring 탈피)."""
    wc = u.get("_wics_corps")
    def _ret(codes):                           # WICS 교집합 적용
        if wc is None:
            return codes or None
        if codes is None:
            return list(wc) or None
        return [c for c in codes if c in wc] or None
    if u.get("company"):
        rows = _universe_rows()
        return _ret([r["corp_code"] for r in rows if u["company"] in (r.get("corp_name") or "")])
    sec = u.get("_sector")
    if sec:                                    # 온톨로지 정규화 sector → 정확 일치
        rows = _universe_rows()
        return _ret([r["corp_code"] for r in rows if (r.get("krx_sector") or "") == sec])
    if u.get("industry"):                      # 폴백: substring
        rows = _universe_rows()
        ind = u["industry"].replace("업", "").strip()
        return _ret([r["corp_code"] for r in rows if ind and ind in (r.get("krx_sector") or "")])
    return _ret(None)


_FACT_LABEL = {
    "감사의견_유형": lambda d: f"감사의견 {d.get('opinion','')}".strip(),
    "핵심감사사항": lambda d: (d.get("topic") or d.get("summary") or "")[:120],
    "계속기업_불확실성": lambda d: (f"계속기업 불확실성 {d.get('basis','')}".strip()
                              + (f" [{'·'.join(d['사유분류'])}]" if d.get('사유분류') else "")),
    "감가상각_변경": lambda d: f"감가상각 {d.get('kind','')} 변경 {d.get('asset','')} {d.get('from','')}→{d.get('to','')}".strip(),
    "회계정책_변경": lambda d: f"회계정책 변경: {d.get('topic','')}".strip(),
    "회계추정_변경": lambda d: f"회계추정 변경: {d.get('topic','')}".strip(),
    "소송_우발부채": lambda d: (d.get("description") or "")[:120],
    "특수관계자_거래": lambda d: (d.get("description") or "")[:120],
    "재고자산_평가방법": lambda d: f"재고 평가방법 {d.get('method','')}".strip(),
    "수익인식_정책": lambda d: f"수익인식 {d.get('method','')}".strip(),
    "감사인_보수": lambda d: f"감사인 {d.get('auditor','')}".strip(),
    "내부회계관리제도_검토의견": lambda d: f"내부회계 {d.get('opinion','')}".strip(),
    "감사_투입시간": lambda d: (f"감사 투입 {d.get('실제시간') or d.get('계약시간'):,}시간"
                            f" ({d.get('auditor','')}, 보수 {d.get('실제보수') or d.get('계약보수') or '-'})"
                            if (d.get('실제시간') or d.get('계약시간')) else "감사 투입시간"),
    "비감사용역_계약": lambda d: (f"비감사용역: {d.get('용역내용','')}"
                             + (f" (보수 {d.get('용역보수')})" if d.get('용역보수') else "")).strip(),
    "감사인_변경사유": lambda d: (f"감사인 변경 — {d.get('사유분류','')}"
                             + (f" ({d.get('전임','?')}→{d.get('후임','?')})"
                                if d.get('전임') or d.get('후임') else "")).strip(),
    "감사보고서_강조사항": lambda d: (f"강조사항[{'·'.join(d.get('주제') or [])}]: "
                                 + (d.get('요지') or '')[:100]).strip(),
    "전기오류_수정": lambda d: f"전기오류수정(재무제표 재작성) — 신호: {'·'.join(d.get('신호') or [])}",
    "정정공시_이력": lambda d: f"정정공시 — {d.get('보고서','')} (접수 {d.get('정정접수일','')})".strip(),
}


def _fact_conclusion(ft: str, d: dict) -> str:
    d = d or {}
    fn = _FACT_LABEL.get(ft)
    if fn:
        return fn(d) or ft
    return " · ".join(f"{k}:{v}" for k, v in d.items())[:120] or ft


_RATIO_METRICS = {"부채비율", "영업이익률", "순이익률", "ROE", "ROA",
                  "유동비율", "매출총이익률", "판관비율"}


def _fmt_won(v) -> str:
    """원 단위 금액 → 조/억 표기."""
    if v is None:
        return "-"
    v = float(v); a = abs(v)
    if a >= 1e12:
        return f"{v/1e12:.2f}조원"
    if a >= 1e8:
        return f"{v/1e8:.1f}억원"
    return f"{v:,.0f}원"


def _fmt_metric(metric: str, v) -> str:
    if v is None:
        return "-"
    return f"{float(v):.1f}%" if metric in _RATIO_METRICS else _fmt_won(v)


# ── 재무제표/주석 청크의 파이프(|) 표 파싱 + 하이라이트(§ 숫자 근거 표 렌더) ──
import re as _re

_UNIT_RE = _re.compile(r"\(?\s*단위\s*[:：]?\s*([가-힣A-Za-z%,\s]+?)\s*\)?$")
_UNIT_INLINE = _re.compile(r"단위\s*[:：]\s*([가-힣A-Za-z%,\s]+?)\s*\)")
# 값 셀: 123 / 1,234 / (1,234) / -3.70 / 75.9% / 12.2%p / 0.1000 / (-)69.1% / (+)
_VALUE_RE = _re.compile(r"^\(?[-+]?[\d,]+(\.\d+)?\)?%?p?$")
_NULLVAL = {"-", "–", "—", "(+)", "(-)", "(±)", "△", "N/A"}
_NUMCELL_RE = _VALUE_RE                                    # 하위호환(다른 참조 대비)

# 재무 표를 붙일지 결정하는 '숫자를 실제로 물었다' 신호(비재무 질의 노이즈 방지)
_FIN_TABLE_CUES = ("얼마", "금액", "구성", "규모", "추이", "증감", "비중", "단가", "원가",
                   "잔액", "장부금액", "합계", "총액", "내역", "명세", "몇", "수치", "규모")


def _is_value(c: str) -> bool:
    """숫자/비율/괄호음수/null(-) 등 '값 셀'인가(재무표 데이터 판별)."""
    c = (c or "").strip()
    if c in _NULLVAL:
        return True
    if not any(ch.isdigit() for ch in c):
        return False
    return bool(_VALUE_RE.match(c.replace("(-)", "-").replace(" ", "")))


def _parse_tables(text: str, unit_hint=None, concepts=None, max_tables: int = 4,
                  max_rows: int = 40) -> list[dict]:
    """재무제표/주석 청크의 파이프(|) 표를 견고하게 파싱.
    핵심: (1) 값 셀(숫자·%·괄호음수·null)로 '데이터 행' 식별, (2) 데이터 행의 최빈 셀수 W로
    열 수 확정(쓰레기 넓은 행에 휘둘리지 않음), (3) 폭이 크게 어긋난 행·빈 행 제거,
    (4) 다단 헤더 조각(' | X')은 '구분' 캡션으로 모으고 마지막 다중텍스트 행을 컬럼헤더로."""
    from collections import Counter
    concepts = [c for c in (concepts or []) if c and len(str(c)) >= 2]
    uh = ""
    if isinstance(unit_hint, (list, tuple)):
        uh = str(unit_hint[0]) if unit_hint else ""
    elif unit_hint:
        uh = str(unit_hint)
    tables: list[dict] = []
    for block in _re.split(r"\n\s*\n", text or ""):
        raw = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not raw:
            continue
        title, unit, groups, parsed = "", uh, [], []       # parsed: (kind, cells) kind=data|text
        for s in raw:
            if s.startswith("[") and s.endswith("]"):       # 청크 헤더
                continue
            if "|" not in s:                                # 단위/제목(파이프 없음)
                m = _UNIT_RE.search(s)
                if m:
                    unit = m.group(1).strip()
                elif not parsed and len(s) <= 45 and not title:
                    title = s
                continue
            cells = [c.strip() for c in s.split("|")]
            m = _UNIT_INLINE.search(s) or _UNIT_RE.search(s)   # '당기 | (단위 : X)' 등
            if m:
                unit = m.group(1).strip()
                cells = [c for c in cells if "단위" not in c]
            while cells and cells[-1] == "":                # 우측 끝 빈 셀 제거(쓰레기 폭 방지)
                cells.pop()
            nonempty = [c for c in cells if c]
            if not nonempty:
                continue
            if any(_is_value(c) for c in cells):
                parsed.append(("data", cells))
            elif len(nonempty) == 1:                        # ' | X' 헤더 조각 → 그룹 라벨
                if nonempty[0] not in groups:
                    groups.append(nonempty[0])
            else:
                parsed.append(("text", cells))              # 다중 텍스트 = 컬럼헤더 후보
        datarows = [c for k, c in parsed if k == "data"]
        if len(datarows) < 2:                               # 데이터 2행 미만 → 표 아님
            continue
        W = Counter(len(c) for c in datarows).most_common(1)[0][0]   # 최빈 데이터 폭
        header = None                                       # 컬럼헤더: 폭 근접한 마지막 다중텍스트 행
        for k, c in parsed:
            if (k == "text" and abs(len(c) - W) <= 1 and sum(1 for x in c if x) >= 2
                    and all(len(x) <= 40 for x in c)):      # 긴 설명 문단은 헤더 아님
                header = (c + [""] * W)[:W]
        rows = []
        if header:
            rows.append({"cells": header, "hl": False, "hdr": True})
        for k, c in parsed:
            if k != "data" or abs(len(c) - W) > 1:          # 폭 크게 어긋난 쓰레기 행 제외
                continue
            cells = (c + [""] * W)[:W]
            label = " ".join(x for x in cells if x and not _is_value(x))[:60]
            hl = bool(concepts) and any(cc in label for cc in concepts)
            rows.append({"cells": cells, "hl": hl})
            if sum(1 for r in rows if not r.get("hdr")) >= max_rows:
                break
        if sum(1 for r in rows if not r.get("hdr")) < 2:
            continue
        tables.append({"title": title, "unit": unit, "groups": groups[:6],
                       "lines": _rows_to_lines(rows), "truncated": len(datarows) > max_rows})
        if len(tables) >= max_tables:
            break
    return tables


def _table_blocks(text: str, max_blocks: int = 3, max_chars: int = 4000) -> list[str]:
    """청크에서 표 후보 블록(원문 그대로)을 추출 — LLM 정규화 입력용.
    빈 줄로 분할, 값 셀이 든 파이프 행 2개 이상인 블록만(제목·단위 줄 포함, 청크헤더 제외)."""
    out = []
    for block in _re.split(r"\n\s*\n", text or ""):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines
                 if not (ln.strip().startswith("[") and ln.strip().endswith("]"))]
        data = sum(1 for ln in lines
                   if "|" in ln and any(_is_value(c.strip()) for c in ln.split("|")))
        if data >= 2:
            out.append("\n".join(lines)[:max_chars])
            if len(out) >= max_blocks:
                break
    return out


_DIGSEQ = _re.compile(r"\d[\d,\.]{2,}")


def _report_label(c: dict) -> str:
    """표 출처(연도·보고서 유형) 라벨. 예: '2025년 · 연결감사보고서', '2026년 1분기 · 별도검토보고서'."""
    fy = c.get("fiscal_year")
    basis = "연결" if c.get("is_consolidated") else "별도"
    assur = c.get("assurance") or "감사"
    period = c.get("period_type") or ""
    ptxt = f" {period}" if period in ("분기", "반기") else ""
    yr = (f"{fy}년{ptxt}" if fy else ptxt).strip()
    lab = f"{basis}{assur}보고서"
    return f"{yr} · {lab}" if yr else lab


def _grid_verified(rows: list, raw: str) -> bool:
    """정규화 격자의 모든 숫자가 원문 블록에 실재하는지 기계검증(콤마 무시).
    하나라도 원문에 없으면(LLM이 계산·보완한 숫자) 실패 → 원문 폴백."""
    rawd = raw.replace(",", "")
    for r in rows:
        for cell in r:
            for m in _DIGSEQ.findall(str(cell)):
                if m.replace(",", "") not in rawd:
                    return False
    return True


def _rows_to_lines(rows: list[dict]) -> list[dict]:
    """격자(grid) 대신 행 단위 표기로 변환 — 각 행을 {label, pairs:[{col,val}], hl}로.
    칼럼명은 기하학적 인덱스가 아닌 **개수 기반 짝짓기**로 부여: 다단 헤더의 라벨열 수가
    데이터행과 달라 생기는 한 칸 밀림을 제거. 값이 연속 블록이면 헤더명 순서대로 짝,
    빈칸 건너뛴 우측 고립값(원문에 헤더 없는 합계열 등)만 무명 처리."""
    hdr_cells = next((r["cells"] for r in rows if r.get("hdr")), None) or []
    names = [c.strip() for c in hdr_cells if c and c.strip()]
    if len(set(names)) <= 1:                      # 전부 중복(예: '차입금 담보' 반복) → 열 이름 생략
        names, hdr_cells = [], []
    out = []
    for r in rows:
        if r.get("hdr"):
            continue
        cells = r["cells"]
        li = 0
        while li < len(cells) and cells[li] and not _is_value(cells[li]):
            li += 1
        label = " ".join(c for c in cells[:max(li, 1)] if c).strip() or "•"
        vals = [(i, cells[i].strip()) for i in range(max(li, 1), len(cells)) if cells[i].strip()]
        if not vals:
            continue
        # 1순위 위치기반: 모든 값의 자리에서 헤더명이 나오면 그대로(비연속 값도 정확, 예: 금호 idx1·5·6)
        pos = [hdr_cells[i].strip() if i < len(hdr_cells) else "" for i, _ in vals]
        if names and all(pos) and all(p != label for p in pos):
            cols = pos
        # 2순위 개수기반: 값이 라벨 직후 연속 블록이면 k번째 값↔k번째 헤더명(다단 라벨열 밀림 보정)
        elif names and all(vals[k + 1][0] == vals[k][0] + 1 for k in range(len(vals) - 1)) \
                and vals[0][0] <= max(li, 1) + 1:
            cols = [(names[k] if k < len(names) and names[k] != label else "") for k in range(len(vals))]
        else:                                     # 무명(우측 고립 합계열 등) — 값만 표시
            cols = [""] * len(vals)
        pairs = [{"col": c, "val": v} for c, (_, v) in zip(cols, vals)]
        out.append({"label": label, "pairs": pairs, "hl": r.get("hl", False)})
    return out


def _roster(items: list[dict]) -> str:
    """다기업 결과 요약 — 전체 원문 프로즈 대신 '기업명 · 산업 · 개수' 로스터(WICS 대분류별)."""
    from src.clients import wics as _wics
    from collections import OrderedDict, defaultdict
    corps = OrderedDict()
    for it in items:
        nm = it.get("corp_name")
        if not nm or nm in corps:
            continue
        b = _wics.brief(it.get("corp_code"), nm) or {}
        corps[nm] = (b.get("dae") or "미분류", b.get("so") or "")
    if not corps:
        return ""
    groups = defaultdict(list)
    for nm, (dae, so) in corps.items():
        groups[dae].append(nm)
    parts = [f"조건에 해당하는 기업은 총 {len(corps)}개사입니다. WICS 산업 대분류별 분포는 다음과 같습니다."]
    for dae, names in sorted(groups.items(), key=lambda x: -len(x[1])):
        shown = ", ".join(names[:20]) + (f" 외 {len(names) - 20}개사" if len(names) > 20 else "")
        parts.append(f"· {dae} ({len(names)}개): {shown}")
    parts.append("각 기업의 세부 산업(소분류)·근거·원문은 아래 카드에서 확인하세요.")
    return "\n".join(parts)


def _find_source(quote: str, evidence: list[dict], ev_norm: list[str]):
    """인용문의 출처 청크와 정확검증 여부 (src, exact) 반환.
    (1) 정규화 정확 포함 → exact=True. (2) 실패 시 distinctive 토큰(긴 숫자·4자+ 한글)
    2개 이상 일치하는 청크 → exact=False(표 인용 등, verified 배지는 유지되지 않으나 원문은 제공)."""
    qn = _norm(quote or "")
    exact = next((c for c, t in zip(evidence, ev_norm) if qn and qn in t), None)
    if exact is not None:
        return exact, True
    toks = _re.findall(r"[\d,]{6,}|[가-힣]{4,}", quote or "")[:8]
    if len(toks) >= 2:
        for c in evidence:
            txt = c.get("text", "") or ""
            if sum(1 for w in toks if w in txt) >= 2:
                return c, False
    return None, False


def _attach_source(it: dict, src: dict, ctx_max: int = 2500) -> None:
    """답변 항목에 출처 청크의 섹션 원문(context)과 DART 식별자를 부착.
    → 앱 내 '원문 보기'(하이라이트) + 섹션 딥링크(viewer.do) 재해소에 사용."""
    it["context"] = (src.get("text") or "")[:ctx_max]
    for f in ("rcept_no", "dcm_no", "note_no", "doc_type"):
        if src.get(f) is not None:
            it[f] = src.get(f)
    if not it.get("corp_code") and src.get("corp_code"):
        it["corp_code"] = src.get("corp_code")     # WICS 등 기업 메타 조회용
    if not it.get("corp_name") and src.get("corp_name"):
        it["corp_name"] = src.get("corp_name")
    if not it.get("section_path"):
        it["section_path"] = src.get("section_path") or ""
    if not it.get("dart_url"):
        it["dart_url"] = src.get("dart_url") or ""


# Fact 타입 → 원문 위치(딥링크 섹션 라우팅 힌트). 감사본문 vs 재무제표/주석 구분.
_FACT_DOCTYPE = {
    "감사의견_유형": "audit", "핵심감사사항": "audit", "계속기업_불확실성": "audit",
    "내부회계관리제도_검토의견": "audit", "감사인_보수": "audit",
    "감가상각_변경": "financial_note", "회계정책_변경": "financial_note",
    "회계추정_변경": "financial_note", "소송_우발부채": "financial_note",
    "특수관계자_거래": "financial_note", "재고자산_평가방법": "financial_note",
    "수익인식_정책": "financial_note",
}

# 존재형 Fact인데 '없음/해당없음'을 담은 부정 사실은 스크리닝 오탐 → 제외
_ABSENCE_TYPES = {"계속기업_불확실성", "소송_우발부채", "특수관계자_거래", "감가상각_변경"}
_NEG_MARKERS = ("해당사항 없", "해당사항이 없", "해당 없", "해당없", "불확실성 없",
                "불확실성이 없", "특이사항 없", "미해당", "관련 중요한 불확실성 없",
                "변경 아닌", "변경 없", "변경사항 없", "유지)")


def _is_absence(ft: str, detail: dict, evidence: str) -> bool:
    if ft not in _ABSENCE_TYPES:
        return False
    blob = (evidence or "") + " " + " ".join(str(v) for v in (detail or {}).values())
    return any(m in blob for m in _NEG_MARKERS)


# 서술형·형용사 detail_contains는 과필터를 일으키므로 결정적으로 무시(프롬프트 보강)
_DETAIL_STOP = ("불확실", "유의", "중요", "있는", "없는", "변경", "존속",
                "사례", "경향", "영향", "전부", "모두", "관련", "여부", "유형",
                "감가상각", "내용연수")  # fact_type 정의어(값 필터 아님) → 과필터 방지


def _clean_detail(d) -> str | None:
    if not d or any(w in str(d) for w in _DETAIL_STOP):
        return None
    return d


# Fact 유형별 값이 담기는 구조화 detail 키 → 스크리닝을 이 필드로 정밀 매칭(근거텍스트 부수언급 오탐 제거)
_VALUE_KEY = {"감사의견_유형": "opinion", "내부회계관리제도_검토의견": "opinion",
              "재고자산_평가방법": "method", "수익인식_정책": "method", "감사인_보수": "auditor"}


def _value_key(fact_types) -> str | None:
    keys = {_VALUE_KEY.get(ft) for ft in (fact_types or [])}
    keys.discard(None)
    return next(iter(keys)) if len(keys) == 1 else None


class QueryEngine:
    def __init__(self, cache_understand: bool = False):
        self.store = OpenSearchStore()
        if not self.store.ping():
            raise SystemExit("OpenSearch 연결 실패 — docker compose up 확인")
        self.claude = ClaudeClient()
        self.embedder = Embedder()
        self._od_cache: dict = {}          # 온디맨드 세션 캐시(재추출 방지·비용최소)
        self._tbl_cache: dict = {}         # 표 정규화 캐시(블록 해시 → 격자, 반복 질의 비용 0)
        self._store_tbl_cache: dict = {}   # 테이블 스토어 캐시(rcept_no → 원본 XML 표 목록)
        self._pool_exec = ThreadPoolExecutor(max_workers=4)  # HyDE 등 병렬(지연 은닉)
        self.reranker = None               # 크로스인코더 재랭킹(로드 실패/OOM이면 폴백)
        if settings.use_reranker:
            try:
                self.reranker = Reranker()
                log.info("재랭킹 활성(%s)", settings.reranker_model)
            except Exception as e:  # noqa: BLE001
                log.warning("재랭킹 로드 실패 → RRF 폴백: %s", e)
        # understand 캐시(검증 반복 시 Haiku 호출 절감, opt-in). 프롬프트가 바뀌면 자동 무효화.
        self._uc_on = cache_understand
        self._uc_path = settings.data_dir / settings.pipeline_version / "understand_cache.json"
        self._uc: dict = {}
        if cache_understand:
            from src.clients import claude as _cl
            self._uc_ver = hashlib.md5(
                (str(getattr(_cl, "SYSTEM_UNDERSTAND", "")) + str(getattr(_cl, "FACT_TYPES", ""))
                 + settings.claude_model_understand   # 모델 교체 시 캐시 자동 무효화
                 ).encode("utf-8")).hexdigest()[:10]
            try:
                data = json.loads(self._uc_path.read_text(encoding="utf-8"))
                if data.get("_version") == self._uc_ver:
                    self._uc = data
            except (OSError, ValueError):
                self._uc = {}
            self._uc["_version"] = self._uc_ver
        self.pg = None
        try:
            from src.clients.postgres import PostgresStore
            pg = PostgresStore()
            if pg.ping():
                self.pg = pg
                log.info("Fact Store(PG) 연결 — 스크리닝 경로 활성")
        except Exception as e:  # noqa: BLE001
            log.warning("PostgreSQL 미연결(스크리닝 경로 비활성): %s", e)
        self._names = None
        self._fact_years = None            # Fact Store 적재 연도 캐시(전수 조회 지원 연도)
        self.onto = Ontology()

    def _understand(self, question: str) -> dict:
        """질의이해(Haiku). 캐시 on이면 동일 질문은 재호출 없이 재사용(검증 반복 비용 0)."""
        import copy
        if self._uc_on and question in self._uc:
            return copy.deepcopy(self._uc[question])
        u = self.claude.understand(question)
        if self._uc_on:
            self._uc[question] = copy.deepcopy(u)
            try:
                self._uc_path.write_text(json.dumps(self._uc, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return u

    def _apply_ontology(self, u: dict, question: str) -> dict:
        """질의이해 출력 정규화(온톨로지): 업종 정확화·부정 집합·값 동의어 확장."""
        u["_sector"] = self.onto.resolve_sector(u.get("industry"))
        # WICS 자동 인식: 질문 속 업종을 WICS 계층으로도 해석.
        # - KRX 미매치(조선·자동차부품·반도체·게임 등) → WICS 단독 스코프(신규 능력)
        # - KRX 매치했지만 WICS 라벨이 더 정밀(예: 운송장비·부품 → 조선) → 교집합으로 좁힘
        # - 라벨 동일(건설=건설 등) → 기존 KRX 동작 유지(골든셋 무회귀). UI 명시 선택은 항상 우선.
        if u.get("industry") and not u.get("_wics_corps"):
            from src.clients import wics as _wics
            hit = _wics.resolve_industry(u["industry"])
            if hit:
                codes, label, wcode = hit
                sec = u.get("_sector") or ""
                ln = label.replace(" ", "").replace("·", "")
                sn = sec.replace(" ", "").replace("·", "")
                # 동일 개념 판정(양방향 포함): 유통↔소매(유통), 철강↔철강금속, 통신↔전기통신 등
                # → KRX(골든셋 검증) 유지. 진짜 더 정밀할 때만(조선⊄운송장비부품) WICS 적용.
                same = bool(sn) and (ln in sn or sn in ln)
                if not sec or not same:
                    u["_wics_corps"] = codes
                    u["_wics_auto"] = {"label": label, "code": wcode, "n": len(codes)}
                    log.info("업종 WICS 자동 인식: '%s' → %s(%s) %d사%s",
                             u["industry"], label, wcode, len(codes),
                             f" (KRX {sec} ∩)" if sec else "")
        exc = list(u.get("screening_detail_excludes") or [])
        exc += self.onto.negation_excludes(question, u.get("screening_fact_types"))
        d = _clean_detail(u.get("screening_detail_contains"))     # 서술형 과필터 제거
        cont = self.onto.expand_detail(
            u.get("screening_fact_types"), d) if d else None       # 값 동의어 OR 확장
        u["screening_detail_contains"] = cont
        # '적정'은 '부적정/비적정'의 substring → 오포함 방지(감사기준상 별개 의견). opinion 유형에만.
        cterms = cont if isinstance(cont, (list, tuple)) else ([cont] if cont else [])
        if _value_key(u.get("screening_fact_types")) == "opinion" and any(t == "적정" for t in cterms):
            exc += ["부적정", "비적정"]
        u["screening_detail_excludes"] = list(dict.fromkeys(exc)) or None
        # 감가상각변경 is-a 회계추정변경(K-IFRS): '감가상각/내용연수' 질의는 둘 다 포함(understand 변동 방어)
        fts = u.get("screening_fact_types")
        if (fts and "회계추정_변경" in fts and "감가상각_변경" not in fts
                and any(k in question for k in ("감가상각", "내용연수", "상각방법", "상각방식"))):
            u["screening_fact_types"] = list(fts) + ["감가상각_변경"]
        # '비슷한/유사한 X' = 유사사례(의미 유사도) → Fact Store(범주 매칭) 대신 벡터로.
        # 참조기업/거친 산업 필터를 걷어내 '의미'로 유사기업을 찾게 함(§1.2 유사사례 정리).
        if self.onto.is_similarity(question):
            u["screening_fact_types"] = None
            u["_similarity"] = True
        # '사례/경향/영향' 개방형 탐색 → 스크리닝(Fact Store) 대신 벡터로
        if self.onto.is_open(question) and u.get("intent") != "스크리닝":
            u["screening_fact_types"] = None
        return u

    def _apply_hints(self, u: dict, hints: dict | None) -> dict:
        """UI 구조화 필터(산업/연도/보고서/Fact 키워드)를 질의이해에 주입 — LLM 추출보다 우선(결정적)."""
        if not hints:
            return u
        if hints.get("sector"):
            u["industry"] = hints["sector"]
        ys = hints.get("years") or ([hints["year"]] if hints.get("year") else [])
        ys = [int(y) for y in ys if str(y).isdigit()]
        if ys:                                    # UI 연도 복수선택(예: 2024+2025) 지원
            u["fiscal_years"] = sorted(set(ys))
        if hints.get("report") == "검토":
            u["assurance"] = "검토"
            u.setdefault("report_period", "분기")
        if hints.get("fact_types"):
            u["screening_fact_types"] = [t for t in hints["fact_types"] if t]
            u["intent"] = "스크리닝"
        if hints.get("exhaustive"):
            u["_force_exhaustive"] = True         # UI 빠른조회=전수 의도 명시 → Fact Store 라우팅
        w = hints.get("wics") or {}               # WICS 대/중/소 선택 → 해당 corp_code 집합으로 스코프
        if w.get("dae") or w.get("jung") or w.get("so"):
            from src.clients import wics as _wics
            u["_wics_corps"] = _wics.corp_codes_for(w.get("dae", ""), w.get("jung", ""), w.get("so", ""))
            u["_wics_sel"] = w
        return u

    def _fact_years_cached(self) -> set:
        if self._fact_years is None:
            try:
                self._fact_years = set(self.pg.fact_years()) if self.pg else set()
            except Exception:  # noqa: BLE001
                self._fact_years = set()
        return self._fact_years

    def _years_covered(self, u: dict) -> bool:
        """선택 연도가 Fact Store 적재 연도(2024·2025)와 겹치는가. 연도 미지정=True.
        2023 등 미적재 연도만 고른 전수 조회는 소수 잔여 사실로 오해를 주므로 온디맨드로 우회."""
        ys = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()]
        if not ys:
            return True
        fy = self._fact_years_cached()
        return (not fy) or any(y in fy for y in ys)

    def run(self, question: str, k: int = 12, per_query_k: int = 10, rrf_k: int = 60,
            fast: bool = False, hints: dict | None = None) -> dict:
        u = self._understand(question)
        u = self._apply_hints(u, hints)
        u = self._apply_ontology(u, question)
        # 검토(분기/반기)는 Fact Store(연차 감사만 적재) 밖 → 벡터/온디맨드로(설계서 §2.0 감사/검토 구분)
        review = u.get("assurance") == "검토" or u.get("report_period") in ("분기", "반기")
        use_facts = bool(self.pg) and not review and not u.get("_similarity")
        if review:
            log.info("경로=검토보고서(분기/반기) → 벡터/온디맨드(Fact Store 우회)")
        # 모든 Fact Store 경로는 결과를 온디맨드(본문검색)로 보완 → 항상 두 방향 답변.
        # 관계·다홉: 감사인 교체·동일감사인·감사인 경유·특수관계자망 (단일기업보다 먼저)
        if use_facts and u.get("graph_relation"):
            res = self._graph(u)
            if res is not None:
                return self._augment_ondemand(question, u, res, k, per_query_k, rrf_k, fast)
        # 표준 지표 밖 '계정 단위' 수치(단일 기업 + 숫자단서) → financial_items 전 계정 조회
        # (단건 프로파일보다 먼저 — '매출채권 얼마' 류는 계정 수치가 정답)
        if (use_facts and u.get("intent") in ("수치", "단건") and u.get("company")
                and not self._known_metric(u.get("metric"))
                and any(c in question for c in _FIN_TABLE_CUES)):
            res = self._account_numeric(u, question)
            if res is not None:
                return self._augment_ondemand(question, u, res, k, per_query_k, rrf_k, fast)
        # §7 단일 기업(기업 1개 + '전부/모두' 없음) → 세부 라우팅(Fact Store/벡터)
        if use_facts and u.get("intent") == "단건" and u.get("company"):
            res = self._single_company(u)
            if res is not None:
                return self._augment_ondemand(question, u, res, k, per_query_k, rrf_k, fast)
        # 계산형/순위 → 정형재무 수치(XBRL) SQL
        if use_facts and u.get("intent") == "수치" and u.get("metric"):
            res = self._numeric(u)
            if res is not None:
                return self._augment_ondemand(question, u, res, k, per_query_k, rrf_k, fast)
        # Layer 2: 스크리닝 → 정리 데이터 전수. 방향 B: '전부/모두/명단' 명시적 완전성 or UI 빠른조회 힌트.
        if (use_facts and u.get("intent") == "스크리닝" and u.get("screening_fact_types")
                and self._years_covered(u)
                and (not settings.factstore_exhaustive_only or _is_exhaustive(question)
                     or u.get("_force_exhaustive"))):
            log.info("경로=FactStore 전수 스크리닝 · types=%s", u.get("screening_fact_types"))
            return self._screen(question, u, k=k, per_query_k=per_query_k, rrf_k=rrf_k, fast=fast)
        if u.get("intent") == "스크리닝" and u.get("screening_fact_types") and not self._years_covered(u):
            log.info("선택 연도 Fact Store 미적재(예:2023) → 온디맨드 우회")
        # 온디맨드 우선: 스키마밖·개방형·검토·유사도·(완전성 신호 없는 스크리닝)은 온디맨드(의미검색)로
        if u.get("intent") == "스크리닝" and u.get("screening_fact_types"):
            log.info("스크리닝이나 완전성 신호 없음 → 온디맨드 의미검색(방향 B)")
        return self._vector(question, u, k=k, per_query_k=per_query_k, rrf_k=rrf_k,
                            fast=fast, ondemand=True)

    def run_stream(self, question: str, k: int = 12, per_query_k: int = 10, rrf_k: int = 60,
                   hints: dict | None = None):
        """2단계 스트리밍: (1) 질의이해+코어(정리 데이터) 즉시 → (2) 온디맨드 보완 이어붙임.
        각 단계를 dict 이벤트로 yield(서버가 NDJSON 전송) → 체감 지연 최소화."""
        u = self._understand(question)
        u = self._apply_hints(u, hints)
        u = self._apply_ontology(u, question)
        yield {"type": "understanding", "understanding": u}
        review = u.get("assurance") == "검토" or u.get("report_period") in ("분기", "반기")
        use_facts = bool(self.pg) and not review and not u.get("_similarity")
        core = None
        if use_facts and u.get("graph_relation"):
            core = self._graph(u)
        elif (use_facts and u.get("intent") in ("수치", "단건") and u.get("company")
              and not self._known_metric(u.get("metric"))
              and any(c in question for c in _FIN_TABLE_CUES)):
            core = self._account_numeric(u, question)   # 계정 단위(financial_items) — 단건보다 먼저
            if core is None and u.get("intent") == "단건":
                core = self._single_company(u)          # 계정 미매칭 → 단건 프로파일 폴백
        elif use_facts and u.get("intent") == "단건" and u.get("company"):
            core = self._single_company(u)
        elif use_facts and u.get("intent") == "수치" and u.get("metric"):
            core = self._numeric(u)
        elif (use_facts and u.get("intent") == "스크리닝" and u.get("screening_fact_types")
              and self._years_covered(u)
              and (not settings.factstore_exhaustive_only or _is_exhaustive(question)
                   or u.get("_force_exhaustive"))):
            core = self._screen_core(u)   # 방향 B: 완전성 질의 or UI 빠른조회 힌트만 Fact Store 전수
        if core is not None and core.get("path") == "disambiguation":   # 동명이의 명확화
            yield {"type": "core", **core}
            yield {"type": "done", "path": "disambiguation"}
            return
        if core is not None and core.get("items"):
            for it in core["items"]:
                it.setdefault("source", "factstore")
            yield {"type": "core", "understanding": u, "items": core["items"],
                   "answer": core.get("answer", ""), "analysis": core.get("analysis", ""),
                   "tables": core.get("tables", []), "path": core.get("path"),
                   "insufficient": core.get("insufficient", False),
                   "verified_count": core.get("verified_count", 0)}
            yield {"type": "progress", "msg": "본문에서 더 찾는 중…"}     # 즉시 코어 뒤 보완 진행표시
            supp, supp_tables, supp_analysis = self._ondemand_supplement(
                question, u, core["items"], k, per_query_k, rrf_k)
            if supp or supp_tables or supp_analysis:
                yield {"type": "supplement", "items": supp, "tables": supp_tables,
                       "analysis": (supp_analysis if not core.get("analysis") else ""),
                       "verified_count": sum(1 for it in supp if it.get("verified"))}
            yield {"type": "done", "added": len(supp),
                   "path": (core.get("path") or "factstore") + ("+ondemand" if supp else "")}
            return
        # 정리 데이터 없음(스키마밖·개방형·검토) → 온디맨드만
        yield {"type": "progress", "msg": "본문에서 찾는 중…"}
        res = self._vector(question, u, k=k, per_query_k=per_query_k, rrf_k=rrf_k, ondemand=True)
        yield {"type": "core", "understanding": u, "items": res.get("items", []),
               "answer": res.get("answer", ""), "analysis": res.get("analysis", ""),
               "tables": res.get("tables", []), "path": res.get("path"),
               "insufficient": res.get("insufficient", False),
               "verified_count": res.get("verified_count", 0)}
        yield {"type": "done", "path": res.get("path")}

    def _financial_tables(self, u: dict, evidence: list[dict], question: str = "",
                          max_src: int = 2, max_out: int = 3) -> list[dict]:
        """근거 중 재무제표/주석 청크의 표를 **LLM 정규화**해 격자(grid)로 제공(§표 재구축 A안).
        규칙 파싱은 원문 블록 '위치 탐지'에만 사용하고, 구조화는 Haiku가 수행 →
        다단헤더·잘린 표·초광폭 표 모두 처리. **모든 숫자는 원문 대조 기계검증**,
        실패 시 grid 없이 원문(raw) 폴백(환각 0). (1) 상위 근거가 재무이고
        (2) 실제로 숫자를 물었을 때만 부착(자기게이팅)."""
        fin = ("financial_stmt", "financial_note", "audit")   # 스토어 v2: 감사보고서 표(보수 등)도 지원
        if not any(c.get("doc_type") in fin for c in evidence[:3]):
            return []
        wants_numbers = (u.get("intent") in ("수치", "단건") or u.get("metric")
                         or any(cue in (question or "") for cue in _FIN_TABLE_CUES))
        if not wants_numbers:                       # 숫자 의도 없음(사례·유사·경향 등) → 표 생략
            return []
        concepts = [str(c) for c in (u.get("key_concepts") or []) if c and len(str(c)) >= 2]
        if u.get("metric"):
            concepts.append(u["metric"])
        out, jobs, used, seen_store = [], [], 0, set()   # jobs=(chunk, raw_block) → LLM 폴백
        for c in evidence:
            if c.get("doc_type") not in fin:
                continue
            blks = _table_blocks(c.get("text", ""))
            if not blks:
                continue
            for b in blks[:max(1, max_out - len(jobs) - len(out))]:
                st = self._store_match(c.get("rcept_no"), b, seen_store)   # 1순위: 원본 XML 스토어
                if st is not None:
                    st.update({"corp_name": c.get("corp_name", ""), "fiscal_year": c.get("fiscal_year"),
                               "dart_url": st.get("dart_url") or c.get("dart_url", ""),
                               "doc_type": st.get("doc_type") or c.get("doc_type"),
                               "report": _report_label(c)})
                    if not st.get("unit"):
                        uh = c.get("unit_hint")
                        st["unit"] = (uh[0] if isinstance(uh, (list, tuple)) and uh else uh or "")
                    if concepts and st.get("grid"):
                        for r in st["grid"]["rows"]:
                            r["hl"] = any(cc in (r["cells"][0] if r["cells"] else "") for cc in concepts)
                    elif concepts and st.get("lines"):
                        for ln in st["lines"]:
                            ln["hl"] = any(cc in ln.get("label", "") for cc in concepts)
                    out.append(st)
                else:
                    jobs.append((c, b))
            used += 1
            if used >= max_src or len(jobs) + len(out) >= max_out:
                break
        if not jobs and not out:
            return []

        def _norm(job):
            c, b = job
            key = hash(b)
            g = self._tbl_cache.get(key)
            if g is None:
                uh = c.get("unit_hint")
                uh = ", ".join(uh) if isinstance(uh, (list, tuple)) else (uh or "")
                try:
                    g = self.claude.normalize_table(b, uh) or {}
                except Exception as e:  # noqa: BLE001
                    log.warning("표 정규화 실패(원문 폴백): %s", e)
                    g = {}
                self._tbl_cache[key] = g
                if len(self._tbl_cache) > 512:
                    self._tbl_cache.pop(next(iter(self._tbl_cache)))
            return c, b, g

        for fu in [self._pool_exec.submit(_norm, j) for j in jobs]:
            c, b, g = fu.result()
            entry = {"corp_name": c.get("corp_name", ""), "fiscal_year": c.get("fiscal_year"),
                     "section_path": c.get("section_path", ""), "dart_url": c.get("dart_url", ""),
                     "doc_type": c.get("doc_type"), "raw": b, "src": "llm", "report": _report_label(c),
                     "title": (g.get("title") or "").strip(), "unit": (g.get("unit") or "").strip()}
            rows = [r for r in (g.get("rows") or []) if isinstance(r, list) and any(str(x).strip() for x in r)]
            cols = [str(x) for x in (g.get("columns") or [])]
            # 칼럼명 절반 이상이 숫자면 헤더 오인(잘린 블록) → 원문 폴백
            bad_hdr = cols[1:] and sum(1 for x in cols[1:] if _is_value(x)) > len(cols[1:]) / 2
            # 형상 게이트(§표 렌더 보수2): LLM 정규화 결과도 스토어와 같은 기준 적용 —
            # 숫자가 다 맞아도 '빈 셀 바다·전치 격자'는 격자로 내보내지 않고 행 단위로 강등
            bad_shape = not self._grid_ok({"columns": cols, "rows": rows})
            if rows and not bad_hdr and not bad_shape and _grid_verified(rows, b):    # 숫자 전수 원문검증 통과 → 격자 렌더
                entry["grid"] = {
                    "columns": [str(x) for x in (g.get("columns") or [])],
                    "rows": [{"cells": [str(x) for x in r],
                              "hl": bool(concepts) and any(cc in str(r[0]) for cc in concepts)}
                             for r in rows[:40]]}
                entry["verified"] = True
            else:                                    # 검증 실패/빈 결과 → 행 단위 파싱 폴백 → 원문
                entry["verified"] = False
                try:                                 # 초광폭 원문 파이프 덩어리 방지: 라벨→값 행 단위 표기
                    parsed = _parse_tables(b, c.get("unit_hint"), concepts)
                    if parsed and parsed[0].get("lines"):
                        entry["lines"] = parsed[0]["lines"][:30]
                        if not entry["unit"]:
                            entry["unit"] = parsed[0].get("unit") or ""
                except Exception:  # noqa: BLE001
                    pass
            out.append(entry)
        return out[:max_out]

    def _store_match(self, rcept_no: str, block: str, seen: set):
        """테이블 스토어(원본 XML 표)에서 블록과 숫자가 겹치는 표 검색.
        매칭되면 격자 엔트리 반환(100% 원본 구조, LLM 불필요), 아니면 None → LLM 폴백."""
        if not self.pg or not rcept_no:
            return None
        ts = self._store_tbl_cache.get(rcept_no)
        if ts is None:
            try:
                ts = self.pg.doc_tables_for(rcept_no)
            except Exception:  # noqa: BLE001  스토어 미구축 등 → LLM 폴백
                ts = []
            for t in ts:
                # numkey(1500자 절단)는 초광폭 표에서 매칭 실패 → rows 전체에서 숫자 집합 구성
                nums = set()
                for r in (t.get("rows") or []):
                    for cell in r:
                        for m in _DIGSEQ.findall(str(cell)):
                            nums.add(m.replace(",", ""))
                t["_numset"] = {n for n in nums if len(n) >= 3}
            self._store_tbl_cache[rcept_no] = ts
            if len(self._store_tbl_cache) > 128:
                self._store_tbl_cache.pop(next(iter(self._store_tbl_cache)))
        if not ts:
            return None
        bnums = {m.replace(",", "") for m in _DIGSEQ.findall(block)}
        bnums = {n for n in bnums if len(n) >= 3}
        if len(bnums) < 3:
            return None
        best, best_sc = None, 0
        for t in ts:
            key = (t["section_path"], t["title"], (t.get("numkey") or "")[:60])
            if key in seen:
                continue
            sc = len(bnums & t["_numset"])
            if sc > best_sc:
                best, best_sc = t, sc
        if best is None:
            return None
        tn = len(best["_numset"]) or 1
        if best_sc < 3 or best_sc < 0.5 * min(len(bnums), tn):   # 겹침 부족 → 미스
            return None
        base = {"title": best.get("title") or "", "unit": best.get("unit") or "",
                "section_path": best.get("section_path") or "", "src": "store",
                "doc_type": best.get("doc_type"), "dart_url": best.get("dart_url") or "",
                "verified": True}
        rows = best.get("rows") or []
        # 격자 품질 게이트(§표 렌더): 희소·초광폭 격자(약정·매트릭스류)는 격자 대신
        # **원본 항목별 정리(lines)**로 강등 — LLM 없이 100% 원본, 빈 셀은 생략.
        # 항목별 정리조차 안 되면 None → LLM 정규화(A안) 폴백.
        if not self._grid_ok(best):
            cols_ = [str(x) for x in (best.get("columns") or [])]
            lines = []
            for r in rows[:30]:
                cells = [str(x) for x in r]
                label = cells[0].strip() if cells else ""
                pairs = [{"col": cols_[j] if j < len(cols_) else "",
                          "val": (cells[j][:240] + "…") if len(cells[j]) > 240 else cells[j]}
                         for j in range(1, len(cells)) if cells[j].strip()]   # 서술형 셀(약정처 등) 과대 방지
                if label and pairs:
                    lines.append({"label": label, "pairs": pairs, "hl": False})
            if len(lines) < 2:
                return None
            seen.add((best["section_path"], best["title"], (best.get("numkey") or "")[:60]))
            return {**base, "lines": lines}
        seen.add((best["section_path"], best["title"], (best.get("numkey") or "")[:60]))
        return {**base,
                "grid": {"columns": [str(x) for x in (best.get("columns") or [])],
                         "rows": [{"cells": [str(x) for x in r], "hl": False}
                                  for r in rows[:40]]}}

    @staticmethod
    def _grid_ok(t: dict) -> bool:
        """스토어 격자 표시 적합성: 헤더 존재 + 빈 셀 비율(전체 60%·초광폭 45%) 이내."""
        cols, rows = t.get("columns") or [], t.get("rows") or []
        if not cols or not rows:
            return False
        cells = [str(c) for r in rows for c in r]
        if not cells:
            return False
        empty = sum(1 for c in cells if not c.strip()) / len(cells)
        return empty <= (0.45 if len(cols) > 14 else 0.6)

    def _rerank(self, question: str, ranked: list, k: int) -> list:
        """크로스인코더 재랭킹(있으면) → 상위 k. 없으면 RRF 순 상위 k."""
        if not self.reranker or len(ranked) <= 1:
            return ranked[:k]
        pairs = [(question, (r["hit"]["_source"].get("text") or "")[:512]) for r in ranked]
        try:
            scores = self.reranker.score(pairs)
        except Exception as e:  # noqa: BLE001
            log.warning("재랭킹 실패(폴백): %s", e)
            return ranked[:k]
        for r, s in zip(ranked, scores):
            r["rr"] = float(s)
        return sorted(ranked, key=lambda x: -x.get("rr", 0.0))[:k]

    def _vector(self, question: str, u: dict, k: int = 12,
                per_query_k: int = 10, rrf_k: int = 60, fast: bool = False,
                ondemand: bool = False, exclude_corps=None,
                supplement: bool = False) -> dict:
        """supplement=True는 '보완 검색'(코어 결과에 덧붙이는 부가 항목) — 경량 합성(Haiku) 유지.
        기본(False)의 온디맨드는 '주답변'이므로 본답변과 같은 모델(Sonnet)로 합성한다(모델 재배치 §77-1)."""
        import copy
        path_label = "ondemand" if ondemand else "vector"
        # 보완검색(exclude/supplement)은 캐시 제외 — 주답변(Sonnet) 캐시에 경량 결과가 섞이지 않게
        cacheable = ondemand and not fast and not exclude_corps and not supplement
        if cacheable and question in self._od_cache:              # 세션 캐시(재추출 방지·비용↓)
            return copy.deepcopy(self._od_cache[question])
        queries = (u.get("expanded_queries") or [])[:(4 if ondemand else 6)] or [question]
        filters = _build_filters(u)
        log.info("경로=%s · 의도=%s · industry=%s · 멀티쿼리=%d · 재랭킹=%s · 필터=%s",
                 path_label, u.get("intent"), u.get("industry"), len(queries),
                 bool(self.reranker), list(filters))
        # HyDE(§5.7)를 병렬로 미리 생성(개방형만) — Haiku 네트워크 대기를 임베딩 GPU작업과 겹쳐 은닉
        hyde_future = None
        if ondemand and not fast and settings.use_hyde:
            hyde_future = self._pool_exec.submit(self.claude.hyde_document, question)
        pool: dict = {}
        vecs = self.embedder.encode(queries)          # 멀티쿼리 배치 임베딩(1 GPU락 — 순차 N회 → 1회)
        for q, vec in zip(queries, vecs):
            for rank, h in enumerate(self.store.hybrid_search(
                    q, vec, filters=filters, k=per_query_k, exclude_corps=exclude_corps)):
                e = pool.setdefault(h["_id"], {"hit": h, "score": 0.0})
                e["score"] += 1.0 / (rrf_k + rank + 1)
        if hyde_future is not None:                   # 병렬 생성된 HyDE 문단으로 추가검색
            hyde = hyde_future.result()
            if hyde:
                hvec = self.embedder.encode([hyde])[0]
                for rank, h in enumerate(self.store.search_dense(
                        hvec, filters=filters, k=per_query_k, exclude_corps=exclude_corps)):
                    e = pool.setdefault(h["_id"], {"hit": h, "score": 0.0})
                    e["score"] += 1.0 / (rrf_k + rank + 1)
                log.info("HyDE 추가검색 적용")
        ranked = sorted(pool.values(), key=lambda x: -x["score"])[:max(k * 3, 30)]  # 재랭킹 후보 풀
        ranked = self._rerank(question, ranked, k)
        evidence = [r["hit"]["_source"] for r in ranked]
        if not evidence:
            return {"understanding": u, "answer": "조건에 맞는 근거를 찾지 못했습니다.",
                    "items": [], "evidence_count": 0, "verified_count": 0, "path": path_label}
        # fast: 합성 건너뜀 · 보완검색(supplement)만 Haiku(비용최소) · 주답변(온디맨드 포함)은 Sonnet
        synth = {"items": []} if fast else self.claude.synthesize(
            question, evidence, cheap=(ondemand and supplement))
        # 생성-평가 분리(하네스): 합성 답변을 독립 감사자(Haiku)가 채점 → 결함이면 Opus 재합성.
        # '명시적 insufficient'(자기신고)만 믿던 기존 게이트를 '외부 채점 verdict'로 강화.
        # 보완검색(supplement)은 answer가 버려지고 items만 쓰이므로 감사·에스컬레이션 제외
        # (읽히는 주답변에만 품질 게이트 — 모델 재배치 §77 원칙, Opus 낭비 방지)
        escalate = bool(not fast and not supplement and evidence and synth.get("insufficient"))
        judged = None
        # answer가 생성됐고(스크리닝 로스터/개방형 포함) 자기신고 불충분이 아니면 감사자 채점.
        # (items 항목화 실패로 fallback 구성된 개방형도 answer 품질은 채점 대상)
        if (not fast and not supplement and settings.use_answer_judge and evidence
                and synth.get("answer") and not synth.get("insufficient")):
            judged = self.claude.judge_answer(question, synth.get("answer", ""),
                                              synth.get("analysis", ""), evidence)
            if judged.get("verdict") in ("revise", "insufficient") or not judged.get("answered", True):
                log.info("감사자 반려(verdict=%s, missing=%s) → 재합성",
                         judged.get("verdict"), (judged.get("missing") or "")[:60])
                escalate = True
        if not fast and settings.use_opus_escalation and escalate:
            log.info("Opus 에스컬레이션(감사자 반려/불충분 → 재합성)")
            synth = self.claude.synthesize(question, evidence, tier="hard")
            # 수정본 재검토(§77-2): 재합성 결과도 감사자 1회 재채점 — 재반려여도 추가 에스컬레이션은
            # 하지 않음(루프 방지). 최종 verdict가 관측 지표(result.judge)에 남는다.
            if settings.use_answer_judge and synth.get("answer"):
                judged = self.claude.judge_answer(question, synth.get("answer", ""),
                                                  synth.get("analysis", ""), evidence)
                judged["rechecked"] = True
                log.info("에스컬레이션 재감사: verdict=%s", judged.get("verdict"))
        # 인용 기계검증: quote가 근거 텍스트에 실제 존재하는가 + 출처 청크(섹션 원문·식별자) 부착
        ev_norm = [_norm(c.get("text", "")) for c in evidence]
        items = []
        for it in synth.get("items", []):
            src, exact = _find_source(it.get("quote", ""), evidence, ev_norm)
            it["verified"] = exact                    # 배지는 정확검증만(엄격 유지)
            if src is not None:
                _attach_source(it, src)               # context(섹션 원문)·rcept_no·dcm_no 등(표 인용 포함)
            items.append(it)
        if not items:      # 합성이 항목화 실패 → 근거 청크로 직접 구성(개방형 빈결과 방지, 환각 0)
            for c in evidence[:8]:
                it = {
                    "corp_code": c.get("corp_code"), "corp_name": c.get("corp_name", ""),
                    "fiscal_year": c.get("fiscal_year"), "is_consolidated": c.get("is_consolidated"),
                    "conclusion": (c.get("section_path") or "")[:60],
                    "quote": (c.get("text") or "")[:200], "section_path": c.get("section_path") or "",
                    "dart_url": c.get("dart_url") or "", "verified": True, "_fallback": True,
                }
                _attach_source(it, c)
                items.append(it)
        verified_n = sum(1 for it in items if it["verified"])
        log.info("합성 항목 %d · 인용검증 통과 %d", len(items), verified_n)
        n_corp = len({it.get("corp_name") for it in items if it.get("corp_name")})
        analysis = synth.get("analysis", "")
        # 다기업 스크리닝 → 기업별 설명 프로즈 금지, 산업별 로스터(기업명·수)로 대체(토큰·가독성)
        if (u.get("intent") == "스크리닝" and n_corp >= 2) or n_corp >= 7:
            analysis = _roster(items) or analysis
        result = {"understanding": u, "answer": synth.get("answer", ""),
                  "analysis": analysis,
                  "tables": self._financial_tables(u, evidence, question),
                  "insufficient": synth.get("insufficient", False),
                  "items": items, "evidence_count": len(evidence),
                  "verified_count": verified_n, "path": path_label}
        if judged is not None:                        # 감사자 채점 결과(관측성·디버깅)
            result["judge"] = {k: judged.get(k)
                               for k in ("verdict", "answered", "grounded", "missing", "rechecked")}
        if cacheable:
            self._od_cache[question] = copy.deepcopy(result)
            if len(self._od_cache) > 256:
                self._od_cache.pop(next(iter(self._od_cache)))
        return result

    def _corp_names(self) -> dict:
        if self._names is None:
            self._names = {r["corp_code"]: r.get("corp_name") for r in _universe_rows()}
        return self._names

    def _facts_to_result(self, rows: list, u: dict, scope: str, note: str = "") -> dict:
        """Fact 행 → 근거 항목(부정 사실 제외, 합성 없이 정형 사실 직접 = 환각 0)."""
        names = self._corp_names()
        items, corp_set, dropped = [], set(), 0
        for r in rows:
            if _is_absence(r["fact_type"], r.get("detail"), r.get("evidence_text")):
                dropped += 1
                continue
            corp_set.add(r["corp_code"])
            ev = r.get("evidence_text") or ""
            items.append({
                "corp_code": r["corp_code"],
                "corp_name": names.get(r["corp_code"], r["corp_code"]),
                "fiscal_year": r["fiscal_year"],
                "is_consolidated": r["is_consolidated"],
                "conclusion": _fact_conclusion(r["fact_type"], r.get("detail")),
                "quote": ev,
                "context": ev,                        # 원문 보기용(팩트 근거 발췌)
                "section_path": r.get("section_path") or "",
                "dart_url": r.get("dart_url") or "",
                "doc_type": _FACT_DOCTYPE.get(r["fact_type"], "audit"),  # 딥링크 섹션 라우팅 힌트
                "verified": r.get("confidence") == "ok",
                "fact_type": r["fact_type"],
            })
        n_corp = len(corp_set)
        verified_n = sum(1 for it in items if it["verified"])
        if dropped:
            log.info("부정('없음') 사실 %d건 제외", dropped)
        answer = (f"{scope} — 기업 {n_corp}개사 · 사실 {len(items)}건 "
                  f"(사전 정리 목록 전수 조회, 원문 확인 {verified_n}건).{note}")
        return {"understanding": u, "answer": answer, "insufficient": not items,
                "analysis": _roster(items) if n_corp >= 2 else "",   # 다기업 로스터(기업별 설명 금지)
                "items": items, "evidence_count": len(items),
                "verified_count": verified_n, "path": "factstore"}

    def _screen_core(self, u: dict) -> dict:
        """스크리닝 정리 데이터 코어(전수 SQL, 즉시) — 스트리밍 1차 응답에 사용.
        전수가 목적이라 limit 넉넉히(500이면 recall 손실). value_key=구조화 필드 매칭."""
        years = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()] or None
        rows = self.pg.screen(
            fact_types=u.get("screening_fact_types") or None,
            fiscal_years=years, corp_codes=_corp_codes(u),
            detail_like=u.get("screening_detail_contains"),
            detail_exclude=u.get("screening_detail_excludes") or None, limit=5000,
            value_key=_value_key(u.get("screening_fact_types")))
        return self._facts_to_result(rows, u, scope=f"{u.get('industry') or '전체'} 중 조건 해당")

    def _screen(self, question: str, u: dict, k: int = 12,
                per_query_k: int = 10, rrf_k: int = 60, fast: bool = False) -> dict:
        """비스트리밍: 정리 데이터 코어 + 온디맨드 보완(두 방향)."""
        return self._augment_ondemand(question, u, self._screen_core(u), k, per_query_k, rrf_k, fast)

    def _ondemand_supplement(self, question, u, core_items, k=12, per_query_k=10, rrf_k=60):
        """코어(정리 데이터)에 더할 온디맨드 보완 (items, tables) 반환(커버·폴백·중복 제외).
        다기업 코어=커버 안 된 '새 기업'만 / 단일기업 코어=같은 기업 '근거 더'. 스트리밍·비스트리밍 공용."""
        extra = getattr(settings, "screen_ondemand_max", 5)
        corps = {it.get("corp_code") for it in core_items if it.get("corp_code")}
        exclude = corps if len(corps) > 1 else None
        try:
            od = self._vector(question, u, k=k, per_query_k=per_query_k, rrf_k=rrf_k,
                              ondemand=True, exclude_corps=exclude, supplement=True)
        except Exception as e:  # noqa: BLE001  보완 실패는 코어 결과 유지
            log.warning("온디맨드 보완 실패(무시): %s", e)
            return [], []
        seen = {(it.get("corp_code"), _norm(it.get("quote", ""))) for it in core_items}
        out = []
        for it in od.get("items", []):
            if it.get("_fallback"):                 # 구체 항목 못 만든 폴백(보일러플레이트) 제외 → 정밀도↑
                continue
            key = (it.get("corp_code"), _norm(it.get("quote", "")))
            if key in seen:                         # 같은 기업·같은 근거 중복 방지
                continue
            it["source"] = "ondemand"
            it["conclusion"] = "[추가 검색] " + (it.get("conclusion") or "")
            out.append(it)
            seen.add(key)
            if len(out) >= extra:
                break
        return out, od.get("tables", []), od.get("analysis", "")

    def _augment_ondemand(self, question, u, res, k=12, per_query_k=10, rrf_k=60,
                          fast: bool = False) -> dict:
        """비스트리밍 두 방향: 정리 데이터 코어 + 온디맨드 보완. 코어 0건이면 온디맨드가 주답변."""
        if not (settings.screen_ondemand and question) or res is None:
            return res
        if fast or res.get("path") == "disambiguation" or res.get("items") is None:
            return res                              # 채점(fast)·명확화 요청엔 보완 안 함
        if not res["items"]:                        # 정리 데이터 0건 → 온디맨드 주답변
            od = self._vector(question, u, k=k, per_query_k=per_query_k, rrf_k=rrf_k, ondemand=True)
            return od if od.get("items") else res
        for it in res["items"]:
            it.setdefault("source", "factstore")
        supp, supp_tables, supp_analysis = self._ondemand_supplement(question, u, res["items"], k, per_query_k, rrf_k)
        if supp_tables and not res.get("tables"):
            res["tables"] = supp_tables             # 단일기업 재무 숫자 질의 등 → 표 끌어올림
        if supp_analysis and not res.get("analysis"):
            res["analysis"] = supp_analysis         # 정형 코어엔 분석 없음 → 온디맨드 분석 끌어올림
        if supp:
            res["items"] += supp
            base = res.get("path") or "factstore"
            res["path"] = base if base.endswith("ondemand") else base + "+ondemand"
            res["ondemand_added"] = len(supp)
            res["insufficient"] = False
            res["answer"] = (res["answer"].rstrip(".")
                             + f" + 본문에서 추가로 찾은 {len(supp)}건(정리 데이터 외 근거, 검토 권장).")
            res["evidence_count"] = len(res["items"])
            res["verified_count"] = res.get("verified_count", 0) + sum(
                1 for it in supp if it.get("verified"))
            log.info("온디맨드 보완 +%d건 → path=%s", len(supp), res["path"])
        return res

    _ACC_STOP = {"얼마", "얼마야", "잔액", "금액", "알려줘", "보여줘", "기준", "연결", "별도",
                 "최근", "현재", "작년", "올해", "그리고", "얼마나", "어떻게", "얼마인지", "관련"}

    def _known_metric(self, metric) -> bool:
        """정형지표(_FIN_EXPR 17+비율) 지원 여부. 미지원 지표(예: '매출채권')는
        계정 조회(financial_items)로 라우팅하기 위해 False."""
        if not metric:
            return False
        try:
            from src.clients.postgres import PostgresStore
            return str(metric) in PostgresStore._FIN_EXPR
        except Exception:  # noqa: BLE001  판단 불가 → 보수적으로 기존 동작(_numeric 우선)
            return True

    def _account_numeric(self, u: dict, question: str = ""):
        """계정 단위 수치(financial_items 전 계정) — 표준 17지표 밖 계정 질문
        ('매출채권 얼마', '개발비 잔액' 등). 단일 기업 한정, 3개년 추이 격자 + 계정별 항목.
        path='financial_items'. 결과 없으면 None(기존 흐름으로 폴백)."""
        if not self.pg or not u.get("company"):
            return None
        codes = _corp_codes(u)
        if not codes:
            return None
        codes = codes[:3]                             # 동명 접두 다수 방어
        comp = str(u.get("company") or "")
        met = str(u.get("metric") or "")              # 미지원 지표명은 가장 정확한 계정 후보
        terms = ([met] if met and met != comp else []) + [
            str(t) for t in (u.get("key_concepts") or [])
            if t and len(str(t)) >= 2 and str(t) != comp and str(t) != met]
        # key_concepts가 비면 질문에서 직접 후보 추출(회사명·불용어 제거, 긴 토큰 우선)
        if not terms and question:
            toks = _re.findall(r"[가-힣A-Za-z]{2,}", question.replace(comp, " "))
            terms = sorted({t for t in toks if t not in self._ACC_STOP and t not in comp},
                           key=len, reverse=True)[:6]
        if not terms:
            return None
        yrs = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()] or [2025, 2024, 2023]
        yrs = sorted(set(yrs))[:3]
        found, used_term = {}, None                   # account_nm → {year: (amount, is_cons)}
        for t in terms:
            rows = []
            for y in yrs:
                try:
                    rows += self.pg.account_lookup(codes, t, y, limit=40)
                except Exception:  # noqa: BLE001
                    return None
            if rows:
                used_term = t
                for r in rows:
                    found.setdefault(r["account_nm"], {})[int(r["fiscal_year"])] = (
                        r["amount"], r["is_consolidated"])
                break
        if not found:
            return None
        names = self._corp_names()
        corp = names.get(codes[0], u.get("company"))
        accounts = sorted(found.items(), key=lambda kv: -max(a for a, _ in kv[1].values() if a is not None))[:12]
        latest = max(yrs)
        grid_rows = []
        for acct, by_y in accounts:
            cells = [acct] + [(_fmt_won(by_y[y][0]) if y in by_y else "-") for y in yrs]
            grid_rows.append({"cells": cells, "hl": acct == used_term})
        table = {"title": f"{used_term} 관련 계정 추이", "unit": "원(조/억 표기)",
                 "columns": None,  # 아래 grid에 포함
                 "grid": {"columns": ["계정"] + [f"{y}년" for y in yrs], "rows": grid_rows},
                 "src": "xbrl", "verified": True, "corp_name": corp, "fiscal_year": latest,
                 "section_path": "XBRL 정형재무(OpenDART 전체 재무제표)", "dart_url": "",
                 "doc_type": "financial_stmt"}
        items = []
        for acct, by_y in accounts:
            amt, cons = by_y.get(latest, (None, True))
            if amt is None:
                (y0, (amt, cons)) = sorted(by_y.items())[-1]
            trend = " · ".join(f"{y}년 {_fmt_won(by_y[y][0])}" for y in yrs if y in by_y)
            items.append({
                "corp_code": codes[0], "corp_name": corp, "fiscal_year": latest,
                "is_consolidated": bool(cons),
                "conclusion": f"{acct}: {latest}년 {_fmt_won(by_y.get(latest, (amt,))[0]) if latest in by_y else _fmt_won(amt)} ({'연결' if cons else '별도'})",
                "quote": trend, "section_path": "XBRL 정형재무", "dart_url": "",
                "verified": True, "fact_type": "정형재무",
            })
        answer = (f"{corp}의 '{used_term}' 관련 계정 {len(accounts)}건 — "
                  f"XBRL 정형재무 FY{yrs[0]}~{yrs[-1]} 기준(연결 우선).")
        log.info("경로=financial_items · corp=%s term=%s 계정 %d건", corp, used_term, len(accounts))
        return {"understanding": u, "answer": answer, "insufficient": False,
                "items": items, "tables": [table], "evidence_count": len(items),
                "verified_count": len(items), "path": "financial_items"}

    def _numeric(self, u: dict):
        """계산형/순위 → 정형재무(XBRL) SQL. 원지표·비율(부채비율/영업이익률/ROE 등). path='financials'."""
        metric = u.get("metric")
        years = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()]
        year = years[0] if years else 2024
        # LLM이 op를 '&lt;'(HTML 이스케이프)·전각(＜)으로 줄 때가 있음 → 정규화(라벨·SQL 필터 모두 영향)
        op_norm = {"&lt;": "<", "&gt;": ">", "&le;": "<=", "&ge;": ">=",
                   "＜": "<", "＞": ">", "≤": "<=", "≥": ">="}
        nop = (u.get("numeric_op") or "").strip()
        nop = op_norm.get(nop, nop) or None
        u["numeric_op"] = nop
        rows = self.pg.financials_screen(
            metric, order=u.get("numeric_order") or "상위", n=u.get("numeric_n") or 10,
            op=nop, value=u.get("numeric_value"),
            year=year, corp_codes=_corp_codes(u))
        if not rows:
            return None
        names = self._corp_names()
        items = []
        for r in rows:
            q = (f"자산 {_fmt_won(r.get('자산총계'))} · 부채 {_fmt_won(r.get('부채총계'))} · "
                 f"자본 {_fmt_won(r.get('자본총계'))} · 매출 {_fmt_won(r.get('매출액'))} · "
                 f"영업이익 {_fmt_won(r.get('영업이익'))} · 순이익 {_fmt_won(r.get('당기순이익'))}")
            items.append({
                "corp_code": r["corp_code"],
                "corp_name": names.get(r["corp_code"], r["corp_code"]),
                "fiscal_year": year, "is_consolidated": True,
                "conclusion": f"{metric} {_fmt_metric(metric, r.get('val'))}",
                "quote": q, "section_path": "재무제표 수치",
                "dart_url": "", "verified": True, "fact_type": "정형재무",
            })
        if u.get("numeric_op") and u.get("numeric_value") is not None:
            opmap = {">": "초과", "<": "미만", ">=": "이상", "<=": "이하"}
            unit = "%" if metric in _RATIO_METRICS else ""
            label = f"{metric} {u['numeric_value']}{unit} {opmap.get(u['numeric_op'], u['numeric_op'])}"
        else:
            label = f"{metric} {u.get('numeric_order') or '상위'}"
        answer = (f"{u.get('industry') or ''} {label} — {len(items)}개사 "
                  f"(FY{year} 재무제표 수치)").strip()
        log.info("경로=정형재무 · metric=%s order=%s n=%d", metric, u.get("numeric_order"), len(items))
        return {"understanding": u, "answer": answer, "insufficient": not items,
                "items": items, "evidence_count": len(items),
                "verified_count": len(items), "path": "financials"}

    def _graph(self, u: dict):
        """관계·다홉 순회(지식그래프): 감사인 교체 · 동일감사인 · 감사인 경유(3홉) · 특수관계자망. path='graph'."""
        rel = u.get("graph_relation")
        if rel == "특수관계자" and u.get("company"):
            return self._graph_parties(u)
        names = self._corp_names()
        years = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()]
        curr = years[-1] if years else 2025
        prev = years[0] if len(years) > 1 else curr - 1
        if rel == "감사인교체":
            rows = self.pg.auditor_transitions(
                prev_year=prev, curr_year=curr, from_auditor=u.get("from_auditor"),
                to_auditor=u.get("to_auditor"), corp_codes=_corp_codes(u))
            items = [{"corp_code": r["corp_code"], "corp_name": names.get(r["corp_code"], r["corp_code"]),
                      "fiscal_year": curr, "is_consolidated": True,
                      "conclusion": f"감사인 교체: {r['prev_aud']} → {r['curr_aud']}",
                      "quote": f"{prev}년 {r['prev_aud']} → {curr}년 {r['curr_aud']}",
                      "section_path": "감사인(연도별 비교)", "dart_url": "",
                      "verified": True, "fact_type": "감사인교체"} for r in rows]
            scope = (f"{u['to_auditor']}(으)로 " if u.get("to_auditor") else "") + \
                    (f"{u['from_auditor']}에서 " if u.get("from_auditor") else "") + "감사인을 바꾼 기업"
            log.info("경로=지식그래프 감사인교체 · %d개사", len(items))
            return {"understanding": u, "answer": f"{scope} — {len(items)}개사 (감사인 변경 {prev}→{curr} 비교)",
                    "insufficient": not items, "items": items, "evidence_count": len(items),
                    "verified_count": len(items), "path": "graph"}
        if rel in ("동일감사인", "감사인경유") and u.get("company"):
            codes, cand = self._resolve_company(u["company"])
            if len(cand) > 1:
                return {"understanding": u, "path": "disambiguation",
                        "answer": f"'{u['company']}'에 해당하는 기업이 여럿입니다: {', '.join(cand[:10])}.",
                        "items": [], "evidence_count": 0, "verified_count": 0, "candidates": cand}
            if not codes:
                return None
            if rel == "동일감사인":
                peers = self.pg.peers_by_auditor(codes[0], year=curr)
                if not peers:
                    return None
                rows = self.pg.screen(fact_types=["감사인_보수"], corp_codes=peers,
                                      fiscal_years=[curr], limit=2000)
                log.info("경로=지식그래프 동일감사인(%s) · 피어 %d", u["company"], len(peers))
                res = self._facts_to_result(rows, u, scope=f"{u['company']}와 같은 감사인을 쓰는 기업")
                res["path"] = "graph"
                return res
            # 감사인경유(3홉)
            fts = u.get("screening_fact_types")
            rows = self.pg.via_auditor_facts(codes[0], fts, year=curr,
                        detail_like=u.get("screening_detail_contains"), value_key=_value_key(fts))
            if not rows:
                return None
            log.info("경로=지식그래프 감사인경유3홉(%s) · %d건", u["company"], len(rows))
            res = self._facts_to_result(rows, u, scope=f"{u['company']} 감사인의 다른 고객사 중 조건 해당")
            res["path"] = "graph"
            return res
        return None

    def _graph_parties(self, u: dict):
        """특수관계자 네트워크(지식그래프 확장): 기업의 특수관계자(엣지 목록) + 상대·집단 공유 피어(다홉)."""
        codes, cand = self._resolve_company(u["company"])
        if len(cand) > 1:
            return {"understanding": u, "path": "disambiguation",
                    "answer": f"'{u['company']}'에 해당하는 기업이 여럿입니다: {', '.join(cand[:10])}.",
                    "items": [], "evidence_count": 0, "verified_count": 0, "candidates": cand}
        if not codes:
            return None
        code = codes[0]
        edges = self.pg.related_parties_of(code)
        if not edges:
            return None                       # 엣지 없음 → 벡터 폴백(특수관계자 서술 검색)
        names = self._corp_names()
        me = names.get(code, code)
        items = []
        for e in edges:
            rel = e.get("relationship") or "특수관계자"
            txn = e.get("txn_type") or ""
            grp = f" · 집단:{e['group_name']}" if e.get("group_name") else ""
            items.append({
                "corp_code": code, "corp_name": me,
                "fiscal_year": e.get("fiscal_year"), "is_consolidated": True,
                "conclusion": f"특수관계자: {e['party_name']} ({rel}{('/' + txn) if txn else ''}){grp}",
                "quote": (e.get("evidence") or "")[:300],
                "section_path": "특수관계자 주석", "dart_url": e.get("dart_url") or "",
                "verified": True, "fact_type": "특수관계자망",
            })
        peers = self.pg.related_party_peers(code)
        note = ""
        if peers:
            pnames = [names.get(p["corp_code"], p["corp_code"]) for p in peers[:5]]
            note = f" · 상대·집단을 공유하는 기업 {len(peers)}개사(예: {', '.join(pnames)})"
        log.info("경로=지식그래프 특수관계자망(%s) · 엣지 %d · 피어 %d", u["company"], len(items), len(peers))
        return {"understanding": u,
                "answer": f"{me}의 특수관계자 {len(items)}곳 (관계 분석){note}",
                "insufficient": not items, "items": items, "evidence_count": len(items),
                "verified_count": len(items), "path": "graph"}

    def _resolve_company(self, name: str) -> tuple:
        """기업명 → (corp_codes, 후보명 목록). 정확일치 → 부분일치 → 역포함
        (LLM이 공식명 'KB금융지주'로 답해도 유니버스명 'KB금융'에 매칭, 3자 이상만)."""
        rows = _universe_rows()
        exact = [r for r in rows if (r.get("corp_name") or "") == name]
        pick = (exact or [r for r in rows if name and name in (r.get("corp_name") or "")]
                or [r for r in rows if name and len(r.get("corp_name") or "") >= 3
                    and (r.get("corp_name") or "") in name])
        codes = [r["corp_code"] for r in pick]
        cand_names = sorted({r.get("corp_name") for r in pick if r.get("corp_name")})
        return codes, cand_names

    def _single_company(self, u: dict):
        """§7 단일 기업 라우팅 — Fact Store로 답하거나 None(→벡터 폴백)."""
        name = u["company"]
        codes, cand_names = self._resolve_company(name)
        if len(cand_names) > 1:  # 동명이의/계열사 → 명확화 요청
            log.info("단일기업 다중후보(%d) → 명확화", len(cand_names))
            return {"understanding": u, "path": "disambiguation",
                    "answer": f"'{name}'에 해당하는 기업이 여럿입니다: {', '.join(cand_names[:10])}. "
                              f"어느 기업인지 구체적으로 알려주세요.",
                    "items": [], "evidence_count": 0, "verified_count": 0,
                    "candidates": cand_names}
        if not codes:
            return None  # 유니버스에 없음 → 벡터 폴백
        kind = u.get("single_kind")
        fact_types = u.get("screening_fact_types")
        # 자유 주제(자유) or 표준 Fact로 매핑 안 된 특정 주제(단일사실+무매핑, 예: 이연법인세·정부보조금)
        # → 12종 Fact Store 밖이므로 벡터(회사 스코프)로. 프로파일/추이는 정형 Fact 유지.
        if not fact_types and kind in ("자유", "단일사실"):
            log.info("단일기업 '%s'(표준Fact 밖) → 벡터 폴백", kind)
            return None
        years = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()] or None
        rows = self.pg.screen(fact_types=fact_types or None, corp_codes=codes,
                              fiscal_years=years,
                              detail_like=u.get("screening_detail_contains"),
                              detail_exclude=u.get("screening_detail_excludes") or None, limit=200,
                              value_key=_value_key(fact_types))
        if not rows:
            log.info("단일기업 Fact 없음 → 벡터 폴백")
            return None
        note = " (과거 연도 자료는 추후 반영 예정)" if kind == "추이" else ""
        label = "항목 조회" if fact_types else "종합 정보"
        log.info("경로=FactStore 단일기업(%s) · %s · %d건", label, name, len(rows))
        return self._facts_to_result(rows, u, scope=f"{name} {label}", note=note)


def cli_run(question: str, k: int = 12):
    eng = QueryEngine()
    res = eng.run(question, k=k)
    u = res["understanding"]
    print("\n" + "=" * 70)
    print(f"질문: {question}")
    print(f"이해: 의도={u.get('intent')} · 산업={u.get('industry')} · "
          f"연결={u.get('is_consolidated')} · 멀티쿼리={len(u.get('expanded_queries') or [])}")
    print(f"근거 {res['evidence_count']}개 · 인용검증 {res.get('verified_count',0)}/{len(res['items'])}")
    print(f"\n[답] {res['answer']}")
    if res.get("insufficient"):
        print("  (근거 불충분)")
    for it in res["items"]:
        mark = "✓검증" if it.get("verified") else "✗미검증"
        basis = "연결" if it.get("is_consolidated") else "별도"
        print(f"\n • {it.get('corp_name','')} ({it.get('fiscal_year','')}·{basis}) [{mark}]")
        print(f"   결론: {it.get('conclusion','')}")
        print(f"   근거: {' '.join((it.get('quote') or '').split())[:150]}")
        print(f"   위치: {it.get('section_path','')}")
        if it.get("dart_url"):
            print(f"   링크: {it['dart_url']}")
    print("=" * 70)
    return res
