import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { MonitoringReport, SystemInfo } from "../api/types";
import { PrAucChart, StatusBadge } from "../components/pipeline";
import { Card, Empty, Loading, PageHeader, Table } from "../components/ui";
import { humanize, pct } from "../lib/format";

export function MonitoringPage() {
  const mon = useQuery({ queryKey: ["monitoring"], queryFn: () => api<MonitoringReport[]>("/monitoring") });
  const info = useQuery({ queryKey: ["system-info"], queryFn: () => api<SystemInfo>("/system/info"), staleTime: Infinity });
  const [selected, setSelected] = useState<string | null>(null);
  if (mon.isLoading) return <Loading />;
  const reports = mon.data ?? [];
  const current = reports.find((r) => r.id === selected) ?? reports.at(-1);
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Monitoring" subtitle={info.data &&
        `Input and prediction drift (PSI: warn ≥ ${info.data.drift.psi_warn}, alert ≥ ${info.data.drift.psi_alert}) and measured performance once labels arrive.`} />
      {reports.length === 0 ? <Card><Empty>No monitoring reports yet.</Empty></Card> : (
        <>
          <Card title="Champion PR-AUC on each monitored month">
            <PrAucChart reports={reports} floor={info.data?.gates.min_pr_auc ?? 0.55} />
          </Card>
          <div className="grid gap-6 lg:grid-cols-5">
            <Card title="Month by month" className="lg:col-span-3">
              <Table>
                <thead><tr><th>Month</th><th>Model</th><th>Status</th><th>PR-AUC</th><th>Precision</th><th>Recall</th><th>Largest drift</th><th>Retrain</th></tr></thead>
                <tbody>{reports.map((r) => (
                  <tr key={r.id} className={r.id === current?.id ? "bg-indigo-50/60" : undefined}>
                    <td><button className="text-indigo-700 hover:underline" onClick={() => setSelected(r.id)}>{r.month}</button></td>
                    <td>v{r.model_version}</td><td><StatusBadge status={r.status} /></td>
                    <td className="tabular-nums">{r.performance.pr_auc.toFixed(3)}</td>
                    <td className="tabular-nums">{pct(r.performance.precision)}</td>
                    <td className="tabular-nums">{pct(r.performance.recall)}</td>
                    <td className="text-xs">{humanize(r.drift.max_feature_psi.feature)} {r.drift.max_feature_psi.psi.toFixed(2)}</td>
                    <td>{r.retrain_run_id ? <Link to={`/runs/${r.retrain_run_id}`} className="text-indigo-700 hover:underline">run →</Link> : "—"}</td>
                  </tr>
                ))}</tbody>
              </Table>
            </Card>
            {current && <DriftCard report={current} alert={info.data?.drift.psi_alert ?? 0.25} warn={info.data?.drift.psi_warn ?? 0.1} />}
          </div>
        </>
      )}
    </div>
  );
}

/** PSI per feature as horizontal bars, sorted; the warn and alert levels are reference lines. */
function DriftCard({ report, warn, alert }: { report: MonitoringReport; warn: number; alert: number }) {
  const rows = Object.entries(report.drift.features).sort((a, b) => b[1].psi - a[1].psi);
  if (report.drift.prediction) rows.unshift(["predictions", report.drift.prediction]);
  const max = Math.max(alert * 1.6, ...rows.map(([, f]) => f.psi));
  const w = (v: number) => `${Math.min(100, (v / max) * 100)}%`;
  return (
    <Card title={`Drift in ${report.month} (PSI vs. training data)`} className="lg:col-span-2">
      <ul className="space-y-1.5">
        {rows.map(([name, f]) => (
          <li key={name} className="grid grid-cols-[9rem_1fr_3rem] items-center gap-2 text-xs">
            <span className="truncate text-slate-700" title={name}>{humanize(name)}</span>
            <span className="relative h-3 rounded-sm bg-slate-100">
              <span className="absolute inset-y-0 left-0 rounded-r bg-[#2a78d6]" style={{ width: w(f.psi) }} />
              <span aria-hidden className="absolute inset-y-[-2px] w-px bg-slate-500" style={{ left: w(warn) }} />
              <span aria-hidden className="absolute inset-y-[-2px] w-px bg-slate-900" style={{ left: w(alert) }} />
            </span>
            <span className="text-right tabular-nums text-slate-800">{f.psi.toFixed(2)}{f.level === "alert" ? " ▲" : f.level === "warn" ? " !" : ""}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-slate-500">Thin marks: grey = warn ({warn}), black = alert ({alert}). ▲ alert, ! warn.</p>
      {report.performance.slices && (
        <>
          <p className="mt-4 text-sm font-medium text-slate-800">Recall by equipment type</p>
          <ul className="mt-1 grid grid-cols-3 gap-2 text-xs">
            {Object.entries(report.performance.slices).map(([k, s]) => (
              <li key={k} className="rounded-lg border border-slate-200 p-2"><span className="block text-slate-500">{k}</span>
                <span className="text-base font-semibold tabular-nums">{pct(s.recall)}</span></li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}
