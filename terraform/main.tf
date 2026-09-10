# ECR repository for container images
resource "aws_ecr_repository" "traider" {
  name = var.ecr_repo_name
  image_tag_mutability = "MUTABLE"
  tags = {
    project = "traider"
  }
}

# DynamoDB table for state storage (on-demand / pay-per-request)
resource "aws_dynamodb_table" "state" {
  name         = var.dynamodb_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "PK"

  attribute {
    name = "PK"
    type = "S"
  }

  # TTL attribute (optional). Enable if you want automatic expiration of stale items.
  ttl {
    attribute_name = var.dynamodb_ttl_attribute
    enabled        = true
  }

  tags = {
    project = "traider"
  }
}

# Lightsail container service
resource "aws_lightsail_container_service" "traider" {
  name  = var.lightsail_service_name
  power = "small"   # options: nano, micro, small, medium, large, xlarge
  scale = 1          # number of nodes for the service

  tags = {
    project = "traider"
  }
}

# Initial Lightsail deployment using the image from ECR. Replace image URL if you prefer a public image for testing.
resource "aws_lightsail_container_service_deployment" "traider_dep" {
  service_name = aws_lightsail_container_service.traider.name

  # Container definition block: container name -> image and ports
  container {
    name  = var.container_name
    image = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.ecr_repo_name}:${var.image_tag}"

    # Example environment variables injected into the container. Use Secrets Manager for sensitive values in production.
    environment {
      AWS_REGION = var.aws_region
    }

    port {
      container_port = var.container_port
      protocol       = "tcp"
    }
  }

  public_endpoint {
    container_name = var.container_name
    container_port = var.container_port
  }

  depends_on = [aws_ecr_repository.traider]
}

# Helpful data source for account id
data "aws_caller_identity" "account" {}

# Note: Lightsail container service needs the image to be accessible. Pushing to ECR and configuring
# permissions for Lightsail to pull the image may be required. For quick testing, replace the image in
# the container block with a public image (e.g., dockerhub) before running apply.
