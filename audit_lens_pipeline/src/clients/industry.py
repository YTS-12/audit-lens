"""감사렌즈 자체 산업분류 로더 — 분류1(11) · 분류2(59) 2단 계층 + 기업별 다중 라벨.

데이터: data/v1/meta/industry_map.json (final_v11 확정본에서 생성, 활성 라벨만).
체계 정의는 industry_taxonomy.TAXONOMY가 단일 출처다(코드·명칭·감사쟁점·포함범위).

설계 원칙(구 WICS 로더와 다른 점):
- 기업당 분류가 1개가 아니라 다중 라벨(중요도 핵심/보조/노출 + 산업관계).
- 필터·업종 인식의 기본 범위는 핵심+보조. '노출'(전방산업·투자)은 기본 제외 —
  철강을 '취급'하는 상사가 철강 검색에 섞여 나오지 않게 하기 위함이다.
"""
from __future__ import annotations

import json
from functools import lru_cache

from config import settings
from src.clients.industry_taxonomy import TAXONOMY

_TIER = {"핵심": 0, "보조": 1, "노출": 2}
_DEFAULT_MAX_TIER = 1          # 핵심+보조


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads((settings.meta_dir / "industry_map.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def available() -> bool:
    return bool(_load().get("map"))


@lru_cache(maxsize=1)
def _name_index() -> dict:
    return {v["corp_name"]: cc for cc, v in _load().get("map", {}).items()}


def labels_of(corp_code: str) -> list[dict]:
    v = _load().get("map", {}).get(corp_code)
    return v["labels"] if v else []


def brief(corp_code: str, corp_name: str = "") -> dict | None:
    """UI 표시용 요약. 다중 라벨을 중요도순으로 제공한다."""
    cc = corp_code or _name_index().get(corp_name, "")
    labs = labels_of(cc)
    if not labs:
        return None
    core = [l for l in labs if l["materiality"] == "핵심"]
    return {
        "labels": [{"code": l["code"], "cls1": l["cls1_name"], "cls2": l["cls2_name"],
                    "materiality": l["materiality"], "rel": l["industry_rel"],
                    "entity": l["entity"], "basis": l["evidence"]} for l in labs],
        "core": " · ".join(l["cls2_name"] for l in core),
        "source": f"감사렌즈 자체 분류 {_load().get('version', '')}".strip(),
    }


def corp_codes_for(cls1: str = "", cls2: str = "", max_tier: int = _DEFAULT_MAX_TIER) -> set | None:
    """선택한 분류(분류1 영문 1자 / 분류2 영문+2자리) → corp_code 집합.

    max_tier: 0=핵심만, 1=핵심+보조(기본), 2=노출 포함 전체.
    """
    cls1, cls2 = (cls1 or "").strip(), (cls2 or "").strip()
    if not (cls1 or cls2):
        return None
    hit = set()
    for cc, v in _load().get("map", {}).items():
        for l in v["labels"]:
            if _TIER[l["materiality"]] > max_tier:
                continue
            if cls2 and l["code"] == cls2:
                hit.add(cc)
                break
            if cls1 and not cls2 and l["cls1"] == cls1:
                hit.add(cc)
                break
    return hit


# ── 질문 문장 속 업종의 자동 인식 ──────────────────────────────
# 값: 분류2 코드 | 분류1 영문 1자 | 코드 리스트(합집합). 명칭은 전부 자체 체계 기준.
_ALIAS: dict[str, object] = {
    "조선": "A02", "조선소": "A02", "중공업": "A02", "조선기자재": "A02",
    "건설": "A01", "건설사": "A01", "플랜트": "A03", "엔지니어링": "A03",
    "방산": "A04", "방위산업": "A04", "항공우주": "A04",
    "반도체": "B01", "칩": "B01", "파운드리": "B01",
    "디스플레이": "B02", "패널": "B02",
    "정유": "B03", "석유": "B03", "화학": "B04", "석유화학": "B04",
    "철강": "B05", "제철": "B05", "비철": "B06", "비철금속": "B06", "금속": "B06",
    "시멘트": "B07", "건자재": "B07", "건축자재": "B07",
    "제지": "B08", "종이": "B08", "목재": "B08", "포장재": "B09", "포장": "B09",
    "완성차": "C01", "자동차부품": "C02", "차부품": "C02", "부품사": "C02",
    "자동차": ["C01", "C02"],
    "전자부품": "C03", "2차전지": "C03", "이차전지": "C03", "배터리": "C03",
    "기계": "C04", "공작기계": "C04", "전기장비": "C05", "전선": "C05",
    "가전": "C06", "전자": ["C03", "C06"],
    "제약": "D01", "신약": "D01", "바이오": "D02", "제약바이오": ["D01", "D02"],
    "의료기기": "D03", "헬스케어": "D",
    "식품": "E01", "음료": "E01", "식음료": "E01", "제과": "E01", "담배": "E02",
    "화장품": "E03", "의류": "E04", "섬유": "E04", "신발": "E04", "패션": "E04",
    "생활용품": "E05", "가구": "E05",
    "백화점": "E06", "편의점": "E06", "마트": "E06", "유통": ["E06", "E07", "E08"],
    "홈쇼핑": "E07", "이커머스": "E07", "상사": "E08", "무역": "E08",
    "게임": "F01", "게임사": "F01", "플랫폼": "F02", "포털": "F02",
    "엔터": "F03", "엔터테인먼트": "F03", "미디어": "F03", "방송": "F03", "콘텐츠": "F03",
    "광고": "F04", "소프트웨어": "F05", "IT서비스": "F05", "SI": "F05",
    "교육": "F06", "호텔": "F07", "레저": "F07", "여행": "F07", "카지노": "F07",
    "외식": "F07", "프랜차이즈": "F07",
    "경비": "F08", "시설관리": "F08", "렌탈": "F09", "리스": "F09",
    "신용평가": "F10",
    "통신": "G01", "통신사": "G01", "이동통신": "G01",
    "전력": "G02", "발전": "G02", "한전": "G02", "가스": "G03", "환경": "G04", "수도": "G04",
    "항공": "H01", "항공사": "H01", "해운": "H02", "해운사": "H02",
    "물류": "H03", "택배": "H03", "운송": "H03", "항만": "H04", "터미널": "H04",
    "은행": "I01", "증권": "I02", "증권사": "I02", "보험": "I03", "손보": "I03", "생보": "I03",
    "카드": "I04", "캐피탈": "I04", "여신": "I04", "저축은행": "I04",
    "핀테크": "I05", "결제": "I05", "페이": "I05",
    "지주": "J", "지주사": "J", "지주회사": "J", "금융지주": "J01",
    "부동산": "K", "부동산개발": "K01", "분양": "K01", "디벨로퍼": "K01",
    "임대": "K02", "리츠": "K03", "신탁": "K03",
    "금융": "I", "소비재": "E", "운송인프라": "H04",
}
_SUFFIX = ("산업", "업종", "업체", "회사", "기업", "종목", "관련주", "사", "업", "주")


@lru_cache(maxsize=1)
def _cls_indexes() -> tuple[dict, dict]:
    """(분류2명→코드, 분류1명→영문자). 공백 제거 정규화명 기준."""
    c2, c1 = {}, {}
    for k, node in TAXONOMY.items():
        c1[node["name"].replace(" ", "")] = k
        for code, name, _d in node["sub"]:
            c2[name.replace(" ", "")] = code
    return c2, c1


def _codes_to_set(sel) -> set:
    keys = sel if isinstance(sel, list) else [sel]
    out: set = set()
    for k in keys:
        got = corp_codes_for(cls1=k) if len(k) == 1 else corp_codes_for(cls2=k)
        out |= got or set()
    return out


def _label_for(sel) -> str:
    keys = sel if isinstance(sel, list) else [sel]
    names = []
    for k in keys:
        if len(k) == 1:
            names.append(TAXONOMY[k]["name"])
        else:
            for code, name, _d in TAXONOMY[k[0]]["sub"]:
                if code == k:
                    names.append(name)
    return "·".join(names)


def resolve_industry(text: str):
    """질문에서 추출된 업종 문자열 → (corp_code 집합, 표시라벨, 계층키) | None.

    KRX 온톨로지 실패 시의 폴백. 핵심+보조 라벨만 스코프에 넣는다."""
    if not text or not available():
        return None
    q = text.replace(" ", "").strip()
    if q not in _ALIAS:
        for suf in _SUFFIX:                        # "조선사"→"조선"
            if q.endswith(suf) and len(q) > len(suf) + 1:
                q = q[: -len(suf)]
                break
    sel = _ALIAS.get(q)
    if sel is None:
        c2, c1 = _cls_indexes()
        sel = c2.get(q) or c1.get(q)
    if sel is None:                                # 부분 일치(2자 이상, 분류2 우선)
        c2, c1 = _cls_indexes()
        cands = [(n, c) for n, c in c2.items() if len(q) >= 2 and (q in n or n in q)]
        cands += [(n, c) for n, c in c1.items() if len(q) >= 2 and (q in n or n in q)]
        if not cands:
            return None
        _, sel = min(cands, key=lambda x: abs(len(x[0]) - len(q)))
    codes = _codes_to_set(sel)
    if not codes:
        return None
    key = sel if isinstance(sel, str) else "+".join(sel)
    return codes, _label_for(sel), key


def taxonomy_ui() -> dict:
    """UI 2단 드롭다운용 계층(분류1 → 분류2). 각 분류1에 공통 감사쟁점 포함."""
    data = _load()
    cls1 = []
    for k, node in TAXONOMY.items():
        cls1.append({"code": k, "name": node["name"], "audit_focus": node["audit_focus"],
                     "sub": [{"code": c, "name": n} for c, n, _d in node["sub"]]})
    return {"as_of": data.get("as_of"), "version": data.get("version"),
            "source": "감사렌즈 자체 산업분류(감사쟁점 기준)", "cls1": cls1}
