# 감사렌즈 AWS 배포 가이드 (배포 기반 / IaC)

로컬 Docker PoC와 **동일한 코드**를 AWS 관리형 서비스로 올린다. 엔드포인트·비밀만 env로 주입한다.

## 구성 (사용자 선택: 상시 GPU)
```
[사용자] → GPU EC2 (BGE-M3 임베딩 + 재랭커 + FastAPI serve, 컨테이너)
                       ├── Amazon OpenSearch Service   (벡터 색인, nori)
                       ├── Amazon Aurora PostgreSQL     (Fact Store + financials)
                       └── Secrets Manager              (ANTHROPIC/OPENDART/KRX + DB PW)
```

## 0. 사전 준비 (사용자)
- AWS 계정 + **결제 활성화** (월 ~$300~750 예상, 아래 비용 참조)
- **AWS CLI** 설치 + `aws configure` (IAM 자격증명, 권한: OpenSearch/RDS/EC2/ECR/Secrets/IAM)
- **Terraform** ≥ 1.5 설치
- **EC2 키페어** 생성 → 이름을 `key_name`에 지정
- 본인 공인 IP 확인 → `allowed_cidr = "<IP>/32"` (★`0.0.0.0/0` 금지)

## 1. 변수 지정
`infra/aws/terraform.tfvars` 생성:
```hcl
region       = "ap-northeast-2"
allowed_cidr = "203.0.113.5/32"   # 본인 IP
key_name     = "my-keypair"
# gpu_instance_type = "g5.xlarge"  # 여유형(선택)
```

## 2. 프로비저닝
```bash
cd infra/aws
terraform init
terraform plan      # ★생성 리소스·비용 검토
terraform apply     # OpenSearch·Aurora·GPU EC2·ECR·Secrets 생성 (~15~25분)
```

## 3. API 키를 Secrets에 주입
```bash
aws secretsmanager put-secret-value --secret-id $(terraform output -raw secret_name) \
  --secret-string '{"PG_PASSWORD":"<terraform이 생성한 값 유지>","ANTHROPIC_API_KEY":"sk-...","OPENDART_API_KEY":"...","KRX_API_KEY":"..."}'
```
> PG_PASSWORD는 Terraform이 생성한 값과 동일해야 함(RDS와 일치).

## 4. 이미지 빌드 & ECR push
```bash
ECR=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin $ECR
docker build -t $ECR:latest ../..     # 루트의 Dockerfile
docker push $ECR:latest
```

## 5. 데이터 마이그레이션 (로컬 → AWS)
로컬 데이터는 이미지에 없다(용량). 두 저장소를 채운다:
- **PostgreSQL(Fact Store+financials)**: 로컬에서 덤프 → Aurora로 복원
  ```bash
  # 로컬(Docker PG)에서 덤프
  docker exec audit-postgres pg_dump -U auditlens auditlens > facts.sql
  # Aurora로 복원 (엔드포인트/PW는 terraform output/Secrets)
  psql "host=$(terraform output -raw aurora_endpoint) dbname=auditlens user=auditlens" < facts.sql
  ```
- **OpenSearch(벡터 816k)**: 관리형 도메인에 **재색인**. 두 방법:
  1. (권장) 파싱 청크를 GPU EC2로 복사 후 `python -m src.cli embed --recreate` (nori 매핑, ~수 시간)
  2. 로컬 인덱스 스냅샷을 S3 경유 복원(embedding이 _source에 없어 재임베드가 더 단순)

## 6. 컨테이너 실행 & 검증
- GPU EC2는 부팅 시 `user_data.sh`로 자동 실행(ECR 풀 + Secrets 주입). 재실행: `sudo docker restart audit-lens`
- 확인: `http://$(terraform output -raw gpu_instance_id의 퍼블릭IP):8000` (=`terraform output web_url`)

## 비용 (대략, 상시 가동)
| 구성 | 시간당 | 월(×730) |
|---|---|---|
| GPU EC2 (g4dn.xlarge T4) | ~$0.53 | ~$390 |
| OpenSearch (r6g.large 1노드) | ~$0.17 | ~$125 |
| Aurora Serverless v2 (0.5~ACU) | ~$0.06+ | ~$45+ |
| 네트워크/기타 | ~$0.05 | ~$40 |
| **합계** | **~$0.8/hr** | **~$600/월** |
- Claude API는 질의당 종량(별도). 절감: Savings Plans, 야간 GPU 중지, g5→g4dn.

## 운영·보안 (★배포 전 강화)
- OpenSearch/Aurora를 **VPC 프라이빗 서브넷**으로, fine-grained access(마스터 유저) 설정
- 웹은 **ALB + ACM(HTTPS)** 뒤로, `allowed_cidr` 대신 인증 도입
- CloudWatch 로그/알람, 백업(Aurora 스냅샷) 설정

## 폐기 (과금 중지)
```bash
terraform destroy
```

---
※ 이 디렉터리는 **검토용 스캐폴딩**이다. `terraform plan`으로 반드시 리소스·비용을 확인하고, 보안 항목을 강화한 뒤 `apply`할 것.
