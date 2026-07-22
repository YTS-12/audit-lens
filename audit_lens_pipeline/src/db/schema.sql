-- 감사렌즈 정형 저장소 스키마 (Aurora PostgreSQL)
-- 벡터(청크)는 OpenSearch에, 정형/재현성 메타는 여기에 둔다.

-- 실행 회차(재현성): 데이터/모델 버전 + as-of 스냅샷
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    pipeline_version TEXT NOT NULL,
    as_of_date    DATE NOT NULL,
    embedding_model  TEXT,
    started_at    TIMESTAMPTZ DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    notes         TEXT
);

-- 대상 기업 유니버스(코스피 전 종목) 스냅샷
CREATE TABLE IF NOT EXISTS companies (
    corp_code     TEXT NOT NULL,           -- DART 고유번호
    stock_code    TEXT,
    corp_name     TEXT,
    market        TEXT DEFAULT 'KOSPI',
    krx_sector    TEXT,                     -- KRX 업종
    induty_code   TEXT,                     -- DART 표준산업분류
    industry_name TEXT,                     -- 검증 후 확정 서비스 라벨(Phase 0)
    fiscal_month  TEXT,                     -- 결산월
    listed_flag   TEXT,                     -- 신규상장/상장폐지/정상 등 예외 플래그
    as_of_date    DATE NOT NULL,
    PRIMARY KEY (corp_code, as_of_date)
);

-- 공시(보고서) 목록: 최근 3년 감사 + 최근 분/반기 검토
CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no      TEXT PRIMARY KEY,         -- 접수번호
    corp_code     TEXT NOT NULL,
    report_nm     TEXT,
    fiscal_year   INT,
    period_type   TEXT,                     -- 연차/반기/분기
    assurance     TEXT,                     -- 감사/검토
    rcept_dt      DATE,
    dcm_no        TEXT,                     -- 연결 감사보고서 첨부문서 번호
    fs_basis      TEXT DEFAULT 'CFS',       -- 수집 기준(연결)
    dart_url      TEXT,                     -- 딥링크(rcpNo+dcmNo)
    fetched       BOOLEAN DEFAULT FALSE,
    parse_status  TEXT DEFAULT 'pending'    -- pending/ok/failed
);

-- 추출된 사실(Fact Store): 스크리닝/집계의 근간
CREATE TABLE IF NOT EXISTS facts (
    fact_id       BIGSERIAL PRIMARY KEY,
    corp_code     TEXT NOT NULL,
    fiscal_year   INT,
    fact_type     TEXT NOT NULL,            -- 온톨로지 표준 타입
    detail        JSONB,                    -- {from,to,asset,...}
    -- 수치형 단위 보존
    value_raw     NUMERIC,
    unit_scale    TEXT,                     -- 원/천원/백만원
    currency      TEXT,                     -- KRW/USD...
    value_krw     NUMERIC,                  -- 정규값
    is_consolidated BOOLEAN,                -- 연결/별도
    -- 근거(인용 추적성)
    evidence_text TEXT,
    section_path  TEXT,                     -- "주석>3.유형자산"
    rcept_no      TEXT,
    dcm_no        TEXT,                     -- 연결 감사보고서 첨부문서 번호
    dart_url      TEXT,                     -- 딥링크(rcpNo+dcmNo)
    confidence    TEXT,                     -- ok/review
    run_id        TEXT REFERENCES runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_facts_screen
    ON facts (fact_type, fiscal_year);
CREATE INDEX IF NOT EXISTS idx_facts_corp ON facts (corp_code);

-- 특수관계자 네트워크(지식그래프 엣지): 기업 → 상대방(계열사/종속기업/보증 등)
-- 기존 특수관계자_거래 Fact에서 상대방 엔티티를 정형화해 적재(설계서 §5.8 지식그래프 확장)
CREATE TABLE IF NOT EXISTS related_parties (
    edge_id      BIGSERIAL PRIMARY KEY,
    corp_code    TEXT NOT NULL,            -- 주체(보고 기업)
    party_name   TEXT NOT NULL,            -- 상대방 명칭(원문)
    party_norm   TEXT,                     -- 정규화 명칭(㈜·(주)·공백 제거)
    relationship TEXT,                     -- 종속기업/관계기업/계열사/모회사/공동기업/기타
    txn_type     TEXT,                     -- 매출/매입/보증/자금대여/차입/지분/기타
    group_name   TEXT,                     -- 기업집단명(예: 농협)
    fiscal_year  INT,
    rcept_no     TEXT,
    dart_url     TEXT,                     -- 딥링크
    fact_id      BIGINT,                   -- 원천 facts.fact_id(멱등 교체 키)
    run_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rp_corp ON related_parties (corp_code);
CREATE INDEX IF NOT EXISTS idx_rp_party ON related_parties (party_norm);
CREATE INDEX IF NOT EXISTS idx_rp_group ON related_parties (group_name);

-- 파싱 실패(품질지표 '못 읽은 문서 비율' + 수동 검수)
CREATE TABLE IF NOT EXISTS parse_failures (
    rcept_no   TEXT,
    reason     TEXT,
    detail     TEXT,
    run_id     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 평가 골든셋(Claude Code + 개발자 공동 검토)
CREATE TABLE IF NOT EXISTS eval_queries (
    eval_id      BIGSERIAL PRIMARY KEY,
    query_text   TEXT NOT NULL,
    expected     JSONB,                     -- [{corp_code, section_path}]
    reviewed_by  TEXT,
    created_at   TIMESTAMPTZ DEFAULT now()
);
