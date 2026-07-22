"""OpenSearch 하이브리드 벡터스토어 클라이언트 (로컬 Docker PoC → AWS 전환 가능).

색인: dense(knn_vector 1024, HNSW cosine) + text(BM25, cjk 분석기) + 메타 필터.
검색: dense / BM25 / 하이브리드(RRF 융합).
로컬 PoC라 보안 비활성. AWS(OpenSearch Service) 전환 시 host/포트/auth만 교체.
※ 한국어 형태소(nori) 분석기는 후속 과제(현재 cjk bigram). dense가 의미 매칭 주담당.
"""
from __future__ import annotations
import hashlib
import logging
from opensearchpy import OpenSearch, helpers
from config import settings

log = logging.getLogger(__name__)

_KEYWORD_FIELDS = ("corp_code", "corp_name", "period_type", "assurance",
                   "doc_type", "note_no", "rcept_no", "dcm_no", "dart_url")


def _index_body(dim: int) -> dict:
    use_nori = getattr(settings, "use_nori", False)
    # 한국어 형태소(nori): 복합어 분해(mixed)+조사/어미 제거 → BM25 정밀도↑. 미설치/off면 cjk bigram.
    settings_block = {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}}
    if use_nori:
        settings_block["analysis"] = {
            "tokenizer": {"nori_mixed": {"type": "nori_tokenizer", "decompound_mode": "mixed"}},
            "analyzer": {"korean": {"type": "custom", "tokenizer": "nori_mixed",
                                    "filter": ["nori_part_of_speech", "lowercase"]}},
        }
    txt_analyzer = "korean" if use_nori else "cjk"
    return {
        "settings": settings_block,
        "mappings": {
            # 임베딩은 kNN 색인에만 두고 _source에는 중복 저장 안 함(인덱스 ~35%↓).
            "_source": {"excludes": ["embedding"]},
            "properties": {
            "embedding": {"type": "knn_vector", "dimension": dim,
                          "method": {"name": "hnsw", "engine": "lucene",
                                     "space_type": "cosinesimil"}},
            "text": {"type": "text", "analyzer": txt_analyzer},
            "section_path": {"type": "text", "analyzer": txt_analyzer,
                             "fields": {"raw": {"type": "keyword"}}},
            "corp_code": {"type": "keyword"}, "corp_name": {"type": "keyword"},
            "period_type": {"type": "keyword"}, "assurance": {"type": "keyword"},
            "doc_type": {"type": "keyword"}, "note_no": {"type": "keyword"},
            "rcept_no": {"type": "keyword"}, "dcm_no": {"type": "keyword"},
            "dart_url": {"type": "keyword"},
            "fiscal_year": {"type": "integer"},
            "is_consolidated": {"type": "boolean"},
            "unit_hint": {"type": "keyword"},
        }},
    }


class OpenSearchStore:
    def __init__(self, host: str | None = None, port: int | None = None,
                 index: str | None = None):
        self.host = host or settings.opensearch_host
        self.port = port or settings.opensearch_port
        self.index = index or settings.opensearch_index
        self.client = OpenSearch(
            hosts=[{"host": self.host, "port": self.port}],
            http_compress=True, use_ssl=settings.opensearch_use_ssl,
            verify_certs=False, ssl_show_warn=False,
            timeout=60, max_retries=3, retry_on_timeout=True,
        )

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False

    def ensure_index(self, recreate: bool = False) -> None:
        exists = self.client.indices.exists(index=self.index)
        if exists and recreate:
            log.info("기존 인덱스 삭제: %s", self.index)
            self.client.indices.delete(index=self.index)
            exists = False
        if not exists:
            self.client.indices.create(index=self.index,
                                       body=_index_body(settings.embedding_dim))
            log.info("인덱스 생성: %s (dim=%d)", self.index, settings.embedding_dim)

    @staticmethod
    def doc_id(chunk: dict) -> str:
        key = f"{chunk['rcept_no']}|{chunk['doc_type']}|{chunk['section_path']}|{chunk['chunk_ix']}"
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    def _action(self, chunk: dict, vector) -> dict:
        fy = str(chunk.get("fiscal_year", "")).strip()
        src = {
            "text": chunk["text"], "section_path": chunk.get("section_path", ""),
            "embedding": vector.tolist() if hasattr(vector, "tolist") else list(vector),
            "fiscal_year": int(fy) if fy.isdigit() else None,
            "is_consolidated": bool(chunk.get("is_consolidated")),
            "unit_hint": chunk.get("unit_hint") or [],
        }
        for f in _KEYWORD_FIELDS:
            src[f] = chunk.get(f, "")
        return {"_op_type": "index", "_index": self.index,
                "_id": self.doc_id(chunk), "_source": src}

    def bulk_index(self, chunks: list[dict], vectors) -> tuple[int, int]:
        actions = (self._action(c, v) for c, v in zip(chunks, vectors))
        ok, errors = helpers.bulk(self.client, actions, chunk_size=500,
                                  raise_on_error=False, request_timeout=120)
        n_err = len(errors) if isinstance(errors, list) else int(errors or 0)
        return ok, n_err

    def refresh(self) -> None:
        self.client.indices.refresh(index=self.index)

    def count(self) -> int:
        try:
            return int(self.client.count(index=self.index).get("count", 0))
        except Exception:
            return 0

    def existing_ids(self, ids: list[str]) -> set[str]:
        """주어진 doc_id 중 인덱스에 이미 있는 것(top-off용, _source 미반환)."""
        if not ids:
            return set()
        resp = self.client.mget(index=self.index, body={"ids": list(ids)},
                                _source=False)
        return {d["_id"] for d in resp.get("docs", []) if d.get("found")}

    # ── 검색 ──
    @staticmethod
    def _filters(filters: dict | None) -> list:
        if not filters:
            return []
        out = []
        for k, v in filters.items():
            out.append({"terms": {k: list(v)}} if isinstance(v, (list, tuple))
                       else {"term": {k: v}})
        return out

    @staticmethod
    def _not_corps(exclude_corps):
        return [{"terms": {"corp_code": list(exclude_corps)}}] if exclude_corps else []

    def search_bm25(self, query: str, filters: dict | None = None, k: int = 20,
                    exclude_corps=None) -> list:
        # 임베딩 전수조사(Phase 1) 반영: 섹션경로도 검색 신호로(제목 질의 recall↑).
        # text가 주 신호, section_path는 보조 가중(^1.4) — 색인은 기존 그대로라 재임베딩 불필요.
        bq = {"must": [{"multi_match": {"query": query,
                                        "fields": ["text", "section_path^1.4"]}}],
              "filter": self._filters(filters)}
        mn = self._not_corps(exclude_corps)
        if mn:
            bq["must_not"] = mn
        body = {"size": k, "query": {"bool": bq}}
        return self.client.search(index=self.index, body=body)["hits"]["hits"]

    def search_dense(self, vector, filters: dict | None = None, k: int = 20,
                     exclude_corps=None) -> list:
        knn = {"embedding": {"vector": vector.tolist() if hasattr(vector, "tolist")
                             else list(vector), "k": k}}
        mn = self._not_corps(exclude_corps)
        if filters or mn:
            f = {"bool": {"filter": self._filters(filters)}}
            if mn:
                f["bool"]["must_not"] = mn
            knn["embedding"]["filter"] = f
        body = {"size": k, "query": {"knn": knn}}
        return self.client.search(index=self.index, body=body)["hits"]["hits"]

    def hybrid_search(self, query: str, vector, filters: dict | None = None,
                      k: int = 10, rrf_k: int = 60, exclude_corps=None) -> list:
        """BM25 + dense 결과를 RRF로 융합(엔진 무관, 견고). exclude_corps=검색에서 제외할 기업."""
        pools = (self.search_bm25(query, filters, k=k * 2, exclude_corps=exclude_corps),
                 self.search_dense(vector, filters, k=k * 2, exclude_corps=exclude_corps))
        fused: dict = {}
        for hits in pools:
            for rank, h in enumerate(hits):
                e = fused.setdefault(h["_id"], {"hit": h, "score": 0.0})
                e["score"] += 1.0 / (rrf_k + rank + 1)
        ranked = sorted(fused.values(), key=lambda x: -x["score"])[:k]
        return [{**r["hit"], "_rrf": round(r["score"], 5)} for r in ranked]
