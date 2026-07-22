# 단일 EC2 Docker — 전체 스택을 한 인스턴스에. 간헐적 사용 최적(stop 시 디스크만 과금).
terraform {
  required_version = ">= 1.5"
  required_providers { aws = { source = "hashicorp/aws", version = "~> 5.0" } }
}
provider "aws" { region = var.region }

data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  # g4dn 미지원 AZ(2d) 배제 — 2a/2b/2c만
  filter {
    name   = "availability-zone"
    values = ["${var.region}a", "${var.region}b", "${var.region}c"]
  }
}
# Deep Learning Base GPU AMI(NVIDIA 드라이버 + Docker + nvidia-container-toolkit 포함)
data "aws_ami" "gpu" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["Deep Learning Base*GPU*Ubuntu 22.04*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"] # g4dn/g5은 x86_64 (ARM64 AMI 배제)
  }
}

resource "aws_security_group" "app" {
  name        = "${var.project}-single"
  description = "audit-lens web 8000 + ssh 22 (allowed CIDR only)"
  vpc_id      = data.aws_vpc.default.id
  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
  ingress {
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

resource "aws_instance" "stack" {
  ami                    = data.aws_ami.gpu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.app.id]
  key_name               = var.key_name
  root_block_device {
    volume_size           = var.disk_gb
    volume_type           = "gp3"
    delete_on_termination = false # terminate해도 데이터 디스크 보존(원치 않으면 true)
  }
  # 부팅 시 docker compose v2 플러그인 보장(DL AMI엔 docker 있음). 코드는 SCP 후 compose up.
  user_data = <<-EOF
    #!/bin/bash
    set -e
    if ! docker compose version >/dev/null 2>&1; then
      mkdir -p /usr/lib/docker/cli-plugins
      curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
        -o /usr/lib/docker/cli-plugins/docker-compose
      chmod +x /usr/lib/docker/cli-plugins/docker-compose
    fi
    mkdir -p /home/ubuntu/audit_lens && chown ubuntu:ubuntu /home/ubuntu/audit_lens
  EOF
  tags = { Name = "${var.project}-single" }
}
