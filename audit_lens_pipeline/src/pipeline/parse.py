"""Stage 5: parse — 연결(없으면 별도) 기준 섹션-인지 청크 생성.

DART XML(<DOCUMENT>) 구조(포맷 인벤토리 결과):
- 본문 사업보고서: SECTION-1(대)>SECTION-2(소)>TITLE. 표는 TABLE/TR/TD/TH/TE.
  'III. 재무에 관한 사항' 아래 SECTION-2: 연결재무제표 / 연결재무제표 주석 /
  재무제표 / 재무제표 주석 … → 제목의 '연결' 포함 여부로 연결/별도 구분.
  주석/재무제표 SECTION-2 안의 번호형 TITLE이 개별 주석·개별 표(문서순서 분할).
- 첨부 감사보고서(_NNNNN.xml): '독립된 감사인의 감사보고서'(감사의견·근거·KAM·강조)와
  '내부회계관리제도 감사/검토의견'만 추출. 첨부 재무제표·주석은 본문과 중복이므로 제외.
  제목 '(첨부)연결재무제표'면 연결 감사보고서, '(첨부)재무제표'면 별도.

산출: data/<ver>/parsed/<corp>/<rcept>.jsonl (청크) + parse_failures.csv 격리.
PHASE0_FINDINGS 반영: 번호형 주석 헤딩, 단위 텍스트 추출, 본문+첨부 동시.
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
NOTE_NO_RE = re.compile(r"^\s*(\d+(?:-\d+)?)\s*[.．]")
FIN_SECTION_KW = "재무에 관한 사항"
FIN_SUBSEC_KW = ("재무제표", "주석", "요약재무정보")
AUDIT_OPINION_SEC = ("감사인의 감사의견", "회계감사인")   # 본문 'V. 회계감사인의 감사의견 등'
AUDIT_KEEP = ("독립된", "내부회계관리제도")
AUDIT_SKIP = ("첨부", "재무제표", "주석", "목차", "외부감사실시내용",
              "감사대상업무", "감사참여자", "감사실시내용", "커뮤니케이션")
CHUNK_CHARS = 1400
CHUNK_OVERLAP = 150


def _txt(el) -> str:
    return " ".join("".join(el.itertext()).split())


def _table_to_text(tbl) -> str:
    rows = []
    for tr in tbl.iter("TR"):
        cells = [_txt(c) for c in tr
                 if isinstance(c.tag, str) and c.tag in ("TD", "TH", "TE")]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _split_by_titles(container):
    """container를 TITLE 경계로 문서순서 분할 → [(title, body_text), ...].
    표는 통째로 직렬화(내부로 재귀 안 함), 그 외 컨테이너는 재귀. 빈 본문 제외."""
    segments, state = [], {"title": None, "buf": []}

    def emit():
        if state["title"] is not None:
            segments.append((state["title"], "\n".join(state["buf"])))

    def add(s):
        if s and s.strip():
            state["buf"].append(s.strip())

    def walk(el):
        for ch in el:
            if not isinstance(ch.tag, str):       # 주석/PI — 꼬리텍스트만
                add(ch.tail)
                continue
            tag = ch.tag
            if tag == "TITLE":
                emit()
                state["title"], state["buf"] = _txt(ch), []
                add(ch.tail)
            elif tag == "TABLE":
                state["buf"].append(_table_to_text(ch))
                add(ch.tail)
            else:                                 # SECTION/P/SPAN/TE 등 — 재귀하며 텍스트 수집
                add(ch.text)                      # (감사보고서 본문은 SECTION 안에 있음)
                walk(ch)
                add(ch.tail)
    walk(container)
    emit()
    return [(t, b) for t, b in segments if b.strip()]


def _chunk_text(body: str):
    body = body.strip()
    if len(body) <= CHUNK_CHARS:
        return [body]
    out, i = [], 0
    step = CHUNK_CHARS - CHUNK_OVERLAP
    while i < len(body):
        out.append(body[i:i + CHUNK_CHARS])
        i += step
    return out


def _make_chunks(meta, path_list, body, is_cons, doc_type):
    dedup = []
    for p in path_list:                       # 연속 중복 제목 제거
        if p and (not dedup or dedup[-1] != p):
            dedup.append(p)
    section_path = " > ".join(dedup)
    units = sorted({u.strip() for u in UNIT_RE.findall(body)})
    m = NOTE_NO_RE.match(dedup[-1]) if dedup else None
    note_no = m.group(1) if m else ""
    basis = "연결" if is_cons else "별도"
    header = (f"[{meta['corp_name']} · {meta['fiscal_year']}년 · "
              f"{meta['assurance']} · {basis} · {section_path}]")
    out = []
    for i, part in enumerate(_chunk_text(body)):
        out.append({
            "corp_code": meta["corp_code"], "corp_name": meta["corp_name"],
            "fiscal_year": meta["fiscal_year"], "assurance": meta["assurance"],
            "period_type": meta["period_type"], "is_consolidated": is_cons,
            "doc_type": doc_type, "section_path": section_path, "note_no": note_no,
            "chunk_ix": i, "unit_hint": units, "has_table": "|" in part,
            "rcept_no": meta["rcept_no"], "dcm_no": meta.get("dcm_no", ""),
            "dart_url": meta.get("dart_url", ""),
            "text": header + "\n" + part,
        })
    return out


def _first_title(sec) -> str:
    for t in sec.iter("TITLE"):
        return _txt(t)
    return ""


def _parse_financial_sections(main, meta):
    chunks = []
    for sec1 in main.iter("SECTION-1"):
        if FIN_SECTION_KW not in _first_title(sec1):
            continue
        sec1_title = _first_title(sec1)
        for sec2 in sec1.iter("SECTION-2"):
            sec2_title = _first_title(sec2)
            if not any(k in sec2_title for k in FIN_SUBSEC_KW):
                continue
            is_cons = "연결" in sec2_title
            doc_type = "financial_note" if "주석" in sec2_title else "financial_stmt"
            for seg_title, body in _split_by_titles(sec2):
                path = [sec1_title, sec2_title, seg_title]
                chunks += _make_chunks(meta, path, body, is_cons, doc_type)
    return chunks


def _parse_audit_opinion(main, meta):
    """본문 'V. 회계감사인의 감사의견 등' SECTION-1 → 감사의견·KAM(전 회사 공통).
    단일파일 보고서(첨부 감사보고서 없음)도 이 섹션으로 감사정보를 확보한다."""
    rep_cons = str(meta.get("is_consolidated", "")).lower() == "true"
    chunks = []
    for sec1 in main.iter("SECTION-1"):
        title = _first_title(sec1)
        if not any(k in title for k in AUDIT_OPINION_SEC):
            continue
        for seg_title, body in _split_by_titles(sec1):
            chunks += _make_chunks(meta, [title, seg_title], body, rep_cons, "audit")
    return chunks


def _classify_audit(root):
    titles = " ".join(_txt(t) for t in root.iter("TITLE")).replace(" ", "")
    is_audit = "감사보고서" in titles or "독립된감사인" in titles
    is_cons = "연결재무제표" in titles or "연결내부회계" in titles
    return is_audit, is_cons


def _parse_audit(root, meta, is_cons):
    basis = "연결감사보고서" if is_cons else "감사보고서"
    chunks = []
    for title, body in _split_by_titles(root):
        tnorm = title.replace(" ", "")
        if any(s in tnorm for s in AUDIT_SKIP):
            continue
        if not any(k in tnorm for k in AUDIT_KEEP):
            continue
        chunks += _make_chunks(meta, [basis, title], body, is_cons, "audit")
    return chunks


def parse_report(zip_path, meta):
    zf = zipfile.ZipFile(zip_path)
    xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not xmls:
        raise ValueError("XML 없음(이미지/PDF 의심)")
    rc = meta["rcept_no"]
    main_name = f"{rc}.xml" if f"{rc}.xml" in xmls else max(
        xmls, key=lambda n: zf.getinfo(n).file_size)
    main = etree.fromstring(zf.read(main_name), parser=_parser)
    chunks = _parse_financial_sections(main, meta)
    chunks += _parse_audit_opinion(main, meta)   # 본문 감사의견 섹션(전 회사 공통)
    for n in xmls:
        if n == main_name:
            continue
        root = etree.fromstring(zf.read(n), parser=_parser)
        is_audit, is_cons = _classify_audit(root)
        if is_audit:
            chunks += _parse_audit(root, meta, is_cons)
    return chunks


def run(limit: int | None = None, corp: str | None = None):
    settings.ensure_dirs()
    parsed_dir = settings.data_dir / settings.pipeline_version / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    disc = list(csv.DictReader((settings.meta_dir / "disclosures.csv").open(encoding="utf-8-sig")))
    if corp:
        disc = [r for r in disc if corp in (r["corp_code"], r["corp_name"])]
    if limit:
        disc = disc[:limit]
    print(f"[parse] 대상 보고서 {len(disc)}건")

    ok = chunks_total = skipped = 0
    failures = []
    for i, r in enumerate(disc, 1):
        zp = settings.raw_dir / r["corp_code"] / f"{r['rcept_no']}.zip"
        if not zp.exists():
            skipped += 1
            continue
        try:
            chunks = parse_report(zp, r)
            if not chunks:
                failures.append({"rcept_no": r["rcept_no"], "corp_name": r["corp_name"],
                                 "reason": "청크 0(섹션 미검출)"})
                continue
            out = parsed_dir / r["corp_code"]
            out.mkdir(parents=True, exist_ok=True)
            with (out / f"{r['rcept_no']}.jsonl").open("w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
            ok += 1
            chunks_total += len(chunks)
        except Exception as e:                       # noqa
            failures.append({"rcept_no": r["rcept_no"], "corp_name": r["corp_name"],
                             "reason": str(e)[:200]})
        if i % 100 == 0:
            print(f"  …{i}/{len(disc)} (ok={ok}, 청크={chunks_total}, 실패={len(failures)})")

    if failures:
        fp = settings.meta_dir / "parse_failures.csv"
        with fp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(failures[0].keys()))
            w.writeheader(); w.writerows(failures)
        print(f"[parse] 실패 {len(failures)}건 → {fp} (Dead-letter)")
    print(f"[parse] 완료: 보고서 {ok} · 청크 {chunks_total} · "
          f"파일없음 {skipped} · 실패 {len(failures)} → {parsed_dir}")
    return ok, chunks_total
