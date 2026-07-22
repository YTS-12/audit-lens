"""Claude API 래퍼 — 질의이해(Haiku)·근거기반 합성(Sonnet).
구조화 출력은 **tool use**로 강제(자유 JSON 파싱의 따옴표 깨짐 회피). 키/모델은 config.
"""
from __future__ import annotations
import json
import logging
import re
import time
from anthropic import Anthropic, APIStatusError, APIConnectionError
from config import settings

log = logging.getLogger(__name__)

_LEAK_RE = re.compile(r"</?(analysis|answer|items)>", re.I)


def _sanitize_synth(d: dict) -> dict:
    """합성 드리프트 가드: 강제 tool use에서도 드물게 문자열 필드 안에 응답 전체를
    태그 형식(…</analysis> <answer>…</answer> <items>[…])으로 재서술하는 사례 관측
    (두산에너빌리티 우발부채, 2026-07). 정규식으로 필드를 회수하고 태그를 제거한다 — 무API·결정적."""
    a, n = d.get("answer") or "", d.get("analysis") or ""
    bad_a, bad_n = bool(_LEAK_RE.search(a)), bool(_LEAK_RE.search(n))
    if not (bad_a or bad_n):
        return d
    blob = "\n".join(x for x, bad in ((n, bad_n), (a, bad_a)) if bad)
    m = re.search(r"<answer>\s*(.*?)\s*(?:</answer>|<items>|$)", blob, re.S | re.I)
    salvaged_ans = m.group(1).strip() if m else ""
    head = re.split(r"<answer>", blob, 1, flags=re.I)[0]           # 태그 앞 = 분석 서술부
    head = re.split(r"<items>", head, 1, flags=re.I)[0]
    salvaged_ana = _LEAK_RE.sub("", head).strip()
    if not d.get("items"):                                          # items는 정상 파싱이 보통 — 비었을 때만
        mi = re.search(r"<items>\s*(\[.*?)\s*(?:</items>|$)", blob, re.S | re.I)
        if mi:
            try:
                got = json.loads(mi.group(1))
                if isinstance(got, list):
                    d["items"] = got
            except (json.JSONDecodeError, ValueError):
                pass
    if bad_a:
        d["answer"] = salvaged_ans or _LEAK_RE.sub(
            "", re.split(r"<items>", a, 1, flags=re.I)[0]).strip()
    elif not a and salvaged_ans:
        d["answer"] = salvaged_ans
    if bad_n:
        d["analysis"] = salvaged_ana
    elif not n and salvaged_ana:
        d["analysis"] = salvaged_ana
    log.warning("합성 태그 유출 감지 → 회수(answer %d자 · analysis %d자 · items %d)",
                len(d.get("answer") or ""), len(d.get("analysis") or ""), len(d.get("items") or []))
    return d


def _coerce_facts(facts) -> list[dict]:
    """tool_use의 facts 필드를 dict 리스트로 정규화.
    모델이 배열 대신 JSON 문자열로 직렬화하는 엣지케이스(대기업)까지 흡수."""
    if isinstance(facts, list):
        return [f for f in facts if isinstance(f, dict)]
    if isinstance(facts, str):
        try:
            v = json.loads(facts)
            if isinstance(v, list):
                return [f for f in v if isinstance(f, dict)]
        except (ValueError, TypeError):
            return []
    return []

# 사전추출 Fact Store 표준 타입 12종(질의이해·추출 공용)
FACT_TYPES = [
    "감사의견_유형", "핵심감사사항", "계속기업_불확실성", "감가상각_변경",
    "회계정책_변경", "회계추정_변경", "소송_우발부채", "특수관계자_거래",
    "재고자산_평가방법", "수익인식_정책", "감사인_보수", "내부회계관리제도_검토의견",
    "감사_투입시간", "비감사용역_계약", "감사인_변경사유",
    "감사보고서_강조사항", "전기오류_수정", "정정공시_이력",
]

SYSTEM_UNDERSTAND = """당신은 한국 코스피 감사보고서 RAG의 질의 분석기다.
사용자 질문을 분석해 analyze_query 도구로 결과를 내라.
- intent: "스크리닝"(~한 기업 전부/조건으로 거르기) | "단건"(특정 1개 기업) | "요약"(개방형) | "수치"
- doc_types: 감사의견·KAM·계속기업=audit, 회계정책·감가상각·주석=financial_note, 재무제표 표=financial_stmt
- expanded_queries: 같은 정보요구를 동의어·하위개념으로 확장한 검색질의 4~6개(온톨로지 확장).
  예 "감가상각방법 변경"→["정액법에서 정률법으로 변경","내용연수 추정 변경","유형자산 회계추정의 변경"]
- **screening_fact_types**: intent가 스크리닝이고 표준 Fact **범주 자체를 전수로 거를 때만** 채워라(전수 SQL).
  "감가상각방법/내용연수 변경"→["감가상각_변경","회계추정_변경"], "계속기업 불확실성"→["계속기업_불확실성"],
  "감사의견"→["감사의견_유형"], "중요한 소송/우발부채"→["소송_우발부채"], "재고 평가방법"→["재고자산_평가방법"],
  "수익인식/진행기준"→["수익인식_정책"], "핵심감사사항/KAM"→["핵심감사사항"], "감사보수/회계법인·외부감사인"→["감사인_보수"],
  "회계정책 변경"→["회계정책_변경"], "내부회계관리제도"→["내부회계관리제도_검토의견"], "특수관계자 거래"→["특수관계자_거래"],
  "감사시간/감사 투입시간"→["감사_투입시간"], "비감사용역/비감사 보수/세무자문 계약"→["비감사용역_계약"],
  "감사인 변경 사유/왜 감사인을 바꿨/주기적 지정으로 변경"→["감사인_변경사유"](단, 단순 '누가 바꿨나' 나열은 graph_relation=감사인교체),
  "강조사항"→["감사보고서_강조사항"], "전기오류수정/재무제표 재작성/과대·과소계상 오류"→["전기오류_수정"],
  "정정공시/기재정정 이력/보고서 정정한 회사"→["정정공시_이력"].
  ★"~사례/경향/영향/어떻게/왜" 같은 **개방형 탐색**이나 표준 범주 밖 주제는 비워라(null → 벡터검색).
- screening_detail_contains: **구체적 값**으로 좁힐 때만(재고 "선입선출"·"총평균"·"개별법", 감사인 "삼일"·"삼정", 수익 "진행기준", 의견 "의견거절").
  ★서술형·형용사(유의한·중요한·있는·불확실성·변경한 등)로는 **절대 채우지 말 것**(과필터로 누락). 없으면 null.
- **수치(계산형·순위)**: "영업이익 상위 10", "부채비율 200% 넘는 기업", "매출 큰 순" 등은 intent="수치"로 하고
  metric(자산총계|부채총계|자본총계|매출액|영업이익|당기순이익|부채비율|영업이익률|순이익률|ROE|ROA) +
  numeric_order(상위/하위) + numeric_n + (임계면)numeric_op·numeric_value를 채워라. 정형재무(XBRL) SQL로 처리.
- **관계·다홉**: "감사인을 바꾼 기업"/"A에서 B로 감사인 변경"→graph_relation="감사인교체"(+from_auditor/to_auditor).
  "X와 같은 감사인 쓰는 기업"→"동일감사인"(company=X). "X 감사인이 감사하는 다른 회사 중 [조건]"→"감사인경유"(company=X + 조건은 screening_fact_types).
  "X의 특수관계자/계열사/특수관계 거래 상대는"·"X와 같은 기업집단(계열)으로 엮인 기업"→"특수관계자"(company=X). 지식그래프 순회로 처리.
- **보고서 유형(감사 vs 검토)**: 질문에 "분기/1분기/3분기/반기/상반기/검토보고서/검토의견/중요한 수정사항(발견)"이 있으면 assurance="검토"(+report_period=분기|반기). "연차/사업보고서/감사보고서/감사의견"만 있거나 불명이면 assurance=null(전체 대상). ※분기·반기는 감사가 아니라 **검토**다. 단어 '검토'가 '살펴봄'의 뜻(예 "회계정책을 검토")일 땐 검토보고서가 아니므로 null.
- screening_detail_excludes: **부정형**('~가 아닌'·'~제외한')에서 제외할 값 리스트(예 "적정이 아닌"→["적정"], "빅4가 아닌"→["삼일","삼정","안진","한영"]). 없으면 null.
- single_kind: intent=단건(기업 1개·'전부/모두' 없음)일 때 세부유형 —
  "단일사실"(특정 항목 질문: 감사의견/감사보수/재고평가 등 → screening_fact_types도 채움) |
  "프로파일"(그 회사 전반 요약: 감사의견·KAM·회계정책 등 한눈에) |
  "추이"(연도별 변화·최근 N년) | "자유"(표준 Fact 밖 특정 주제, 예 IFRS16 영향). 단건 아니면 null.
도메인: 연결(CFS)/별도(OFS), 감사의견(적정/한정/부적정/의견거절), 핵심감사사항(KAM),
계속기업 불확실성. K-IFRS에서 감가상각방법 변경=회계추정의 변경."""

SYSTEM_SYNTH = """당신은 회계사를 돕는 분석가다. **제공된 '근거'에만 기반**해 answer 도구로 답하라.
근거에 없으면 지어내지 말 것(불충분하면 insufficient=true).
각 내용은 반드시 해당 JSON 필드에만 담아라 — **필드 값 문자열 안에 <analysis>·<answer>·<items> 같은
태그를 쓰거나 다른 필드 내용을 재서술하는 것 절대 금지**(태그 없이 순수 본문만).

【출력 3요소】
1) answer: 질문에 대한 핵심 결론 2~3문장(헤드라인). 기업을 나열하지 말 것.
2) analysis: **질문 의도에 맞춘 충실한 분석 8~14문장**(문단 2~3개 분량, 풍부하게). 근거들을 종합해
   다음을 빠짐없이 다뤄라: ① 배경·맥락(왜 이 이슈가 생겼나), ② 핵심 내용의 구체적 서술(수치·조건·
   대상 포함), ③ 기업 간 공통점과 차이, ④ 회계·감사 관점의 의미(관련 기준·판단 포인트), ⑤ 회계사가
   유의할 실무 시사점·후속 확인사항. 질문 유형별 강조:
   - ★여러 기업을 거르는 스크리닝(기업 2곳 이상 나열): analysis는 **2~3문장 총평만**(전체 경향·유의점).
     **기업별 개별 설명을 analysis에 절대 쓰지 말 것** — 기업별 내용은 items의 conclusion으로 충분하다.
   - 개방형(사례·경향): 전체 경향 → 대표 사례 몇 개를 구체적으로 → 주의 깊게 볼 지점.
   - 단건/프로파일: 그 기업의 상황을 배경부터 영향까지 충분히 풀어 서술(8~14문장 유지).
   - 수치: 값의 규모·구성·비중, 전기 대비 증감, 업종 맥락(근거에 있을 때)을 숫자와 함께 해석.
   근거에 있는 내용만 사용하되 answer보다 훨씬 구체적이고 길게. 표(재무 수치)가 함께 제공되면 그 숫자를
   본문에 직접 인용해 해석하라. 단, 근거 없는 추측·일반론으로 분량만 늘리지 말 것(모든 문장은 근거 기반).
3) items: 조건에 맞는 기업을 **하나씩** 담아라(answer/analysis에 기업명을 죽 나열하지 말 것).
   같은 기업이 여러 근거에 있으면 하나로 합친다.

【item 규칙】corp_name·conclusion·quote 필수.
- conclusion: 그 기업이 **무엇을·왜·어떤 맥락·어떤 영향인지 2~4문장**으로 구체적으로(단순 라벨/한 단어 금지).
- quote: 근거 청크에서 **글자 그대로 복사한 원문**(요약·수정 금지, 이후 기계 검증).
- section_path·dart_url: 근거에 적힌 값을 그대로.

★질문이 "사례/경향/영향" 등 개방형이어도 관련 기업을 items에 하나씩 담아라.
**근거가 하나라도 있으면 items를 절대 비우지 말 것.**"""

SYSTEM_TABLE = """당신은 한국 공시(감사보고서·재무제표 주석)의 파이프(|) 표 텍스트를 깔끔한 격자로
정규화하는 도구다. normalize_table 도구로만 답하라.
규칙:
- **숫자는 원문 그대로 복사**(수정·계산·생성 금지). 원문에 없는 숫자를 만들면 안 된다(이후 기계검증으로 걸러짐).
- 다단 헤더는 '상위 하위'처럼 합쳐 칼럼명 하나로. 열 이름이 원문에 없으면 값의 의미로 추정하고 끝에 '(추정)'을 붙인다.
- 표가 중간에 잘려 있으면 있는 부분만 정규화한다(추측으로 채우지 말 것).
- 거래처/사업장별로 열이 매우 많은 표는 행·열을 바꿔(전치) 읽기 쉽게 구성해도 된다(라벨-숫자 대응은 유지).
  단, **항목명(약정·계약·소송 등)이 이미 행 라벨인 표는 행 구조를 유지**하라(항목을 열로 눕히는 전치 금지).
  대부분이 빈 셀인 격자를 만들지 말 것 — 값이 있는 항목만 행으로 정리하는 편이 낫다.
- 첫 칼럼은 행 라벨. title(표 제목)과 unit(단위)도 원문에서 찾으면 채운다.
- 당기/전기가 섞여 있으면 칼럼명에 명시(예: '당기 금액', '전기 금액').
- 원문에 실질적 표가 없으면 rows를 빈 배열로."""

TABLE_TOOL = {
    "name": "normalize_table",
    "description": "파이프 표 텍스트의 정규화 격자",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "unit": {"type": "string"},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "첫 칼럼=행 라벨 이름(예:'계정'), 이후 값 칼럼명"},
            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                     "description": "각 행 = [라벨, 값1, 값2, ...] (columns와 같은 길이)"},
        },
        "required": ["columns", "rows"],
    },
}

SYSTEM_HYDE = """당신은 한국 감사보고서·재무제표 주석의 문체로 '가상의 근거 문단'을 작성한다.
사용자 질문에 대해, 실제 감사보고서/주석에 있을 법한 3~4문장 한국어 문단을 생성하라.
목적은 검색 임베딩용이므로 사실 정확성보다 **해당 주제의 전형적 서술·회계 용어·표현**을 담는 것이 중요하다.
회사명·구체 수치는 일반화하고 감사·회계 전문 용어를 자연스럽게 포함하라. 설명 없이 문단만 출력."""

UNDERSTAND_TOOL = {
    "name": "analyze_query",
    "description": "질문 분석 결과",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["스크리닝", "단건", "요약", "수치"]},
            "company": {"type": ["string", "null"]},
            "industry": {"type": ["string", "null"]},
            "fiscal_years": {"type": ["array", "null"], "items": {"type": "integer"}},
            "is_consolidated": {"type": ["boolean", "null"]},
            "doc_types": {"type": ["array", "null"],
                          "items": {"type": "string",
                                    "enum": ["audit", "financial_note", "financial_stmt"]}},
            "key_concepts": {"type": "array", "items": {"type": "string"}},
            "expanded_queries": {"type": "array", "items": {"type": "string"}},
            "screening_fact_types": {
                "type": ["array", "null"], "items": {"type": "string", "enum": FACT_TYPES},
                "description": "스크리닝이 겨냥하는 표준 Fact 타입(있으면 Fact Store 전수조회)"},
            "screening_detail_contains": {
                "type": ["string", "null"], "description": "Fact 세부를 좁힐 구체적 값(서술형 금지)"},
            "screening_detail_excludes": {
                "type": ["array", "null"], "items": {"type": "string"},
                "description": "부정형('~가 아닌')에서 제외할 값 리스트"},
            "single_kind": {
                "type": ["string", "null"],
                "description": "단건 세부유형: 단일사실|프로파일|추이|자유"},
            "metric": {
                "type": ["string", "null"],
                "description": "수치질의 지표(정형재무): 자산총계|부채총계|자본총계|유동자산|비유동자산|"
                               "유동부채|비유동부채|현금및현금성자산|매출액|매출원가|매출총이익|"
                               "판매비와관리비|영업이익|당기순이익|영업활동현금흐름|투자활동현금흐름|"
                               "재무활동현금흐름|부채비율|영업이익률|순이익률|ROE|ROA|유동비율|"
                               "매출총이익률|판관비율"},
            "numeric_order": {"type": ["string", "null"], "description": "순위: 상위|하위"},
            "numeric_n": {"type": ["integer", "null"], "description": "상위/하위 N개(기본 10)"},
            "numeric_op": {"type": ["string", "null"], "description": "비교연산: >|<|>=|<="},
            "numeric_value": {"type": ["number", "null"],
                              "description": "임계값(예: 부채비율 200% 넘는 → 200)"},
            "graph_relation": {
                "type": ["string", "null"],
                "description": "관계·다홉 질의: 감사인교체(연도간 감사인 변경) | 동일감사인(같은 감사인 쓰는 기업) "
                               "| 감사인경유(특정기업 감사인의 다른 고객사 중 조건) "
                               "| 특수관계자(특정기업의 특수관계자·계열사 관계망). 아니면 null"},
            "from_auditor": {"type": ["string", "null"], "description": "감사인교체 전(前) 감사인"},
            "to_auditor": {"type": ["string", "null"], "description": "감사인교체 후(後) 감사인"},
            "assurance": {"type": ["string", "null"],
                          "description": "보증유형 감사|검토. 분기/반기/검토보고서/검토의견=검토, 불명이면 null"},
            "report_period": {"type": ["string", "null"],
                              "description": "보고서 기간 연차|분기|반기. 불명이면 null"},
        },
        "required": ["intent", "expanded_queries"],
    },
}

SYNTH_TOOL = {
    "name": "answer",
    "description": "근거 기반 답변",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "description": "핵심 결론 1~2문장(헤드라인)"},
            "analysis": {"type": "string",
                         "description": "질문 의도에 맞춘 상세 분석 4~7문장(근거 종합·맥락·시사점)"},
            "insufficient": {"type": "boolean"},
            "items": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "corp_name": {"type": "string"},
                    "fiscal_year": {"type": "string"},
                    "is_consolidated": {"type": "boolean"},
                    "conclusion": {"type": "string"},
                    "quote": {"type": "string"},
                    "section_path": {"type": "string"},
                    "dart_url": {"type": "string"},
                },
                "required": ["corp_name", "conclusion", "quote"],
            }},
        },
        "required": ["answer", "items"],
    },
}

# ── 답변 감사자(Generator–Evaluator 분리) — 합성 답변을 독립 채점 ──
SYSTEM_JUDGE = """당신은 감사보고서 분석 답변을 검수하는 '검토 회계사'다. 답을 새로 쓰지 말고,
주어진 [질문]·[답변]·[근거]만으로 답변 품질을 냉정하게 채점하라. 관대하게 주지 말 것.
평가 축(각각 판단):
- answered: 답변이 질문이 **실제로 물은 것**에 답했는가. 예) '증감률'을 물었는데 금액만 있으면 미흡.
- grounded: 답변의 수치·주장이 [근거]와 모순되지 않는가(근거에 없는 값을 지어내지 않았는가).
- 종합 verdict: pass(그대로 내보내도 됨) | revise(재작성 필요) | insufficient(근거 자체가 부족).
missing에는 '무엇이 빠졌거나 틀렸는지'를 한 줄로. 근거가 답을 뒷받침하면 사소한 문장 다듬기로 revise하지 말 것
(revise는 실질적 결함일 때만). 정형 사실 나열형(스크리닝·계정표) 답변은 근거와 일치하면 pass."""

JUDGE_TOOL = {
    "name": "judge_answer",
    "description": "합성 답변의 질문 응답성·근거 정합성 채점",
    "input_schema": {
        "type": "object",
        "properties": {
            "answered": {"type": "boolean", "description": "질문이 물은 것에 실제로 답했는가"},
            "grounded": {"type": "boolean", "description": "근거와 모순 없는가(환각 없음)"},
            "verdict": {"type": "string", "description": "pass|revise|insufficient"},
            "missing": {"type": "string", "description": "빠졌거나 틀린 점 한 줄(pass면 빈 문자열)"},
        },
        "required": ["answered", "grounded", "verdict"],
    },
}


# ── Fact Store 추출(Layer 2 적재) — FACT_TYPES는 상단 정의 재사용 ──
SYSTEM_EXTRACT = """당신은 한국 코스피 감사보고서·재무제표 주석에서 표준 사실(Fact)을 추출하는 분석가다.
제공된 발췌에 **실제로 존재하는 사실만** extract_facts 도구로 추출하라(없는 타입은 생략, 절대 지어내지 말 것).
★**부정·부재는 Fact가 아니다**: "계속기업 불확실성 없음", "해당사항 없음", "중요한 소송 없음", "변경 없음"처럼 **문제·이벤트가 없다는 진술은 추출 금지**. 다음 이벤트형은 **실제로 발생·기재됐을 때만** 추출: 계속기업_불확실성·소송_우발부채·특수관계자_거래·감가상각_변경·회계정책_변경·회계추정_변경. (반면 감사의견_유형·감사인_보수·재고자산_평가방법·수익인식_정책·내부회계관리제도_검토의견·핵심감사사항은 항상 존재하므로 값을 정상 추출.)
표준 타입 12종으로 매핑:
- 감사의견_유형: detail={opinion: 적정|한정|부적정|의견거절}
- 핵심감사사항: KAM 주제마다 1건. detail={topic, summary}
- 계속기업_불확실성: 중요한 불확실성 기재 시. detail={basis}
- 감가상각_변경: 방법/내용연수 변경. detail={kind:방법|내용연수, from, to, asset}
- 회계정책_변경: detail={topic, description}
- 회계추정_변경: detail={topic, description}  (K-IFRS에서 감가상각방법 변경은 회계추정의 변경이기도 함)
- 소송_우발부채: 중요한 것. detail={description}
- 특수관계자_거래: 유의할 거래. detail={description}
- 재고자산_평가방법: detail={method: 예 총평균법|선입선출법}
- 수익인식_정책: 진행기준 등 유의 정책. detail={method, description}
- 감사인_보수: detail={auditor: 회계법인명}; 감사보수 금액이 있으면 value_raw+unit_scale+currency
- 내부회계관리제도_검토의견: detail={opinion}
**evidence_quote는 근거에서 글자 그대로 복사한 원문**(이후 기계검증). 금액은 단위(원/천원/백만원)·통화 동반.
확실하면 confidence='ok', 애매하면 'review'."""

EXTRACT_TOOL = {
    "name": "extract_facts",
    "description": "감사보고서·주석에서 표준 Fact를 추출",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "fact_type": {"type": "string", "enum": FACT_TYPES},
                    "detail": {"type": "object",
                               "description": "타입별 핵심 속성(예: {\"opinion\":\"적정\"})"},
                    "evidence_quote": {"type": "string",
                                       "description": "근거에서 글자 그대로 복사한 원문"},
                    "section_ref": {"type": "string", "description": "근거 번호 또는 섹션경로"},
                    "value_raw": {"type": ["number", "null"], "description": "수치형이면 숫자값"},
                    "unit_scale": {"type": ["string", "null"], "description": "원/천원/백만원"},
                    "currency": {"type": ["string", "null"], "description": "KRW/USD 등"},
                    "confidence": {"type": "string", "enum": ["ok", "review"]},
                },
                "required": ["fact_type", "detail", "evidence_quote", "confidence"],
            }},
        },
        "required": ["facts"],
    },
}

# ── 특수관계자 네트워크(지식그래프 확장) — 기존 특수관계자_거래 Fact에서 상대방 엔티티 정형화(Haiku·저비용) ──
SYSTEM_PARTIES = """당신은 한국 감사보고서 '특수관계자 거래' 서술에서 거래 상대방을 정형 추출하는 분석가다.
문장에 **명시된 고유명 상대방(기업/개인)만** extract_parties로 뽑아라(추측 금지, 없으면 빈 배열).
- name: 상대방 명칭 원문 그대로(예 "건진건설㈜","㈜극동건설","화승코퍼레이션"). '종속기업'·'특수관계자'·'계열사' 같은 **총칭만 있고 고유명사가 없으면 제외**.
- relationship: 종속기업|관계기업|계열사|모회사|공동기업|기타 (문맥상 가장 근접, 불명이면 기타)
- txn_type: 매출|매입|보증|자금대여|차입|지분|기타 (그 상대방과의 거래 성격)
- group_name: 문장에 기업집단명이 있으면(예 "농협 기업집단"→"농협") 채우고 없으면 null.
동일 상대방이 여러 번 나오면 1건으로 합쳐라."""

PARTIES_TOOL = {
    "name": "extract_parties",
    "description": "특수관계자 거래 상대방을 정형 추출",
    "input_schema": {
        "type": "object",
        "properties": {
            "parties": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "상대방 명칭 원문"},
                    "relationship": {"type": "string",
                                     "description": "종속기업|관계기업|계열사|모회사|공동기업|기타"},
                    "txn_type": {"type": "string",
                                 "description": "매출|매입|보증|자금대여|차입|지분|기타"},
                    "group_name": {"type": ["string", "null"], "description": "기업집단명(없으면 null)"},
                },
                "required": ["name"],
            }},
        },
        "required": ["parties"],
    },
}


SYSTEM_REWRITE = """당신은 감사보고서 분석 서비스의 후속 질문 해석기다.
이전 대화 맥락(회사·주제·연도·직전 문답)과 새 질문을 받아, 새 질문을 **혼자 봐도 완전한
독립 질문 한 문장(한국어)**으로 재작성한다.
- 새 질문이 이미 독립적(회사·주제가 명시됨)이면 그대로 반환하고 used_context=false.
- 대명사·생략("그럼 작년은?", "거기 감사의견은?", "얼마나 늘었어?")은 맥락의 회사·주제·연도로 치환.
- 새 질문에 다른 회사가 나오면 회사만 교체하고 주제는 유지(예: "SK하이닉스는?" → 같은 주제로 SK하이닉스 질문).
- 질문의 의도를 바꾸거나 맥락에 없는 조건을 지어내지 말 것. 재작성문은 짧고 자연스럽게.
- context_summary는 실제로 적용한 맥락만 "삼성전자 · 매출채권 · 2024→2025" 식으로 요약(미사용이면 빈 문자열)."""

REWRITE_TOOL = {
    "name": "resolve_followup",
    "description": "후속 질문을 이전 대화 맥락으로 독립 질문으로 재작성",
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved": {"type": "string", "description": "독립 실행 가능한 재작성 질문(한국어 한 문장)"},
            "used_context": {"type": "boolean", "description": "이전 맥락을 실제로 사용했는지"},
            "context_summary": {"type": "string",
                                "description": "적용한 맥락 요약(예: 삼성전자 · 매출채권 · 2024→2025)"},
        },
        "required": ["resolved", "used_context"],
    },
}


class ClaudeClient:
    # 일시적 서버오류(과부하 529·레이트리밋 429·5xx·연결끊김)는 백오프 재시도
    _TRANSIENT = (408, 409, 429, 500, 502, 503, 529)

    def __init__(self):
        if not settings.anthropic_api_key:
            raise SystemExit("ANTHROPIC_API_KEY 없음 (.env 확인)")
        # max_retries: SDK 내장 백오프(429/5xx/overloaded). 아래 _tool_call이 한 겹 더 감싼다.
        self.client = Anthropic(api_key=settings.anthropic_api_key, max_retries=5)

    def _create_with_retry(self, **kw):
        """messages.create + 일시오류 백오프(SDK 재시도 소진 후에도 한 겹 더)."""
        for attempt in range(3):
            try:
                return self.client.messages.create(**kw)
            except (APIStatusError, APIConnectionError) as e:
                code = getattr(e, "status_code", None)
                transient = isinstance(e, APIConnectionError) or code in self._TRANSIENT
                if transient and attempt < 2:
                    wait = 8 * (attempt + 1)
                    log.warning("Claude 일시오류(%s) → %ds 후 재시도(%d/3)",
                                code or "conn", wait, attempt + 2)
                    time.sleep(wait)
                    continue
                raise

    def _tool_call(self, model, system, user, tool, max_tokens, cache: bool = True) -> dict:
        # 프롬프트 캐싱(§6.2): 정적 system+tool 프리픽스를 캐시 → 반복 호출 시 입력 단가 ~10%.
        # tool·system 두 지점에 브레이크포인트(둘 다 정적). 캐시 최소토큰 미만이면 API가 무시(무해).
        sys_arg = ([{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                   if cache else system)
        tools_arg = [{**tool, "cache_control": {"type": "ephemeral"}}] if cache else [tool]
        resp = self._create_with_retry(
            model=model, max_tokens=max_tokens, system=sys_arg, tools=tools_arg,
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}])
        us = getattr(resp, "usage", None)
        if us is not None:  # 캐시 적중 확인용(디버그)
            log.debug("usage in=%s cache_w=%s cache_r=%s out=%s",
                      getattr(us, "input_tokens", None),
                      getattr(us, "cache_creation_input_tokens", None),
                      getattr(us, "cache_read_input_tokens", None),
                      getattr(us, "output_tokens", None))
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        log.warning("tool_use 블록 없음")
        return {}

    def understand(self, question: str) -> dict:
        # Sonnet 5: 의도·계정 추출 흔들림(예: 매출채권→유동자산 근사) 감소. 토크나이저 여유분 반영.
        return self._tool_call(settings.claude_model_understand, SYSTEM_UNDERSTAND,
                               question, UNDERSTAND_TOOL, max_tokens=1600)

    def rewrite_followup(self, question: str, context: str) -> dict:
        """AI 대화 탭: 대화 맥락 기반 후속 질문 재작성 — 재작성문이 곧 '맥락 해석' 표시."""
        user = f"=== 이전 대화 맥락 ===\n{context}\n\n=== 새 질문 ===\n{question}"
        return self._tool_call(settings.claude_model_understand, SYSTEM_REWRITE,
                               user, REWRITE_TOOL, max_tokens=650)

    def synthesize(self, question: str, evidence: list[dict], cheap: bool = False,
                   tier: str | None = None) -> dict:
        """근거 기반 합성. cheap=True→Haiku(온디맨드 비용최소). tier='hard'→Opus(에스컬레이션)."""
        blocks = []
        for i, c in enumerate(evidence, 1):
            basis = "연결" if c.get("is_consolidated") else "별도"
            blocks.append(
                f"[근거{i}] {c.get('corp_name','')} {c.get('fiscal_year','')} {basis} "
                f"| {c.get('section_path','')}\n딥링크: {c.get('dart_url','')}\n{c.get('text','')}")
        user = f"질문: {question}\n\n=== 근거 ({len(evidence)}개) ===\n" + "\n\n".join(blocks)
        if tier == "hard":
            model = settings.claude_model_hard        # Opus — 난도/실패 에스컬레이션
        elif cheap:
            model = settings.claude_model_router      # Haiku — 온디맨드 비용최소
        else:
            model = settings.claude_model_workhorse   # Sonnet — 기본
        return _sanitize_synth(self._tool_call(
            model, SYSTEM_SYNTH, user, SYNTH_TOOL,
            max_tokens=4096 if (cheap and tier != "hard") else 9216))  # Sonnet 5 토큰 +30% 여유

    def judge_answer(self, question: str, answer: str, analysis: str,
                     evidence: list[dict]) -> dict:
        """생성-평가 분리(하네스): 합성 답변을 독립 채점(Haiku, 저비용).
        verdict=revise/insufficient면 상위 호출부가 Opus 재합성 발동. 실패 시 pass 폴백(비차단)."""
        blocks = []
        for i, c in enumerate(evidence[:8], 1):        # 채점엔 상위 근거 8개면 충분(비용 절감)
            blocks.append(f"[근거{i}] {c.get('corp_name','')} {c.get('fiscal_year','')} "
                          f"| {(c.get('text') or '')[:600]}")
        user = (f"[질문]\n{question}\n\n[답변]\n{answer}\n\n[분석]\n{analysis or '(없음)'}\n\n"
                f"[근거 {len(evidence)}개 중 상위]\n" + "\n\n".join(blocks))
        try:
            return self._tool_call(settings.claude_model_router, SYSTEM_JUDGE,
                                   user, JUDGE_TOOL, max_tokens=400)
        except Exception:  # noqa: BLE001  채점 실패는 답변을 막지 않음(fail-open)
            log.exception("judge_answer 실패 — pass 처리")
            return {"answered": True, "grounded": True, "verdict": "pass", "missing": ""}

    def normalize_table(self, raw: str, unit_hint: str = "") -> dict:
        """파이프 표 원문 → 정규화 격자(columns/rows). Haiku(저비용)·캐시드 시스템 프롬프트.
        숫자는 이후 원문 대조 기계검증되므로 여기선 구조화만 담당."""
        user = f"단위 힌트: {unit_hint or '없음'}\n=== 원문 표 ===\n{raw[:4000]}"
        return self._tool_call(settings.claude_model_router, SYSTEM_TABLE,
                               user, TABLE_TOOL, max_tokens=3000)

    def hyde_document(self, question: str) -> str:
        """HyDE(§5.7) — 질문에 대한 '가상 근거 문단'(Haiku). 그 임베딩으로 추가검색(개방형 recall↑)."""
        try:
            resp = self._create_with_retry(
                model=settings.claude_model_router, max_tokens=400,
                system=[{"type": "text", "text": SYSTEM_HYDE,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": question}])
            parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            return " ".join(parts).strip()
        except Exception as e:  # noqa: BLE001  HyDE 실패는 일반 검색으로 진행
            log.warning("HyDE 생성 실패(무시): %s", e)
            return ""

    @staticmethod
    def _extract_user(header: str, sections: list[dict]) -> str:
        blocks = [f"[근거{i}] {c.get('section_path','')}\n{c.get('text','')}"
                  for i, c in enumerate(sections, 1)]
        return (f"{header}\n\n=== 보고서 발췌 ({len(sections)}개 섹션) ===\n"
                + "\n\n".join(blocks))

    def extract_facts(self, header: str, sections: list[dict]) -> list[dict]:
        """감사보고서·핵심 주석 섹션 → 표준 Fact 12종(tool-use, Sonnet) — 동기.
        대기업은 Fact가 많아 max_tokens 8192(잘림 방지); 문자열 직렬화·빈결과면 1회 재시도."""
        user = self._extract_user(header, sections)
        for _ in range(2):
            out = self._tool_call(settings.claude_model_workhorse, SYSTEM_EXTRACT,
                                  user, EXTRACT_TOOL, max_tokens=8192)
            facts = _coerce_facts(out.get("facts") if isinstance(out, dict) else None)
            if facts:
                return facts
        return []

    def extract_parties(self, text: str) -> list[dict]:
        """특수관계자 거래 서술 → 상대방 엔티티(Haiku·캐싱). 지식그래프 엣지 재료."""
        out = self._tool_call(settings.claude_model_router, SYSTEM_PARTIES,
                              text, PARTIES_TOOL, max_tokens=1024)
        ps = out.get("parties") if isinstance(out, dict) else None
        return [p for p in (ps or []) if isinstance(p, dict) and p.get("name")]

    # ── Batch API (대량 추출, 50% 할인·비동기) ──
    def extract_batch_params(self, header: str, sections: list[dict]) -> dict:
        """Batch 요청 1건의 params(messages.create 인자와 동일 형식).
        캐싱(§6.2): 800+건이 동일 system+tool 프리픽스를 공유 → 반복분 캐시 읽기로 비용↓."""
        return {
            "model": settings.claude_model_workhorse,
            "max_tokens": 8192,   # 대기업 Fact 다수 → 잘림 방지(캡, 실사용분만 과금)
            "system": [{"type": "text", "text": SYSTEM_EXTRACT,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": [{**EXTRACT_TOOL, "cache_control": {"type": "ephemeral"}}],
            "tool_choice": {"type": "tool", "name": EXTRACT_TOOL["name"]},
            "messages": [{"role": "user", "content": self._extract_user(header, sections)}],
        }

    def submit_batch(self, requests: list[dict]):
        return self.client.messages.batches.create(requests=requests)

    def retrieve_batch(self, batch_id: str):
        return self.client.messages.batches.retrieve(batch_id)

    def batch_results(self, batch_id: str):
        return self.client.messages.batches.results(batch_id)

    @staticmethod
    def facts_from_message(message) -> list[dict]:
        for block in getattr(message, "content", []):
            if getattr(block, "type", None) == "tool_use":
                return _coerce_facts(dict(block.input).get("facts"))
        return []
