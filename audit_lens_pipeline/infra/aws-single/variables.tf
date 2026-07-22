# 단일 EC2 Docker 배포 변수 (간헐적 사용 최적). terraform.tfvars로 오버라이드.
variable "region" {
  type    = string
  default = "ap-northeast-2" # 서울
}
variable "project" {
  type    = string
  default = "audit-lens"
}
variable "allowed_cidr" {
  description = "웹(8000)/SSH(22) 허용 CIDR (본인 IP/32). ★0.0.0.0/0 금지."
  type        = string
}
variable "key_name" {
  description = "EC2 SSH 키페어 이름(사전 생성)"
  type        = string
}
variable "instance_type" {
  description = "전체 스택(앱 GPU + OpenSearch + PG)을 올릴 GPU 인스턴스"
  type        = string
  default     = "g4dn.xlarge" # T4 16GB, 16GB RAM, 4 vCPU. ~$0.53/hr(켤 때만)
}
variable "disk_gb" {
  description = "루트 디스크(EBS gp3): OpenSearch 색인+PG+모델+데이터. 정지 중 유일 과금(~$0.08/GB·월)"
  type        = number
  default     = 150 # ~$12/월(정지 시에도)
}
