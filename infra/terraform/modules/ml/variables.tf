variable "name" {
  type = string
}

variable "serving_image_tag" {
  description = "Serving image for the endpoint's first deployment; later versions are deployed by the pipeline."
  type        = string
}

variable "instance_type" {
  type    = string
  default = "ml.m5.large"
}

variable "min_instances" {
  description = "At least 2 so a canary deployment always has capacity on both fleets."
  type        = number
  default     = 2
}

variable "max_instances" {
  type    = number
  default = 4
}

variable "invocations_per_instance" {
  description = "Target invocations per instance per minute for autoscaling."
  type        = number
  default     = 600
}

variable "latency_budget_ms" {
  description = "p99 model latency that triggers the rollback alarm. Above the promotion gate's p95 budget (100 ms), so it fires on regressions in production, not on models the gates accepted."
  type        = number
  default     = 200
}

variable "capture_retention_days" {
  type    = number
  default = 90
}

variable "alarm_topic_arn" {
  type    = string
  default = ""
}

variable "ecs_cluster_arn" {
  type = string
}

variable "pipeline_task_definition_arn" {
  description = "Worker task definition used for scheduled pipeline runs."
  type        = string
}

variable "pipeline_task_definition_arn_prefix" {
  description = "Task definition ARN without the revision, for the scheduler's RunTask permission."
  type        = string
}

variable "pipeline_task_role_arns" {
  description = "Execution and task role ARNs of the worker task (the scheduler passes them to ECS)."
  type        = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "task_security_group_id" {
  type = string
}
