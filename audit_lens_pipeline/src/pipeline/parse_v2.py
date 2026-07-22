# -*- coding: utf-8 -*-
"""파서 v2 — 구조 우선 청킹(임베딩 전수조사 2026-07 반영). v1(parse.py)은 불변, 출력=parsed_v2/.

v1 대비 개선 4가지(전수조사에서 확인된 문제와 1:1 대응):
① 블록(문단/표) 경계 청킹 — 1,400자 기계 절단 제거(문장·숫자 중간 절단 0 목표)
② 표는 통째 우선, 한도 초과 시 '행 단위' 분할하되 [표 캡션+단위+칼럼 헤더행]을 매 파트에 반복
   → '이어진 표 헤더행 유실 32.7만 청크' 해소, 어느 파트를 검색해도 칼럼-행 매칭 유지
③ rowspan/colspan 격자 전개(tables._expand_table 재사용) + 다단 헤더 병합(_merge_header)
   → '행별 칼럼 수 불일치 84%' 해소, 병합 연속칸은 값 반복(각 행이 자체 완결 → 검색 신호↑)
④ 표 직후 각주(주1·(*) 등)를 표 마지막 파트에 결합 → '각주 분리 13.2만' 해소
부가: 표별 단위 캡처(직전 '(단위: X)'), 청크 메타에 table_title 추가.
"""
from __future__ import annotations
import csv
import json
import re
import zipfile

from config import settings
from src.pipeline.parse import (_txt, _first_title, _classify_audit, _parser,
                                UNIT_RE, NOTE_NO_RE, FIN_SECTION_KW, FIN_SUBSEC_KW,
                                AUDIT_OPINION_SEC, AUDIT_SKIP, AUDIT_KEEP)
from src.pipeline.tables import _expand_table, _merge_header, _SPAN

CHUNK_CHARS = 1400
FOOT_RE = re.compile(r"^\s*(\(\*|\(주\s*\d|주\s*\d+\)|※)")
SENT_SPLIT = re.compile(r"(?<=다\.)\s+|(?<=음\.)\s+|(?<=함\.)\s+")


# ── 블록 수집: TITLE 경계 분할 + TABLE은 요소 그대로 보존 ──
def _blocks_by_titles(container):
    """[(title, blocks)] — blocks = [("t", text) | ("tbl", Element)] 문서 순서 유지."""
    segments, state = [], {"title": None, "blocks": [], "buf": []}

    def flush_text():
        t = "\n".join(state["buf"]).strip()
        if t:
            state["blocks"].append(("t", t))
        state["buf"] = []

    def emit():
        flush_text()
        if state["title"] is not None and state["blocks"]:
            segments.append((state["title"], state["blocks"]))
        state["blocks"] = []

    def add(s):
        if s and s.strip():
            state["buf"].append(s.strip())

    def walk(el):
        for ch in el:
            if not isinstance(ch.tag, str):
                add(ch.tail)
                continue
            if ch.tag == "TITLE":
                emit()
                state["title"] = _txt(ch)
                add(ch.tail)
            elif ch.tag == "TABLE":
                flush_text()
                state["blocks"].append(("tbl", ch))
                add(ch.tail)
            else:
                add(ch.text)
                walk(ch)
                add(ch.tail)
    walk(container)
    emit()
    return segments


# ── 표 → (헤더행 문자열, 데이터행 문자열들) — 병합셀 전개·값 반복 ──
def _table_lines(tbl):
    rows, flags = _expand_table(tbl)
    if not rows:
        return None, []
    width = max(len(r) for r in rows)
    rows = [(r + [""] * width)[:width] for r in rows]
    hrows = [r for r, f in zip(rows, flags) if f]
    body = [r for r, f in zip(rows, flags) if not f]
    if not hrows and body and not re.search(r"\d", " ".join(body[0])):
        hrows, body = [body[0]], body[1:]
    cols = _merge_header(hrows, width) if hrows else []
    header_line = " | ".join(c.strip() for c in cols) if cols else ""
    data = []
    for r in body:
        cells = [c[1:] if c.startswith(_SPAN) else c for c in r]   # 병합 연속칸=값 반복(자체 완결 행)
        line = " | ".join(x.strip() for x in cells).strip()
        if line.replace("|", "").strip():
            data.append(line)
    return header_line, data


def _split_long_text(text: str) -> list[str]:
    """텍스트 블록을 문장 경계 우선으로 CHUNK_CHARS 이하 조각들로(숫자 중간 절단 방지)."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        pieces = [line] if len(line) <= CHUNK_CHARS else SENT_SPLIT.split(line)
        for p in pieces:
            while len(p) > CHUNK_CHARS:                 # 극단 장문: 공백 경계 강제 분할
                cut = p.rfind(" ", 0, CHUNK_CHARS)
                cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
                head, p = p[:cut], p[cut:].lstrip()
                if len(cur) + len(head) + 1 > CHUNK_CHARS and cur:
                    out.append(cur); cur = ""
                cur = (cur + "\n" + head).strip()
            if len(cur) + len(p) + 1 > CHUNK_CHARS and cur:
                out.append(cur); cur = ""
            cur = (cur + "\n" + p).strip()
    if cur:
        out.append(cur)
    return out


def _make_chunks_v2(meta, path_list, blocks, is_cons, doc_type):
    dedup = []
    for p in path_list:
        if p and (not dedup or dedup[-1] != p):
            dedup.append(p)
    section_path = " > ".join(dedup)
    m = NOTE_NO_RE.match(dedup[-1]) if dedup else None
    note_no = m.group(1) if m else ""
    basis = "연결" if is_cons else "별도"
    header = (f"[{meta['corp_name']} · {meta['fiscal_year']}년 · "
              f"{meta['assurance']} · {basis} · {section_path}]")

    parts: list[dict] = []                              # {"text":…, "table_title":…}
    last_unit = ""                                      # 직전 텍스트에서 캡처한 표 단위
    last_caption = dedup[-1] if dedup else ""           # 표 제목 후보(직전 짧은 텍스트 줄)
    buf: list[str] = []                                 # 텍스트·짧은 표 패킹 버퍼(청크 폭증 방지)
    buf_meta = {"table_title": ""}

    def flush_buf():
        if buf:
            parts.append({"text": "\n".join(buf), "table_title": buf_meta["table_title"]})
            buf.clear(); buf_meta["table_title"] = ""

    def buf_len():
        return sum(len(x) + 1 for x in buf)

    def pack(piece: str, table_title: str = ""):
        if buf and buf_len() + len(piece) > CHUNK_CHARS:
            flush_buf()
        buf.append(piece)
        if table_title:
            buf_meta["table_title"] = table_title

    def add_text_part(t):
        for piece in _split_long_text(t):
            pack(piece)

    i = 0
    while i < len(blocks):
        kind, payload = blocks[i]
        if kind == "t":
            u = UNIT_RE.findall(payload)
            if u:
                last_unit = u[-1].strip()
            lines = [ln for ln in payload.split("\n") if ln.strip()]
            if lines and len(lines[-1]) <= 60 and not FOOT_RE.match(lines[-1]):
                last_caption = lines[-1]                # 다음 표의 제목 후보
            add_text_part(payload)
            i += 1
            continue
        # 표 블록
        header_line, data = _table_lines(payload)
        if not data:
            i += 1
            continue
        caption = f"〔표: {last_caption or dedup[-1]}〕" + (f" (단위: {last_unit})" if last_unit else "")
        prefix = caption + ("\n" + header_line if header_line else "")
        budget = CHUNK_CHARS - len(prefix) - len(header) - 8
        tbl_parts, cur, cur_len = [], [], 0
        for ln in data:
            if cur and cur_len + len(ln) + 1 > budget:
                tbl_parts.append(cur); cur, cur_len = [], 0
            cur.append(ln); cur_len += len(ln) + 1
        if cur:
            tbl_parts.append(cur)
        # ④ 표 직후 각주/짧은 부연은 마지막 파트에 결합
        foot = ""
        if i + 1 < len(blocks) and blocks[i + 1][0] == "t":
            nxt = blocks[i + 1][1]
            if FOOT_RE.match(nxt.strip()) or len(nxt) <= 400:
                foot = nxt.strip()
                i += 1
        tbl_title = last_caption or (dedup[-1] if dedup else "")
        if len(tbl_parts) == 1:                          # 짧은 표(파트 1개) → 주변 텍스트와 패킹
            t = prefix + "\n" + "\n".join(tbl_parts[0])
            if foot and len(t) + len(foot) + 1 <= CHUNK_CHARS:
                t += "\n" + foot; foot = ""
            if len(t) <= CHUNK_CHARS:
                pack(t, table_title=tbl_title)
            else:
                flush_buf()
                parts.append({"text": t, "table_title": tbl_title})
        else:                                            # 긴 표 → 독립 파트(캡션+헤더행 반복)
            flush_buf()
            for pi, rows in enumerate(tbl_parts):
                t = prefix + "\n" + "\n".join(rows)
                if foot and pi == len(tbl_parts) - 1 and len(t) + len(foot) + 1 <= CHUNK_CHARS + 400:
                    t += "\n" + foot; foot = ""
                parts.append({"text": t, "table_title": tbl_title})
        if foot:
            pack(caption + "\n" + foot, table_title=tbl_title)
        i += 1

    flush_buf()                                          # 잔여 버퍼 마감
    units_all = sorted({u.strip() for p in parts for u in UNIT_RE.findall(p["text"])})
    out = []
    for ix, p in enumerate(parts):
        out.append({
            "corp_code": meta["corp_code"], "corp_name": meta["corp_name"],
            "fiscal_year": meta["fiscal_year"], "assurance": meta["assurance"],
            "period_type": meta["period_type"], "is_consolidated": is_cons,
            "doc_type": doc_type, "section_path": section_path, "note_no": note_no,
            "chunk_ix": ix, "unit_hint": units_all, "has_table": "|" in p["text"],
            "table_title": p["table_title"],
            "rcept_no": meta["rcept_no"], "dcm_no": meta.get("dcm_no", ""),
            "dart_url": meta.get("dart_url", ""),
            "text": header + "\n" + p["text"],
        })
    return out


# ── 보고서 처리(섹션 탐색은 v1과 동일 기준) ──
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
            for seg_title, blocks in _blocks_by_titles(sec2):
                chunks += _make_chunks_v2(meta, [sec1_title, sec2_title, seg_title],
                                          blocks, is_cons, doc_type)
    return chunks


def _parse_audit_opinion(main, meta):
    rep_cons = str(meta.get("is_consolidated", "")).lower() == "true"
    chunks = []
    for sec1 in main.iter("SECTION-1"):
        title = _first_title(sec1)
        if not any(k in title for k in AUDIT_OPINION_SEC):
            continue
        for seg_title, blocks in _blocks_by_titles(sec1):
            chunks += _make_chunks_v2(meta, [title, seg_title], blocks, rep_cons, "audit")
    return chunks


def _parse_audit(root, meta, is_cons):
    basis = "연결감사보고서" if is_cons else "감사보고서"
    chunks = []
    for title, blocks in _blocks_by_titles(root):
        tnorm = title.replace(" ", "")
        if any(s in tnorm for s in AUDIT_SKIP):
            continue
        if not any(k in tnorm for k in AUDIT_KEEP):
            continue
        chunks += _make_chunks_v2(meta, [basis, title], blocks, is_cons, "audit")
    return chunks


def parse_report_v2(zip_path, meta):
    zf = zipfile.ZipFile(zip_path)
    xmls = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    if not xmls:
        raise ValueError("XML 없음(이미지/PDF 의심)")
    rc = meta["rcept_no"]
    main_name = f"{rc}.xml" if f"{rc}.xml" in xmls else max(
        xmls, key=lambda n: zf.getinfo(n).file_size)
    from lxml import etree
    main = etree.fromstring(zf.read(main_name), parser=_parser)
    chunks = _parse_financial_sections(main, meta)
    chunks += _parse_audit_opinion(main, meta)
    for n in xmls:
        if n == main_name:
            continue
        root = etree.fromstring(zf.read(n), parser=_parser)
        is_audit, is_cons = _classify_audit(root)
        if is_audit:
            chunks += _parse_audit(root, meta, is_cons)
    return chunks


def run(limit: int | None = None, corp: str | None = None, corps: set | None = None):
    settings.ensure_dirs()
    out_root = settings.data_dir / settings.pipeline_version / "parsed_v2"
    out_root.mkdir(parents=True, exist_ok=True)
    disc = list(csv.DictReader((settings.meta_dir / "disclosures.csv").open(encoding="utf-8-sig")))
    if corp:
        disc = [r for r in disc if corp in (r["corp_code"], r["corp_name"])]
    if corps:
        disc = [r for r in disc if r["corp_code"] in corps or r["corp_name"] in corps]
    if limit:
        disc = disc[:limit]
    print(f"[parse_v2] 대상 보고서 {len(disc)}건")
    ok = total = 0
    failures = []
    for i, r in enumerate(disc, 1):
        zp = settings.raw_dir / r["corp_code"] / f"{r['rcept_no']}.zip"
        if not zp.exists():
            continue
        try:
            chunks = parse_report_v2(zp, r)
            if not chunks:
                failures.append({"rcept_no": r["rcept_no"], "reason": "청크 0"})
                continue
            out = out_root / r["corp_code"]
            out.mkdir(parents=True, exist_ok=True)
            with (out / f"{r['rcept_no']}.jsonl").open("w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
            ok += 1
            total += len(chunks)
        except Exception as e:  # noqa: BLE001
            failures.append({"rcept_no": r["rcept_no"], "reason": str(e)[:150]})
        if i % 200 == 0:
            print(f"  …{i}/{len(disc)} ok={ok} 청크={total} 실패={len(failures)}")
    print(f"[parse_v2] 완료 ok={ok} 청크={total} 실패={len(failures)}")
    if failures:
        fp = out_root / "parse_v2_failures.json"
        fp.write_text(json.dumps(failures, ensure_ascii=False, indent=1), encoding="utf-8")
    return ok, total, failures
