import clsx from "clsx";
import { useState } from "react";
import type { GateCheck, MonitoringReport, Run, Step } from "../api/types";
import { formatValue, humanize } from "../lib/format";
import { Badge } from "./ui";

type Tone = "slate" | "green" | "amber" | "red" | "sky" | "violet";

export const STATUS_TONE: Record<string, Tone> = {
  succeeded: "green", approved: "green", ok: "green", validated: "green", passed: "green",
  running: "sky", queued: "sky", pending: "amber", warn: "amber", blocked: "amber",
  failed: "red", rejected: "red", alert: "red", skipped: "slate",
};
const ICON: Record<string, string> = { ok: "✓", succeeded: "✓", passed: "✓", warn: "!", blocked: "■", alert: "▲", failed: "✕" };

/** Status is never colour alone: every badge carries an icon and the word. */
export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-slate-400">—</span>;
  return <Badge tone={STATUS_TONE[status] ?? "slate"}>{ICON[status] ? `${ICON[status]} ` : ""}{humanize(status)}</Badge>;
}

export function runTitle(r: Run): string {
  const p = r.params as Record<string, string | undefined>;
  if (r.kind === "ingest") return `Ingest ${p.month}${p.scenario && p.scenario !== "none" ? ` · ${p.scenario.replaceAll("+", " + ")}` : ""}`;
  if (r.kind === "monitor") return `Monitor ${p.month}`;
  return `Train → holdout ${p.holdout_month ?? "latest"}${r.model_version ? ` · v${r.model_version}` : ""}`;
}

const HIDDEN = new Set(["checks"]);

export function StepList({ steps }: { steps: Step[] }) {
  return (
    <ol className="space-y-2">
      {steps.map((s, i) => (
        <li key={`${s.name}-${i}`} className="rounded-lg border border-slate-200 p-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium text-slate-800">{i + 1}. {humanize(s.name)}</span>
            <span className="flex items-center gap-2 text-xs text-slate-500">
              {s.seconds !== undefined && `${s.seconds.toFixed(1)} s`} <StatusBadge status={s.status} />
            </span>
          </div>
          <dl className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
            {Object.entries(s.detail).filter(([k]) => !HIDDEN.has(k)).map(([k, v]) => (
              <div key={k} className="flex gap-2"><dt className="text-slate-500">{humanize(k)}</dt>
                <dd className="truncate text-slate-800" title={formatValue(v)}>{formatValue(v)}</dd></div>
            ))}
          </dl>
          {Array.isArray(s.detail.checks) && <GateTable checks={s.detail.checks as GateCheck[]} />}
        </li>
      ))}
    </ol>
  );
}

export function GateTable({ checks }: { checks: GateCheck[] }) {
  return (
    <ul className="mt-2 space-y-1 text-xs">
      {checks.map((c) => (
        <li key={c.name} className="flex items-start gap-2">
          <span aria-hidden className={clsx("font-bold", c.passed ? "text-emerald-700" : "text-red-700")}>{c.passed ? "✓" : "✕"}</span>
          <span><span className="font-medium text-slate-800">{humanize(c.name)}</span>
            <span className="sr-only">{c.passed ? " passed" : " failed"}</span>
            <span className="text-slate-500"> — {c.detail}</span></span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Champion PR-AUC per monitored month: one series, so no legend (the title names it). The dashed line
 * is the promotion floor. Hover (or focus) a month for the full reading; the table on the page is the
 * accessible data view.
 */
export function PrAucChart({ reports, floor }: { reports: MonitoringReport[]; floor: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 1100, H = 260, L = 40, R = 16, T = 14, B = 28;
  if (reports.length === 0) return null;
  const x = (i: number) => L + (reports.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (reports.length - 1));
  const y = (v: number) => T + (1 - v) * (H - T - B);
  const path = reports.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r.performance.pr_auc).toFixed(1)}`).join(" ");
  const h = hover === null ? null : reports[hover];
  return (
    <figure className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img"
        aria-label={`Champion PR-AUC by month, ${reports.map((r) => `${r.month}: ${r.performance.pr_auc.toFixed(2)}`).join(", ")}`}
        onMouseLeave={() => setHover(null)}>
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="#e2e8f0" strokeWidth={1} />
            <text x={L - 6} y={y(t) + 3} textAnchor="end" fontSize={10} fill="#64748b">{t.toFixed(2)}</text>
          </g>
        ))}
        <line x1={L} x2={W - R} y1={y(floor)} y2={y(floor)} stroke="#64748b" strokeDasharray="4 4" strokeWidth={1} />
        <text x={L + 6} y={y(floor) + 12} fontSize={10} fill="#52514e">promotion floor {floor.toFixed(2)}</text>
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="#94a3b8" strokeWidth={1} />}
        <path d={path} fill="none" stroke="#2a78d6" strokeWidth={2} strokeLinejoin="round" />
        {reports.map((r, i) => (
          <g key={r.id}>
            <circle cx={x(i)} cy={y(r.performance.pr_auc)} r={r.status === "alert" ? 5 : 4}
              fill={r.status === "alert" ? "#ffffff" : "#2a78d6"} stroke="#2a78d6" strokeWidth={2} />
            <text x={x(i)} y={H - 10} textAnchor="middle" fontSize={10} fill="#64748b">{r.month.slice(2)}</text>
            <rect x={x(i) - 30} y={T} width={60} height={H - T - B} fill="transparent" tabIndex={0}
              aria-label={`${r.month}: PR-AUC ${r.performance.pr_auc.toFixed(3)}, ${r.status}`}
              onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)} />
          </g>
        ))}
      </svg>
      {h && hover !== null && (
        <div className="pointer-events-none absolute top-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs shadow-md"
          style={{ left: `${Math.min(70, (x(hover) / W) * 100)}%` }}>
          <p className="font-semibold text-slate-900">{h.month} · champion v{h.model_version}</p>
          <p className="text-slate-700">PR-AUC {h.performance.pr_auc.toFixed(3)} · precision {(h.performance.precision * 100).toFixed(0)}% · recall {(h.performance.recall * 100).toFixed(0)}%</p>
          <p className="mt-1"><StatusBadge status={h.status} /></p>
        </div>
      )}
      <figcaption className="mt-1 text-xs text-slate-500">Open markers: monitoring raised an alert that month.</figcaption>
    </figure>
  );
}
