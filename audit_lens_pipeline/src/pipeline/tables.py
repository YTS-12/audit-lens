"""Stage 5b: tables — 원본 XML에서 재무 표를 '구조 그대로' 추출(테이블 스토어, §표 재구축 D안).

parse.py가 표를 파이프 텍스트로 눌러 구조(rowspan/colspan·THEAD)가 소실되던 것을 보완:
본문 'III. 재무에 관한 사항'의 TABLE 요소를 격자(columns/rows)로 전개해 PG doc_tables에 적재.
서빙 시 청크의 숫자와 대조해 해당 표를 찾아 100% 정확한 격자를 렌더(LLM 정규화는 폴백).
"""
from __future__ import annotations
import csv
import json
import re
import zipfile
from lxml import etree
from config import settings

_parser = etree.XMLParser(recover=True, huge_tree=True)
UNIT_RE = re.compile(r"\(\s*단위\s*[:：]\s*([^)]{1,40})\)")
FIN_SECTION_KW = "재무에 관한 사항"
FIN_SUBSEC_KW = ("재무제표", "주석", "요약재무정보")
_NUM_RE = re.compile(r"\d[\d,]{2,}")
MAX_ROWS = 80
# 추출 계측(전수 분석·재구축 리포트용): run()/분석 스크립트가 읽음
_STATS = {"tables": 0, "drop_nohdr": 0, "transposed": 0, "dup_fixed": 0}
# 헤더 단계가 이 단어들로만 이뤄지면 '의미 없는 그룹 캡션'으로 보고 라벨에서 제외
_GENERIC_LVL = {"합계", "구간", "구분", "내역", "유형", "소계", "계", "총계", "항목", "금액"}
MAX_COLS = 30


def _txt(el) -> str:
    return " ".join("".join(el.itertext()).split()).replace("　", " ").strip()


_SPAN = "\x1d"                                        # colspan/rowspan 연속칸 마커(표시 시 공백 처리)


def _expand_table(tbl):
    """TABLE → (rows[[str]], header_flags[bool]) — rowspan/colspan 전개(격자 복원).
    병합 연속칸은 _SPAN 마커로 표시해 '진짜 같은 값'(예: 건수 16|16)과 구분."""
    rows, flags, pending = [], [], {}      # pending: col → [남은행수, 텍스트]
    for tr in tbl.iter("TR"):
        cells = [c for c in tr if isinstance(c.tag, str) and c.tag in ("TD", "TH", "TE")]
        if not cells and not pending:
            continue
        row = {c: _SPAN + v[1] for c, v in pending.items()}  # 이전 행 rowspan 이어짐칸
        hdr = bool(cells) and all(x.tag == "TH" for x in cells)
        col, newspans = 0, {}
        for c in cells:
            while col in row:
                col += 1
            t = _txt(c)
            cs = min(int(c.get("COLSPAN") or 1), MAX_COLS)
            rs = int(c.get("ROWSPAN") or 1)
            for k in range(cs):
                while col in row:
                    col += 1
                row[col] = t if k == 0 else _SPAN + t
                if rs > 1:
                    newspans[col] = [rs - 1, t]
                col += 1
        pending = {c: [l - 1, t] for c, (l, t) in pending.items() if l > 1}
        pending.update(newspans)
        if not row:
            continue
        width = min(max(row) + 1, MAX_COLS)
        rows.append([row.get(i, "") for i in range(width)])
        flags.append(hdr)
        if len(rows) > MAX_ROWS + 12:
            break
    return rows, flags


def _merge_header(hrows: list[list[str]], width: int) -> list[str]:
    """다단 THEAD → 칼럼명 병합. 전 열 공통 값(캡션형: '책임준공' 등)은 제외하고
    열마다 위→아래 고유 값 중 **마지막 2단**만 이어 붙임(과잉 병합 방지).
    예: 정비사업+컨소시엄(전체) → '정비사업 컨소시엄(전체)'.
    같은 이름이 여러 열에 반복되면(그룹 반복 표: 브릿지론|기타 × 만기구간)
    상위 단계를 추가로 붙여 구별한다(§표 재구축 보수, 2026-07)."""
    parts_all = []
    for j in range(width):
        parts = []
        for r in hrows:
            v = (r[j] if j < len(r) else "").lstrip(_SPAN)
            if not v:
                continue
            vals = [x.lstrip(_SPAN) for x in r if x.lstrip(_SPAN)]
            if len(set(vals)) == 1 and len(vals) >= max(2, width - 1):
                continue                              # 행 전체 동일 = 그룹 캡션 → 제외
            if not parts or parts[-1] != v:
                parts.append(v)
        if len(parts) > 1:                            # 일반 연결어만으로 된 중간 단계 제거
            parts = [p for i, p in enumerate(parts)   # (예: '합계 구간' — 의미 없는 그룹 캡션)
                     if i == len(parts) - 1 or not set(p.split()) <= _GENERIC_LVL]
        if len(parts) > 1:                            # 하위가 상위를 그대로 포함 → 상위 중복 제거
            parts = [p for i, p in enumerate(parts)   # ('제공받은 지급보증' + '제공받은 지급보증 1')
                     if i == len(parts) - 1 or not parts[i + 1].startswith(p)]
        parts_all.append(parts)
    # 리프(가장 구체적 단계)에서 시작해, 동률이 생길 때만 **실제로 값이 갈리는 최근접 상위 단계**를
    # 덧붙인다(무의미한 그룹 캡션 층 건너뜀). 예: 브릿지론|합계 구간|3개월 이내 →
    # '합계 구간'은 두 그룹이 동일해 무익 → "브릿지론 3개월 이내"(과잉 병합 제거, 2026-07).
    sel = [[-2, -1] if len(p) > 1 else ([-1] if p else []) for p in parts_all]

    def _label(j: int) -> str:
        p = parts_all[j]
        return " ".join(p[i] for i in sorted(sel[j]) if -i <= len(p))

    for _round in range(3):
        cols = [_label(j) for j in range(width)]
        groups: dict[str, list[int]] = {}
        for j, c in enumerate(cols):
            if c:
                groups.setdefault(c, []).append(j)
        tied = [g for g in groups.values() if len(g) > 1]
        if not tied:
            break
        if _round == 0:
            _STATS["dup_fixed"] += 1
        changed = False
        for g in tied:
            for k in range(-3, -7, -1):               # 이미 쓴 -2 위 단계부터 위로
                if k in sel[g[0]]:
                    continue
                vals = {parts_all[j][k] if -k <= len(parts_all[j]) else "" for j in g}
                if len(vals) > 1:                     # 이 단계가 동률을 실제로 가름
                    for j in g:
                        sel[j].append(k)
                    changed = True
                    break
        if not changed:
            break
    cols = [_label(j) for j in range(width)]
    if cols and not cols[0]:
        cols[0] = "구분"
    return cols


def _tbl_record(tbl, title: str, unit: str):
    rows, flags = _expand_table(tbl)
    if not rows:
        return None
    width = max(len(r) for r in rows)
    rows = [(r + [""] * width)[:width] for r in rows]
    hrows = [r for r, f in zip(rows, flags) if f]
    body = [r for r, f in zip(rows, flags) if not f]
    if not body:                                      # THEAD 없는 표: 첫 행이 무숫자면 헤더로
        return None
    if not hrows and body and not any(_NUM_RE.search(c) for c in body[0]):
        hrows, body = [body[0]], body[1:]
    period = ""
    kept = []
    for r in body:                                    # 표 내부 '당기 | (단위: 백만원)' 행 처리
        clean = [x.lstrip(_SPAN) for x in r]
        joined = " ".join(x for x in dict.fromkeys(clean) if x)
        m = UNIT_RE.search(joined)
        nonempty = [x for x in dict.fromkeys(clean) if x]
        if m and len(nonempty) <= 2:
            unit = unit or m.group(1).strip()
            rest = joined.replace(m.group(0), "").strip()
            if rest and len(rest) <= 12:
                period = rest                         # 당기/전기/당분기말 등
            continue
        kept.append(r)
    body = kept
    nums = []
    for r in body:
        for c in r:
            if not c.startswith(_SPAN):               # 병합 연속칸 중복 집계 방지
                nums += [m.replace(",", "") for m in _NUM_RE.findall(c)]
    if len(nums) < 2 or not body:                     # 숫자 2개 미만 = 서술형 표 → 제외
        return None
    cols = _merge_header(hrows, width) if hrows else []
    # 병합 연속칸 → 공백(표시 관례), 전열이 빈 칼럼 제거(colspan 팽창 정리)
    body = [[("" if c.startswith(_SPAN) else c) for c in r] for r in body]
    keep = [j for j in range(width)
            if any((r[j] if j < len(r) else "").strip() for r in body)
            or (j < len(cols) and cols[j].strip())]
    body = [[r[j] if j < len(r) else "" for j in keep] for r in body]
    cols = [cols[j] if j < len(cols) else "" for j in keep] if cols else []
    if not [c for c in cols if c.strip()]:            # 헤더 전무 = 표시 불능 격자 → 저장 제외
        _STATS["drop_nohdr"] += 1
        return None
    # ※전치(행↔열 교환) 금지: 원문 방향을 그대로 보존한다(§표 재구축 원칙, 2026-07-23).
    # 한때 초광폭·희소 격자를 세로형으로 돌려 저장했으나 ①감사 도구는 DART 원문과 나란히
    # 대조하는 것이 주 사용법이라 방향이 다르면 신뢰가 깨지고 ②전치해도 빈 셀 비율은 그대로여서
    # 가독성 이득이 없었다. 넓고 빈 표는 서빙 단계의 격자 게이트(_grid_ok)가 걸러
    # '항목별 정리(목록)'로 내보낸다 — 목록은 표로 오인되지 않아 방향 혼동이 없다.
    _STATS["tables"] += 1
    full_title = f"{title} ({period})" if title and period else (title or period or "")
    return {"title": full_title[:120], "unit": (unit or "")[:40],
            "columns": cols, "rows": body[:MAX_ROWS],
            "numkey": " ".join(dict.fromkeys(nums))[:1500]}


def _walk_tables(sec, sec_path: list[str]):
    """SECTION-2 내부를 문서순으로 걸으며 (섹션경로, 제목, 단위, TABLE) 수집."""
    out, cur_title, cur_unit = [], "", ""
    def walk(el):
        nonlocal cur_title, cur_unit
        for ch in el:
            if not isinstance(ch.tag, str):
                continue
            if ch.tag == "TITLE":
                cur_title = _txt(ch)
            elif ch.tag == "TABLE":
                out.append((list(sec_path) + ([cur_title] if cur_title else []),
                            cur_title, cur_unit, ch))
            else:
                t = (ch.text or "").strip() if ch.tag in ("P", "SPAN") else ""
                m = UNIT_RE.search(_txt(ch)) if ch.tag in ("P", "SPAN") else None
                if m:
                    cur_unit = m.group(1).strip()
                elif t and len(t) <= 60 and ch.tag == "P":
                    cur_title = t                     # 표 직전 짧은 P = 표 제목 관례
                walk(ch)
    walk(sec)
    return out


def extract_filing(zip_path, meta) -> list[dict]:
    """한 필링의 본문 재무 섹션 표 전부 → 스토어 레코드 리스트."""
    zf = zipfile.ZipFile(zip_path)
    xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not xmls:
        return []
    rc = meta["rcept_no"]
    main_name = f"{rc}.xml" if f"{rc}.xml" in xmls else max(
        xmls, key=lambda n: zf.getinfo(n).file_size)
    main = etree.fromstring(zf.read(main_name), parser=_parser)
    recs = []
    for sec1 in main.iter("SECTION-1"):
        t1 = next((_txt(t) for t in sec1.iter("TITLE")), "")
        if FIN_SECTION_KW not in t1:
            continue
        for sec2 in sec1.iter("SECTION-2"):
            t2 = next((_txt(t) for t in sec2.iter("TITLE")), "")
            if not any(k in t2 for k in FIN_SUBSEC_KW):
                continue
            is_cons = "연결" in t2
            doc_type = "financial_note" if "주석" in t2 else "financial_stmt"
            for path, title, unit, tbl in _walk_tables(sec2, [t1, t2]):
                r = _tbl_record(tbl, title, unit)
                if r:
                    r.update({"corp_code": meta["corp_code"], "rcept_no": rc,
                              "fiscal_year": int(meta.get("fiscal_year") or 0),
                              "is_consolidated": is_cons, "doc_type": doc_type,
                              "section_path": " > ".join(p for p in path if p)[:300],
                              "dart_url": meta.get("dart_url", "")})
                    recs.append(r)
    return recs


_FIN_DOCTYPES = ("financial_stmt", "financial_note")


def extract_filing_extra(zip_path, meta) -> list[dict]:
    """보고서 '전체'로 표 추출 확장(§표 스토어 v2) — 기존 재무 섹션 외:
    (a) 본문 나머지 SECTION-1(사업의 내용·감사의견 등) → doc_type=report_body/audit
    (b) 첨부 XML(감사·검토보고서 등) → doc_type=audit/attachment
    재무 섹션은 기존 run()이 담당하므로 여기선 건너뜀(증분 병행)."""
    zf = zipfile.ZipFile(zip_path)
    xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not xmls:
        return []
    rc = meta["rcept_no"]
    main_name = f"{rc}.xml" if f"{rc}.xml" in xmls else max(
        xmls, key=lambda n: zf.getinfo(n).file_size)
    recs = []

    def _emit(path, title, unit, tbl, doc_type):
        r = _tbl_record(tbl, title, unit)
        if r:
            r.update({"corp_code": meta["corp_code"], "rcept_no": rc,
                      "fiscal_year": int(meta.get("fiscal_year") or 0),
                      "is_consolidated": "연결" in " ".join(path),
                      "doc_type": doc_type,
                      "section_path": " > ".join(p for p in path if p)[:300],
                      "dart_url": meta.get("dart_url", "")})
            recs.append(r)

    # (a) 본문의 비재무 SECTION-1 전부
    main = etree.fromstring(zf.read(main_name), parser=_parser)
    for sec1 in main.iter("SECTION-1"):
        t1 = next((_txt(t) for t in sec1.iter("TITLE")), "")
        if FIN_SECTION_KW in t1:
            continue                                   # 재무 섹션은 기존 적재분
        dt = "audit" if ("감사" in t1 or "검토" in t1) else "report_body"
        for path, title, unit, tbl in _walk_tables(sec1, [t1]):
            _emit(path, title, unit, tbl, dt)

    # (b) 첨부 XML(감사보고서·내부회계 등)
    for n in xmls:
        if n == main_name:
            continue
        try:
            r2 = etree.fromstring(zf.read(n), parser=_parser)
        except Exception:  # noqa: BLE001
            continue
        att_title = next((_txt(t) for t in r2.iter("TITLE")), "") or "첨부문서"
        norm = att_title.replace(" ", "")
        dt = "audit" if ("감사" in norm or "검토" in norm) else "attachment"
        for path, title, unit, tbl in _walk_tables(r2, [att_title]):
            _emit(path, title, unit, tbl, dt)
    return recs


def run_extra(limit: int | None = None, corp: str | None = None):
    """보고서 전체 확장분 배치(재무 섹션 제외 증분). 재실행 안전."""
    from src.clients.postgres import PostgresStore
    pg = PostgresStore()
    pg.ensure_doc_tables()
    disc = list(csv.DictReader((settings.meta_dir / "disclosures.csv").open(encoding="utf-8-sig")))
    if corp:
        disc = [r for r in disc if corp in (r["corp_code"], r["corp_name"])]
    if limit:
        disc = disc[:limit]
    done = pg.doc_tables_rcepts_extra()
    todo = [r for r in disc if r["rcept_no"] not in done]
    print(f"[tables-extra] 대상 {len(disc)} · 스킵 {len(disc)-len(todo)} · 진행 {len(todo)}")
    ok = n_tbl = fail = miss = 0
    for i, r in enumerate(todo, 1):
        zp = settings.raw_dir / r["corp_code"] / f"{r['rcept_no']}.zip"
        if not zp.exists():
            miss += 1
            continue
        try:
            recs = extract_filing_extra(zp, r)
            pg.replace_doc_tables_extra(r["rcept_no"], recs)
            ok += 1
            n_tbl += len(recs)
        except Exception as e:                        # noqa: BLE001
            fail += 1
            if fail <= 5:
                print(f"  실패 {r['corp_name']} {r['rcept_no']}: {str(e)[:120]}")
        if i % 100 == 0:
            print(f"  …{i}/{len(todo)} (필링 {ok} · 표 {n_tbl} · 실패 {fail})", flush=True)
    print(f"[tables-extra] 완료: 필링 {ok} · 표 {n_tbl} · 실패 {fail} · zip없음 {miss}")
    return ok, n_tbl


def run(limit: int | None = None, corp: str | None = None):
    """전체 필링 배치 추출 → PG doc_tables 적재(기존 rcept는 교체)."""
    from src.clients.postgres import PostgresStore
    pg = PostgresStore()
    pg.ensure_doc_tables()
    disc = list(csv.DictReader((settings.meta_dir / "disclosures.csv").open(encoding="utf-8-sig")))
    if corp:
        disc = [r for r in disc if corp in (r["corp_code"], r["corp_name"])]
    if limit:
        disc = disc[:limit]
    done = pg.doc_tables_rcepts()                     # 재실행 안전(이미 적재된 필링 스킵)
    todo = [r for r in disc if r["rcept_no"] not in done]
    print(f"[tables] 대상 {len(disc)} · 스킵(적재됨) {len(disc)-len(todo)} · 진행 {len(todo)}")
    ok = n_tbl = fail = miss = 0
    for i, r in enumerate(todo, 1):
        zp = settings.raw_dir / r["corp_code"] / f"{r['rcept_no']}.zip"
        if not zp.exists():
            miss += 1
            continue
        try:
            recs = extract_filing(zp, r)
            pg.replace_doc_tables(r["rcept_no"], recs)
            ok += 1
            n_tbl += len(recs)
        except Exception as e:                        # noqa: BLE001
            fail += 1
            if fail <= 5:
                print(f"  실패 {r['corp_name']} {r['rcept_no']}: {str(e)[:120]}")
        if i % 100 == 0:
            print(f"  …{i}/{len(todo)} (필링 {ok} · 표 {n_tbl} · 실패 {fail})", flush=True)
    print(f"[tables] 완료: 필링 {ok} · 표 {n_tbl} · 실패 {fail} · zip없음 {miss}")
    return ok, n_tbl
