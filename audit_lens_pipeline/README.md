# 감사렌즈 데이터 파이프라인 (v1)

코스피 전 종목의 감사보고서·재무제표를 수집하고, **데이터 우선(Phase 0)** 으로
포맷·단위·산업분류를 분석해 설계를 확정하기 위한 실행 스크립트.

## 확정 결정 (v1)
- **범위**: 코스피 전 종목, 전 산업, 처음부터 전체. (코스닥 제외)
- **보고서**: 최근 3개년 사업보고서(감사) + 최근 1개 분기/반기(검토).
- **임베딩**: BGE-M3(자체 호스팅) — Phase 0에서 Upstage/Cohere와 A/B.
- **벡터 저장소**: Amazon OpenSearch(하이브리드+필터). 정형/Fact는 PostgreSQL.
- **산업 라벨**: KRX·DART 둘 다 검증 후 Phase 0 결과로 결정.
- **인용**: 원문 그대로 인용 → 기계 글자 대조 검증.
- **재현성**: `PIPELINE_VERSION` + `AS_OF_DATE` 스냅샷.

## 설치
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 키 입력 (OpenDART / KRX / Anthropic)
```

## 실행 (수동 트리거, 데이터 우선 순서)
```bash
python -m src.cli universe --enrich   # ① 코스피 유니버스(+업종코드)
python -m src.cli discover            # ② 최근 3년 감사 + 최근 분/반기 검토
python -m src.cli fetch               # ③ 원본 보고서 최우선 수집(멱등)
python -m src.cli phase0 --deep 5     # ④ 약 500 표본 분석(+Claude 심층 5건)
```
③·④의 산출물(`data/v1/phase0/embedding_spec.json`, `industry_validation.json`)을
보고 **설계를 확정한 뒤** 다음 단계로 진입한다.

## 다음 단계 (Phase 0 확정 후 구현)
`parse`(섹션·표·단위 정규화) → `embed`(BGE-M3→OpenSearch) →
`extract`(Fact 추출, Batch+캐싱) → `validate`(골든셋 회귀·인용 검증).

## 디렉터리
```
config.py                 설정/키 로딩
src/clients/opendart.py   OpenDART 클라이언트
src/clients/krx.py        KRX 클라이언트(키/명세는 사용자 제공)
src/pipeline/build_universe.py  ① 유니버스
src/pipeline/discover.py        ② 공시 탐색
src/pipeline/fetch.py           ③ 원본 수집
src/pipeline/phase0_analyze.py  ④ Phase 0 분석
src/db/schema.sql         PostgreSQL 스키마
src/cli.py                파이프라인 러너
data/<version>/...        산출물(raw/meta/phase0)
```

## 주의
- KRX OpenAPI의 정확한 서비스 ID·필드는 `src/clients/krx.py`의 `_SERVICE_*`,
  `_FIELD_MAP`을 사용자 서비스 목록에 맞게 교체.
- OpenDART는 일 호출 한도가 있으므로 `OPENDART_RATE_PER_SEC`로 조절.
- 운영 시 키는 AWS Secrets Manager로 이전.
