# The ML half of the platform: data and artifact buckets, the serving image registry, a managed
# MLflow tracking server, the SageMaker real-time endpoint (canary deployments with automatic
# rollback, data capture, autoscaling) and the scheduled pipeline runs.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.name
  buckets = {
    data      = "${var.name}-data-${local.account}"
    artifacts = "${var.name}-artifacts-${local.account}"
  }
  endpoint = "${var.name}-machine-failure"
}

# ---------------------------------------------------------------- S3: datasets, captured traffic, MLflow artifacts
resource "aws_kms_key" "ml" {
  #checkov:skip=CKV2_AWS_64:The default key policy is intended here: it delegates to IAM, and the roles below grant use of this key explicitly
  description         = "Encrypts datasets, captured predictions, model artifacts and the serving image"
  enable_key_rotation = true
}

resource "aws_s3_bucket" "this" {
  #checkov:skip=CKV_AWS_144:Cross-region replication is a disaster-recovery decision per deployment; datasets can be regenerated from source
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events; pipeline runs are started by the API and the schedule
  #checkov:skip=CKV_AWS_18:Access is audited with CloudTrail data events rather than S3 server access logs
  for_each = local.buckets
  bucket   = each.value
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled" # a dataset version can never be silently overwritten
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.ml.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.this["data"].id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  rule {
    id     = "expire-captured-traffic"
    status = "Enabled"
    filter {
      prefix = "capture/"
    }
    expiration {
      days = var.capture_retention_days
    }
  }
  rule {
    id     = "tier-old-datasets"
    status = "Enabled"
    filter {
      prefix = "validated/"
    }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.this]
}

# ---------------------------------------------------------------- serving image
resource "aws_ecr_repository" "serving" {
  name                 = "${var.name}-serving"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ml.arn
  }
}

# ---------------------------------------------------------------- IAM: one role for SageMaker (endpoint + MLflow)
resource "aws_iam_role" "sagemaker" {
  name = "${var.name}-sagemaker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })
}

resource "aws_iam_role_policy" "sagemaker" {
  #checkov:skip=CKV_AWS_355:ecr:GetAuthorizationToken and cloudwatch:PutMetricData do not support resource-level permissions
  #checkov:skip=CKV_AWS_290:Same two actions; PutMetricData is constrained to the SageMaker namespace by condition
  name = "ml-data-and-artifacts"
  role = aws_iam_role.sagemaker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Buckets"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
        Resource = flatten([for b in aws_s3_bucket.this : [b.arn, "${b.arn}/*"]])
      },
      {
        Sid      = "Kms"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.ml.arn
      },
      {
        Sid      = "PullServingImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.serving.arn
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:${local.partition}:logs:${local.region}:${local.account}:log-group:/aws/sagemaker/*"
      },
      {
        Sid       = "Metrics"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringLike = { "cloudwatch:namespace" = "/aws/sagemaker/*" } }
      },
      {
        Sid      = "ReadRegistry"
        Effect   = "Allow"
        Action   = ["sagemaker-mlflow:*"]
        Resource = aws_sagemaker_mlflow_tracking_server.this.arn
      },
    ]
  })
}

# ---------------------------------------------------------------- MLflow (SageMaker managed)
resource "aws_sagemaker_mlflow_tracking_server" "this" {
  tracking_server_name            = "${var.name}-mlflow"
  role_arn                        = aws_iam_role.sagemaker.arn
  artifact_store_uri              = "s3://${aws_s3_bucket.this["artifacts"].bucket}/mlflow"
  tracking_server_size            = "Small"
  automatic_model_registration    = false
  weekly_maintenance_window_start = "Sun:03:00"
}

# ---------------------------------------------------------------- SageMaker endpoint
resource "aws_sagemaker_model" "initial" {
  name               = "${local.endpoint}-initial"
  execution_role_arn = aws_iam_role.sagemaker.arn
  primary_container {
    image = "${aws_ecr_repository.serving.repository_url}:${var.serving_image_tag}"
    environment = {
      MLP_MLFLOW_TRACKING_URI = aws_sagemaker_mlflow_tracking_server.this.arn
      MLP_MODEL_NAME          = "machine-failure"
      MLP_CAPTURE_DB          = "false" # SageMaker data capture writes to S3 instead
    }
  }
}

resource "aws_sagemaker_endpoint_configuration" "initial" {
  name        = "${local.endpoint}-initial"
  kms_key_arn = aws_kms_key.ml.arn
  production_variants {
    variant_name           = "primary"
    model_name             = aws_sagemaker_model.initial.name
    instance_type          = var.instance_type
    initial_instance_count = var.min_instances
  }
  data_capture_config {
    enable_capture              = true
    initial_sampling_percentage = 20
    destination_s3_uri          = "s3://${aws_s3_bucket.this["data"].bucket}/capture"
    kms_key_id                  = aws_kms_key.ml.arn
    capture_options {
      capture_mode = "Input"
    }
    capture_options {
      capture_mode = "Output"
    }
  }
}

resource "aws_sagemaker_endpoint" "this" {
  name                 = local.endpoint
  endpoint_config_name = aws_sagemaker_endpoint_configuration.initial.name
  deployment_config {
    blue_green_update_policy {
      termination_wait_in_seconds = 300
      traffic_routing_configuration {
        type                     = "CANARY"
        wait_interval_in_seconds = 600
        canary_size {
          type  = "CAPACITY_PERCENT"
          value = 10
        }
      }
    }
    auto_rollback_configuration {
      alarms {
        alarm_name = aws_cloudwatch_metric_alarm.endpoint_5xx.alarm_name
      }
      alarms {
        alarm_name = aws_cloudwatch_metric_alarm.endpoint_latency.alarm_name
      }
    }
  }
  lifecycle {
    # After the first deployment, the pipeline owns the endpoint config (one per model version).
    ignore_changes = [endpoint_config_name]
  }
}

# ---------------------------------------------------------------- alarms (also the canary's rollback triggers)
resource "aws_cloudwatch_metric_alarm" "endpoint_5xx" {
  alarm_name          = "${local.endpoint}-5xx"
  alarm_description   = "Model server errors: rolls a canary deployment back automatically"
  namespace           = "AWS/SageMaker"
  metric_name         = "Invocation5XXErrors"
  dimensions          = { EndpointName = local.endpoint, VariantName = "primary" }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "endpoint_latency" {
  alarm_name          = "${local.endpoint}-latency"
  alarm_description   = "p99 model latency above budget: rolls a canary deployment back automatically"
  namespace           = "AWS/SageMaker"
  metric_name         = "ModelLatency" # microseconds
  dimensions          = { EndpointName = local.endpoint, VariantName = "primary" }
  extended_statistic  = "p99"
  period              = 60
  evaluation_periods  = 3
  threshold           = var.latency_budget_ms * 1000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "drift" {
  alarm_name          = "${var.name}-model-drift"
  alarm_description   = "The monitoring run published a drift alert (custom metric from the pipeline)"
  namespace           = "MLOps/Monitoring"
  metric_name         = "DriftAlert"
  dimensions          = { Model = "machine-failure" }
  statistic           = "Maximum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
}

# ---------------------------------------------------------------- autoscaling on invocations per instance
resource "aws_appautoscaling_target" "endpoint" {
  service_namespace  = "sagemaker"
  resource_id        = "endpoint/${aws_sagemaker_endpoint.this.name}/variant/primary"
  scalable_dimension = "sagemaker:variant:DesiredInstanceCount"
  min_capacity       = var.min_instances
  max_capacity       = var.max_instances
}

resource "aws_appautoscaling_policy" "endpoint" {
  name               = "${local.endpoint}-invocations"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.endpoint.service_namespace
  resource_id        = aws_appautoscaling_target.endpoint.resource_id
  scalable_dimension = aws_appautoscaling_target.endpoint.scalable_dimension
  target_tracking_scaling_policy_configuration {
    target_value       = var.invocations_per_instance
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "SageMakerVariantInvocationsPerInstance"
    }
  }
}

# ---------------------------------------------------------------- scheduled pipeline runs (EventBridge Scheduler → ECS task)
resource "aws_iam_role" "scheduler" {
  name = "${var.name}-pipeline-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "run-pipeline-task"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecs:RunTask", Resource = "${var.pipeline_task_definition_arn_prefix}:*" },
      { Effect = "Allow", Action = "iam:PassRole", Resource = var.pipeline_task_role_arns },
    ]
  })
}

resource "aws_scheduler_schedule" "monitor" {
  #checkov:skip=CKV_AWS_297:The schedule's input is a fixed command with no data; an AWS-owned key is sufficient
  name                = "${var.name}-daily-monitoring"
  schedule_expression = "cron(30 2 * * ? *)"
  flexible_time_window {
    mode                      = "FLEXIBLE"
    maximum_window_in_minutes = 30
  }
  target {
    arn      = var.ecs_cluster_arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = var.pipeline_task_definition_arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [var.task_security_group_id]
        assign_public_ip = false
      }
    }
    input = jsonencode({ containerOverrides = [{ name = "worker", command = ["python", "-m", "app.cli", "monitor-latest"] }] })
  }
}
