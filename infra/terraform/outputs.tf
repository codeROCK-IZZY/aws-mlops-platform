output "app_url" {
  value = module.app.app_url
}

output "ecr_api_repository" {
  value = module.app.ecr_api_url
}

output "ecr_web_repository" {
  value = module.app.ecr_web_url
}

output "ecs_cluster" {
  value = module.app.cluster_name
}

output "migrate_command" {
  description = "Run database migrations as a one-off ECS task."
  value       = module.app.migrate_command
}

output "deploy_role_arn" {
  value = module.app.deploy_role_arn
}

output "data_bucket" {
  value = module.ml.data_bucket
}

output "serving_repository" {
  value = module.ml.serving_repository_url
}

output "mlflow_tracking_server_arn" {
  value = module.ml.mlflow_tracking_server_arn
}

output "sagemaker_endpoint" {
  value = module.ml.endpoint_name
}
