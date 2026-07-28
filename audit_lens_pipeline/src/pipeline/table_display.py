"""표 청크 표시용 역직렬화 — parse_v2의 파이프 직렬화(검색용)를 읽기 형태로.

직렬화 형식은 parse_v2(_table_lines·_make_chunks_v2)가 생성하며 우리가 전적으로 통제한다:

    〔표: 제목〕 (단위: 백만원)
    머리1 | 머리2 | …
    값1 | 값2 | …

이 모듈은 그 형식만을 되읽는 결정적 짝(역직렬화기)이다 — 휴리스틱 추측이 아니다.
〔 〕(U+3014/15)는 공시 원문에 등장하지 않는 우리 전용 마커라 표 시작 판정이 확정적이다.

원칙: 인용 기계검증(quote 문자열 대조)은 원문 직렬화 기준으로 끝난 뒤에 호출되며,
여기서는 '표시용' 필드(quote_display·context_display)만 만든다. 원본 필드는 불변.
"""
from __future__ import annotations

import re

_CAP = "〔표:"
_SEP = " | "
_ROW_MARK = "▸ "


def has_table(text: str) -> bool:
    """우리 표 캡션 마커 존재 여부(확정 판정)."""
    return _CAP in (text or "")


def _pair_row(header: list[str], line: str) -> str:
    """데이터 행 한 줄 → '머리: 값 · 머리: 값'. 머리가 없거나 폭이 어긋나면 원문 유지(추측 금지)."""
    cells = [c.strip() for c in line.split("|")]
    if not header or len(cells) != len(header):
        return _ROW_MARK + line.strip()
    pairs = [f"{h}: {v}" if h and h != v else v
             for h, v in zip(header, cells) if v]
    return _ROW_MARK + " · ".join(pairs) if pairs else _ROW_MARK + line.strip()


def _walk(text: str):
    """청크 텍스트를 (kind, line, header) 시퀀스로 순회.
    kind: 'text' | 'caption' | 'header' | 'row'. header는 현재 표의 머리 셀 목록."""
    header: list[str] = []
    in_table = False
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith(_CAP):
            header, in_table = [], True
            yield "caption", line, header
            continue
        if in_table and _SEP in s:
            if not header:
                header = [c.strip() for c in s.split("|")]
                yield "header", line, header
            else:
                yield "row", line, header
            continue
        if in_table and s and _SEP not in s:
            in_table = False                       # 각주·부연 → 표 종료
        yield "text", line, header


_CAP_RE = re.compile(r"^〔표:\s*(.*?)〕\s*(?:\(단위:\s*(.*?)\))?\s*$")


def display_text(text: str) -> str | None:
    """청크(context) 전체의 표시용 변환. 표가 없으면 None.

    캡션 중복 접기(A안): 긴 표를 쪼갠 조각마다 재주입된 캡션·단위는 검색용 반복이므로,
    '제목·단위 완전 동일 + 사이에 다른 본문 없음'일 때만 두 번째부터 생략한다.
    같은 제목에 단위만 바뀌면 단위만 표기한다. 판정은 문자열 동일성 비교(결정적)."""
    if not has_table(text):
        return None
    out = []
    prev_title = prev_unit = None
    sep_content = True          # 직전 캡션 이후 표 밖 본문이 있었는가
    for kind, line, header in _walk(text):
        if kind == "header":
            continue                               # 머리행은 각 행에 병합되므로 생략
        if kind == "row":
            out.append(_pair_row(header, line))
            continue
        if kind == "caption":
            m = _CAP_RE.match(line.strip())
            title, unit = (m.group(1), m.group(2) or "") if m else (line.strip(), "")
            if title == prev_title and not sep_content:
                if unit != prev_unit and unit:
                    out.append(f"(단위: {unit})")   # 같은 표 이어짐 + 단위 변경만 고지
                prev_unit = unit
            else:
                out.append(line)
                prev_title, prev_unit = title, unit
            sep_content = False
            continue
        # kind == 'text'
        out.append(line)
        if line.strip():
            sep_content = True                     # 본문이 끼면 다음 캡션은 접지 않는다
    return "\n".join(out)


def display_quote(quote: str, chunk_text: str) -> str | None:
    """인용문(표 행의 부분집합)의 표시용 변환.

    출처 청크에서 (행 → 머리) 대응을 만들고, 인용문에 실제로 포함된 행만
    같은 방식으로 변환한다. 대응되는 행이 없으면 None(원문 유지)."""
    if not quote or not has_table(chunk_text) or "|" not in quote:
        return None
    qn = re.sub(r"\s", "", quote)
    out, matched = [], False
    for kind, line, header in _walk(chunk_text):
        # 행 끝의 빈 셀 파이프는 인용 시 흔히 탈락하므로 비교에서만 제거(원문 불변)
        ln = re.sub(r"\s", "", line).rstrip("|")
        if not ln or ln not in qn:
            continue
        if kind == "caption":
            out.append(line.strip())
        elif kind == "row":
            out.append(_pair_row(header, line))
            matched = True
        elif kind == "text" and len(ln) >= 8:      # 표 밖 문장이 함께 인용된 경우
            out.append(line.strip())
    return "\n".join(out) if matched else None
