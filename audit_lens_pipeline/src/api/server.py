"""감사렌즈 웹 백엔드 (FastAPI).

query 엔진(BGE-M3 + OpenSearch + Claude)을 기동 시 1회 로드하고,
/api/query 로 질의를 받아 근거·딥링크가 붙은 답을 반환한다.
정적 UI는 web/index.html. 실행: `python -m src.cli serve` → http://127.0.0.1:8000
"""
from __future__ import annotations
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from config import settings
from src.pipeline.query import QueryEngine   # embed→ST를 torch보다 먼저 import
from src.clients.claude import _sanitize_synth   # 저장 payload 태그 유출 정화(읽기 시점 방어)

log = logging.getLogger(__name__)
_WEB = Path(__file__).resolve().parents[2] / "web"
# Next.js 정적 빌드(web_next/)가 있으면 새 UI를 루트로, 기존 UI는 /legacy 로 보존(롤백 안전판).
_WEB_NEXT = Path(__file__).resolve().parents[2] / "web_next"
_state: dict = {}
_audit = logging.getLogger("audit")          # 접근/질의 감사로그(§11.2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    if settings.audit_log_path:               # 감사로그 파일(누가·언제·무엇) — 지정 시 파일로도 기록
        h = logging.FileHandler(settings.audit_log_path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s AUDIT %(message)s"))
        _audit.addHandler(h)
        _audit.setLevel(logging.INFO)
        log.info("감사로그 활성: %s", settings.audit_log_path)
    log.info("query 엔진 로딩 (BGE-M3 + OpenSearch + Claude)…")
    _state["engine"] = QueryEngine()
    log.info("준비 완료. http://127.0.0.1:8000 접속")
    yield
    _state.clear()


app = FastAPI(title="감사렌즈 · Audit Report Intelligence", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])   # 로컬 미리보기(다른 origin)에서도 호출 가능


class QueryIn(BaseModel):
    question: str = ""
    k: int = 12
    sector: str = ""            # UI 산업 필터(krx_sector)
    year: str = ""             # UI 연도 단일(하위호환)
    years: list[str] = []      # UI 연도 복수선택(2023/2024/2025)
    report: str = ""           # UI 보고서: "" or "검토"(분·반기)
    fact_types: list[str] = [] # UI Fact 키워드 빠른조회(직접 지정)
    exhaustive: bool = False    # 빠른조회=전수 의도
    wics_dae: str = ""          # WICS 대분류 코드(2자리)
    wics_jung: str = ""         # WICS 중분류 코드(4자리)
    wics_so: str = ""           # WICS 소분류 코드(6자리)


def _hints(p: "QueryIn") -> dict:
    return {"sector": p.sector, "year": p.year, "years": p.years, "report": p.report,
            "fact_types": p.fact_types, "exhaustive": p.exhaustive,
            "wics": {"dae": p.wics_dae, "jung": p.wics_jung, "so": p.wics_so}}


def _jsafe(obj: dict) -> dict:
    """JSON 직렬화 안전화: understanding의 내부 set(_wics_corps 등)을 제거(원본 불변).
    이 값들은 필터용 내부 상태라 클라이언트에 불필요 → 응답에서 제외."""
    u = obj.get("understanding")
    if isinstance(u, dict) and any(isinstance(v, (set, frozenset)) for v in u.values()):
        obj = {**obj, "understanding": {k: v for k, v in u.items()
                                        if not isinstance(v, (set, frozenset))}}
    return obj


def _enrich_wics(res: dict) -> dict:
    """결과 항목에 기업별 WICS 분류·출처·근거를 부착(UI 배지·툴팁용)."""
    try:
        from src.clients import wics as _wics
        for it in res.get("items", []):
            b = _wics.brief(it.get("corp_code"), it.get("corp_name", ""))
            if b:
                it["wics"] = b
    except Exception:  # noqa: BLE001
        pass
    return res


# Fact Store 빠른조회 프리셋(18종, 그룹 구분) — UI '원클릭 전수 조회' 칩
FACT_PRESETS = [
    # 의견·감사인
    {"group": "의견·감사인", "label": "감사의견", "question": "감사의견", "fact_types": ["감사의견_유형"]},
    {"group": "의견·감사인", "label": "핵심감사사항", "question": "핵심감사사항", "fact_types": ["핵심감사사항"]},
    {"group": "의견·감사인", "label": "내부회계 검토의견", "question": "내부회계관리제도 검토의견", "fact_types": ["내부회계관리제도_검토의견"]},
    {"group": "의견·감사인", "label": "감사인·보수", "question": "감사인 감사보수", "fact_types": ["감사인_보수"]},
    {"group": "의견·감사인", "label": "감사 투입시간", "question": "감사 투입시간", "fact_types": ["감사_투입시간"]},
    {"group": "의견·감사인", "label": "감사인 변경사유", "question": "감사인 변경 사유", "fact_types": ["감사인_변경사유"]},
    # 위험 신호
    {"group": "위험 신호", "label": "계속기업 불확실성", "question": "계속기업 불확실성", "fact_types": ["계속기업_불확실성"]},
    {"group": "위험 신호", "label": "소송·우발부채", "question": "소송 우발부채", "fact_types": ["소송_우발부채"]},
    {"group": "위험 신호", "label": "강조사항", "question": "감사보고서 강조사항", "fact_types": ["감사보고서_강조사항"]},
    {"group": "위험 신호", "label": "재무제표 재작성", "question": "전기오류수정 재무제표 재작성", "fact_types": ["전기오류_수정"]},
    {"group": "위험 신호", "label": "정정공시 이력", "question": "정정공시 이력", "fact_types": ["정정공시_이력"]},
    # 정책·추정
    {"group": "정책·추정", "label": "재고 평가방법", "question": "재고자산 평가방법", "fact_types": ["재고자산_평가방법"]},
    {"group": "정책·추정", "label": "수익인식 정책", "question": "수익인식 정책", "fact_types": ["수익인식_정책"]},
    {"group": "정책·추정", "label": "회계정책 변경", "question": "회계정책 변경", "fact_types": ["회계정책_변경"]},
    {"group": "정책·추정", "label": "회계추정 변경", "question": "회계추정 변경", "fact_types": ["회계추정_변경"]},
    {"group": "정책·추정", "label": "감가상각 변경", "question": "감가상각방법 변경", "fact_types": ["감가상각_변경", "회계추정_변경"]},
    # 거래·독립성
    {"group": "거래·독립성", "label": "특수관계자 거래", "question": "특수관계자 거래", "fact_types": ["특수관계자_거래"]},
    {"group": "거래·독립성", "label": "비감사용역 계약", "question": "비감사용역 계약", "fact_types": ["비감사용역_계약"]},
]


def require_auth(x_auth_password: str | None = Header(default=None)):
    """AUTH_PASSWORD 설정 시 X-Auth-Password 헤더를 검증(비어있으면 통과=로컬 개방)."""
    pw = settings.auth_password
    if pw and x_auth_password != pw:
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")


@app.get("/api/auth-required")
def auth_required():
    """UI가 로그인 게이트를 띄울지 결정(비밀번호 설정 여부만 노출, 값은 비노출)."""
    return {"required": bool(settings.auth_password)}


@app.get("/api/meta")
def meta():
    """UI 필터용 메타: 산업 섹터 목록 + 연도 + Fact Store 빠른조회 프리셋."""
    import csv as _csv
    sectors = []
    try:
        rows = list(_csv.DictReader((settings.meta_dir / "universe.csv").open(encoding="utf-8-sig")))
        sectors = sorted({(r.get("krx_sector") or "").strip()
                          for r in rows if (r.get("krx_sector") or "").strip()})
    except Exception:  # noqa: BLE001
        pass
    fact_years = []                                # Fact Store(전수 조회) 지원 연도 — DB 실측
    eng = _state.get("engine")
    try:
        if eng is not None and getattr(eng, "pg", None):
            fact_years = eng.pg.fact_years()
    except Exception:  # noqa: BLE001
        pass
    if not fact_years:
        fact_years = [2024, 2025]                  # 폴백(현재 적재 기준)
    wics = {}
    try:
        from src.clients import wics as _wics
        wics = _wics.taxonomy_ui()
    except Exception:  # noqa: BLE001
        pass
    return {"sectors": sectors, "years": ["2023", "2024", "2025"],
            "fact_years": [str(y) for y in fact_years], "fact_presets": FACT_PRESETS,
            "wics": wics}


@app.get("/api/deeplink", dependencies=[Depends(require_auth)])
def deeplink(url: str, q: str = "", doct: str = "", section: str = ""):
    """DART 딥링크를 '해당 섹션 바로 열기'(viewer.do+텍스트 프래그먼트)로 승격. 실패 시 원본."""
    from src.clients.opendart import resolve_section_deeplink
    try:
        return {"url": resolve_section_deeplink(url, q, doct, section)}
    except Exception:  # noqa: BLE001
        return {"url": url}


@app.post("/api/login")
def login(_ok: None = Depends(require_auth)):
    """로그인 폼 검증용(성공 200 / 실패 401)."""
    return {"ok": True}


class FeedbackIn(BaseModel):
    message: str = ""
    contact: str = ""


@app.post("/api/feedback", dependencies=[Depends(require_auth)])
def feedback(payload: FeedbackIn, request: Request):
    """사용자 개선 요청 수집 — PG feedback 테이블 + data/feedback.jsonl 이중 보존(EBS)."""
    msg = (payload.message or "").strip()[:2000]
    if len(msg) < 5:
        raise HTTPException(400, "내용을 5자 이상 입력해 주세요.")
    contact = (payload.contact or "").strip()[:200]
    ip = request.client.host if request.client else "?"
    eng = _state.get("engine")
    saved = False
    try:
        if eng is not None and getattr(eng, "pg", None):
            eng.pg.ensure_feedback()
            eng.pg.add_feedback(ip, msg, contact)
            saved = True
    except Exception:  # noqa: BLE001
        log.exception("feedback PG 저장 실패")
    try:                                            # 파일 백업(볼륨 마운트 → 컨테이너 재생성에도 보존)
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "ip": ip, "message": msg, "contact": contact}
        with (settings.data_dir / "feedback.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        saved = True
    except Exception:  # noqa: BLE001
        log.exception("feedback 파일 저장 실패")
    if not saved:
        raise HTTPException(500, "저장에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    _audit.info("ip=%s FEEDBACK %r", ip, msg[:120])
    return {"ok": True}


class EvFbIn(BaseModel):
    verdict: str = ""          # up | down
    question: str = ""
    corp_code: str = ""
    corp_name: str = ""
    quote: str = ""
    path: str = ""


@app.post("/api/evidence-feedback", dependencies=[Depends(require_auth)])
def evidence_feedback(payload: EvFbIn, request: Request):
    """근거 채택(up)/반려(down) 로깅 — 재랭킹·추출 프롬프트 개선 신호(설계 §10.9)."""
    if payload.verdict not in ("up", "down"):
        raise HTTPException(400, "verdict는 up/down")
    ip = request.client.host if request.client else "?"
    eng = _state.get("engine")
    try:
        if eng is not None and getattr(eng, "pg", None):
            eng.pg.ensure_evidence_feedback()
            eng.pg.add_evidence_feedback(ip, payload.verdict, payload.question[:500],
                                         payload.corp_code[:20], payload.corp_name[:100],
                                         payload.quote[:500], payload.path[:40])
    except Exception:  # noqa: BLE001
        log.exception("evidence_feedback 저장 실패")
    try:
        import datetime
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "ip": ip,
               "verdict": payload.verdict, "q": payload.question[:300],
               "corp": payload.corp_name, "quote": payload.quote[:200], "path": payload.path}
        with (settings.data_dir / "evidence_feedback.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass
    _audit.info("ip=%s EVFB %s corp=%s q=%r", ip, payload.verdict, payload.corp_name,
                payload.question[:80])
    return {"ok": True}


@app.get("/api/evidence-feedback")
def evidence_feedback_list(pw: str = "", x_auth_password: str | None = Header(default=None)):
    """운영자 확인용 근거 피드백 목록."""
    p = settings.auth_password
    if p and pw != p and x_auth_password != p:
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    eng = _state.get("engine")
    items = []
    try:
        if eng is not None and getattr(eng, "pg", None):
            eng.pg.ensure_evidence_feedback()
            items = eng.pg.list_evidence_feedback()
    except Exception:  # noqa: BLE001
        log.exception("evidence_feedback 조회 실패")
    up = sum(1 for i in items if i.get("verdict") == "up")
    return {"count": len(items), "up": up, "down": len(items) - up, "items": items}


@app.get("/api/eval-candidates")
def eval_candidates_list(pw: str = "", harvest: int = 0, status: str = "new",
                         x_auth_password: str | None = Header(default=None)):
    """운영자용: 반려(👎)→골든셋 후보 목록. harvest=1이면 최신 반려를 먼저 집계 적재."""
    p = settings.auth_password
    if p and pw != p and x_auth_password != p:
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    eng = _state.get("engine")
    items, harvested = [], 0
    try:
        if eng is not None and getattr(eng, "pg", None):
            if harvest:
                harvested = eng.pg.harvest_downvotes()
            items = eng.pg.list_eval_candidates(status=status)
    except Exception:  # noqa: BLE001
        log.exception("eval_candidates 조회 실패")
    return {"count": len(items), "harvested": harvested, "items": items}


@app.get("/api/feedback")
def feedback_list(pw: str = "", x_auth_password: str | None = Header(default=None)):
    """운영자 확인용 피드백 목록(비밀번호 필요 — 브라우저는 ?pw=, API는 헤더)."""
    p = settings.auth_password
    if p and pw != p and x_auth_password != p:
        raise HTTPException(401, "비밀번호가 올바르지 않습니다.")
    eng = _state.get("engine")
    items = []
    try:
        if eng is not None and getattr(eng, "pg", None):
            eng.pg.ensure_feedback()
            items = eng.pg.list_feedback()
    except Exception:  # noqa: BLE001
        log.exception("feedback 조회 실패")
    return {"count": len(items), "items": items}


# ── AI 대화(맥락 기억 챗봇) — 별도 탭 전용. 검색 흐름(/api/query*)은 무상태 그대로 ──
def _plain(obj):
    """JSONB 저장·응답용 완전 평문화(set → list 등)."""
    return json.loads(json.dumps(obj, ensure_ascii=False,
                                 default=lambda o: list(o) if isinstance(o, (set, frozenset)) else str(o)))


def _chat_state_from_u(u: dict, prev: dict) -> dict:
    """턴 종료 후 대화 상태 갱신 — 새 이해에 값이 있으면 교체, 없으면 유지(기억)."""
    st = dict(prev or {})
    if u.get("company"):
        st["company"] = u["company"]
    kc = [str(c) for c in (u.get("key_concepts") or []) if c]
    if u.get("metric"):
        kc = [u["metric"]] + [c for c in kc if c != u["metric"]]
    if kc:
        st["concepts"] = kc[:4]
    ys = [int(y) for y in (u.get("fiscal_years") or []) if str(y).isdigit()]
    if ys:
        st["years"] = ys
    if u.get("industry"):
        st["industry"] = u["industry"]
    return st


def _chat_chips(state: dict) -> list[dict]:
    """'기억 중' 칩 — state 필드를 사용자에게 그대로 노출(투명성)."""
    chips = []
    if state.get("company"):
        chips.append({"k": "company", "label": state["company"]})
    if state.get("concepts"):
        chips.append({"k": "concepts", "label": " · ".join(state["concepts"][:3])})
    if state.get("years"):
        chips.append({"k": "years", "label": " · ".join(str(y) for y in state["years"][:3])})
    if state.get("industry"):
        chips.append({"k": "industry", "label": state["industry"]})
    return chips


_CHAT_PATH_LABEL = [("financial_items", "계정 표 · ✓XBRL"), ("financials", "재무지표 · ✓XBRL"),
                    ("factstore", "전수 조회"), ("graph", "관계 분석"), ("ondemand", "본문 근거"),
                    ("disambiguation", "확인 필요")]


def _chat_summary(path: str, n_items: int, n_tables: int) -> str:
    """접힌 이전 답변의 한 줄 요약(질문 옆 표시)."""
    label = next((v for k, v in _CHAT_PATH_LABEL if k in (path or "")), "답변")
    parts = [label]
    if n_items:
        parts.append(f"{n_items}건")
    if n_tables:
        parts.append(f"표 {n_tables}개")
    return " · ".join(parts)


def _chat_suggestions(state: dict, path: str) -> list[str]:
    """추천 후속 질문 — 파이프라인이 실제로 답할 수 있는 템플릿만."""
    comp = state.get("company") or ""
    p = path or ""
    if "financial_items" in p or "financials" in p:
        return ["3개년 추이도 보여줘", "감사의견은 어때?", "핵심감사사항은 뭐였어?"]
    if comp:
        return ["재무 수치도 보여줘", "핵심감사사항은 뭐였어?", "특수관계자 거래 있어?"]
    return ["감사의견이 비적정인 기업 전부 보여줘", "계속기업 불확실성이 있는 기업은?"]


def _ctx_text(state: dict, last_q: str, last_summary: str) -> str:
    parts = []
    if state.get("company"):
        parts.append("회사: " + str(state["company"]))
    if state.get("concepts"):
        parts.append("주제: " + ", ".join(map(str, state["concepts"])))
    if state.get("years"):
        parts.append("연도: " + ", ".join(map(str, state["years"])))
    if state.get("industry"):
        parts.append("산업: " + str(state["industry"]))
    if last_q:
        parts.append("직전 질문: " + last_q)
    if last_summary:
        parts.append("직전 답변 요약: " + last_summary)
    return "\n".join(parts) or "(맥락 없음)"


class ChatIn(BaseModel):
    question: str = ""
    thread_id: int | None = None


class ThreadNewIn(BaseModel):
    seed: dict | None = None    # 검색 탭 브리지: {question, understanding, answer, items, tables, path…}


class ThreadStateIn(BaseModel):
    state: dict = {}


def _chat_pg():
    eng = _state.get("engine")
    if eng is None:
        raise HTTPException(503, "엔진 준비 중입니다. 잠시 후 다시 시도하세요.")
    if getattr(eng, "pg", None) is None:
        raise HTTPException(503, "대화 저장소(PostgreSQL)가 연결되지 않았습니다.")
    eng.pg.ensure_chat()
    return eng


@app.get("/api/chat/threads", dependencies=[Depends(require_auth)])
def chat_threads():
    eng = _chat_pg()
    return {"threads": eng.pg.chat_list_threads()}


@app.post("/api/chat/threads", dependencies=[Depends(require_auth)])
def chat_thread_new(payload: ThreadNewIn):
    """새 대화 생성. seed(검색 브리지)가 있으면 첫 문답·상태를 물려받는다."""
    eng = _chat_pg()
    seed = payload.seed or {}
    u = seed.get("understanding") or {}
    state = _chat_state_from_u(u, {})
    title = (seed.get("question") or "새 대화").strip()[:60]
    tid = eng.pg.chat_create_thread(title=title, state=state)
    messages = []
    if seed.get("question"):
        pl = _plain({k: seed.get(k) for k in
                     ("answer", "analysis", "items", "tables", "path",
                      "verified_count", "insufficient")})
        summary = _chat_summary(seed.get("path") or "", len(seed.get("items") or []),
                                len(seed.get("tables") or []))
        mid = eng.pg.chat_add_message(tid, seed["question"], seed["question"], summary, pl)
        messages = [{"id": mid, "question": seed["question"], "resolved": seed["question"],
                     "summary": summary, "payload": pl}]
    return {"id": tid, "title": title, "state": state, "chips": _chat_chips(state),
            "messages": messages, "suggestions": _chat_suggestions(state, seed.get("path") or "")}


@app.get("/api/chat/threads/{tid}", dependencies=[Depends(require_auth)])
def chat_thread_get(tid: int):
    eng = _chat_pg()
    th = eng.pg.chat_get_thread(tid)
    if th is None:
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    st = th.get("state") or {}
    msgs = eng.pg.chat_messages(tid)
    for m in msgs:                    # 과거 저장분 태그 유출 정화(가드 도입 전 오염 payload 방어)
        if isinstance(m.get("payload"), dict):
            m["payload"] = _sanitize_synth(m["payload"])
    return {**th, "chips": _chat_chips(st), "messages": msgs,
            "suggestions": _chat_suggestions(st, "")}


@app.post("/api/chat/threads/{tid}/state", dependencies=[Depends(require_auth)])
def chat_thread_state(tid: int, payload: ThreadStateIn):
    """'기억 중' 칩 × 제거 / 비우기 — 상태를 사용자가 직접 제어."""
    eng = _chat_pg()
    if eng.pg.chat_get_thread(tid) is None:
        raise HTTPException(404, "대화를 찾을 수 없습니다.")
    st = {k: v for k, v in (payload.state or {}).items()
          if k in ("company", "concepts", "years", "industry") and v}
    eng.pg.chat_update_thread(tid, state=st)
    return {"state": st, "chips": _chat_chips(st)}


@app.post("/api/chat/stream", dependencies=[Depends(require_auth)])
def api_chat_stream(payload: ChatIn, request: Request):
    """AI 대화 턴(NDJSON): 후속질문 재작성(맥락 해석) → 기존 파이프라인 스트리밍 재사용
    → 상태·메시지 저장 + 추천 후속질문. 검색 탭과 동일한 이벤트에 chat_meta/chat_done만 추가."""
    eng = _chat_pg()
    q = (payload.question or "").strip()
    if not q:
        raise HTTPException(400, "질문이 비어 있습니다.")
    ip = request.client.host if request.client else "?"
    tid = payload.thread_id
    thread = eng.pg.chat_get_thread(tid) if tid else None
    if thread is None:
        tid = eng.pg.chat_create_thread(title=q[:60])
        thread = {"id": tid, "title": q[:60], "state": {}}
    state0 = thread.get("state") or {}

    def gen():
        try:
            # 1) 후속 질문 재작성(맥락이 있을 때만) — 재작성문이 곧 '맥락 해석' 표시
            resolved, used_ctx, ctx_sum = q, False, ""
            if state0:
                msgs = eng.pg.chat_messages(tid)
                last = msgs[-1] if msgs else {}
                try:
                    rw = eng.claude.rewrite_followup(
                        q, _ctx_text(state0, last.get("question") or "", last.get("summary") or ""))
                    r2 = (rw.get("resolved") or "").strip()
                    if r2 and rw.get("used_context"):
                        resolved, used_ctx, ctx_sum = r2, True, rw.get("context_summary") or ""
                except Exception:  # noqa: BLE001  재작성 실패 → 원문 그대로(안전)
                    log.exception("후속질문 재작성 실패 — 원문으로 진행")
            yield json.dumps({"type": "chat_meta", "thread_id": tid, "resolved": resolved,
                              "used_context": used_ctx, "context_summary": ctx_sum},
                             ensure_ascii=False) + "\n"
            # 2) 기존 파이프라인 그대로 스트리밍(경로·표·검증 전부 재사용)
            final = {"answer": "", "analysis": "", "items": [], "tables": [], "path": "",
                     "verified_count": 0, "insufficient": False}
            u_last: dict = {}
            for ev in eng.run_stream(resolved, k=12):
                if ev.get("type") in ("core", "supplement"):
                    _enrich_wics(ev)
                    final["items"] += ev.get("items") or []
                    final["tables"] += ev.get("tables") or []
                    final["verified_count"] += ev.get("verified_count") or 0
                    if ev.get("answer"):
                        final["answer"] = ev["answer"]
                    if ev.get("analysis") and not final["analysis"]:
                        final["analysis"] = ev["analysis"]
                    if ev.get("insufficient"):
                        final["insufficient"] = True
                if ev.get("type") == "understanding":
                    u_last = ev.get("understanding") or {}
                if ev.get("path"):
                    final["path"] = ev["path"]
                yield json.dumps(_jsafe(ev), ensure_ascii=False,
                                 default=lambda o: list(o) if isinstance(o, (set, frozenset)) else str(o)) + "\n"
            # 3) 상태 갱신·메시지 저장·추천 후속질문
            u_clean = {k: v for k, v in u_last.items() if not isinstance(v, (set, frozenset))}
            new_state = _chat_state_from_u(u_clean, state0)
            summary = _chat_summary(final["path"], len(final["items"]), len(final["tables"]))
            pl = _plain({**final, "resolved": resolved, "used_context": used_ctx,
                         "context_summary": ctx_sum})
            mid = eng.pg.chat_add_message(tid, q, resolved, summary, pl)
            eng.pg.chat_update_thread(tid, state=new_state)
            yield json.dumps({"type": "chat_done", "thread_id": tid, "message_id": mid,
                              "summary": summary, "state": new_state,
                              "chips": _chat_chips(new_state),
                              "suggestions": _chat_suggestions(new_state, final["path"])},
                             ensure_ascii=False) + "\n"
            _audit.info("ip=%s CHAT t=%s path=%s q=%r rq=%r", ip, tid, final["path"],
                        q[:120], resolved[:120])
        except Exception as e:                      # noqa
            _audit.info("ip=%s CHAT-ERROR t=%s q=%r", ip, tid, q[:120])
            log.exception("chat stream 실패")
            yield json.dumps({"type": "error", "msg": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/query", dependencies=[Depends(require_auth)])
def api_query(payload: QueryIn, request: Request):
    eng = _state.get("engine")
    if eng is None:
        raise HTTPException(503, "엔진 준비 중입니다. 잠시 후 다시 시도하세요.")
    q = (payload.question or "").strip()
    if not q:
        raise HTTPException(400, "질문이 비어 있습니다.")
    ip = request.client.host if request.client else "?"
    try:
        res = _jsafe(_enrich_wics(eng.run(q, k=payload.k, hints=_hints(payload))))
        _audit.info("ip=%s path=%s items=%d q=%r", ip, res.get("path"),
                    len(res.get("items", [])), q[:200])
        return res
    except Exception as e:                          # noqa
        _audit.info("ip=%s ERROR q=%r", ip, q[:200])
        log.exception("query 실패")
        raise HTTPException(500, f"질의 처리 오류: {e}")


@app.post("/api/query/stream", dependencies=[Depends(require_auth)])
def api_query_stream(payload: QueryIn, request: Request):
    """2단계 스트리밍(NDJSON): 코어 즉시 → 온디맨드 보완 이어붙임 → 완료. 체감 지연 최소화."""
    eng = _state.get("engine")
    if eng is None:
        raise HTTPException(503, "엔진 준비 중입니다. 잠시 후 다시 시도하세요.")
    q = (payload.question or "").strip()
    if not q:
        raise HTTPException(400, "질문이 비어 있습니다.")
    ip = request.client.host if request.client else "?"

    def gen():
        n_items, path = 0, "?"
        try:
            for ev in eng.run_stream(q, k=payload.k, hints=_hints(payload)):
                if ev.get("type") in ("core", "supplement"):
                    _enrich_wics(ev)                  # 항목에 WICS 분류·출처·근거 부착
                    n_items += len(ev.get("items", []))
                if ev.get("path"):
                    path = ev["path"]
                yield json.dumps(_jsafe(ev), ensure_ascii=False,
                                 default=lambda o: list(o) if isinstance(o, (set, frozenset)) else str(o)) + "\n"
            _audit.info("ip=%s path=%s items=%d q=%r (stream)", ip, path, n_items, q[:200])
        except Exception as e:                      # noqa
            _audit.info("ip=%s STREAM-ERROR q=%r", ip, q[:200])
            log.exception("stream 실패")
            yield json.dumps({"type": "error", "msg": str(e)}, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/health")
def health():
    return {"ready": "engine" in _state}


@app.get("/")
def index():
    # no-cache: 배포 직후 구버전 UI가 브라우저 캐시에 남는 문제 방지(재검증 후 사용)
    nxt = _WEB_NEXT / "index.html"
    target = nxt if nxt.exists() else (_WEB / "index.html")
    return FileResponse(str(target), headers={"Cache-Control": "no-cache"})


@app.get("/legacy")
def legacy():
    """기존(바닐라) UI — 신 UI 배포 후에도 비교·롤백용으로 접근 가능."""
    return FileResponse(str(_WEB / "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
def favicon():
    p = _WEB_NEXT / "favicon.ico"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(404)


if (_WEB_NEXT / "_next").exists():               # Next 정적 자산(css/js 청크)
    from fastapi.staticfiles import StaticFiles
    app.mount("/_next", StaticFiles(directory=str(_WEB_NEXT / "_next")), name="next_assets")
