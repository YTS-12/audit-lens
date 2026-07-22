"""Stage: graph-parties — 특수관계자 지식그래프 확장(설계서 §5.8).

기존 `특수관계자_거래` Fact의 근거 서술에서 **상대방 엔티티**(계열사·종속기업 등)를
Haiku로 정형 추출해 `related_parties`(그래프 엣지) 테이블에 적재한다.
추가 문서 수집·재임베딩 없이 **이미 있는 Fact만 재사용**하므로 비용은 소량(Haiku·캐싱).

멱등: fact_id 단위로 교체. 이미 처리한 Fact는 건너뜀(--force로 전체 재구축).
"""
from __future__ import annotations
import logging
import re
from config import settings
from src.clients.claude import ClaudeClient
from src.clients.postgres import PostgresStore

log = logging.getLogger(__name__)

_SUFFIX = re.compile(r"(주식회사|㈜|\(주\)|\(유\)|유한회사|유한책임회사|합자회사|합명회사|\s)")


def _norm(name: str) -> str:
    """상대방 명칭 정규화(법인격 표기·공백 제거) → 기업 간 공유상대 매칭용."""
    return _SUFFIX.sub("", name or "").strip()


def _group_norm(g: str | None) -> str | None:
    if not g:
        return None
    return re.sub(r"(기업집단|그룹|계열|\s)", "", g).strip() or None


def run(force: bool = False, limit: int | None = None) -> None:
    pg = PostgresStore()
    if not pg.ping():
        raise SystemExit("PostgreSQL 미연결 — docker compose up 확인")
    pg.ensure_parties_table()
    claude = ClaudeClient()

    facts = pg.related_party_source_facts(limit=limit)
    done = set() if force else pg.parties_done_fact_ids()
    todo = [f for f in facts if f["fact_id"] not in done]
    log.info("특수관계자_거래 Fact %d건 중 처리대상 %d건(이미 %d건 완료)",
             len(facts), len(todo), len(facts) - len(todo))

    n_edge, n_fact, n_fail = 0, 0, 0
    for f in todo:
        detail = f.get("detail") or {}
        text = (f.get("evidence_text") or detail.get("description") or "").strip()
        if not text:
            continue
        try:
            parties = claude.extract_parties(text)
        except Exception as e:  # noqa: BLE001  일시오류는 건너뛰고 재개(멱등)
            n_fail += 1
            log.warning("fact_id=%s 추출 실패(건너뜀, 재실행 시 이어서 처리): %s", f["fact_id"], e)
            continue
        edges = []
        for p in parties:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            edges.append({
                "party_name": name, "party_norm": _norm(name),
                "relationship": (p.get("relationship") or "기타")[:20],
                "txn_type": (p.get("txn_type") or "기타")[:20],
                "group_name": _group_norm(p.get("group_name")),
            })
        pg.replace_party_edges(f["fact_id"], f, edges)
        n_edge += len(edges); n_fact += 1
        if n_fact % 20 == 0:
            log.info("  진행 %d/%d Fact · 누적 엣지 %d", n_fact, len(todo), n_edge)

    total = pg.parties_count()
    log.info("완료 — 이번 %d Fact에서 엣지 %d건 생성 · related_parties 총 %d건(지식그래프)",
             n_fact, n_edge, total)
    if n_fail:
        log.warning("일시오류로 %d건 미처리 — `graph-parties` 재실행 시 이어서 처리됩니다", n_fail)
