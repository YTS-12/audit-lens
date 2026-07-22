"""중앙 설정. .env 또는 환경변수에서 로딩한다.
운영에서는 키를 AWS Secrets Manager로 옮기고 load_secret()만 교체하면 된다.
"""
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 키
    opendart_api_key: str = ""
    krx_api_key: str = ""
    anthropic_api_key: str = ""

    # 웹 접근 비밀번호(설정 시 UI·API에 인증 요구; 비어있으면 인증 없음=로컬 개방).
    # 코드에 하드코딩 금지 — .env의 AUTH_PASSWORD로만 주입(예: AWS 인스턴스).
    auth_password: str = ""

    # 경로
    data_dir: Path = Path("./data")

    # 수집 파라미터
    target_market: str = "KOSPI"
    years_back: int = 3
    as_of_date: str = "2026-06-24"
    pipeline_version: str = "v1"

    # 모델 — 역할 분담: understand(이해·재작성)=Sonnet 5, router(표 정규화·HyDE·저비용 합성)=Haiku,
    # workhorse(기본 합성)=Sonnet 5, hard(에스컬레이션)=Opus
    claude_model_router: str = "claude-haiku-4-5-20251001"
    claude_model_understand: str = "claude-sonnet-5"
    claude_model_workhorse: str = "claude-sonnet-5"
    claude_model_hard: str = "claude-opus-4-8"

    # 임베딩 (BGE-M3, GPU) — dim 1024 검증됨
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    embedding_device: str = "cuda"          # GPU. CPU면 'cpu'
    embed_batch_size: int = 32
    embed_max_seq_length: int = 1024        # 청크 ~1400자 커버

    # 재랭킹 (크로스인코더, BGE-M3와 짝) — 하이브리드 후보 재정렬로 정확도↑
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_device: str = "cpu"            # 서버 GPU(임베더)와 경합 회피. 후보 ~30개라 CPU도 빠름
    use_reranker: bool = True               # 모델 없거나 OOM이면 자동 폴백(RRF 순)

    # 한국어 형태소 분석기(nori) — BM25 토큰화 개선. 적용은 --recreate 재색인 필요.
    use_nori: bool = True                   # False면 cjk bigram(폴백)

    # 스크리닝 온디맨드 보완: Fact Store 전수(정밀 코어)에 더해, 사전추출 12종 밖이거나
    # 추출 누락된 후보를 벡터+Haiku로 소량 보완(recall↑). False면 순수 Fact Store만.
    screen_ondemand: bool = True
    screen_ondemand_max: int = 5            # 보완으로 추가할 최대 기업 수(비용·지연 제어)

    # 온디맨드 우선(방향 B): Fact Store 전수 스크리닝은 '전부/모두/명단' 등 명시적 완전성 질의에만 사용.
    # 나머지(애매한 스크리닝)는 온디맨드(의미검색)로 → 오라우팅·범주덤프 방지. False면 기존(스크리닝 항상 Fact Store).
    factstore_exhaustive_only: bool = True

    # Opus 에스컬레이션: 근거는 있는데 합성이 불충분한 어려운 질의를 Opus로 1회 재합성(품질↑·게이트로 비용통제)
    use_opus_escalation: bool = True

    # HyDE: 개방형/온디맨드 질의에 '가상 근거 문단'을 만들어 그 임베딩으로 추가검색(recall↑, +Haiku 1회)
    use_hyde: bool = True

    # 답변 감사자(생성-평가 분리): 합성 답변을 독립 채점(Haiku 1회) → 반려 시 Opus 재합성(품질↑, 저비용)
    use_answer_judge: bool = True

    # 접근/질의 감사로그: 경로 지정 시 질의를 파일에 기록(누가·언제·무엇). 비면 stdout 로그만.
    audit_log_path: str = ""

    # TLS(자기서명): cert/key 지정 시 HTTPS로 서빙(비면 HTTP). 비밀번호·데이터 전송 암호화.
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    # AWS Secrets Manager: 시크릿 이름 지정 시 키를 거기서 로드(.env보다 우선). 인스턴스 IAM 역할 필요.
    aws_secret_name: str = ""
    aws_region: str = "ap-northeast-2"

    # 벡터스토어 (로컬 Docker OpenSearch — AWS 전환 시 host만 교체)
    opensearch_host: str = "localhost"
    opensearch_port: int = 9200
    opensearch_index: str = "audit_chunks_v2"  # 파서 v2 인덱스(표 자체완결 100%) — v1은 롤백용 보존
    opensearch_use_ssl: bool = False

    # 정형 저장소 (PostgreSQL Fact Store — 로컬 Docker; AWS Aurora 전환 시 host만 교체)
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_db: str = "auditlens"
    pg_user: str = "auditlens"
    pg_password: str = "auditlens"

    # 호출 제어
    opendart_rate_per_sec: float = 2.0
    http_timeout: int = 30
    http_max_retry: int = 4

    # Phase 0
    phase0_sample_size: int = 500

    # ── 파생 경로 ──
    @property
    def raw_dir(self) -> Path:      # 원본 ZIP/XML
        return self.data_dir / self.pipeline_version / "raw"

    @property
    def meta_dir(self) -> Path:     # 유니버스/공시 목록 스냅샷
        return self.data_dir / self.pipeline_version / "meta"

    @property
    def phase0_dir(self) -> Path:   # Phase 0 산출물
        return self.data_dir / self.pipeline_version / "phase0"

    def ensure_dirs(self) -> None:
        for p in (self.raw_dir, self.meta_dir, self.phase0_dir):
            p.mkdir(parents=True, exist_ok=True)


def _load_from_secrets_manager(s: "Settings") -> None:
    """aws_secret_name 설정 시 Secrets Manager에서 키를 로드해 덮어쓴다(운영 보안; 실패 시 .env 유지)."""
    if not s.aws_secret_name:
        return
    try:
        import json
        import boto3
        raw = boto3.client("secretsmanager", region_name=s.aws_region
                           ).get_secret_value(SecretId=s.aws_secret_name)
        for k, v in json.loads(raw.get("SecretString") or "{}").items():
            field = k.lower()
            if v and hasattr(s, field):
                setattr(s, field, v)
        import logging
        logging.getLogger(__name__).info("Secrets Manager 로드 완료: %s", s.aws_secret_name)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Secrets Manager 로드 실패(.env 사용): %s", e)


settings = Settings()
_load_from_secrets_manager(settings)
