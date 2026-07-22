# 감사렌즈 AWS 배포 변수 — 적용 전 검토/수정. terraform.tfvars로 오버라이드.
variable "region" {
  description = "AWS 리전"
  type        = string
  default     = "ap-northeast-2" # 서울
}

variable "project" {
  description = "리소스 접두어"
  type        = string
  default     = "audit-lens"
}

variable "allowed_cidr" {
  description = "웹/관리 접근 허용 CIDR (본인 IP/32 권장). 0.0.0.0/0 금지."
  type        = string
  # 예: "203.0.113.5/32" — 반드시 지정
}

variable "key_name" {
  description = "EC2 SSH 키페어 이름(사전 생성)"
  type        = string
}

# ── 임베딩 GPU 인스턴스(사용자 선택: 상시 GPU) ──
variable "gpu_instance_type" {
  description = "BGE-M3+재랭커 자체호스팅 GPU 인스턴스"
  type        = string
  default     = "g4dn.xlarge" # T4 16GB, ~$0.53/hr. 여유형은 g5.xlarge
}

# ── OpenSearch Service(벡터스토어) ──
variable "os_instance_type" {
  type    = string
  default = "r6g.large.search" # kNN용 메모리. ~$0.17/hr
}
variable "os_volume_gb" {
  type    = number
  default = 30 # 색인 ~10GB + 여유
}

# ── Aurora PostgreSQL(Fact Store+financials) ──
variable "aurora_min_acu" {
  type    = number
  default = 0.5 # Serverless v2 최소(~$0.06/hr)
}
variable "aurora_max_acu" {
  type    = number
  default = 4
}
variable "db_name" {
  type    = string
  default = "auditlens"
}
variable "db_user" {
  type    = string
  default = "auditlens"
}
