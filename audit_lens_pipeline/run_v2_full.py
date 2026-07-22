# -*- coding: utf-8 -*-
"""v2 전량 파이프라인(분리 실행용): 전량 재파싱 → audit_chunks_v2 전량 임베딩.
실행: dart-rag python으로 detached 실행, 로그 = data/v1/v2_full.log"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = sys.stdout
print("[v2_full] 프로세스 기동", flush=True)          # 생존 신호(임포트 전)

from src.pipeline import parse_v2  # noqa: E402  (경량 — lxml만)

t0 = time.time()
print("=== 전량 파싱 v2 시작 ===", flush=True)
ok, total, fails = parse_v2.run()
print(f"=== 파싱 완료 ok={ok} 청크={total} 실패={len(fails)} ({(time.time()-t0)/60:.0f}분) ===", flush=True)
print("=== 전량 임베딩 시작(audit_chunks_v2) ===", flush=True)
from src.pipeline import embed  # noqa: E402  (torch — 파싱 완료 후 지연 임포트)
embed.run(src_subdir="parsed_v2", index="audit_chunks_v2", recreate=True)
print(f"=== 전량 임베딩 완료 (총 {(time.time()-t0)/3600:.1f}시간) ===", flush=True)
