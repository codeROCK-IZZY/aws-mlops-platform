export type Role = "admin" | "approver" | "engineer" | "viewer";

export interface User { id: string; email: string; full_name: string; role: Role; is_active: boolean; created_at: string }

export interface SystemInfo {
  env: string; model_name: string; mlflow_tracking_uri: string; deploy_target: "local" | "sagemaker";
  approval_policy: "auto" | "manual";
  gates: { min_pr_auc: number; max_regression: number; max_ece: number; min_slice_recall: number; max_p95_latency_ms: number };
  drift: { psi_warn: number; psi_alert: number; retrain_on_drift: boolean };
}

export interface Step {
  name: string; status: "running" | "succeeded" | "failed" | "blocked" | "skipped"; seconds?: number;
  detail: Record<string, unknown>;
}

export interface Run {
  id: string; kind: "ingest" | "train" | "monitor"; trigger: string; status: "queued" | "running" | "succeeded" | "failed" | "blocked";
  params: Record<string, unknown>; steps: Step[]; result: Record<string, unknown> | null; model_version: string | null;
  created_by: string | null; created_at: string; started_at: string | null; finished_at: string | null;
}

export interface ModelVersion {
  version: string; aliases: string[]; threshold: number; tags: Record<string, string>; run_id: string | null;
  created_at: number; metrics?: Record<string, number>;
}

export interface GateCheck { name: string; passed: boolean; value: number | null; limit: number | null; detail: string }
export interface GateReport { passed: boolean; checks: GateCheck[]; candidate: Record<string, unknown>; champion: Record<string, unknown> | null }

export interface Promotion {
  id: string; version: string; from_version: string | null; action: "promote" | "rollback";
  status: "pending" | "approved" | "rejected"; gate_report: Partial<GateReport>; requested_by: string;
  decided_by: string | null; decided_at: string | null; note: string | null; created_at: string; pipeline_run_id: string | null;
}

export interface Dataset {
  month: string; status: "validated" | "rejected"; scenario: string; rows: number; sha256: string | null;
  contract: { passed: boolean; errors: string[]; warnings: string[]; stats: Record<string, number> } | null; created_at: string;
}

export interface FeatureDrift { psi: number; level: "ok" | "warn" | "alert"; null_rate?: number }
export interface DriftReport {
  status: "ok" | "warn" | "alert"; features: Record<string, FeatureDrift>; prediction?: { psi: number; level: "ok" | "warn" | "alert" };
  max_feature_psi: { feature: string; psi: number }; drifted_features: string[];
}
export interface Performance {
  pr_auc: number; roc_auc: number; brier: number; ece: number; precision: number; recall: number; alerts: number;
  slices?: Record<string, { recall: number; precision?: number; n?: number }>;
}
export interface MonitoringReport {
  id: string; month: string; model_version: string; status: "ok" | "warn" | "alert"; drift: DriftReport;
  performance: Performance; created_at: string; retrain_run_id: string | null;
}

export interface Overview {
  champion: ModelVersion | null; challenger: ModelVersion | null; pending_promotions: number;
  datasets: Record<string, number>; latest_monitoring: MonitoringReport | null; recent_runs: Run[];
  settings: { approval_policy: string; deploy_target: string; psi_alert: number; retrain_on_drift: boolean };
}

export interface AuditEvent {
  id: number; ts: string; actor: string; action: string; entity: string; entity_id: string | null;
  data: Record<string, unknown>; hash: string;
}
