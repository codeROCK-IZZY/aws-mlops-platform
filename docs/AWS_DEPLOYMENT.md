# AWS deployment

> The Terraform in [infra/terraform](../infra/terraform) is complete and passes `terraform validate`, but
> it **has not been applied**: nothing described here is running. Applying it creates billable resources
> (about $790 a month at the defaults; see below).

## What gets created

| Module | Resources |
|---|---|
| `network` | VPC across 2 AZs, public and private subnets, NAT gateway (one by default), S3 gateway endpoint |
| `data` | RDS PostgreSQL 17 (encrypted, private, deletion protection), Secrets Manager secrets for the database URL and the JWT key |
| `app` | ECS cluster; Fargate services for the API (autoscaled on CPU), the pipeline worker (2 vCPU / 8 GB) and the dashboard; ALB (HTTPS when a certificate is given); ECR repositories; CloudWatch logs, alarms and a budget; an optional GitHub OIDC deploy role |
| `ml` | KMS key (rotating); versioned, TLS-only, KMS-encrypted S3 buckets for data and artifacts; ECR repository for the serving image; SageMaker managed MLflow tracking server; SageMaker model, endpoint config (data capture → S3) and endpoint with a **canary deployment policy and auto-rollback alarms**; endpoint autoscaling on invocations per instance; the `model-drift` alarm; an EventBridge Scheduler schedule that runs `monitor-latest` daily as an ECS task |

The worker's task role can:
- read and write the two buckets
- use the KMS key
- call the MLflow tracking server
- create SageMaker models and endpoint configs, and update this one endpoint
- pass the SageMaker execution role
- publish metrics to `MLOps/Monitoring` only

The SageMaker execution role can:
- read the buckets
- pull the serving image
- write its own logs and metrics

## Deploy

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars    # region, alarm e-mail, budget, optional certificate
cp backend.hcl.example backend.hcl              # S3 state bucket + lock table
terraform init -backend-config=backend.hcl
terraform plan -out tfplan
```
```bash
terraform apply tfplan
```

Then build and push the images, and run the migration task. The API, worker and serving images are the
same backend image; the serving repository holds the tag SageMaker pulls.

```bash
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker build -t <ecr_api_repository>:v1 backend && docker push <ecr_api_repository>:v1
docker tag <ecr_api_repository>:v1 <serving_repository>:v1 && docker push <serving_repository>:v1
docker build -t <ecr_web_repository>:v1 frontend && docker push <ecr_web_repository>:v1
terraform apply -var image_tag=v1 -var serving_image_tag=v1
$(terraform output -raw migrate_command)
```

The first model then needs data and a training run (through the dashboard or the API). The first model
is promoted by the `first-model` policy and deployed to the endpoint as a canary over the bootstrap
config.

## Cost estimate (us-east-1, defaults, always on)

| Item | Approx. per month |
|---|---|
| SageMaker managed MLflow tracking server (Small) | ~$440 |
| SageMaker endpoint, 2 × ml.m5.large (minimum 2, so a canary always has capacity) | ~$170 |
| ECS Fargate: API 0.5 vCPU, worker 2 vCPU / 8 GB, dashboard 0.25 vCPU | ~$110 |
| NAT gateway (one) | ~$35 |
| Application Load Balancer | ~$20 |
| RDS db.t4g.micro + 20 GB | ~$15 |
| S3, KMS, CloudWatch, ECR | ~$10 |
| **Total** | **~$800** |

These are estimates from public on-demand prices. Check the AWS pricing pages for your region before
applying; MLflow tracking-server prices in particular vary by region. The `monthly_budget_usd` variable
(default 800) creates an AWS Budget that e-mails when the forecast passes 80% and when actual spend passes 100%.

**Cheaper set-ups:**
- **A self-hosted MLflow server on the existing ECS cluster** (0.5 vCPU) instead of the managed one saves
  about $400 a month. The code only needs a different `MLP_MLFLOW_TRACKING_URI`.
- **One endpoint instance** in non-production (`min_instances = 1`). Canary deployments then briefly run
  on a single instance per fleet.
- **SageMaker Serverless Inference** for low, spiky traffic. It does not support the canary policy, so
  rollback would rely on the dashboard.

## Teardown

```bash
terraform destroy
```

RDS has deletion protection and a final snapshot, and the buckets are versioned. Turn deletion
protection off and empty the buckets (including old versions) first if you really want everything gone.
