variable "aws_region" {
  description = "AWS region to create resources in"
  type        = string
  default     = "eu-central-1"
}

variable "ecr_repo_name" {
  description = "ECR repository name for Traider images"
  type        = string
  default     = "traider-repo"
}

variable "dynamodb_table_name" {
  description = "DynamoDB table name to store state"
  type        = string
  default     = "traider-state"
}

variable "dynamodb_ttl_attribute" {
  description = "TTL attribute name for DynamoDB (optional)"
  type        = string
  default     = "ttl"
}

variable "lightsail_service_name" {
  description = "Lightsail container service name"
  type        = string
  default     = "traider-service"
}

variable "container_name" {
  description = "Container name used inside the Lightsail deployment"
  type        = string
  default     = "traider"
}

variable "container_port" {
  description = "Port the container listens on"
  type        = number
  default     = 8080
}

variable "image_tag" {
  description = "Image tag to deploy (push image to ECR first)"
  type        = string
  default     = "latest"
}
