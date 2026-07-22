"""표 기반 Fact 추출(무API 규칙) — 감사_투입시간·비감사용역_계약.

사업보고서 'V. 회계감사인의 감사의견' 섹션의 DART 표준 서식 표에서 칼럼 매칭으로 추출.
LLM 호출 0. 서식 표준화 실측(감사용역 표 헤더 일치 290/291)이 규칙 추출의 근거.
멱등: (corp_code, fiscal_year) 단위 replace — 증분 실행 시 다른 기업 Fact는 건드리지 않는다.
"""
from __future__ import annotations
import glob
import json
import logging
import re
from config import settings
from src.clients.postgres import PostgresStore

log = logging.getLogger(__name__)

RUN_ID = "rule_tables_v1"
# ※ 강조사항의 '요약 표' 기반 추출은 폐기(rowspan 열밀림으로 KAM 오염) —
#   현행은 감사보고서 본문 문단 앵커("감사의견에는 영향을 미치지 않는 사항으로서") 방식.
FACT_TYPES = ("감사_투입시간", "비감사용역_계약", "감사인_변경사유",
              "감사보고서_강조사항", "전기오류_수정", "정정공시_이력")
PARSE_TYPES = FACT_TYPES[:-1]      # run()이 재구축하는 파싱 기반 유형(정정공시_이력은 별도 함수)

AUD_MAP = [("삼일", "삼일"), ("삼정", "삼정"), ("KPMG", "삼정"), ("안진", "안진"),
           ("딜로이트", "안진"), ("한영", "한영"), ("EY", "한영")]

_NUM_SEP = re.compile(r"^\d{1,3}([.,]\d{3})+$")
_CUR_ROW = re.compile(r"\(\s*당\s*기\s*\)")
_NAS_EMPTY = {"-", "", "해당사항 없음", "해당없음", "해당 사항 없음", "N/A", "없음"}


def _aud_norm(name: str) -> str:
    for pat, norm in AUD_MAP:
        if pat in name:
            return norm
    return re.sub(r"(주식회사 ?|회계법인|\s)", "", name or "")


def _num(s: str):
    """'1,491시간'·'1.724시간'(천단위 오타)·'95' → int. 실패 시 None."""
    s = re.sub(r"[^\d.,]", "", s or "")
    if not s:
        return None
    if _NUM_SEP.match(s):
        return int(re.sub(r"[.,]", "", s))
    try:
        return int(float(s.replace(",", "")))
    except ValueError:
        return None


# 감사인 변경 서술: 헤딩 뒤 본문에서 사유·전후 감사인 추출
_CHG_HEAD = re.compile(r"회계\s*감사인의\s*변경")
_CHG_FT = re.compile(r"([가-힣A-Za-z]{2,20}회계법인)\s*에서\s*([가-힣A-Za-z]{2,20}회계법인)")
_CHG_NONE = re.compile(r"(해당\s*사항\s*없|해당없|변경\s*(사항|내역)?\s*없)")


def _classify_change(txt: str) -> str:
    t = txt.replace(" ", "")
    if "주기적지정" in t or ("지정" in t and "제11조" in t):
        return "주기적 지정"
    if "직권지정" in t or "감리" in t:
        return "직권 지정"
    if "지정기간만료" in t or "지정기간의만료" in t:
        return "지정기간 만료 후 신규선임"
    if "자유선임" in t or "자유수임" in t:
        return "자유선임"
    if "계약기간만료" in t or "계약만료" in t or "감사계약기간의만료" in t:
        return "계약기간 만료 후 신규선임"
    if "지정" in t or "제11조" in t or "증권선물위원회" in t or "증선위" in t:
        return "지정(외감법 제11조)"
    return "기타"


# ── 강조사항(EOM, KSA 706): 표준 문구 앵커 — 표가 아닌 감사보고서 본문 문단 ──
_EMP_CUE = re.compile(r"감사의견에는?\s*영향을 미치지 않는 사항으로서")
_EMP_END = re.compile(r"핵심감사사항|재무제표에 대한 경영진|기타사항|감사보고서의 이용")
_EMP_NOTE = re.compile(r"주석\s*\d+")
_EMP_TOPICS = [("재무제표 재작성(오류)", ("오류",)), ("자본잠식", ("자본잠식", "전액 잠식")),
               ("거래정지·상장폐지", ("거래.?정지", "상장폐지")), ("계속기업", ("계속기업",)),
               ("합병·분할·양수도", ("합병", "분할", "양수도")), ("매각·중단영업", ("매각", "중단영업")),
               ("소송", ("소송",)), ("특수관계자", ("특수관계자",)),
               ("공정가치·평가", ("공정가치",)), ("채무·차입", ("금융채", "차입", "채무"))]

# ── 전기오류수정(재작성): 희귀·고가치 레드플래그. 보일러플레이트(기준서 개정 서술) 배제 ──
_ERR_ROW = re.compile(r"^\s*전기오류수정")                     # 자본변동표 등 표 행
_ERR_NARR = re.compile(r"오류[가-힣,\s]{0,30}(수정|반영)[가-힣\s]{0,25}재작성하였")
_ERR_BOILER = re.compile(r"(아닌\s*한|아니라면|명확히|개정\s*기준서|기준서[가-힣\s]{0,15}개정)")


def _emp_topics(txt: str) -> list[str]:
    out = [label for label, kws in _EMP_TOPICS if any(re.search(k, txt) for k in kws)]
    return out or ["기타"]


def _scan_report(path, years: set[str]):
    """한 보고서(jsonl)에서 표(감사용역·비감사용역)의 당기 행 + 변경 서술 + 강조사항 + 전기오류수정."""
    audit_rows, nas_rows, emp_rows, chg = [], [], [], None
    err_hits = []                              # 전기오류수정 신호(표행·서술)
    for ln in open(path, encoding="utf-8"):
        c = json.loads(ln)
        if c.get("assurance") != "감사" or c.get("period_type") != "연차":
            return [], [], [], None, []
        if str(c.get("fiscal_year")) not in years:
            return [], [], [], None, []
        t_full = c["text"]
        # ── 강조사항 문단(전 청크 대상) — '강조사항' + 표준 문구가 근접할 때만 ──
        i = t_full.find("강조사항")
        if i >= 0:
            cue = _EMP_CUE.search(t_full, i, min(len(t_full), i + 160))
            if cue:
                body = t_full[cue.end(): cue.end() + 1400]
                end = _EMP_END.search(body)
                body = body[: end.start()] if end else body
                body = body.strip().lstrip(",· ").strip()
                if len(body) > 60:                      # 실질 내용 없는 리드문만인 문단 제외
                    emp_rows.append({"body": body, "chunk": c,
                                     "notes": _EMP_NOTE.findall(body)[:3]})
        # ── 전기오류수정(전 청크 대상): ①표 행 ②서술(보일러플레이트 배제) ──
        for line in t_full.split("\n"):
            if "|" in line and _ERR_ROW.match(line) and re.search(r"[\d,]{4,}", line):
                err_hits.append({"kind": "표 행(자본변동표 등)", "line": line[:280], "chunk": c})
                break
        m = _ERR_NARR.search(t_full)
        if m:
            ctx = t_full[max(0, m.start() - 60): m.end() + 60]
            if not _ERR_BOILER.search(ctx):
                err_hits.append({"kind": "서술(재작성)", "line": ctx.replace("\n", " ")[:280], "chunk": c})
        if "회계감사인" not in (c.get("section_path") or ""):
            continue
        # ── 감사인 변경 서술(비표) — 헤딩 뒤 텍스트에서 당사 변경만 ──
        if chg is None:
            m = _CHG_HEAD.search(c["text"])
            if m:
                seg = c["text"][m.end(): m.end() + 700]
                seg = re.split(r"\n[0-9가-하]{1}[\.\)]\s|※", seg)[0].strip()  # 다음 헤딩 전까지
                first = seg.split("니다.")[0] + "니다." if "니다." in seg else seg  # 당사 문장 우선
                if seg and not _CHG_NONE.search(first):
                    ft = _CHG_FT.search(first)
                    if ft or ("변경" in first and "회계법인" in first) or "선임" in first:
                        chg = {"text": first[:450], "chunk": c,
                               "from": ft.group(1) if ft else None,
                               "to": ft.group(2) if ft else None,
                               "kind": _classify_change(first)}
        hdr_a = hdr_n = None
        for line in c["text"].split("\n"):
            if "|" not in line:
                continue
            cells = [x.strip() for x in line.split("|")]
            joined = " ".join(cells)
            if cells[0].startswith("사업연도") and any("시간" in x for x in cells) \
               and ("감사인" in joined or "검토인" in joined):
                hdr_a, hdr_n = (line, cells), None
                continue
            if cells[0].startswith("사업연도") and "계약체결일" in joined and "용역내용" in joined:
                hdr_n, hdr_a = (line, cells), None
                continue
            if not _CUR_ROW.search(cells[0]):
                continue
            if hdr_a:
                h = hdr_a[1]

                def col(key):
                    for i, x in enumerate(h):
                        if key in x:
                            return cells[i].strip() if i < len(cells) else None
                    return None
                audit_rows.append({
                    "auditor": col("감사인") or col("검토인") or "",
                    "contract_fee": _num(col("계약내역 보수")),
                    "contract_hours": _num(col("계약내역 시간")),
                    "actual_fee": _num(col("실제수행내역 보수")),
                    "actual_hours": _num(col("실제수행내역 시간")),
                    "row_line": line, "hdr_line": hdr_a[0], "chunk": c,
                })
            elif hdr_n:
                h = hdr_n[1]

                def coln(key):
                    for i, x in enumerate(h):
                        if key in x:
                            return cells[i].strip() if i < len(cells) else None
                    return None
                content = coln("용역내용") or ""
                if content in _NAS_EMPTY:
                    continue
                nas_rows.append({
                    "content": content, "signed": coln("계약체결일"),
                    "period": coln("용역수행기간"), "fee": coln("용역보수"),
                    "network_firm": coln("네트워크"),
                    "row_line": line, "hdr_line": hdr_n[0], "chunk": c,
                })
    return audit_rows, nas_rows, emp_rows, chg, err_hits


def _facts_for_report(path, years: set[str]) -> list[dict]:
    audit_rows, nas_rows, emp_rows, chg, err_hits = _scan_report(path, years)
    facts, seen_a, seen_n = [], set(), set()
    for r in audit_rows:                      # ① 감사_투입시간 — 당기 대표 1행(실제 우선)
        hours = r["actual_hours"] or r["contract_hours"]
        if not hours or not (10 <= hours <= 500_000):
            continue
        key = (hours, r["auditor"])
        if key in seen_a:
            continue
        seen_a.add(key)
        c = r["chunk"]
        facts.append(dict(
            corp_code=c["corp_code"], fiscal_year=int(c["fiscal_year"]),
            fact_type="감사_투입시간", value_raw=hours, unit_scale="시간",
            detail={"auditor": r["auditor"], "auditor_norm": _aud_norm(r["auditor"]),
                    "실제시간": r["actual_hours"], "계약시간": r["contract_hours"],
                    "실제보수": r["actual_fee"], "계약보수": r["contract_fee"],
                    "기준": "실제수행" if r["actual_hours"] else "감사계약",
                    "보수단위힌트": (c.get("unit_hint") or [None])[0]},
            evidence_text=r["hdr_line"] + "\n" + r["row_line"],
            section_path=c["section_path"], rcept_no=c["rcept_no"],
            dcm_no=c.get("dcm_no"), dart_url=c.get("dart_url")))
        break
    for r in nas_rows:                        # ② 비감사용역_계약 — 당기 유효 계약 전부
        key = (r["content"], r["fee"], r["signed"])
        if key in seen_n:
            continue
        seen_n.add(key)
        c = r["chunk"]
        d = {"용역내용": r["content"], "계약체결일": r["signed"],
             "용역수행기간": r["period"], "용역보수": r["fee"],
             "보수단위힌트": (c.get("unit_hint") or [None])[0]}
        if r["network_firm"] and r["network_firm"] not in _NAS_EMPTY:
            d["네트워크회계법인"] = r["network_firm"]
        facts.append(dict(
            corp_code=c["corp_code"], fiscal_year=int(c["fiscal_year"]),
            fact_type="비감사용역_계약", value_raw=_num(r["fee"]), unit_scale=None,
            detail=d, evidence_text=r["hdr_line"] + "\n" + r["row_line"],
            section_path=c["section_path"], rcept_no=c["rcept_no"],
            dcm_no=c.get("dcm_no"), dart_url=c.get("dart_url")))
    if emp_rows:                              # ③ 감사보고서_강조사항 — 본문 문단(코프-연도당 최장 1건)
        r = max(emp_rows, key=lambda x: len(x["body"]))
        c = r["chunk"]
        body = r["body"]
        facts.append(dict(
            corp_code=c["corp_code"], fiscal_year=int(c["fiscal_year"]),
            fact_type="감사보고서_강조사항", value_raw=None, unit_scale=None,
            detail={"요지": body[:220], "주제": _emp_topics(body),
                    "주석참조": r["notes"] or None},
            evidence_text=("강조사항 — 감사의견에는 영향을 미치지 않는 사항으로서 " + body)[:900],
            section_path=c["section_path"], rcept_no=c["rcept_no"],
            dcm_no=c.get("dcm_no"), dart_url=c.get("dart_url")))
    if err_hits:                              # ④ 전기오류_수정 — 신호 병합, 기업-연도당 1건
        kinds = sorted({h["kind"] for h in err_hits})
        best = next((h for h in err_hits if "표 행" in h["kind"]), err_hits[0])
        c = best["chunk"]
        facts.append(dict(
            corp_code=c["corp_code"], fiscal_year=int(c["fiscal_year"]),
            fact_type="전기오류_수정", value_raw=None, unit_scale=None,
            detail={"신호": kinds},
            evidence_text=best["line"],
            section_path=c["section_path"], rcept_no=c["rcept_no"],
            dcm_no=c.get("dcm_no"), dart_url=c.get("dart_url")))
    if chg:                                   # ⑤ 감사인_변경사유 — 당기 실제 변경만(부재 미생성)
        c = chg["chunk"]
        d = {"사유분류": chg["kind"]}
        if chg["from"]:
            d["전임"], d["전임_norm"] = chg["from"], _aud_norm(chg["from"])
        if chg["to"]:
            d["후임"], d["후임_norm"] = chg["to"], _aud_norm(chg["to"])
        facts.append(dict(
            corp_code=c["corp_code"], fiscal_year=int(c["fiscal_year"]),
            fact_type="감사인_변경사유", value_raw=None, unit_scale=None,
            detail=d, evidence_text=chg["text"],
            section_path=c["section_path"], rcept_no=c["rcept_no"],
            dcm_no=c.get("dcm_no"), dart_url=c.get("dart_url")))
    return facts


def run(corps: set[str] | None = None, years=None) -> int:
    """corps=None이면 전 기업(전량 재구축), 지정하면 해당 기업만 증분 replace."""
    years = {str(y) for y in (years or (2024, 2025))}
    base = settings.data_dir / settings.pipeline_version / "parsed_v2"
    paths = []
    for p in sorted(glob.glob(str(base / "*" / "*.jsonl"))):
        cc = p.replace("\\", "/").rsplit("/", 2)[-2]
        if corps and cc not in corps:
            continue
        paths.append((cc, p))

    facts = []
    for _, p in paths:
        facts.extend(_facts_for_report(p, years))

    pg = PostgresStore()
    with pg.conn.cursor() as cur:
        cur.execute("""INSERT INTO runs (run_id, pipeline_version, as_of_date, notes)
                       VALUES (%s, %s, CURRENT_DATE, '무API 규칙: 감사_투입시간·비감사용역_계약')
                       ON CONFLICT (run_id) DO NOTHING""", (RUN_ID, settings.pipeline_version))
        # (corp, fy) 단위 replace — 증분 시 다른 기업 보존
        keys = {(f["corp_code"], f["fiscal_year"]) for f in facts}
        if corps and not keys:                # 신규 보고서에 표가 없어도 대상 기업은 청소 대상 아님
            log.info("[extract-tables] 대상 기업 표 없음 — 변경 없음")
            return 0
        if corps:
            for cc, fy in keys:
                cur.execute("DELETE FROM facts WHERE fact_type = ANY(%s) AND corp_code=%s AND fiscal_year=%s",
                            (list(PARSE_TYPES), cc, fy))
        else:
            cur.execute("DELETE FROM facts WHERE fact_type = ANY(%s)", (list(PARSE_TYPES),))
        for f in facts:
            cur.execute("""INSERT INTO facts (corp_code, fiscal_year, fact_type, detail, value_raw,
                             unit_scale, evidence_text, section_path, rcept_no, dcm_no, dart_url,
                             confidence, run_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ok',%s)""",
                        (f["corp_code"], f["fiscal_year"], f["fact_type"],
                         json.dumps(f["detail"], ensure_ascii=False), f["value_raw"], f["unit_scale"],
                         f["evidence_text"], f["section_path"], f["rcept_no"], f["dcm_no"],
                         f["dart_url"], RUN_ID))
    log.info("[extract-tables] 적재 %d건 (기업-연도 %d)", len(facts), len(keys))
    print(f"[extract-tables] 적재 {len(facts)}건 (기업-연도 {len(keys)})")
    classify_going_concern(corps=corps, pg=pg)
    return len(facts)


# ── 계속기업_불확실성 사유 분류(기존 LLM Fact의 근거문을 규칙으로 강화) ──
_GC_REASONS = [("자본잠식", ("자본잠식", "전액 잠식", "자본총계가 부", "부(-)의 자본")),
               ("영업손실·결손", ("영업손실", "당기순손실", "누적결손", "결손금", "누적 손실")),
               ("유동성(유동부채 초과)", ("유동부채가 유동자산을 초과", "유동부채 초과", "유동자산을 초과")),
               ("차입·상환 부담", ("차입금", "만기", "상환", "채무불이행", "연체", "차입약정")),
               ("거래정지·상장폐지", ("거래정지", "매매거래 정지", "상장폐지")),
               ("소송·규제", ("소송", "영업정지", "규제"))]


def classify_going_concern(corps: set[str] | None = None, pg: PostgresStore | None = None) -> int:
    """계속기업_불확실성 Fact의 evidence_text를 키워드 분류해 detail.사유분류(리스트) 추가."""
    pg = pg or PostgresStore()
    with pg.conn.cursor() as cur:
        q = "SELECT fact_id, evidence_text FROM facts WHERE fact_type='계속기업_불확실성'"
        params: list = []
        if corps:
            q += " AND corp_code = ANY(%s)"; params.append(list(corps))
        cur.execute(q, params or None)
        rows = cur.fetchall()
        n = 0
        for fid, ev in rows:
            ev = ev or ""
            reasons = [label for label, kws in _GC_REASONS if any(k in ev for k in kws)] or ["기타"]
            cur.execute("UPDATE facts SET detail = jsonb_set(coalesce(detail,'{}'::jsonb), '{사유분류}', %s::jsonb) WHERE fact_id=%s",
                        (json.dumps(reasons, ensure_ascii=False), fid))
            n += 1
    log.info("[extract-tables] 계속기업 사유분류 %d건 갱신", n)
    return n


def run_correction_history() -> int:
    """정정공시_이력: disclosures.csv의 [기재정정] 보고서 → 기업-연도 Fact(전체 재구축, 멱등).
    증분 파이프라인이 disclosures.csv에 정정 보고서를 append하므로 매 실행 최신 반영."""
    import csv as _csv
    pg = PostgresStore()
    p = settings.meta_dir / "disclosures.csv"
    rows = [r for r in _csv.DictReader(p.open(encoding="utf-8-sig"))
            if "기재정정" in (r.get("report_nm") or "").replace(" ", "")
            and str(r.get("fiscal_year") or "").isdigit() and int(r["fiscal_year"]) >= 2024]
    with pg.conn.cursor() as cur:
        cur.execute("""INSERT INTO runs (run_id, pipeline_version, as_of_date, notes)
                       VALUES (%s, %s, CURRENT_DATE, '무API 규칙 Fact') ON CONFLICT (run_id) DO NOTHING""",
                    (RUN_ID, settings.pipeline_version))
        cur.execute("DELETE FROM facts WHERE fact_type='정정공시_이력'")
        for r in rows:
            d = {"보고서": r.get("report_nm"), "정정접수일": r.get("rcept_dt"),
                 "보고서유형": r.get("period_type")}
            cur.execute("""INSERT INTO facts (corp_code, fiscal_year, fact_type, detail,
                             evidence_text, section_path, rcept_no, dcm_no, dart_url, confidence, run_id)
                           VALUES (%s,%s,'정정공시_이력',%s,%s,'공시 이력',%s,%s,%s,'ok',%s)""",
                        (r["corp_code"], int(r["fiscal_year"]), json.dumps(d, ensure_ascii=False),
                         f"{r.get('report_nm')} (접수 {r.get('rcept_dt')})",
                         r["rcept_no"], r.get("dcm_no") or None, r.get("dart_url") or None, RUN_ID))
    log.info("[extract-tables] 정정공시_이력 %d건 재구축", len(rows))
    print(f"[extract-tables] 정정공시_이력 {len(rows)}건")
    return len(rows)
