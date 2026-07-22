output "instance_id" {
  description = "start/stop에 사용"
  value       = aws_instance.stack.id
}
output "public_ip" {
  value = aws_instance.stack.public_ip
}
output "web_url" {
  value = "http://${aws_instance.stack.public_ip}:8000"
}
output "ssh" {
  value = "ssh -i <key>.pem ubuntu@${aws_instance.stack.public_ip}"
}
output "start_cmd" {
  value = "aws ec2 start-instances --region ${var.region} --instance-ids ${aws_instance.stack.id}"
}
output "stop_cmd" {
  value = "aws ec2 stop-instances --region ${var.region} --instance-ids ${aws_instance.stack.id}"
}
