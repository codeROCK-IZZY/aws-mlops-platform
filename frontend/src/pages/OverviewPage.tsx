import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { MonitoringReport, Overview, SystemInfo } from "../api/types";
import { PrAucChart, runTitle, StatusBadge } from "../components/pipeline";
import { Card, Empty, Loading, PageHeader, Stat, Table } from "../components/ui";
import { dateTime, pct } from "../lib/format";

export function OverviewPage() {
  const ov = useQuery({ queryKey: ["overview"], queryFn: () => api<Overview>("/overview"), refetchInterval: 5000 });
  const mon = useQuery({ queryKey: ["monitoring"], queryFn: () => api<MonitoringReport[]>("/monitoring") });
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => api<SystemInfo>("/system/info"), staleTime: Infinity });
  if (ov.isLoading) return <Loading />;
  const o = ov.data;
  if (!o) return null;
  const latest = o.latest_monitoring;
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Overview" subtitle="The production model, what monitoring says about it, and what the pipeline did recently." />
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Champion" value={o.champion ? `v${o.champion.version}` : "none"}
          sub={o.champion ? `holdout PR-AUC ${Number(o.champion.tags.pr_auc).toFixed(3)} · threshold ${o.champion.threshold.toFixed(3)}` : "train a first model"} />
        <Stat label="Awaiting approval" value={o.pending_promotions}
          sub={o.challenger ? <Link to="/models" className="text-indigo-700 hover:underline">challenger v{o.challenger.version} →</Link> : `approval policy: ${o.settings.approval_policy}`} />
        <Stat label="Latest monitoring" value={latest ? <StatusBadge status={latest.status} /> : "—"}
          sub={latest ? `${latest.month} · PR-AUC ${latest.performance.pr_auc.toFixed(3)} · precision ${pct(latest.performance.precision)}` : "no report yet"} />
        <Stat label="Datasets" value={Object.values(o.datasets).reduce((a, b) => a + b, 0)}
          sub={Object.entries(o.datasets).map(([k, v]) => `${v} ${k}`).join(" · ") || "none ingested"} />
      </div>
      <Card title="Champion PR-AUC on each monitored month">
        {mon.data && mon.data.length ? <PrAucChart reports={mon.data} floor={info.data?.gates.min_pr_auc ?? 0.55} />
          : <Empty>No monitoring reports yet.</Empty>}
      </Card>
      <Card title="Recent pipeline runs" actions={<Link to="/runs" className="text-sm text-indigo-700 hover:underline">All runs</Link>}>
        {o.recent_runs.length === 0 ? <Empty>No runs yet.</Empty> : (
          <Table>
            <thead><tr><th>Run</th><th>Trigger</th><th>Status</th><th>Outcome</th><th>Started</th></tr></thead>
            <tbody>{o.recent_runs.map((r) => (
              <tr key={r.id}>
                <td><Link to={`/runs/${r.id}`} className="text-indigo-700 hover:underline">{runTitle(r)}</Link></td>
                <td>{r.trigger}</td><td><StatusBadge status={r.status} /></td>
                <td className="max-w-md truncate text-xs text-slate-600">{String(r.result?.reason ?? r.result?.status ?? "")}</td>
                <td className="whitespace-nowrap">{dateTime(r.created_at)}</td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}
