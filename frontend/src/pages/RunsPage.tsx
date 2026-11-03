import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { Dataset, Run } from "../api/types";
import { runTitle, StatusBadge, StepList } from "../components/pipeline";
import { Button, Card, Empty, ErrorBox, Field, Loading, PageHeader, Select, Table } from "../components/ui";
import { useAuth } from "../lib/auth";
import { dateTime } from "../lib/format";

export function RunsPage() {
  const { can } = useAuth();
  const [kind, setKind] = useState("");
  const runs = useQuery({ queryKey: ["runs", kind], queryFn: () => api<Run[]>(`/runs${kind ? `?kind=${kind}` : ""}`), refetchInterval: 4000 });
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Pipeline runs" subtitle="Every ingest, training and monitoring run, step by step." />
      {can("engineer") && <StartRuns />}
      <Card title="Runs" actions={
        <Select aria-label="Filter by kind" value={kind} onChange={(e) => setKind(e.target.value)} className="w-36">
          <option value="">All kinds</option><option value="ingest">Ingest</option><option value="train">Train</option><option value="monitor">Monitor</option>
        </Select>}>
        {runs.isLoading ? <Loading /> : runs.data?.length === 0 ? <Empty>No runs.</Empty> : (
          <Table>
            <thead><tr><th>Run</th><th>Trigger</th><th>Status</th><th>Outcome</th><th>By</th><th>Created</th></tr></thead>
            <tbody>{runs.data?.map((r) => (
              <tr key={r.id}>
                <td><Link to={`/runs/${r.id}`} className="text-indigo-700 hover:underline">{runTitle(r)}</Link></td>
                <td>{r.trigger}</td><td><StatusBadge status={r.status} /></td>
                <td className="max-w-md truncate text-xs text-slate-600" title={String(r.result?.reason ?? "")}>
                  {String(r.result?.reason ?? r.result?.error ?? r.result?.status ?? "")}</td>
                <td className="text-xs">{r.created_by}</td>
                <td className="whitespace-nowrap">{dateTime(r.created_at)}</td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function StartRuns() {
  const qc = useQueryClient();
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: () => api<Dataset[]>("/datasets") });
  const months = (datasets.data ?? []).filter((d) => d.status === "validated").map((d) => d.month);
  const [holdout, setHoldout] = useState("");
  const [month, setMonth] = useState("");
  const done = () => void qc.invalidateQueries({ queryKey: ["runs"] });
  const train = useMutation({ mutationFn: () => api<Run>("/runs/train", { body: holdout ? { holdout_month: holdout } : {} }), onSuccess: done });
  const monitor = useMutation({ mutationFn: () => api<Run>("/runs/monitor", { body: { month: month || months.at(-1) } }), onSuccess: done });
  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card title="Train a candidate">
        <div className="flex items-end gap-2">
          <Field label="Holdout month" hint="Trained on the four validated months before it; gated against the champion on this month.">
            <Select value={holdout} onChange={(e) => setHoldout(e.target.value)}>
              <option value="">Latest validated</option>{months.map((m) => <option key={m}>{m}</option>)}
            </Select>
          </Field>
          <Button onClick={() => train.mutate()} busy={train.isPending} className="mb-5">Start training</Button>
        </div>
        <ErrorBox error={train.error} />
      </Card>
      <Card title="Monitor a month">
        <div className="flex items-end gap-2">
          <Field label="Month" hint="Scores it with the champion; drift and performance alerts trigger a retraining run.">
            <Select value={month} onChange={(e) => setMonth(e.target.value)}>
              <option value="">Latest validated</option>{months.map((m) => <option key={m}>{m}</option>)}
            </Select>
          </Field>
          <Button onClick={() => monitor.mutate()} busy={monitor.isPending} disabled={!months.length} className="mb-5">Run monitoring</Button>
        </div>
        <ErrorBox error={monitor.error} />
      </Card>
    </div>
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const run = useQuery({
    queryKey: ["run", id], queryFn: () => api<Run>(`/runs/${id}`),
    refetchInterval: (q) => (q.state.data && ["queued", "running"].includes(q.state.data.status) ? 2000 : false),
  });
  if (run.isLoading) return <Loading />;
  if (!run.data) return <div className="p-6"><ErrorBox error={run.error} /></div>;
  const r = run.data;
  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 p-6">
      <PageHeader title={runTitle(r)} subtitle={<>Triggered {r.trigger === "drift" ? "by monitoring" : `by ${r.created_by}`} · {dateTime(r.created_at)}</>}
        actions={<StatusBadge status={r.status} />} />
      {typeof r.result?.reason === "string" && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">{r.result.reason}</p>
      )}
      {typeof r.params.reason === "string" && <p className="text-sm text-slate-600">Why this run started: {r.params.reason}</p>}
      <Card title="Steps">{r.steps.length ? <StepList steps={r.steps} /> : <Empty>Waiting for the worker…</Empty>}</Card>
      <Link to="/runs" className="text-sm text-indigo-700 hover:underline">← All runs</Link>
    </div>
  );
}
