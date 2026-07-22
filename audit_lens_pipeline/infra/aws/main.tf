# 감사렌즈 AWS 인프라 — 검토용 스캐폴딩(★apply 전 반드시 검토·수정).
# 구성: OpenSearch Service(벡터) + Aurora PostgreSQL(Fact Store) + GPU EC2(BGE-M3 serve) + Secrets.
# 로컬 Docker PoC와 동일 코드 — 엔드포인트/비밀만 env로 주입.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}
provider "aws" { region = var.region }

data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
# Deep Learning GPU AMI(NVIDIA 드라이버+Docker 포함) — 리전별 최신 조회
data "aws_ami" "gpu" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Deep Learning*Base*GPU*Ubuntu 22.04*"]
  }
}

# ── 비밀: DB 비밀번호 + 외부 API 키(값은 apply 후 콘솔/CLI로 채움) ──
resource "random_password" "db" {
  length  = 24
  special = false
}
resource "aws_secretsmanager_secret" "app" {
  name = "${var.project}-secrets"
}
resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  # ★ ANTHROPIC/OPENDART/KRX 키는 apply 후 실제 값으로 교체(placeholder).
  secret_string = jsonencode({
    PG_PASSWORD       = random_password.db.result
    ANTHROPIC_API_KEY = "REPLACE_ME"
    OPENDART_API_KEY  = "REPLACE_ME"
    KRX_API_KEY       = "REPLACE_ME"
  })
}

# ── 보안그룹 ──
resource "aws_security_group" "app" {
  name   = "${var.project}-app"
  vpc_id = data.aws_vpc.default.id
  ingress { # 웹(허용 CIDR만). 운영은 ALB+ACM 권장.
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
  ingress { # SSH(허용 CIDR만)
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_security_group" "data" { # OpenSearch/Aurora — 앱에서만 접근
  name   = "${var.project}-data"
  vpc_id = data.aws_vpc.default.id
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── OpenSearch Service(벡터스토어) ──
resource "aws_opensearch_domain" "vec" {
  domain_name    = "${var.project}-os"
  engine_version = "OpenSearch_2.13" # 로컬 2.18과 호환. nori 지원.
  cluster_config { instance_type = var.os_instance_type; instance_count = 1 }
  ebs_options {
    ebs_enabled = true
    volume_size = var.os_volume_gb
    volume_type = "gp3"
  }
  node_to_node_encryption { enabled = true }
  encrypt_at_rest { enabled = true }
  domain_endpoint_options { enforce_https = true }
  # ★운영: VPC 배치 + fine-grained access(마스터 유저) 권장. 스캐폴딩은 단순화.
}

# ── Aurora PostgreSQL Serverless v2(Fact Store + financials) ──
resource "aws_db_subnet_group" "aurora" {
  name       = "${var.project}-db-subnets"
  subnet_ids = data.aws_subnets.default.ids
}
resource "aws_rds_cluster" "pg" {
  cluster_identifier   = "${var.project}-pg"
  engine               = "aurora-postgresql"
  engine_mode          = "provisioned"
  engine_version       = "16.4"
  database_name        = var.db_name
  master_username      = var.db_user
  master_password      = random_password.db.result
  db_subnet_group_name = aws_db_subnet_group.aurora.name
  vpc_security_group_ids = [aws_security_group.data.id]
  skip_final_snapshot  = true
  serverlessv2_scaling_configuration {
    min_capacity = var.aurora_min_acu
    max_capacity = var.aurora_max_acu
  }
}
resource "aws_rds_cluster_instance" "pg" {
  cluster_identifier = aws_rds_cluster.pg.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.pg.engine
  engine_version     = aws_rds_cluster.pg.engine_version
}

# ── ECR(이미지 저장소) ──
resource "aws_ecr_repository" "app" { name = var.project }

# ── EC2 GPU 인스턴스용 IAM(ECR 풀 + Secrets 읽기) ──
resource "aws_iam_role" "ec2" {
  name = "${var.project}-ec2"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ecr" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}
resource "aws_iam_role_policy" "secrets" {
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.app.arn }]
  })
}
resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project}-ec2"
  role = aws_iam_role.ec2.name
}

# ── GPU EC2(BGE-M3 + 재랭커 + FastAPI serve) ──
resource "aws_instance" "gpu" {
  ami                    = data.aws_ami.gpu.id
  instance_type          = var.gpu_instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  key_name               = var.key_name
  root_block_device { volume_size = 100 } # 모델 캐시+로그
  # user_data: ECR 로그인 → 이미지 풀 → Secrets를 env로 주입해 컨테이너 실행
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region      = var.region
    ecr_url     = aws_ecr_repository.app.repository_url
    secret_arn  = aws_secretsmanager_secret.app.arn
    os_endpoint = aws_opensearch_domain.vec.endpoint
    pg_host     = aws_rds_cluster.pg.endpoint
    db_name     = var.db_name
    db_user     = var.db_user
  })
  tags = { Name = "${var.project}-gpu" }
}
