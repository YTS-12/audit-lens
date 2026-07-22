# 감사렌즈 서빙 이미지 — GPU(BGE-M3 임베딩 + 재랭커) + FastAPI serve
# 대상: AWS EC2 GPU 인스턴스(g4dn/g5, NVIDIA 드라이버 + nvidia-container-toolkit).
# 실행:  docker run --gpus all -p 8000:8000 --env-file .env.aws audit-lens
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    KMP_DUPLICATE_LIB_OK=TRUE \
    HF_HOME=/models \
    PYTHONPATH=/app

# 시스템 패키지(최소)
RUN apt-get update && apt-get install -y --no-install-recommends curl openssl \
    && rm -rf /var/lib/apt/lists/*

# 파이썬 의존성 (레이어 캐시 위해 먼저)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 코드 (.dockerignore로 data/·.venv 등 제외)
COPY . .

# (선택) 모델을 이미지에 미리 구워 콜드스타트 단축 — 이미지 ~+5GB. 주석 해제 시 적용.
# RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
#     SentenceTransformer('BAAI/bge-m3'); CrossEncoder('BAAI/bge-reranker-v2-m3')"

EXPOSE 8000
# host 0.0.0.0 = 컨테이너 외부 노출(ALB/보안그룹으로 접근 통제)
CMD ["python", "-m", "src.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]
