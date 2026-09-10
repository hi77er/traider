output "ecr_repository_url" {
  description = "ECR repository URL"
  value       = aws_ecr_repository.traider.repository_url
}

output "dynamodb_table_name" {
  description = "DynamoDB table name used for state"
  value       = aws_dynamodb_table.state.name
}

output "lightsail_service_name" {
  description = "Lightsail container service name"
  value       = aws_lightsail_container_service.traider.name
}

output "lightsail_service_url" {
  description = "Public endpoint URL for the Lightsail container service (if available)"
  value       = aws_lightsail_container_service.traider.url
  # Note: The `url` attribute may be empty until a deployment is active. Check the Lightsail console if empty.
}
