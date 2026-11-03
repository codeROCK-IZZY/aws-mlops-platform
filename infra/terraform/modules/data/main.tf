# PostgreSQL (RDS): users, dataset catalogue, pipeline runs, promotions, monitoring, audit trail; plus application secrets.

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "db_instance_class" { type = string }
variable "db_multi_az" { type = bool }

# ---------------------------------------------------------------- secrets
# Generated here, stored in Secrets Manager, injected into containers by ECS.
# They also exist in the Terraform state, which is why the state bucket must be
# encrypted and access-restricted (see docs/AWS_DEPLOYMENT.md).
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg://mlp:${random_password.db.result}@${aws_db_instance.this.address}:5432/mlp?sslmode=require"
}

resource "aws_secretsmanager_secret" "jwt" {
  name                    = "${var.name}/jwt-secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "jwt" {
  secret_id     = aws_secretsmanager_secret.jwt.id
  secret_string = random_password.jwt.result
}


# ---------------------------------------------------------------- database
resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL: ingress only from application tasks (rule added by the app module)"
  vpc_id      = var.vpc_id
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres17"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # log queries slower than 1 s
  }
}

resource "aws_db_instance" "this" {
  identifier                      = "${var.name}-db"
  engine                          = "postgres"
  engine_version                  = "17"
  instance_class                  = var.db_instance_class
  allocated_storage               = 20
  max_allocated_storage           = 100
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "mlp"
  username                        = "mlp"
  password                        = random_password.db.result
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.this.name
  multi_az                        = var.db_multi_az
  publicly_accessible             = false
  backup_retention_period         = 7
  backup_window                   = "03:00-04:00"
  maintenance_window              = "sun:04:30-sun:05:30"
  auto_minor_version_upgrade      = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-db-final"
  copy_tags_to_snapshot           = true
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

output "db_security_group_id" { value = aws_security_group.db.id }
output "db_instance_id" { value = aws_db_instance.this.identifier }
output "database_url_secret_arn" { value = aws_secretsmanager_secret.database_url.arn }
output "jwt_secret_arn" { value = aws_secretsmanager_secret.jwt.arn }
