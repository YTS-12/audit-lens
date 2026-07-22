"""회계·감사 온톨로지 v1 로더(경량 통제 어휘).

질의이해(LLM) 출력 뒤에 붙는 **정규화 단계**의 지식원:
- resolve_sector: 업종 별칭 → 정확 KRX sector명 (substring 충돌 방지: 금속≠비금속, 의약품→제약)
- negation_excludes: '~가 아닌/제외' → 분류체계로 집합 제외값 유도 (비적정→[적정], 빅4아닌→[삼일..])
- expand_detail: 값 동의어 확장 (진행기준→[진행기준,기간에 걸쳐,진행률])
- is_open: '사례/경향' 등 개방형 탐색 감지 → 벡터 경로 선호

파일이 없거나 깨져도 무해(전부 no-op 폴백)하게 동작한다.
설계 §5.8: 온톨로지는 멀티쿼리와 Fact 추출을 동시에 구동하는 공용 자산.
"""
from __future__ import annotations
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "ontology" / "audit_ontology.yaml"


class Ontology:
    def __init__(self, path: str | Path | None = None):
        self.concepts, self.industries = {}, {}
        self.neg_markers, self.open_markers = [], []
        self._alias2sector: dict[str, str] = {}
        try:
            import yaml
            d = yaml.safe_load(Path(path or _DEFAULT_PATH).read_text(encoding="utf-8"))
            self.concepts = d.get("concepts", {}) or {}
            self.industries = d.get("industries", {}) or {}
            self.neg_markers = d.get("negation_markers", []) or []
            self.open_markers = d.get("open_markers", []) or []
            for key, v in self.industries.items():
                sec = v.get("sector")
                if not sec:
                    continue
                self._alias2sector[key] = sec
                self._alias2sector[sec] = sec
                for a in v.get("aliases", []) or []:
                    self._alias2sector[a] = sec
            log.info("온톨로지 로드: 개념 %d · 업종 %d", len(self.concepts), len(self.industries))
        except Exception as e:  # noqa: BLE001
            log.warning("온톨로지 로드 실패(폴백 동작): %s", e)

    # ── 업종 정규화 ──
    def resolve_sector(self, industry: str | None) -> str | None:
        if not industry:
            return None
        cand = [industry, industry.replace("업", "").strip()]
        for k in cand:
            if k in self._alias2sector:
                return self._alias2sector[k]
        # 별칭이 질의에 포함되면 매칭(긴 별칭 우선)
        for a in sorted(self._alias2sector, key=len, reverse=True):
            if a and (a in industry):
                return self._alias2sector[a]
        return None

    # ── 부정형 → 제외 집합 ──
    def negation_excludes(self, question: str, fact_types) -> list[str]:
        if not question or not any(m in question for m in self.neg_markers):
            return []
        exc: list[str] = []
        for ft in (fact_types or []):
            c = self.concepts.get(ft, {})
            for gname, members in (c.get("groups") or {}).items():
                if gname in question:                 # "빅4가 아닌"
                    exc += members
            exc += (c.get("positive") or [])          # 감사의견 "비적정" → 적정 제외
        return list(dict.fromkeys(exc))

    # ── 값 동의어 확장(OR 매칭용 리스트) ──
    def expand_detail(self, fact_types, detail):
        if not detail:
            return detail
        for ft in (fact_types or []):
            for canon, syns in (self.concepts.get(ft, {}).get("value_synonyms") or {}).items():
                if canon in str(detail) or any(s in str(detail) or str(detail) in s for s in syns):
                    return syns
        return detail

    def is_open(self, question: str) -> bool:
        return bool(question) and any(m in question for m in self.open_markers)

    # 유사도('비슷한/유사한 X') — 범주 매칭(Fact Store)이 아니라 의미 유사도(벡터)로 처리해야 하는 신호
    _SIM_MARKERS = ("비슷", "유사", "닮")

    def is_similarity(self, question: str) -> bool:
        return bool(question) and any(m in question for m in self._SIM_MARKERS)
