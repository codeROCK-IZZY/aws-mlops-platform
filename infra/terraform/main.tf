# MLOps platform on AWS: control plane (ECS Fargate + RDS) and the ML side (S3, managed MLflow,
# SageMaker endpoint, scheduled pipeline runs). See docs/AWS_DEPLOYMENT.md for cost, deploy and teardown.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  # Names are deterministic, so the control plane and the ML module can refer to each other without a cycle.
  data_bucket     = "${var.name}-data-${local.account}"
  artifact_bucket = "${var.name}-artifacts-${local.account}"
  endpoint        = "${var.name}-machine-failure"
  mlflow_arn      = "arn:${local.partition}:sagemaker:${var.region}:${local.account}:mlflow-tracking-server/${var.name}-mlflow"
  sagemaker_role  = "arn:${local.partition}:iam::${local.account}:role/${var.name}-sagemaker"

  ml_environment = [
    { name = "MLP_DATA_BUCKET", value = local.data_bucket },
    { name = "MLP_ARTIFACT_BUCKET", value = local.artifact_bucket },
    { name = "MLP_S3_REGION", value = var.region },
    { name = "MLP_MLFLOW_TRACKING_URI", value = local.mlflow_arn },
    { name = "MLP_DEPLOY_TARGET", value = "sagemaker" },
    { name = "MLP_SAGEMAKER_ENDPOINT", value = local.endpoint },
    { name = "MLP_SAGEMAKER_ROLE_ARN", value = local.sagemaker_role },
    { name = "MLP_SERVING_IMAGE_URI", value = "${module.ml.serving_repository_url}:${var.serving_image_tag}" },
    { name = "MLP_APPROVAL_POLICY", value = var.approval_policy },
  ]
}

module "network" {
  source             = "./modules/network"
  name               = var.name
  cidr               = var.vpc_cidr
  single_nat_gateway = var.single_nat_gateway
}

module "data" {
  source             = "./modules/data"
  name               = var.name
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  db_instance_class  = var.db_instance_class
  db_multi_az        = var.db_multi_az
}

# What the API and the pipeline worker may do on AWS: read/write datasets and artifacts, use MLflow,
# deploy new model versions to the one endpoint, and publish the drift metric. Nothing else.
data "aws_iam_policy_document" "task_ml" {
  statement {
    sid     = "Datasets"
    actions = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      "arn:${local.partition}:s3:::${local.data_bucket}", "arn:${local.partition}:s3:::${local.data_bucket}/*",
      "arn:${local.partition}:s3:::${local.artifact_bucket}", "arn:${local.partition}:s3:::${local.artifact_bucket}/*",
    ]
  }
  statement {
    sid       = "KmsForBuckets"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [module.ml.kms_key_arn]
  }
  statement {
    sid       = "Mlflow"
    actions   = ["sagemaker-mlflow:*", "sagemaker:CreatePresignedMlflowTrackingServerUrl", "sagemaker:DescribeMlflowTrackingServer"]
    resources = [local.mlflow_arn]
  }
  statement {
    sid     = "DeployEndpoint"
    actions = ["sagemaker:CreateModel", "sagemaker:CreateEndpointConfig", "sagemaker:UpdateEndpoint", "sagemaker:DescribeEndpoint"]
    resources = [
      "arn:${local.partition}:sagemaker:${var.region}:${local.account}:model/machine-failure-*",
      "arn:${local.partition}:sagemaker:${var.region}:${local.account}:endpoint-config/${local.endpoint}-*",
      "arn:${local.partition}:sagemaker:${var.region}:${local.account}:endpoint/${local.endpoint}",
    ]
  }
  statement {
    sid       = "PassSageMakerRole"
    actions   = ["iam:PassRole"]
    resources = [local.sagemaker_role]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["sagemaker.amazonaws.com"]
    }
  }
  statement {
    sid       = "DriftMetric"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["MLOps/Monitoring"]
    }
  }
}

module "app" {
  source                  = "./modules/app"
  name                    = var.name
  region                  = var.region
  vpc_id                  = module.network.vpc_id
  public_subnet_ids       = module.network.public_subnet_ids
  private_subnet_ids      = module.network.private_subnet_ids
  db_security_group_id    = module.data.db_security_group_id
  database_url_secret_arn = module.data.database_url_secret_arn
  jwt_secret_arn          = module.data.jwt_secret_arn
  image_tag               = var.image_tag
  certificate_arn         = var.certificate_arn
  alarm_email             = var.alarm_email
  monthly_budget_usd      = var.monthly_budget_usd
  github_repository       = var.github_repository
  db_instance_id          = module.data.db_instance_id
  extra_environment       = local.ml_environment
  task_policy_json        = data.aws_iam_policy_document.task_ml.json
}

module "ml" {
  source                              = "./modules/ml"
  name                                = var.name
  serving_image_tag                   = var.serving_image_tag
  instance_type                       = var.sagemaker_instance_type
  ecs_cluster_arn                     = module.app.cluster_arn
  pipeline_task_definition_arn        = module.app.worker_task_definition_arn
  pipeline_task_definition_arn_prefix = module.app.worker_task_definition_arn_prefix
  pipeline_task_role_arns             = module.app.task_role_arns
  private_subnet_ids                  = module.network.private_subnet_ids
  task_security_group_id              = module.app.task_security_group_id
}
