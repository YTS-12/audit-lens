"""OpenDART API 클라이언트.
참고: https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001

제공 메서드
- corp_codes()           : 고유번호 전체 (corp_code, corp_name, stock_code)
- company(corp_code)     : 기업개황 (induty_code, corp_cls, 결산월 등)
- list_disclosures(...)  : 공시검색 (정기보고서 rcept_no)
- download_document(...) : 공시서류 원본파일(ZIP) 저장
- financials(...)        : 단일회사 전체 재무제표(fnlttSinglAcntAll)
"""
from __future__ import annotations
import io
import re
import time
import zipfile
from pathlib import Path
from typing import Iterator
import requests
from tenacity import retry, stop_after_attempt, wait_exponential
from lxml import etree

BASE = "https://opendart.fss.or.kr/api"

# 정기보고서 보고서코드
REPRT_CODE = {"annual": "11011", "half": "11012", "q1": "11013", "q3": "11014"}


class OpenDartError(Exception):
    """OpenDART API가 HTTP 200 + 오류 XML(예: <status>014</status> '파일이 존재하지
    않습니다')을 반환했을 때. document.xml은 [첨부정정] 보고서 등에 014를 준다."""
    def __init__(self, rcept_no: str, status: str, message: str):
        self.rcept_no, self.status, self.message = rcept_no, status, message
        super().__init__(f"OpenDART [{status}] {message} (rcept_no={rcept_no})")


class OpenDartClient:
    def __init__(self, api_key: str, rate_per_sec: float = 2.0,
                 timeout: int = 30, max_retry: int = 4,
                 viewer_rate_per_sec: float = 1.5):
        self.key = api_key
        self.timeout = timeout
        self.max_retry = max_retry
        self._min_interval = 1.0 / rate_per_sec if rate_per_sec else 0.0
        self._last = 0.0
        # DART 뷰어(dart.fss.or.kr)는 OpenDART API와 별개 호스트 → 별도 스로틀.
        # 스로틀 없이 연타하면 IP가 차단된다(RemoteDisconnected). 기본 1.5 req/s.
        self._viewer_min = 1.0 / viewer_rate_per_sec if viewer_rate_per_sec else 0.0
        self._viewer_last = 0.0
        self._s = requests.Session()

    def _viewer_throttle(self):
        if self._viewer_min:
            wait = self._viewer_min - (time.time() - self._viewer_last)
            if wait > 0:
                time.sleep(wait)
        self._viewer_last = time.time()

    def _throttle(self):
        if self._min_interval:
            wait = self._min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
        self._last = time.time()

    def _get(self, path: str, **params):
        @retry(stop=stop_after_attempt(self.max_retry),
               wait=wait_exponential(multiplier=1, max=20), reraise=True)
        def _call():
            self._throttle()
            params["crtfc_key"] = self.key
            r = self._s.get(f"{BASE}/{path}", params=params, timeout=self.timeout)
            r.raise_for_status()
            return r
        return _call()

    # ── 고유번호 (ZIP→XML) ──
    def corp_codes(self) -> list[dict]:
        r = self._get("corpCode.xml")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml = zf.read(zf.namelist()[0])
        root = etree.fromstring(xml)
        out = []
        for el in root.findall("list"):
            out.append({
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "stock_code": (el.findtext("stock_code") or "").strip(),
                "modify_date": (el.findtext("modify_date") or "").strip(),
            })
        return out

    # ── 기업개황 ──
    def company(self, corp_code: str) -> dict:
        return self._get("company.json", corp_code=corp_code).json()

    # ── 공시검색 (페이지네이션) ──
    def list_disclosures(self, corp_code: str | None, bgn_de: str, end_de: str,
                         pblntf_ty: str = "A") -> Iterator[dict]:
        """corp_code=None이면 전(全)시장 조회(증분 파이프라인용) — 기간 내 정기공시 전체."""
        page = 1
        while True:
            params = dict(bgn_de=bgn_de, end_de=end_de, pblntf_ty=pblntf_ty,
                          page_no=page, page_count=100)
            if corp_code:
                params["corp_code"] = corp_code
            j = self._get("list.json", **params).json()
            if j.get("status") != "000":
                return  # 013=데이터 없음 등
            for item in j.get("list", []):
                yield item
            if page >= int(j.get("total_page", 1)):
                return
            page += 1

    # ── 원본 문서 다운로드 (ZIP 검증 포함) ──
    def download_document(self, rcept_no: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = dest_dir / f"{rcept_no}.zip"
        if out.exists() and out.stat().st_size > 0:
            with open(out, "rb") as f:
                if f.read(2) == b"PK":
                    return out              # 멱등: 정상 ZIP만 건너뜀
            out.unlink()                    # 과거 잘못 저장된 오류응답(.zip) 제거 후 재시도
        r = self._get("document.xml", rcept_no=rcept_no)
        content = r.content
        if content[:2] != b"PK":            # OpenDART 오류는 200 + XML(<status>014</status> 등)
            status, message = self._parse_api_error(content)
            raise OpenDartError(rcept_no, status, message)
        out.write_bytes(content)
        return out

    @staticmethod
    def _parse_api_error(content: bytes) -> tuple[str, str]:
        txt = content[:600].decode("utf-8", "replace")
        s = re.search(r"<status>([^<]+)</status>", txt)
        m = re.search(r"<message>([^<]+)</message>", txt)
        return (s.group(1) if s else "?"), (m.group(1) if m else txt[:150])

    # ── 단일회사 전체 재무제표 ──
    def financials(self, corp_code: str, year: int, reprt: str = "annual",
                   fs_div: str = "CFS") -> dict:
        # fs_div: CFS=연결(기본), OFS=별도
        return self._get("fnlttSinglAcntAll.json", corp_code=corp_code,
                         bsns_year=str(year), reprt_code=REPRT_CODE[reprt],
                         fs_div=fs_div).json()

    # ── 공시 첨부문서 목록 & 연결 감사보고서 dcmNo 해소 (딥링크용) ──
    # DART 뷰어(dsaf001/main.do?rcpNo=)는 한 공시의 첨부문서들을
    #   <option value="rcpNo=...&dcmNo=...">날짜&nbsp; 문서명</option>
    # 드롭다운으로 제공한다(문서명 예: '연결감사보고서','감사보고서','내부회계관리제도운영보고서').
    # 이 목록을 파싱해 문서명으로 '연결' 감사보고서를 골라 dcmNo를 얻는다.
    # (현대건설 rcpNo=20260318001395 → '연결감사보고서' dcmNo=11142436 으로 검증됨)
    _OPTION_RE = re.compile(
        r'<option[^>]*value="rcpNo=(\d+)&(?:amp;)?dcmNo=(\d+)"[^>]*>(.*?)</option>',
        re.S)
    _TAG_RE = re.compile(r"<[^>]+>")
    _DATE_RE = re.compile(r"\d{4}\.\d{2}\.\d{2}")

    def _viewer_html(self, rcept_no: str) -> str:
        @retry(stop=stop_after_attempt(self.max_retry + 2),
               wait=wait_exponential(multiplier=2, min=2, max=60), reraise=True)
        def _call():
            self._viewer_throttle()
            r = self._s.get("https://dart.fss.or.kr/dsaf001/main.do",
                            params={"rcpNo": rcept_no}, timeout=self.timeout,
                            headers={"User-Agent":
                                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            r.raise_for_status()
            return r.text
        return _call()

    def list_attached_docs(self, rcept_no: str) -> list[dict]:
        """한 공시(rcept_no)의 첨부문서 목록 [{dcm_no, title}] 반환(순서 보존)."""
        from html import unescape
        try:
            html = self._viewer_html(rcept_no)
        except Exception:
            return []
        docs, seen = [], set()
        for m in self._OPTION_RE.finditer(html):
            dcm = m.group(2)
            inner = self._TAG_RE.sub(" ", m.group(3))
            inner = unescape(inner).replace("\xa0", " ")
            inner = self._DATE_RE.sub(" ", inner)          # 날짜(2026.03.18) 제거
            title = " ".join(inner.split())
            if dcm and dcm not in seen:
                seen.add(dcm)
                docs.append({"dcm_no": dcm, "title": title})
        return docs

    def resolve_report_dcm(self, rcept_no: str, assurance: str = "감사"):
        """보고서 유형에 맞는 '연결' 문서 dcmNo를 고르고, 없으면 '별도'로 폴백한다.
        - assurance='감사'(연차) → 연결감사보고서 우선, 없으면 (별도)감사보고서
        - assurance='검토'(분/반기) → 연결검토보고서 우선, 없으면 (별도)검토보고서
        returns (dcm_no, is_consolidated, title). 문서 자체가 없으면 (None, None, None)."""
        docs = self.list_attached_docs(rcept_no)
        if not docs:
            return None, None, None
        norm = lambda s: s.replace(" ", "")
        if assurance == "검토":                  # 분기/반기
            cons_kw = ("연결재무제표에대한검토보고서", "연결검토보고서")
            base = "검토보고서"
        else:                                     # 감사(연차)
            cons_kw = ("연결재무제표에대한감사보고서", "연결감사보고서",
                       "연결재무제표에대한감사인의감사보고서")
            base = "감사보고서"
        for kw in cons_kw:                        # 1) 연결 우선(표기 변형)
            for d in docs:
                if kw in norm(d["title"]):
                    return d["dcm_no"], True, d["title"]
        for d in docs:                            # 2) '연결' + 기준문서 동시 포함
            t = norm(d["title"])
            if "연결" in t and base in t:
                return d["dcm_no"], True, d["title"]
        for d in docs:                            # 3) 별도 폴백(연결 없음)
            t = norm(d["title"])
            if base in t and "연결" not in t:
                return d["dcm_no"], False, d["title"]
        return None, None, None

    def consolidated_doc_no(self, rcept_no: str) -> str | None:
        """연결 감사보고서 dcmNo만 반환(없으면 None). 하위호환용 얇은 래퍼."""
        dcm, is_cons, _ = self.resolve_report_dcm(rcept_no, "감사")
        return dcm if is_cons else None

    @staticmethod
    def deep_link(rcept_no: str, dcm_no: str | None = None) -> str:
        base = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
        return f"{base}&dcmNo={dcm_no}" if dcm_no else base


# ── DART 뷰어 섹션 딥링크(viewer.do) 해소 ─────────────────────────────
# main.do?rcpNo= 페이지의 목차(treeData: text·eleId·offset·length·dtd)를 파싱해,
# 답변 섹션에 해당하는 노드로 viewer.do 링크를 만든다(+텍스트 프래그먼트로 스크롤/하이라이트).
# → 기존 '문서만 열기'(main.do)에서 '해당 섹션 바로 열기'로 개선. TOC는 rcpNo당 1회 캐시.
import urllib.parse as _urlparse
import html as _html

_DART_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_TOC_CACHE: dict[str, list[dict]] = {}
_NODE_TEXT_CACHE: dict[str, str] = {}          # 노드 내용(정규화) 캐시 — fetch-verify용
_LINK_CACHE: dict[str, str] = {}               # 최종 해소 링크 캐시(반복 클릭 대비)
_NODE_BLK = re.compile(r"var node1 = \{\};(.*?)treeData\.push", re.S)
_NODE_KV = re.compile(r"node1\['(\w+)'\]\s*=\s*\"([^\"]*)\"")


def _parse_toc(rcept_no: str, dcm_no: str | None = None) -> list[dict]:
    # TOC(treeData)는 dcmNo에 따라 다르다 → dcmNo 포함해 요청·캐시(문서별 정확 매칭).
    key = f"{rcept_no}:{dcm_no or ''}"
    if key in _TOC_CACHE:
        return _TOC_CACHE[key]
    params = {"rcpNo": rcept_no}
    if dcm_no:
        params["dcmNo"] = dcm_no
    nodes: list[dict] = []
    try:
        r = requests.get("https://dart.fss.or.kr/dsaf001/main.do",
                         params=params, headers=_DART_UA, timeout=15)
        r.raise_for_status()
        for blk in _NODE_BLK.findall(r.text):
            d = dict(_NODE_KV.findall(blk))
            if d.get("eleId") and d.get("offset"):
                nodes.append(d)
    except Exception:  # noqa: BLE001  실패 시 원본 링크로 폴백
        nodes = []
    _TOC_CACHE[key] = nodes
    return nodes


def _hint_score(t_norm: str, doc_type: str, leaf: str) -> int:
    """노드 제목 우선순위. doc_type에 결정적: 재무→'재무제표' 노드, 감사→'독립된 감사인의
    감사보고서'(의견·KAM 본문). 표지(감사보고서 title)·정정신고·외부감사실시내용은 감점."""
    is_body = any(k in t_norm for k in ("독립된감사인", "감사인의감사보고서",
                                        "감사인의검토보고서", "핵심감사"))
    is_fin = "재무제표" in t_norm
    sc = 0
    if doc_type in ("financial_stmt", "financial_note"):
        sc += 8 if is_fin else (2 if is_body else 0)
    elif doc_type == "audit":
        sc += 8 if is_body else (1 if is_fin else 0)
    else:                                           # doc_type 미상 → 감사본문 약우선
        sc += 6 if is_body else (5 if is_fin else 0)
    if leaf and len(leaf) >= 3 and (leaf in t_norm or t_norm in leaf):
        sc += 3
    if "정정신고" in t_norm:                          # 표지·정정 안내(목표 아님)
        sc -= 5
    if "외부감사실시내용" in t_norm:
        sc -= 4
    return sc


def _viewer_url(node: dict) -> str:
    return ("https://dart.fss.or.kr/report/viewer.do"
            f"?rcpNo={node['rcpNo']}&dcmNo={node['dcmNo']}&eleId={node['eleId']}"
            f"&offset={node['offset']}&length={node['length']}&dtd={node.get('dtd', 'dart4.xsd')}")


def _node_text(node: dict) -> str:
    """노드(viewer.do) 내용을 태그·공백 제거 정규화해 반환(fetch-verify용, 캐시)."""
    key = f"{node['rcpNo']}:{node['dcmNo']}:{node['eleId']}"
    if key in _NODE_TEXT_CACHE:
        return _NODE_TEXT_CACHE[key]
    txt = ""
    try:
        r = requests.get(_viewer_url(node), headers=_DART_UA, timeout=15)
        r.raise_for_status()
        txt = re.sub(r"\s+", "", _html.unescape(re.sub(r"<[^>]+>", " ", r.text)))
    except Exception:  # noqa: BLE001
        txt = ""
    _NODE_TEXT_CACHE[key] = txt
    return txt


def _distinctive_tokens(quote: str) -> list[str]:
    """인용문에서 검색용 distinctive 토큰(영문 단어·4자+ 한글·6자+ 숫자, 공백 제거)."""
    out: list[str] = []
    for t in re.findall(r"[A-Za-z][A-Za-z0-9]{2,}|[가-힣]{4,}|[\d]{4,}", quote or ""):
        if t not in out:
            out.append(t)
    return out[:10]


def _text_fragment(quote: str) -> str:
    """prose(표 아님)일 때만 텍스트 프래그먼트 생성 → 브라우저 스크롤+하이라이트."""
    q = " ".join((quote or "").split())
    if not q or q.count("|") >= 2:                 # 표 셀 구분 다수 → 매칭 실패 가능 → 생략
        return ""
    q = q.split("|")[0].strip()
    if len(q) > 50:                                # 단어 경계로 절단(중간 절단 시 매칭 실패)
        q = q[:50].rsplit(" ", 1)[0]
    digits = sum(c.isdigit() for c in q)
    if len(q) < 6 or digits > len(q) * 0.4:        # 숫자 위주(수치)면 생략
        return ""
    return "#:~:text=" + _urlparse.quote(q)


def resolve_section_deeplink(dart_url: str, quote: str = "", doc_type: str = "",
                             section: str = "") -> str:
    """dart_url(main.do)을 '답변이 실제로 있는 섹션' viewer.do 링크로 승격.
    (1) 제목 힌트로 후보 정렬 → (2) 작은 노드는 내용을 받아 인용 토큰 존재를 확인(정확 착지).
    거대 재무노드는 힌트 신뢰(다운로드 회피). 실패하면 원본 main.do 반환."""
    m = re.search(r"rcpNo=(\d+)", dart_url or "")
    if not m:
        return dart_url
    rcpno, dm = m.group(1), re.search(r"dcmNo=(\d+)", dart_url or "")
    dcmno = dm.group(1) if dm else None
    ckey = f"{dart_url}|{(quote or '')[:60]}|{doc_type}"
    if ckey in _LINK_CACHE:
        return _LINK_CACHE[ckey]
    nodes = _parse_toc(rcpno, dcmno)
    cand = [n for n in nodes if not dcmno or n.get("dcmNo") == dcmno] or nodes
    if not cand:                                     # 목차 파싱 불가(예: 정정공시 rcpNo↔dcmNo 오결합 데이터) → 원본 유지
        return dart_url
    norm = lambda s: (s or "").replace(" ", "")
    leaf = norm((section or "").split(">")[-1])
    ranked = sorted(cand, key=lambda n: -_hint_score(norm(n.get("text")), doc_type, leaf))
    top = ranked[0]
    keys = _distinctive_tokens(quote)
    chosen, checked = None, 0
    # 재무·거대 노드는 힌트 신뢰(다운로드 회피). 감사 등 작은 노드만 내용 확인(정확 착지).
    big_or_fin = int(top.get("length") or 0) > 120000 or doc_type in ("financial_stmt", "financial_note")
    if keys and len(keys) >= 2 and not big_or_fin:  # 2토큰 이상 일치할 때만 신뢰(오탐 방지)
        for n in ranked:
            if int(n.get("length") or 0) > 120000 or checked >= 4:
                continue
            checked += 1
            content = _node_text(n)
            if content and sum(1 for k in keys if norm(k) in content) >= 2:
                chosen = n
                break
    if chosen is None:                              # 확인 안 함/실패 → 힌트 1위
        chosen = top
    url = _viewer_url(chosen) + _text_fragment(quote)
    _LINK_CACHE[ckey] = url
    return url
