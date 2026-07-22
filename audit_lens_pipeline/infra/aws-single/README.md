# 감사렌즈 — 단일 EC2 Docker 배포 (간헐적 사용 최적)

전체 스택(앱 GPU + OpenSearch(nori) + PostgreSQL)을 **EC2 한 대**에 Docker로. 
**끄면 디스크만 과금(~$12/월), 켜면 데이터 그대로 복귀.** 노트북 불필요(폰/콘솔로 켜고 끔).

## 왜 이 구성인가
| | 관리형(`infra/aws`) | **단일 EC2(여기)** |
|---|---|---|
| 상시 | ~$600/월 | ~$400/월 |
| **정지 시** | ~$180/월(안 멈춤) | **~$12/월** ✅ |
| 데이터 | 관리형 보존 | EBS 볼륨 보존(stop/start 무손실) |

## 0. 사전 준비
- AWS 계정+결제 · AWS CLI(`aws configure`) · Terraform ≥1.5 · EC2 키페어 · 본인 IP

## 1. 프로비저닝
```bash
cd infra/aws-single
cat > terraform.tfvars <<EOF
allowed_cidr = "<내IP>/32"
key_name     = "<키페어이름>"
EOF
terraform init && terraform apply   # EC2 + 보안그룹 + 디스크 (~3분)
```

## 2. 코드·데이터 업로드 (로컬 → EC2)
```bash
IP=$(terraform output -raw public_ip)
# 코드(데이터 제외, .dockerignore가 거름) — 앱 루트 전체
scp -i <key>.pem -r ../../*  ubuntu@$IP:/home/ubuntu/audit_lens/
# (선택) 재색인용 파싱 청크 + Fact 덤프
scp -i <key>.pem -r ../../data/v1/parsed  ubuntu@$IP:/home/ubuntu/audit_lens/data/v1/
docker exec audit-postgres pg_dump -U auditlens auditlens > /tmp/facts.sql   # 로컬에서
scp -i <key>.pem /tmp/facts.sql ubuntu@$IP:/home/ubuntu/audit_lens/
```

## 3. 키 설정 & 스택 기동 (EC2 안에서)
```bash
ssh -i <key>.pem ubuntu@$IP
cd audit_lens
cat > .env <<EOF
PG_PASSWORD=<원하는_비번>
ANTHROPIC_API_KEY=sk-...
OPENDART_API_KEY=...
KRX_API_KEY=...
EOF
docker compose -f docker-compose.aws.yml up -d --build   # 최초 빌드~10분
```

## 4. 데이터 적재 (최초 1회)
```bash
# Fact Store 복원
docker compose -f docker-compose.aws.yml exec -T postgres psql -U auditlens auditlens < facts.sql
# 벡터 재색인(nori, GPU) — 파싱 청크 필요, ~수 시간
docker compose -f docker-compose.aws.yml exec app python -m src.cli embed --recreate
# (financials가 덤프에 포함 안 됐으면) 재무 적재
docker compose -f docker-compose.aws.yml exec app python -m src.cli extract --batch --year 2024  # 필요 시
```
→ 접속: `terraform output web_url` (http://<IP>:8000)

## ★ 간헐적 사용 — 켜고 끄기 (노트북 불필요)
```bash
terraform output stop_cmd   # 출력된 명령 실행 → 정지(디스크만 과금)
terraform output start_cmd  # 다시 켜기 → 볼륨 보존돼 그대로 복귀
```
- **AWS 콘솔(휴대폰)** EC2 → 인스턴스 우클릭 → 시작/중지 로도 가능
- **자동 스케줄**: EventBridge로 "매일 밤 자동 중지" 규칙(선택)
- start 후 컨테이너는 `restart: always`로 자동 기동. 퍼블릭 IP는 재시작 시 바뀔 수 있음(고정하려면 Elastic IP).

## 비용
| 상태 | 시간당 | 비고 |
|---|---|---|
| 켜짐(사용 중) | ~$0.53/hr | GPU g4dn. 하루 2시간×20일 ≈ ~$21/월 |
| **정지** | **~$0** | 디스크(150GB gp3) ~$12/월만 |
- 예: 간헐적(하루 2h×20일) ≈ **$21 + $12 = ~$33/월**. Claude API는 질의당 별도.

## 폐기 (완전 $0)
```bash
terraform destroy   # ★ delete_on_termination=false라 디스크는 남을 수 있음 → 콘솔에서 볼륨 확인·삭제
```

---
※ 검토용 스캐폴딩. `terraform plan`으로 확인 후 `apply`. 운영 노출 시 HTTPS(ALB/Caddy)·인증 추가 권장.
