import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { Dataset, Run } from "../api/types";
import { StatusBadge } from "../components/pipeline";
import { Button, Card, Empty, ErrorBox, Field, Input, Loading, PageHeader, Table } from "../components/ui";
import { useAuth } from "../lib/auth";
import { dateTime, pct } from "../lib/format";

const SCENARIOS = ["sensor_drift", "fleet_mix", "new_failure"] as const;
const LABELS: Record<string, string> = {
  sensor_drift: "Sensor drift (recalibrated pump vibration sensors)",
  fleet_mix: "Fleet mix (more compressors)",
  new_failure: "New failure mode (seal leaks)",
};

export function DatasetsPage() {
  const { can } = useAuth();
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => api<Dataset[]>("/datasets"), refetchInterval: 5000 });
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Datasets" subtitle="Monthly telemetry batches: validated against the data contract, versioned in S3 with a content hash." />
      {can("engineer") && <IngestCard />}
      <Card title="Batches">
        {ds.isLoading ? <Loading /> : !ds.data?.length ? <Empty>No data ingested yet.</Empty> : (
          <Table>
            <thead><tr><th>Month</th><th>Scenario</th><th>Status</th><th>Rows</th><th>Failure rate</th><th>Contract</th><th>SHA-256</th><th>Ingested</th></tr></thead>
            <tbody>{ds.data.map((d) => (
              <tr key={d.month}>
                <td className="font-medium">{d.month}</td>
                <td className="text-xs">{d.scenario === "none" ? "stable" : d.scenario.replaceAll("+", " + ").replaceAll("_", " ")}</td>
                <td><StatusBadge status={d.status} /></td>
                <td className="tabular-nums">{d.rows.toLocaleString()}</td>
                <td className="tabular-nums">{pct(d.contract?.stats.label_rate, 1)}</td>
                <td className="max-w-xs text-xs text-slate-600">{d.contract?.errors.length ? d.contract.errors.join("; ")
                  : d.contract?.warnings.length ? `${d.contract.warnings.length} warning(s)` : "all checks passed"}</td>
                <td><code className="text-xs text-slate-500">{d.sha256?.slice(0, 12) ?? "—"}</code></td>
                <td className="whitespace-nowrap">{dateTime(d.created_at)}</td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function IngestCard() {
  const qc = useQueryClient();
  const [month, setMonth] = useState("2027-01");
  const [effects, setEffects] = useState<string[]>([]);
  const ingest = useMutation({
    mutationFn: () => api<Run>("/datasets/ingest", { body: { month, scenario: effects.length ? effects.join("+") : "none" } }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["datasets"] }); void qc.invalidateQueries({ queryKey: ["runs"] }); },
  });
  return (
    <Card title="Ingest a month of telemetry">
      <p className="mb-3 text-sm text-slate-600">Generates a synthetic batch for 240 units, optionally with real-world changes mixed in, and runs it through the contract.</p>
      <div className="flex flex-wrap items-end gap-4">
        <Field label="Month"><Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} className="w-40" /></Field>
        <fieldset className="text-sm">
          <legend className="mb-1 font-medium text-slate-700">Changes in this month</legend>
          <div className="flex flex-wrap gap-3">
            {SCENARIOS.map((s) => (
              <label key={s} className="flex items-center gap-1.5">
                <input type="checkbox" checked={effects.includes(s)}
                  onChange={(e) => setEffects(e.target.checked ? [...effects, s] : effects.filter((x) => x !== s))} />
                {LABELS[s]}
              </label>
            ))}
          </div>
        </fieldset>
        <Button onClick={() => ingest.mutate()} busy={ingest.isPending} disabled={!month}>Ingest</Button>
      </div>
      <div className="mt-2"><ErrorBox error={ingest.error} /></div>
    </Card>
  );
}
