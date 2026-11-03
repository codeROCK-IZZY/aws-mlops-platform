variable "name" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
variable "db_security_group_id" { type = string }
variable "db_instance_id" { type = string }
variable "database_url_secret_arn" { type = string }
variable "jwt_secret_arn" { type = string }
variable "image_tag" { type = string }
variable "certificate_arn" { type = string }
variable "alarm_email" { type = string }
variable "monthly_budget_usd" { type = number }
variable "github_repository" { type = string }

variable "extra_environment" {
  description = "Additional environment variables for the API and worker (ML platform settings)."
  type        = list(object({ name = string, value = string }))
  default     = []
}

variable "task_policy_json" {
  description = "IAM policy for the application task role (S3 datasets, MLflow, SageMaker deployment)."
  type        = string
}
