output "web_url" {
  description = "웹 UI/API (허용 CIDR에서 접근)"
  value       = "http://${aws_instance.gpu.public_ip}:8000"
}
output "opensearch_endpoint" {
  value = aws_opensearch_domain.vec.endpoint
}
output "aurora_endpoint" {
  value = aws_rds_cluster.pg.endpoint
}
output "ecr_repository_url" {
  description = "이미지 push 대상"
  value       = aws_ecr_repository.app.repository_url
}
output "secret_name" {
  description = "API 키 실제 값으로 교체할 Secrets Manager 이름"
  value       = aws_secretsmanager_secret.app.name
}
output "gpu_instance_id" {
  value = aws_instance.gpu.id
}
