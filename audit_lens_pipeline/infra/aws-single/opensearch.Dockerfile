# OpenSearch + 한국어 형태소(nori) 플러그인 — 이미지에 미리 구움(로컬처럼 수동설치 불필요).
FROM opensearchproject/opensearch:2.18.0
RUN /usr/share/opensearch/bin/opensearch-plugin install --batch analysis-nori
