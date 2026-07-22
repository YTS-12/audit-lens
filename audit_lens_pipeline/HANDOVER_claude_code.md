# 감사렌즈 — Claude Code 인수인계서

> 목적: 설계·프로토타입·파이프라인 스켈레톤이 준비된 상태에서, **Claude Code가 실제 키로 데이터를 수집·검증하고 다음 단계를 구현**하도록 인계한다.
> 함께 보는 문서: `audit_rag_design.md`(설계서 전체), `audit_lens_ui.html`(UI 프로토타입), `audit_lens_pipeline/`(실행 스크립트).

---

## 1. 한 줄 정의
회계사가 자연어로 물으면, **코스피 전 종목의 연결 감사보고서·재무제표**를 근거로 **기업명 + DART 딥링크 + 보고서 내 위치 + 원문 스니펫**을 제시하는 RAG 도구.

## 2. 지금까지 확정된 것 (변경 금지 전제)
| 항목 | 결정 |
|---|---|
| 범위 | **코스피 전 종목, 전 산업, 처음부터 전체** (코스닥 제외) |
| 보고서 | 최근 3개년 **사업보고서(감사)** + 최근 1개 **분기/반기(검토)** |
| 수집 기준 | **연결(CFS) 감사보고서 첨부문서 기준**. 별도(OFS)는 보조 |
| 딥링크 | `https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}&dcmNo={dcmNo}` — 예: `rcpNo=20260318001395&dcmNo=11142436`(현대건설 연결 감사보고서) |
| 임베딩 | **BGE-M3 자체 호스팅**(dense+sparse 하이브리드). Phase 0에서 Upstage/Cohere와 A/B |
| 벡터 저장소 | **Amazon OpenSearch**(BM25+kNN+필터). 정형/Fact는 PostgreSQL |
| LLM | Claude API — 라우팅 Haiku 4.5 / 합성 Sonnet 4.6 / 고난도 Opus 4.8, 대량추출 Batch+캐싱 |
| 검색 | 질의이해→멀티쿼리(+온톨로지)→**3경로**(벡터 / Fact Store / 온디맨드 추출) |
| 오케스트레이션 | LangGraph + 멀티에이전트, 도구는 MCP |
| 인용 | **원문 그대로 인용 → 기계 글자 대조 검증**, 실패 주장 폐기 |
| 재현성 | `PIPELINE_VERSION` + `AS_OF_DATE` 스냅샷, 모델/데이터 버전 로깅 |
| 산업 라벨 | KRX·DART 둘 다 검증 후 **Phase 0 결과로 결정** |
| 골든셋 | **Claude Code + 개발자 공동 검토**로 구축(대표 질의 30~50건) |

## 3. 리포지토리 현황
```
audit_lens_pipeline/
  config.py                 설정/키(.env). 운영은 Secrets Manager
  src/clients/opendart.py   ✅ corpCode/company/list/document/financials + dcmNo 해소 + deep_link
  src/clients/krx.py        ⚠️ 인터페이스만. 서비스ID/필드 교체 필요
  src/pipeline/build_universe.py  ✅ 코스피 유니버스(+--enrich 업종코드)
  src/pipeline/discover.py        ✅ 3년 감사 + 최근 분/반기, 연결 dcmNo 해소
  src/pipeline/fetch.py           ✅ 원본 ZIP 멱등 다운로드
  src/pipeline/phase0_analyze.py  ✅ 약 500 표본 단위·구조·산업 분석
  src/db/schema.sql         ✅ companies/disclosures/facts/parse_failures/eval/runs
  src/cli.py                ✅ universe/discover/fetch/phase0 (+parse/embed/extract/validate=TODO)
```
검증 완료: 11개 파일 컴파일 통과, 보고서 분류(연차=감사/분·반기=검토)·단위 탐지·딥링크 빌더 동작.
미검증(네트워크/키 필요): 실제 OpenDART·KRX 호출.

## 4. ⚠️ Claude Code가 먼저 확인·보정할 것 (필수)

### 4.1 KRX OpenAPI 서비스 ID·필드명 (사용자 키로 확인)
- 파일: `src/clients/krx.py` 의 `_BASE`, `_SERVICE_KOSPI_BASE`, `_FIELD_MAP`.
- 할 일: KRX OpenAPI '주식' 카테고리에서 **코스피 종목 명단 + 업종/섹터**를 주는 정확한 서비스 ID와 응답 필드명(종목코드/종목명/업종)을 확인해 교체.
- 인증 방식(헤더 `AUTH_KEY` 등)·요청 파라미터(`basDd` 등)·페이지네이션·호출한도 확인.
- 참고: https://openapi.krx.co.kr → 서비스 이용 → 주식.

### 4.2 연결 감사보고서 dcmNo 해소 (딥링크 핵심)
- 파일: `src/clients/opendart.py` 의 `consolidated_doc_no()`.
- 현재는 DART 뷰어(`dsaf001/main.do?rcpNo=`) HTML에서 `viewDoc('rcpNo','dcmNo',...)` 패턴과 "연결" 키워드 근접 매칭으로 추정. **실제 첨부문서 트리 구조를 확인**해 정확히 "연결재무제표에 대한 감사보고서" 노드의 `dcmNo`를 잡도록 보정.
- 검증 기준 사례: `rcpNo=20260318001395` → `dcmNo=11142436` (현대건설 연결 감사보고서)가 나와야 함.
- 대안 검토: 첨부문서 목록을 주는 더 안정적인 경로(있으면)로 교체.

### 4.3 OpenDART 운영 제약
- 일 호출 한도 확인 → `OPENDART_RATE_PER_SEC`·재시도 튜닝.
- `document.xml` ZIP 내부 구조(여러 하위문서) 확인 → 연결 감사보고서 파일 식별 규칙 확정.

## 5. 즉시 실행 순서 (데이터 우선)
```bash
cd audit_lens_pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # OPENDART/KRX/ANTHROPIC 키 입력
python -m src.cli universe --enrich     # ① 코스피 유니버스(+업종코드)
python -m src.cli discover              # ② 보고서 탐색 + 연결 dcmNo 딥링크
python -m src.cli fetch                 # ③ 원본 최우선 수집
python -m src.cli phase0 --deep 5       # ④ 약 500 표본 분석(+Claude 심층 5건)
```
→ 산출물 `data/v1/phase0/embedding_spec.json`·`industry_validation.json` 을 **개발자와 함께 검토**하고,
   가정과 다르면 **설계서(`audit_rag_design.md`) 해당 절을 갱신**한 뒤 다음 단계로.

## 6. 다음 구현 단계 (Phase 0 확정 후)
| 단계 | 내용 | 설계 참조 |
|---|---|---|
| `parse` | 연결 감사보고서·주석 섹션 분해, 표 정규화, **단위 전파·정규화**, 비전 폴백(이미지표), 실패 격리 | §3, §3.5.2 |
| `embed` | 섹션-인지 청킹(+단위·통화 헤더) → BGE-M3 → OpenSearch 하이브리드 색인 | §4 |
| `extract` | v1 Fact 12종 추출(Batch+캐싱) → PostgreSQL `facts` | §0.1, §5.2 |
| `ontology` | 통제어휘/동의어(정액법↔정률법, 회계추정 변경 규칙 등) → 멀티쿼리·추출 공용 | §5.8 |
| `query` | 질의이해→멀티쿼리→3경로→재랭킹→합성→**기계 인용검증** | §5.7, §7 |
| `validate` | 골든셋 회귀(추출 정밀/재현율·인용 정확도·파싱 실패율) | §0.1, §11 |

### v1 사전추출 Fact 12종
① 감사의견 유형 ② KAM 주제 ③ 계속기업 불확실성 ④ 감가상각방법/내용연수 변경 ⑤ 회계정책 변경 ⑥ 회계추정 변경 ⑦ 소송·우발부채 ⑧ 특수관계자 거래 ⑨ 재고자산 평가방법 ⑩ 수익인식 정책 ⑪ 감사인·감사보수 ⑫ 내부회계관리제도 검토의견.
→ 목록 밖 주제는 **온디맨드 추출**(산업 필터로 후보 좁힌 뒤 질의시점 추출). 자주 들어오면 다음 회차에 사전추출로 승격.

## 7. 반드시 지킬 규약
- **연결(CFS) 기준** 수집·임베딩·인용. 재무제표 API는 `fs_div=CFS`.
- 모든 근거에 **딥링크(rcpNo+dcmNo) + 섹션 경로 + 원문 스니펫**.
- 수치는 **단위·통화 동반**(XBRL unitRef 우선, 없으면 표 단위선언 전파). 단위 없는 수치 적재 금지.
- 답변에 **"적재 N개 기업 기준·누락 가능"** 고지.
- 인용은 **기계 글자 대조** 통과분만. LLM 자기판단 금지.
- 키는 코드/리포 금지 → `.env`(개발) / Secrets Manager(운영).

## 8. 리스크·주의
- DART 원문 표가 이미지인 경우 텍스트 추출 실패 → **비전 폴백** 필요(`parse`에서). `phase0`가 `parse_failure_rate`로 규모를 먼저 알려줌.
- 업종코드(KRX vs DART)와 실제 사업 불일치(지주회사 등) → Phase 0 검증·예외목록.
- 정기보고서 시즌(3·5월) 폭주 → 증분·재개 견고화.
- 데이터 약관·상업적 이용 가능 여부(OpenDART·KRX)는 **법무 확인 필요**(기술 외 선결 게이트).

## 9. 첫 작업 체크리스트 (순서대로)
- [ ] `.env`에 3개 키 입력, `pip install -r requirements.txt`
- [ ] **4.1** KRX 서비스 ID·필드 교체 후 `universe --enrich` 소량 검증
- [ ] **4.2** `consolidated_doc_no()`를 현대건설 사례로 검증(→ dcmNo=11142436)
- [ ] `discover` → `fetch` 소량(예: 20사) 파일럿
- [ ] `phase0` 실행 → `embedding_spec.json`·`industry_validation.json` 개발자 공동 리뷰
- [ ] 리뷰 결과로 설계서 갱신 → `parse` 착수
