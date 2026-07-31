"""Stage 6: embed — 파싱 청크 → BGE-M3(dense, GPU) → OpenSearch 하이브리드 색인.

⚠️ 중요: `sentence_transformers`를 `torch`보다 **먼저** import해야 한다.
   (Windows/conda에서 torch 먼저 로드 시 네이티브 라이브러리 충돌로 segfault.
    검증: ST→torch 순서면 정상, torch→ST 순서면 SIGSEGV.)
   그래서 이 모듈은 ST를 최상단에서 import하고, torch는 직접 import하지 않는다.
"""
from __future__ import annotations
# ── ST를 가장 먼저 (torch보다 앞) ──
from sentence_transformers import SentenceTransformer, CrossEncoder  # noqa: E402 (순서 의도적)

import csv
import json
import logging
import os
import threading
from config import settings
from src.clients.opensearch_store import OpenSearchStore

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
log = logging.getLogger(__name__)

# 모델 추론 직렬화 락 — 멀티유저 동시요청이 단일 GPU/모델을 두고 경합·OOM하지 않도록 '질의 큐'화.
# (임베더·재랭커가 공유. 추론이 병목이라 여기서 직렬화하는 게 자연스러움.)
_INFER_LOCK = threading.Lock()


class Embedder:
    """BGE-M3 dense 임베딩(GPU)."""
    def __init__(self):
        self.model = SentenceTransformer(settings.embedding_model,
                                         device=settings.embedding_device)
        self.model.max_seq_length = settings.embed_max_seq_length
        if settings.embedding_device == "cuda":
            self.model = self.model.half()      # fp16 — GPU 처리량 ↑ (8GB VRAM 절약)

    def encode(self, texts: list[str]):
        with _INFER_LOCK:                        # GPU 직렬화(멀티유저 큐)
            return self.model.encode(texts, normalize_embeddings=True,
                                     batch_size=settings.embed_batch_size,
                                     show_progress_bar=False)


class Reranker:
    """크로스인코더 재랭킹(BGE-reranker-v2-m3, GPU). 하이브리드 후보를 질문-청크 쌍으로 재점수.
    ST를 먼저 import하는 이 모듈에 둬 torch 순서 문제 회피. 로드 실패 시 예외→상위에서 폴백."""
    def __init__(self):
        dev = settings.reranker_device
        kw = {"torch_dtype": "float16"} if dev == "cuda" else {}
        self.model = CrossEncoder(settings.reranker_model, device=dev,
                                  max_length=512, automodel_args=kw)

    def score(self, pairs: list[tuple[str, str]]):
        with _INFER_LOCK:                        # 모델 추론 직렬화(멀티유저 큐)
            return self.model.predict(pairs, batch_size=settings.embed_batch_size,
                                      show_progress_bar=False)


def _corp_codes_for_sector(sector: str) -> set[str]:
    uni = csv.DictReader((settings.meta_dir / "universe.csv").open(encoding="utf-8-sig"))
    return {u["corp_code"] for u in uni if u.get("krx_sector") == sector}


def _chunk_files(corp: str | None, sector: str | None, src_subdir: str = "parsed"):
    parsed = settings.data_dir / settings.pipeline_version / src_subdir
    codes = _corp_codes_for_sector(sector) if sector else None
    for path in sorted(parsed.glob("*/*.jsonl")):
        cc = path.parent.name
        if corp and corp != cc:
            continue
        if codes is not None and cc not in codes:
            continue
        yield path


def run(limit: int | None = None, sector: str | None = None, corp: str | None = None,
        recreate: bool = False, index_batch: int = 2000, missing_only: bool = False,
        src_subdir: str = "parsed_v2", index: str | None = None):
    """기본 소스는 parsed_v2(서빙 인덱스와 동일 세대).

    ⚠️ 기본값이 "parsed"(v1)였던 시절, 인자 없는 `cli embed` 실행이 v1 청크를
    v2 인덱스에 섞을 수 있었다(무음 오염). v1 청크 재색인이 필요하면 src_subdir와
    index를 모두 명시적으로 지정할 것 — 소스와 인덱스 세대는 항상 함께 움직여야 한다."""
    store = OpenSearchStore(index=index)
    if not store.ping():
        raise SystemExit(f"OpenSearch 연결 실패 ({store.host}:{store.port}) — "
                         "Docker/엔진을 확인하세요 (docker compose -f infra/docker-compose.yml up -d)")
    store.ensure_index(recreate=recreate)
    embedder = Embedder()
    log.info("embed 시작: model=%s device=%s index=%s",
             settings.embedding_model, settings.embedding_device, store.index)

    files = list(_chunk_files(corp, sector, src_subdir=src_subdir))
    log.info("대상 파일 %d개", len(files))

    if missing_only:
        log.info("top-off 모드: 이미 색인된 청크는 건너뛰고 빠진 것만 임베딩")

    buf: list[dict] = []
    n = ok = err = fdone = skipped = 0
    expected: dict[str, int] = {}       # rcept_no → 파일 행수(색인 후 자동 대조용)
    dup_files = 0                       # 파일 내 문서 ID 충돌(=덮어쓰기 유실) 감지 수

    def flush():
        nonlocal ok, err
        if not buf:
            return
        vecs = embedder.encode([c["text"] for c in buf])
        o, e = store.bulk_index(buf, vecs)
        ok += o
        err += e
        buf.clear()

    stop = False
    for path in files:
        chunks = [json.loads(ln) for ln in path.open(encoding="utf-8") if ln.strip()]
        ids = [store.doc_id(c) for c in chunks]
        if chunks:
            expected[chunks[0].get("rcept_no", path.stem)] = len(chunks)
            ndup = len(ids) - len(set(ids))
            if ndup:                     # bulk가 같은 ID를 '성공'으로 덮어쓰므로 여기서만 잡힌다
                dup_files += 1
                log.error("문서 ID 충돌 %d건 — %s (chunk_ix 중복: 색인 시 덮어쓰기 유실)", ndup, path)
        if missing_only and chunks:
            have = store.existing_ids(ids)
            kept = [c for c, i in zip(chunks, ids) if i not in have]
            skipped += len(chunks) - len(kept)
            chunks = kept
        for c in chunks:
            buf.append(c)
            n += 1
            if len(buf) >= index_batch:
                flush()
            if limit and n >= limit:
                stop = True
                break
        fdone += 1
        if fdone % 50 == 0:
            flush()
            log.info("…%d/%d 파일 · 신규청크 %d (ok=%d err=%d · 기존건너뜀 %d)",
                     fdone, len(files), n, ok, err, skipped)
        if stop:
            break
    flush()
    store.refresh()
    # 색인 후 자동 대조(안전판): 보고서별 '파일 행수 = 인덱스 문서수'가 항상 성립해야 한다.
    # limit 실행은 파일을 중간 절단하므로 대조 제외. 불일치는 무음 유실·스테일 잔존의 신호.
    mismatch = 0
    if limit is None:
        for rc, exp in expected.items():
            got = int(store.client.count(index=store.index, body={
                "query": {"term": {"rcept_no": rc}}}).get("count", 0))
            if got != exp:
                mismatch += 1
                log.error("색인 검증 불일치: rcept=%s 파일행수=%d 색인=%d", rc, exp, got)
        log.info("[embed] 색인 검증: 보고서 %d건 대조 · 불일치 %d · ID충돌 파일 %d",
                 len(expected), mismatch, dup_files)
    log.info("[embed] 완료: 파일 %d · 신규청크 %d · 색인 ok=%d err=%d · 기존 %d · index count=%d",
             fdone, n, ok, err, skipped, store.count())
    return n, ok, err
