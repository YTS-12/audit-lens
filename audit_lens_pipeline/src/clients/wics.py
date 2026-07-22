"""WICS(FnGuide WISE 산업분류) 로더 — 대/중/소 3단계 계층 + 기업별 분류·출처·근거.

데이터: data/v1/meta/wics_map.json(기업→분류·출처·근거), wics_taxonomy.json(계층).
- 출처: 'WMI500'(FnGuide 공식) 또는 'LLM추정'(회사명·KRX업종 기반, 근거·확신도 기록).
- 필터: 대/중/소 코드 선택 → 해당 corp_code 집합.
"""
from __future__ import annotations
import json
from functools import lru_cache
from config import settings


@lru_cache(maxsize=1)
def _load():
    md = settings.meta_dir
    m, tax = {}, {}
    try:
        m = json.loads((md / "wics_map.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    try:
        tax = json.loads((md / "wics_taxonomy.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return m, tax


def available() -> bool:
    m, _ = _load()
    return bool(m)


@lru_cache(maxsize=1)
def _name_index() -> dict:
    return {v.get("corp_name"): cc for cc, v in _load()[0].items() if v.get("corp_name")}


def wics_of(corp_code: str) -> dict | None:
    """기업의 WICS 분류·출처·근거 레코드."""
    return _load()[0].get(corp_code)


@lru_cache(maxsize=1)
def _gcode_index() -> dict:
    """G코드 → (WI대명, WI소명, G명, WI대코드, WI소코드). WI26 트리에서 파생(라벨 단일 출처)."""
    _, tax = _load()
    idx = {}
    for dc, d in (tax.get("tree") or {}).items():
        for jc, j in d.get("mid", {}).items():
            for s in j.get("sub", []):
                idx[s["code"]] = (d["name"], j["name"], s["name"], dc, jc)
    return idx


def brief(corp_code: str, corp_name: str = "") -> dict | None:
    """UI 표시용 요약: 대/소/WICS소·출처·확신도·근거. 라벨은 WI26 트리에서 파생(공식 명칭)."""
    v = wics_of(corp_code) if corp_code else None
    if not v and corp_name:
        v = wics_of(_name_index().get(corp_name, ""))
    if not v:
        return None
    g = _gcode_index().get(v.get("code") or "")
    dae, jung, so = (g[0], g[1], g[2]) if g else (v.get("dae"), v.get("jung"), v.get("so"))
    return {"dae": dae, "jung": jung, "so": so,
            "code": ("G" + v["code"]) if v.get("code") else None,
            "source": "공식(WMI500)" if v.get("source") == "WMI500" else v.get("source"),
            "confidence": v.get("confidence"),
            "basis": v.get("rationale") or v.get("basis")}


def _gset_for(dae: str = "", jung: str = "", so: str = "") -> set | None:
    """선택(WI대코드 'WI###' / WI소코드 'WI#####' / G코드 6자리) → 해당 G코드 집합."""
    _, tax = _load()
    tree = tax.get("tree") or {}
    if so:
        return {so}
    if jung:
        for d in tree.values():
            j = d.get("mid", {}).get(jung)
            if j:
                return {s["code"] for s in j.get("sub", [])}
        return set()
    if dae:
        d = tree.get(dae)
        if not d:
            return set()
        return {s["code"] for j in d.get("mid", {}).values() for s in j.get("sub", [])}
    return None


def corp_codes_for(dae: str = "", jung: str = "", so: str = "") -> set | None:
    """선택한 WI26 계층(대 WI###/소 WI#####/WICS소 G 6자리)에 해당하는 corp_code 집합."""
    dae, jung, so = (dae or "").strip(), (jung or "").strip(), (so or "").strip()
    if not (dae or jung or so):
        return None
    gset = _gset_for(dae, jung, so)
    if not gset:
        return set()
    return {cc for cc, v in _load()[0].items() if (v.get("code") or "") in gset}


# ── 질문 문장 속 업종의 WICS 자동 인식 ──
# 계층명 자체(소>중>대 우선) + 일상 별칭. KRX 온톨로지가 못 잡는 정밀 업종(조선·부품·반도체 등) 커버.
_ALIAS = {
    "조선": "조선", "조선소": "조선", "중공업": "조선",
    "자동차부품": "자동차부품", "차부품": "자동차부품", "부품사": "자동차부품",
    "완성차": "251020", "자동차": "자동차",           # '자동차'=WI300 대분류(완성차+부품)
    "반도체": "반도체", "칩": "반도체와반도체장비",
    "디스플레이": "디스플레이", "패널": "디스플레이패널",
    "게임": "게임소프트웨어", "게임사": "게임소프트웨어",
    "엔터": "방송과엔터테인먼트", "엔터테인먼트": "방송과엔터테인먼트", "미디어": "미디어",
    "바이오": "제약", "제약바이오": "건강관리", "신약": "제약",  # 코스피 바이오 대형사 공식분류=제약
    "은행": "은행", "증권": "증권", "증권사": "증권",
    "손보": "손해보험", "생보": "생명보험", "보험": "보험", "카드": "카드",
    "항공": "항공사", "항공사": "항공사", "해운": "해운사", "해운사": "해운사",
    "물류": "항공화물운송과물류", "방산": "우주항공과국방", "방위산업": "우주항공과국방",
    "철강": "철강", "화학": "화학", "정유": "석유와가스", "석유화학": "화학",
    "통신": "통신서비스", "통신사": "통신서비스", "이동통신": "무선통신서비스",
    "소프트웨어": "소프트웨어", "IT서비스": "IT서비스", "전자": "전자장비와기기",
    "화장품": "화장품", "의류": "의류", "섬유": "의류", "신발": "의류",
    "호텔": "호텔,레저", "레저": "호텔,레저", "여행": "호텔,레저", "백화점": "백화점",
    "유통": "소매(유통)", "식품": "식품", "음료": "음료", "제과": "식품",
    "전력": "전기유틸리티", "가스": "가스유틸리티", "유틸리티": "유틸리티",
    "기계": "기계", "건자재": "건축자재", "건축자재": "건축자재", "가구": "가구",
    "제지": "종이와목재", "비철": "비철금속", "금속": "비철금속",
    "전기장비": "전기장비", "2차전지": "전기장비",
    "헬스케어": "건강관리", "의료기기": "건강관리장비와용품", "제약": "제약",
    "상사": "상사", "무역": "무역회사와판매업체", "부동산": "부동산",
    "핸드셋": "핸드셋", "휴대폰": "핸드셋", "컴퓨터": "컴퓨터",
    "교육": "교육", "출판": "출판", "광고": "광고", "벤처캐피탈": "창업투자",
}

_SUFFIX = ("산업", "업종", "업체", "회사", "기업", "종목", "관련주", "사", "업", "주")


@lru_cache(maxsize=1)
def _wics_name_index() -> dict:
    """정규화명 → 계층키(WI대 'WI###'/WI소 'WI#####'/G 6자리). 이름 충돌 시 상위(대분류)가 이김
    — '자동차'·'건설'처럼 여러 레벨에 같은 이름이 있으면 더 포괄적인 집합이 재현율에 안전."""
    _, tax = _load()
    idx: dict[str, str] = {}
    tree = tax.get("tree", {})
    for dc, d in tree.items():
        for jc, j in d.get("mid", {}).items():
            for s in j.get("sub", []):
                idx[s["name"].replace(" ", "")] = s["code"]
    for dc, d in tree.items():
        for jc, j in d.get("mid", {}).items():
            idx[j["name"].replace(" ", "")] = jc
    for dc, d in tree.items():
        idx[d["name"].replace(" ", "")] = dc
    return idx


def _level_kw(code: str) -> dict:
    """계층키 → corp_codes_for 인자."""
    if code.startswith("WI") and len(code) == 5:
        return {"dae": code}
    if code.startswith("WI"):
        return {"jung": code}
    return {"so": code}


def resolve_industry(text: str):
    """질문에서 추출된 업종 문자열 → (corp_code 집합, 표시라벨, 계층키) | None.
    KRX 온톨로지 실패 시의 폴백으로 호출된다(기존 검증된 업종 동작은 불변)."""
    if not text or not available():
        return None
    q = text.replace(" ", "").strip()
    for suf in _SUFFIX:                            # "조선사"→"조선", "반도체업체"→"반도체"
        if q.endswith(suf) and len(q) > len(suf) + 1:
            q = q[: -len(suf)]
            break
    q = _ALIAS.get(q, q)
    idx = _wics_name_index()
    name = q.replace(" ", "")
    code = idx.get(name)
    if not code and name.isdigit() and len(name) == 6:  # 별칭이 G코드 직접 지정("완성차")
        code = name
        g = _gcode_index().get(name)
        name = g[2] if g else name
    if not code:                                   # 부분 일치(양방향, 2자 이상) — 정밀(G>WI소>WI대) 우선
        rank = {6: 3}
        cands = [(n, c) for n, c in idx.items()
                 if len(name) >= 2 and (name in n or n in name)]
        if not cands:
            return None
        def _prec(c):                              # G(6자리)=3 > WI소(7자)=2 > WI대(5자)=1
            return 3 if not c.startswith("WI") else (2 if len(c) == 7 else 1)
        name, code = max(cands, key=lambda x: (_prec(x[1]), -abs(len(x[0]) - len(name))))
    codes = corp_codes_for(**_level_kw(code))
    if not codes:
        return None
    return codes, name, code


def taxonomy_ui() -> dict:
    """UI 3단 드롭다운용 계층(대분류 → 중분류 → 소분류, 정렬)."""
    _, tax = _load()
    tree = tax.get("tree", {})
    dae = []
    for dcode in sorted(tree):
        d = tree[dcode]
        jung = []
        for jcode in sorted(d.get("mid", {})):
            j = d["mid"][jcode]
            subs = sorted(j.get("sub", []), key=lambda s: s["code"])
            jung.append({"code": jcode, "name": j["name"], "so": subs})
        dae.append({"code": dcode, "name": d["name"], "jung": jung})
    return {"as_of": tax.get("as_of"), "source": tax.get("source", "FnGuide WMI500 (WICS)"),
            "dae": dae}
