"""PostgreSQL Fact Store 클라이언트(psycopg3).

벡터(청크)는 OpenSearch에, 정형 Fact는 여기(facts 테이블)에 둔다.
스크리닝/집계 질의(Layer 2)는 SQL로 전수·정확·고속 처리.
로컬 Docker PoC → AWS Aurora 전환 시 DSN(host)만 교체.
스키마는 컨테이너 init(`src/db/schema.sql`)에서 자동 생성됨.
"""
from __future__ import annotations
import logging
import threading
import psycopg
from psycopg.types.json import Json
from config import settings

log = logging.getLogger(__name__)


def _dsn() -> str:
    return (f"host={settings.pg_host} port={settings.pg_port} dbname={settings.pg_db} "
            f"user={settings.pg_user} password={settings.pg_password}")


_FACT_COLS = ("corp_code", "fiscal_year", "fact_type", "detail", "value_raw",
              "unit_scale", "currency", "value_krw", "is_consolidated",
              "evidence_text", "section_path", "rcept_no", "dcm_no", "dart_url",
              "confidence", "run_id")


class PostgresStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or _dsn()
        self._local = threading.local()   # 스레드별 연결(멀티유저 동시요청 안전)

    @property
    def conn(self) -> psycopg.Connection:
        # psycopg 연결은 스레드 안전이 아니므로 스레드마다 별도 연결 유지(=경량 풀).
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            c = psycopg.connect(self.dsn, autocommit=True)
            self._local.conn = c
        return c

    def ping(self) -> bool:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            return True
        except Exception as e:
            log.warning("PG 연결 실패: %s", e)
            return False

    def ensure_ready(self) -> None:
        """facts 테이블은 컨테이너 init에서 생성됨. FK 대상 run 행만 보장."""
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, pipeline_version, as_of_date, embedding_model) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING",
                (settings.pipeline_version, settings.pipeline_version,
                 settings.as_of_date, settings.embedding_model))

    # ── 적재(재개가능·멱등) ──
    def extracted_rcepts(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rcept_no FROM facts WHERE rcept_no IS NOT NULL")
            return {r[0] for r in cur.fetchall()}

    def replace_facts(self, rcept_no: str, rows: list[dict]) -> None:
        """보고서 단위로 멱등 교체(재추출 시 중복 방지)."""
        sql = (f"INSERT INTO facts ({', '.join(_FACT_COLS)}) "
               f"VALUES ({', '.join(['%s'] * len(_FACT_COLS))})")
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM facts WHERE rcept_no = %s", (rcept_no,))
            for r in rows:
                cur.execute(sql, (
                    r.get("corp_code"), r.get("fiscal_year"), r.get("fact_type"),
                    Json(r.get("detail") or {}), r.get("value_raw"), r.get("unit_scale"),
                    r.get("currency"), r.get("value_krw"), r.get("is_consolidated"),
                    r.get("evidence_text"), r.get("section_path"), rcept_no,
                    r.get("dcm_no"), r.get("dart_url"), r.get("confidence"),
                    r.get("run_id") or settings.pipeline_version))

    # ── 테이블 스토어(원본 XML 표, §표 재구축 D안) ──
    def ensure_doc_tables(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS doc_tables ("
                " id BIGSERIAL PRIMARY KEY, corp_code TEXT, rcept_no TEXT,"
                " fiscal_year INT, is_consolidated BOOLEAN, doc_type TEXT,"
                " section_path TEXT, title TEXT, unit TEXT,"
                " columns JSONB, rows JSONB, numkey TEXT, dart_url TEXT)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_doct_rcept ON doc_tables(rcept_no)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_doct_corp ON doc_tables(corp_code, fiscal_year)")

    def doc_tables_rcepts(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rcept_no FROM doc_tables")
            return {r[0] for r in cur.fetchall()}

    def replace_doc_tables(self, rcept_no: str, recs: list[dict]) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM doc_tables WHERE rcept_no = %s", (rcept_no,))
            for r in recs:
                cur.execute(
                    "INSERT INTO doc_tables (corp_code, rcept_no, fiscal_year, is_consolidated,"
                    " doc_type, section_path, title, unit, columns, rows, numkey, dart_url)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (r["corp_code"], rcept_no, r["fiscal_year"], r["is_consolidated"],
                     r["doc_type"], r["section_path"], r["title"], r["unit"],
                     Json(r["columns"]), Json(r["rows"]), r["numkey"], r.get("dart_url", "")))

    def doc_tables_rcepts_extra(self) -> set[str]:
        """확장분(비재무 doc_type) 적재된 필링(재실행 스킵용)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rcept_no FROM doc_tables "
                        "WHERE doc_type NOT IN ('financial_stmt','financial_note')")
            return {r[0] for r in cur.fetchall()}

    def replace_doc_tables_extra(self, rcept_no: str, recs: list[dict]) -> None:
        """확장분만 멱등 교체(기존 재무 섹션 행은 보존)."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM doc_tables WHERE rcept_no = %s "
                        "AND doc_type NOT IN ('financial_stmt','financial_note')", (rcept_no,))
            for r in recs:
                cur.execute(
                    "INSERT INTO doc_tables (corp_code, rcept_no, fiscal_year, is_consolidated,"
                    " doc_type, section_path, title, unit, columns, rows, numkey, dart_url)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (r["corp_code"], rcept_no, r["fiscal_year"], r["is_consolidated"],
                     r["doc_type"], r["section_path"], r["title"], r["unit"],
                     Json(r["columns"]), Json(r["rows"]), r["numkey"], r.get("dart_url", "")))

    def doc_tables_for(self, rcept_no: str) -> list[dict]:
        """한 필링의 표 전부(서빙 시 숫자 매칭용 — 필링당 수백 행 수준)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT section_path, title, unit, columns, rows, numkey, dart_url, doc_type,"
                " fiscal_year FROM doc_tables WHERE rcept_no = %s", (rcept_no,))
            cols = ("section_path", "title", "unit", "columns", "rows", "numkey",
                    "dart_url", "doc_type", "fiscal_year")
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── 사용자 피드백(개선 요청) ──
    def ensure_feedback(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                " id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(),"
                " ip TEXT, message TEXT, contact TEXT)")

    def add_feedback(self, ip: str, message: str, contact: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO feedback (ip, message, contact) VALUES (%s,%s,%s)",
                        (ip, message, contact))

    def list_feedback(self, limit: int = 300) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, created_at, ip, message, contact FROM feedback "
                        "ORDER BY id DESC LIMIT %s", (limit,))
            return [{"id": r[0], "created_at": str(r[1]), "ip": r[2],
                     "message": r[3], "contact": r[4]} for r in cur.fetchall()]

    # ── 근거 채택/반려 피드백(설계 §10.9 휴먼 인 더 루프) ──
    def ensure_evidence_feedback(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS evidence_feedback ("
                " id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(),"
                " ip TEXT, verdict TEXT, question TEXT, corp_code TEXT, corp_name TEXT,"
                " quote TEXT, path TEXT)")

    def add_evidence_feedback(self, ip, verdict, question, corp_code, corp_name, quote, path) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO evidence_feedback (ip, verdict, question, corp_code, corp_name, quote, path)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (ip, verdict, question, corp_code, corp_name, quote, path))

    def list_evidence_feedback(self, limit: int = 500) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, created_at, verdict, question, corp_name, quote, path"
                        " FROM evidence_feedback ORDER BY id DESC LIMIT %s", (limit,))
            cols = ("id", "created_at", "verdict", "question", "corp_name", "quote", "path")
            return [dict(zip(cols, (r[0], str(r[1]), *r[2:]))) for r in cur.fetchall()]

    # ── 반려(👎) → 골든셋 후보 승격(프로덕션→평가 파이프라인, 하네스) ──
    def ensure_eval_candidates(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS eval_candidates ("
                " id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(),"
                " question TEXT NOT NULL, source TEXT DEFAULT 'downvote',"
                " down_count INT DEFAULT 1, sample_corp TEXT, sample_quote TEXT,"
                " status TEXT DEFAULT 'new', note TEXT DEFAULT '',"
                " UNIQUE (question))")

    def harvest_downvotes(self, min_downvotes: int = 1) -> int:
        """evidence_feedback의 반려(down)를 질문 단위로 집계 → eval_candidates에 멱등 적재.
        이미 승격/기각(promoted/rejected) 처리된 질문은 재적재하지 않음. 반환=신규/갱신 건수."""
        self.ensure_eval_candidates()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT question, count(*),"
                " (array_agg(corp_name ORDER BY id DESC))[1],"
                " (array_agg(quote ORDER BY id DESC))[1]"
                " FROM evidence_feedback WHERE verdict='down'"
                " AND question IS NOT NULL AND length(trim(question)) >= 4"
                " GROUP BY question HAVING count(*) >= %s", (min_downvotes,))
            rows = cur.fetchall()
            n = 0
            for q, cnt, corp, quote in rows:
                cur.execute(
                    "INSERT INTO eval_candidates (question, down_count, sample_corp, sample_quote)"
                    " VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (question) DO UPDATE SET down_count=EXCLUDED.down_count,"
                    "   sample_corp=EXCLUDED.sample_corp, sample_quote=EXCLUDED.sample_quote"
                    " WHERE eval_candidates.status='new'",   # 검토 완료건은 건드리지 않음
                    (q, cnt, corp, (quote or "")[:400]))
                n += cur.rowcount
            return n

    def list_eval_candidates(self, status: str = "new", limit: int = 200) -> list[dict]:
        self.ensure_eval_candidates()
        with self.conn.cursor() as cur:
            q = ("SELECT id, created_at, question, down_count, sample_corp, sample_quote, status, note"
                 " FROM eval_candidates")
            params: list = []
            if status and status != "all":
                q += " WHERE status=%s"; params.append(status)
            q += " ORDER BY down_count DESC, id DESC LIMIT %s"; params.append(limit)
            cur.execute(q, params)
            cols = ("id", "created_at", "question", "down_count", "sample_corp",
                    "sample_quote", "status", "note")
            return [dict(zip(cols, (r[0], str(r[1]), *r[2:]))) for r in cur.fetchall()]

    def set_eval_candidate_status(self, cand_id: int, status: str, note: str = "") -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE eval_candidates SET status=%s, note=%s WHERE id=%s",
                        (status, note, cand_id))

    # ── 조회/통계 ──
    def count_facts(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM facts")
            return int(cur.fetchone()[0])

    def fact_type_counts(self) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT fact_type, count(*) FROM facts "
                        "GROUP BY fact_type ORDER BY 2 DESC")
            return cur.fetchall()

    def fact_years(self, min_count: int = 100) -> list[int]:
        """Fact Store에 유의미하게 적재된 회계연도 = '빠른 전수 조회' 지원 연도.
        (연차 감사보고서만 적재 → 현재 2024·2025. 소량 연도는 임계값으로 제외.)"""
        with self.conn.cursor() as cur:
            cur.execute("SELECT fiscal_year FROM facts WHERE fiscal_year IS NOT NULL "
                        "GROUP BY fiscal_year HAVING count(*) >= %s ORDER BY fiscal_year",
                        (min_count,))
            return [int(r[0]) for r in cur.fetchall()]

    # ── Layer 2: 스크리닝 SQL(전수·정확) ──
    def screen(self, fact_types=None, fiscal_years=None, corp_codes=None,
               detail_like=None, detail_exclude=None, limit: int = 500,
               value_key: str | None = None) -> list[dict]:
        """value_key(예: 'method'/'opinion'/'auditor') 지정 시 값 매칭을 **구조화 필드**
        `detail->>value_key`로 한정(근거텍스트의 부수 언급 오탐 제거). 없으면 detail::text+근거 매칭."""
        q = ["SELECT corp_code, fiscal_year, fact_type, detail, is_consolidated,",
             "value_raw, unit_scale, currency, evidence_text, section_path,",
             "dcm_no, dart_url, confidence FROM facts WHERE 1=1"]
        params: list = []
        if fact_types:
            q.append("AND fact_type = ANY(%s)"); params.append(list(fact_types))
        if fiscal_years:
            q.append("AND fiscal_year = ANY(%s)"); params.append([int(y) for y in fiscal_years])
        if corp_codes:
            q.append("AND corp_code = ANY(%s)"); params.append(list(corp_codes))
        if detail_like:
            likes = detail_like if isinstance(detail_like, (list, tuple)) else [detail_like]
            ors = []
            for v in likes:
                if value_key:
                    ors.append("detail->>%s ILIKE %s"); params += [value_key, f"%{v}%"]
                else:
                    ors.append("detail::text ILIKE %s OR evidence_text ILIKE %s"); params += [f"%{v}%", f"%{v}%"]
            q.append("AND (" + " OR ".join(ors) + ")")
        # 부정형('~가 아닌'): 해당 값을 담은 사실 제외
        for m in (detail_exclude or []):
            if value_key:
                q.append("AND COALESCE(detail->>%s,'') NOT ILIKE %s"); params += [value_key, f"%{m}%"]
            else:
                q.append("AND detail::text NOT ILIKE %s AND COALESCE(evidence_text,'') NOT ILIKE %s")
                params += [f"%{m}%", f"%{m}%"]
        q.append("ORDER BY corp_code, fiscal_year LIMIT %s"); params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(" ".join(q), params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── 계산형/순위(XBRL 정형재무) — v2: 원지표 17종 + 비율 8종 ──
    _FIN_RAW = ("자산총계", "부채총계", "자본총계", "유동자산", "비유동자산", "유동부채",
                "비유동부채", "현금및현금성자산", "매출액", "매출원가", "매출총이익",
                "판매비와관리비", "영업이익", "당기순이익",
                "영업활동현금흐름", "투자활동현금흐름", "재무활동현금흐름")
    _FIN_EXPR = {**{m: m for m in _FIN_RAW},
        "부채비율": "부채총계/NULLIF(자본총계,0)*100",
        "영업이익률": "영업이익/NULLIF(매출액,0)*100",
        "순이익률": "당기순이익/NULLIF(매출액,0)*100",
        "ROE": "당기순이익/NULLIF(자본총계,0)*100",
        "ROA": "당기순이익/NULLIF(자산총계,0)*100",
        "유동비율": "유동자산/NULLIF(유동부채,0)*100",
        "매출총이익률": "매출총이익/NULLIF(매출액,0)*100",
        "판관비율": "판매비와관리비/NULLIF(매출액,0)*100",
    }
    _OPS = {">", "<", ">=", "<=", "=", "!="}
    _FIN_SUMMARY = ("자산총계", "부채총계", "자본총계", "매출액", "영업이익", "당기순이익")

    def financials_screen(self, metric, order="상위", n=10, op=None, value=None,
                          year=2024, corp_codes=None) -> list[dict]:
        """정형재무 계산형/순위 조회. metric은 원지표(17) 또는 비율(부채비율/유동비율/ROE 등).
        피벗 후 지표식 계산 → op/value 필터 + order/n 정렬."""
        expr = self._FIN_EXPR.get(metric)
        if not expr:
            return []
        pivot_cols = ", ".join(
            f"MAX(amount) FILTER (WHERE metric='{m}') AS {m}" for m in self._FIN_RAW)
        pivot = (f"SELECT corp_code, {pivot_cols} FROM financials WHERE fiscal_year=%s ")
        params: list = [int(year)]
        if corp_codes:
            pivot += "AND corp_code = ANY(%s) "; params.append(list(corp_codes))
        pivot += "GROUP BY corp_code"
        sel = ", ".join(dict.fromkeys(self._FIN_SUMMARY + ((metric,) if metric in self._FIN_RAW else ())))
        q = (f"WITH f AS ({pivot}) SELECT corp_code, ({expr}) AS val, {sel} "
             f"FROM f WHERE ({expr}) IS NOT NULL")
        if op in self._OPS and value is not None:
            q += f" AND ({expr}) {op} %s"; params.append(float(value))
        q += f" ORDER BY val {'ASC' if order == '하위' else 'DESC'} LIMIT %s"
        params.append(int(n))
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def account_lookup(self, corp_codes, account_like: str, year: int,
                       limit: int = 30) -> list[dict]:
        """financial_items 계정 단위 조회(예: '매출채권') — 온디맨드 수치 근거 보강용."""
        q = ("SELECT corp_code, fiscal_year, is_consolidated, sj_div, account_nm, amount "
             "FROM financial_items WHERE fiscal_year=%s AND account_nm ILIKE %s "
             "AND amount IS NOT NULL")
        params: list = [int(year), f"%{account_like}%"]
        if corp_codes:
            q += " AND corp_code = ANY(%s)"; params.append(list(corp_codes))
        q += " ORDER BY corp_code, ord LIMIT %s"; params.append(int(limit))
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # ── 관계 순회(지식그래프·다홉): 감사인 교체 · 동일감사인 · 감사인 경유 ──
    @staticmethod
    def _aud_norm(col: str) -> str:
        """감사인명 정규화(‘삼일회계법인’/‘삼일 회계법인’→‘삼일’) — 교체 오탐 방지."""
        return (f"regexp_replace(COALESCE({col},''),"
                r"'(회계법인|유한회사|유한|감사반|\s)','','g')")

    def _aud_cte(self) -> str:
        an = self._aud_norm("detail->>'auditor'")
        return (f"a AS (SELECT DISTINCT ON (corp_code, fiscal_year) corp_code, fiscal_year, "
                f"{an} AS aud, detail->>'auditor' AS raw FROM facts "
                f"WHERE fact_type='감사인_보수' AND detail->>'auditor' IS NOT NULL "
                f"ORDER BY corp_code, fiscal_year, {an})")

    def auditor_transitions(self, prev_year=2024, curr_year=2025,
                            from_auditor=None, to_auditor=None, corp_codes=None) -> list[dict]:
        """[다홉·시계열] 두 사업연도 사이 감사인이 바뀐 기업(전기→당기 감사인 포함)."""
        q = [f"WITH {self._aud_cte()} "
             "SELECT c.corp_code, p.raw prev_aud, c.raw curr_aud "
             "FROM (SELECT * FROM a WHERE fiscal_year=%s) p "
             "JOIN (SELECT * FROM a WHERE fiscal_year=%s) c USING(corp_code) "
             "WHERE p.aud<>c.aud AND p.aud<>'' AND c.aud<>''"]
        params: list = [int(prev_year), int(curr_year)]
        if from_auditor:
            q.append("AND p.raw ILIKE %s"); params.append(f"%{from_auditor}%")
        if to_auditor:
            q.append("AND c.raw ILIKE %s"); params.append(f"%{to_auditor}%")
        if corp_codes:
            q.append("AND c.corp_code = ANY(%s)"); params.append(list(corp_codes))
        q.append("ORDER BY c.corp_code")
        with self.conn.cursor() as cur:
            cur.execute(" ".join(q), params)
            return [{"corp_code": r[0], "prev_aud": r[1], "curr_aud": r[2]} for r in cur.fetchall()]

    def peers_by_auditor(self, corp_code: str, year=2025, exclude_self=True) -> list[str]:
        """[2홉] 기업 → (그 해 감사인) → 같은 감사인을 쓰는 다른 기업들."""
        with self.conn.cursor() as cur:
            cur.execute(
                f"WITH {self._aud_cte()}, me AS (SELECT aud FROM a WHERE corp_code=%s AND fiscal_year=%s LIMIT 1) "
                "SELECT DISTINCT f.corp_code FROM a f, me "
                "WHERE f.fiscal_year=%s AND f.aud=me.aud AND me.aud<>''"
                + (" AND f.corp_code<>%s" if exclude_self else ""),
                ([corp_code, int(year), int(year), corp_code] if exclude_self
                 else [corp_code, int(year), int(year)]))
            return [r[0] for r in cur.fetchall()]

    def via_auditor_facts(self, corp_code: str, fact_types, year=2025,
                          detail_like=None, value_key=None) -> list[dict]:
        """[3홉] 기업 → 감사인 → 그 감사인의 다른 고객사들 → 그중 특정 Fact를 가진 회사의 Fact."""
        peers = self.peers_by_auditor(corp_code, year=year)
        if not peers:
            return []
        return self.screen(fact_types=fact_types, corp_codes=peers, fiscal_years=[int(year)],
                           detail_like=detail_like, value_key=value_key, limit=2000)

    # ── 특수관계자 네트워크(지식그래프 확장): 기존 특수관계자_거래 Fact → 상대방 엣지 ──
    def ensure_parties_table(self) -> None:
        """related_parties(엣지) 테이블 보장. 컨테이너 이미 기동 상태라 런타임 생성."""
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS related_parties ("
                "edge_id BIGSERIAL PRIMARY KEY, corp_code TEXT NOT NULL, party_name TEXT NOT NULL,"
                "party_norm TEXT, relationship TEXT, txn_type TEXT, group_name TEXT,"
                "fiscal_year INT, rcept_no TEXT, dart_url TEXT, fact_id BIGINT, run_id TEXT)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rp_corp ON related_parties (corp_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rp_party ON related_parties (party_norm)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rp_group ON related_parties (group_name)")

    def related_party_source_facts(self, limit: int | None = None) -> list[dict]:
        """엣지 추출 원천 = 특수관계자_거래 Fact(근거 텍스트 포함)."""
        q = ("SELECT fact_id, corp_code, fiscal_year, detail, evidence_text, rcept_no, dart_url "
             "FROM facts WHERE fact_type='특수관계자_거래' ORDER BY fact_id")
        params: list = []
        if limit:
            q += " LIMIT %s"; params.append(int(limit))
        with self.conn.cursor() as cur:
            cur.execute(q, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def parties_count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM related_parties")
            return int(cur.fetchone()[0])

    def parties_done_fact_ids(self) -> set:
        with self.conn.cursor() as cur:
            cur.execute("SELECT DISTINCT fact_id FROM related_parties WHERE fact_id IS NOT NULL")
            return {r[0] for r in cur.fetchall()}

    # ── AI 대화(맥락 기억 챗봇) 스레드/메시지 ──
    def ensure_chat(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_threads ("
                " id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT now(),"
                " updated_at TIMESTAMPTZ DEFAULT now(), title TEXT DEFAULT '',"
                " state JSONB DEFAULT '{}'::jsonb)")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_messages ("
                " id BIGSERIAL PRIMARY KEY, thread_id BIGINT NOT NULL,"
                " created_at TIMESTAMPTZ DEFAULT now(), question TEXT, resolved TEXT,"
                " summary TEXT DEFAULT '', payload JSONB)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_chatmsg_thread"
                        " ON chat_messages (thread_id, id)")

    def chat_create_thread(self, title: str = "", state: dict | None = None) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO chat_threads (title, state) VALUES (%s,%s) RETURNING id",
                        ((title or "")[:120], Json(state or {})))
            return int(cur.fetchone()[0])

    def chat_list_threads(self, limit: int = 30) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.title, t.updated_at,"
                " (SELECT count(*) FROM chat_messages m WHERE m.thread_id = t.id)"
                " FROM chat_threads t ORDER BY t.updated_at DESC LIMIT %s", (limit,))
            return [{"id": r[0], "title": r[1], "updated_at": str(r[2]), "n": int(r[3])}
                    for r in cur.fetchall()]

    def chat_get_thread(self, tid: int) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, title, state FROM chat_threads WHERE id=%s", (tid,))
            r = cur.fetchone()
            return {"id": r[0], "title": r[1], "state": r[2] or {}} if r else None

    def chat_update_thread(self, tid: int, state: dict | None = None,
                           title: str | None = None) -> None:
        with self.conn.cursor() as cur:
            if state is not None:
                cur.execute("UPDATE chat_threads SET state=%s, updated_at=now() WHERE id=%s",
                            (Json(state), tid))
            if title is not None:
                cur.execute("UPDATE chat_threads SET title=%s, updated_at=now() WHERE id=%s",
                            (title[:120], tid))

    def chat_add_message(self, tid: int, question: str, resolved: str, summary: str,
                         payload: dict) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_messages (thread_id, question, resolved, summary, payload)"
                " VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (tid, question, resolved, (summary or "")[:300], Json(payload or {})))
            mid = int(cur.fetchone()[0])
            cur.execute("UPDATE chat_threads SET updated_at=now() WHERE id=%s", (tid,))
            return mid

    def chat_messages(self, tid: int, limit: int = 50) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, question, resolved, summary, payload, created_at"
                        " FROM chat_messages WHERE thread_id=%s ORDER BY id LIMIT %s",
                        (tid, limit))
            return [{"id": r[0], "question": r[1], "resolved": r[2], "summary": r[3],
                     "payload": r[4] or {}, "created_at": str(r[5])} for r in cur.fetchall()]

    def replace_party_edges(self, fact_id, src: dict, edges: list[dict]) -> None:
        """Fact 단위로 멱등 교체(재추출 중복 방지)."""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM related_parties WHERE fact_id=%s", (fact_id,))
            for e in edges:
                cur.execute(
                    "INSERT INTO related_parties (corp_code, party_name, party_norm, relationship,"
                    "txn_type, group_name, fiscal_year, rcept_no, dart_url, fact_id, run_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (src.get("corp_code"), e.get("party_name"), e.get("party_norm"),
                     e.get("relationship"), e.get("txn_type"), e.get("group_name"),
                     src.get("fiscal_year"), src.get("rcept_no"), src.get("dart_url"),
                     fact_id, settings.pipeline_version))

    def related_parties_of(self, corp_code: str, limit: int = 200) -> list[dict]:
        """[1홉] 기업 X → X가 보고한 특수관계자(상대방) 목록."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT party_name, relationship, txn_type, group_name, fiscal_year, dart_url, evidence "
                "FROM (SELECT rp.*, f.evidence_text AS evidence FROM related_parties rp "
                "LEFT JOIN facts f ON f.fact_id=rp.fact_id WHERE rp.corp_code=%s) t "
                "ORDER BY party_name LIMIT %s", (corp_code, int(limit)))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def related_party_peers(self, corp_code: str, limit: int = 200) -> list[dict]:
        """[다홉] 기업 X → (공유 상대방/기업집단) → 같은 상대·집단으로 엮인 다른 기업들."""
        with self.conn.cursor() as cur:
            cur.execute(
                "WITH mine AS (SELECT party_norm, group_name FROM related_parties WHERE corp_code=%s) "
                "SELECT rp.corp_code, "
                "  string_agg(DISTINCT rp.party_name, ', ') AS via_party, "
                "  string_agg(DISTINCT rp.group_name, ', ') FILTER (WHERE rp.group_name IS NOT NULL) AS via_group "
                "FROM related_parties rp WHERE rp.corp_code<>%s AND ("
                "  rp.party_norm IN (SELECT party_norm FROM mine WHERE COALESCE(party_norm,'')<>'') "
                "  OR rp.group_name IN (SELECT group_name FROM mine WHERE COALESCE(group_name,'')<>'')) "
                "GROUP BY rp.corp_code LIMIT %s", (corp_code, corp_code, int(limit)))
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c and not c.closed:
            c.close()
            self._local.conn = None
