import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { ModelVersion, Promotion } from "../api/types";
import { GateTable, StatusBadge } from "../components/pipeline";
import { Badge, Button, Card, Empty, ErrorBox, Input, Loading, PageHeader, Table } from "../components/ui";
import { useAuth } from "../lib/auth";
import { dateTime } from "../lib/format";

export function ModelsPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api<ModelVersion[]>("/models") });
  const promos = useQuery({ queryKey: ["promotions"], queryFn: () => api<Promotion[]>("/promotions") });
  const refresh = () => { for (const k of ["models", "promotions", "overview"]) void qc.invalidateQueries({ queryKey: [k] }); };
  const rollback = useMutation({ mutationFn: () => api("/models/rollback", { body: { note: "manual rollback from the dashboard" } }), onSuccess: refresh });
  const pending = promos.data?.filter((p) => p.status === "pending") ?? [];
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Models & approvals" subtitle="Registered versions, their gate results, and every promotion decision."
        actions={can("approver") && <Button variant="secondary" onClick={() => { if (confirm("Roll back to the previous champion?")) rollback.mutate(); }}
          busy={rollback.isPending}>Roll back champion</Button>} />
      <ErrorBox error={rollback.error} />
      {pending.map((p) => <PendingCard key={p.id} promo={p} canDecide={can("approver")} onDone={refresh} />)}
      <Card title="Model versions">
        {models.isLoading ? <Loading /> : !models.data?.length ? <Empty>No models registered.</Empty> : (
          <Table>
            <thead><tr><th>Version</th><th>Alias</th><th>Trained on</th><th>Holdout</th><th>PR-AUC</th><th>ECE</th><th>Recall</th><th>p95 latency</th><th>Gates</th></tr></thead>
            <tbody>{models.data.map((m) => (
              <tr key={m.version}>
                <td className="font-medium">v{m.version}</td>
                <td className="space-x-1">{m.aliases.map((a) => <Badge key={a} tone={a === "champion" ? "violet" : "sky"}>{a}</Badge>)}</td>
                <td className="text-xs">{m.tags.train_months?.replaceAll(",", ", ")}</td>
                <td>{m.tags.holdout_month}</td>
                <td className="tabular-nums">{m.metrics?.pr_auc?.toFixed(3) ?? "—"}</td>
                <td className="tabular-nums">{m.metrics?.ece?.toFixed(3) ?? "—"}</td>
                <td className="tabular-nums">{m.metrics?.recall !== undefined ? `${(m.metrics.recall * 100).toFixed(0)}%` : "—"}</td>
                <td className="tabular-nums">{m.metrics?.p95_latency_ms !== undefined ? `${m.metrics.p95_latency_ms.toFixed(1)} ms` : "—"}</td>
                <td><StatusBadge status={m.tags.gates} /></td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
      <Card title="Promotion history">
        {!promos.data?.length ? <Empty>No promotions yet.</Empty> : (
          <Table>
            <thead><tr><th>Change</th><th>Status</th><th>Requested by</th><th>Decided by</th><th>Note</th><th>When</th></tr></thead>
            <tbody>{promos.data.map((p) => (
              <tr key={p.id}>
                <td>{p.action === "rollback" ? "Rollback" : "Promote"} {p.from_version ? `v${p.from_version} → ` : ""}v{p.version}</td>
                <td><StatusBadge status={p.status} /></td><td className="text-xs">{p.requested_by}</td>
                <td className="text-xs">{p.decided_by ?? "—"}</td><td className="max-w-xs truncate text-xs">{p.note || "—"}</td>
                <td className="whitespace-nowrap">{dateTime(p.decided_at ?? p.created_at)}</td>
              </tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

export function PendingCard({ promo, canDecide, onDone }: { promo: Promotion; canDecide: boolean; onDone: () => void }) {
  const [note, setNote] = useState("");
  const decide = useMutation({
    mutationFn: (verb: "approve" | "reject") => api(`/promotions/${promo.id}/${verb}`, { body: { note } }),
    onSuccess: onDone,
  });
  const cand = promo.gate_report.candidate as Record<string, number> | undefined;
  const champ = promo.gate_report.champion as Record<string, number> | null | undefined;
  return (
    <Card title={`Promote v${promo.version}${promo.from_version ? ` to replace v${promo.from_version}` : ""}?`}
      className="border-amber-300" actions={<Badge tone="amber">! awaiting approval</Badge>}>
      <div className="grid gap-6 md:grid-cols-2">
        <div>
          <p className="mb-2 text-sm text-slate-600">Requested by {promo.requested_by} · {dateTime(promo.created_at)}. Both models scored on the same holdout month:</p>
          {cand && (
            <dl className="grid grid-cols-3 gap-2 text-sm">
              <dt className="text-slate-500" />
              <dt className="text-xs font-semibold uppercase text-slate-500">Candidate</dt>
              <dt className="text-xs font-semibold uppercase text-slate-500">Champion</dt>
              {(["pr_auc", "recall", "precision", "ece"] as const).map((k) => (
                <div key={k} className="contents">
                  <dd className="text-slate-500">{k.replace("_", "-").toUpperCase()}</dd>
                  <dd className="tabular-nums font-medium">{cand[k]?.toFixed(3)}</dd>
                  <dd className="tabular-nums">{champ?.[k]?.toFixed(3) ?? "—"}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
        <div>
          <p className="text-sm font-medium text-slate-800">Gates</p>
          <GateTable checks={promo.gate_report.checks ?? []} />
        </div>
      </div>
      {canDecide ? (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Input aria-label="Decision note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} className="max-w-sm" />
          <Button onClick={() => decide.mutate("approve")} busy={decide.isPending}>Approve & deploy</Button>
          <Button variant="secondary" onClick={() => decide.mutate("reject")} disabled={decide.isPending}>Reject</Button>
        </div>
      ) : <p className="mt-4 text-sm text-slate-500">An approver decides on promotions.</p>}
      <div className="mt-2"><ErrorBox error={decide.error} /></div>
    </Card>
  );
}
