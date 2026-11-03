output "data_bucket" {
  value = aws_s3_bucket.this["data"].bucket
}

output "artifact_bucket" {
  value = aws_s3_bucket.this["artifacts"].bucket
}

output "serving_repository_url" {
  value = aws_ecr_repository.serving.repository_url
}

output "mlflow_tracking_server_arn" {
  description = "Use as MLP_MLFLOW_TRACKING_URI (with the sagemaker-mlflow plugin)."
  value       = aws_sagemaker_mlflow_tracking_server.this.arn
}

output "endpoint_name" {
  value = aws_sagemaker_endpoint.this.name
}

output "sagemaker_role_arn" {
  value = aws_iam_role.sagemaker.arn
}

output "kms_key_arn" {
  value = aws_kms_key.ml.arn
}
