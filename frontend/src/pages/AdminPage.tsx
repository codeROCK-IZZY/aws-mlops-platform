import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { AuditEvent, Role, User } from "../api/types";
import { Badge, Button, Card, ErrorBox, Field, Input, Loading, PageHeader, Select, Stat, Table } from "../components/ui";
import { useAuth } from "../lib/auth";
import { dateTime, humanize } from "../lib/format";

interface Stats { users: number; runs_by_status: Record<string, number>; runs_by_trigger: Record<string, number>; audit_events: number }
interface JobRow { id: number; type: string; status: string; attempts: number; last_error: string | null; created_at: string; payload: Record<string, unknown> }
interface Audit { chain: { valid: boolean; events: number; broken_at: number | null }; events: AuditEvent[] }

const sum = (o: Record<string, number>) => Object.values(o).reduce((a, b) => a + b, 0);
const parts = (o: Record<string, number>) => Object.entries(o).map(([k, v]) => `${v} ${humanize(k)}`).join(" · ");
const ROLES: Role[] = ["viewer", "engineer", "approver", "admin"];

export function AdminPage() {
  const stats = useQuery({ queryKey: ["admin-stats"], queryFn: () => api<Stats>("/admin/stats") });
  const jobs = useQuery({ queryKey: ["admin-jobs"], queryFn: () => api<JobRow[]>("/admin/jobs?limit=20"), refetchInterval: 5000 });
  const audit = useQuery({ queryKey: ["admin-audit"], queryFn: () => api<Audit>("/admin/audit?limit=30") });
  const s = stats.data;
  return (
    <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
      <PageHeader title="Administration" subtitle="Users and roles, the job queue, and the tamper-evident audit trail." />
      {s && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <Stat label="Users" value={s.users} />
          <Stat label="Pipeline runs" value={sum(s.runs_by_status)} sub={parts(s.runs_by_status)} />
          <Stat label="Runs by trigger" value={s.runs_by_trigger.drift ?? 0} sub={`started by monitoring · ${parts(s.runs_by_trigger)}`} />
          <Stat label="Audit events" value={s.audit_events} />
        </div>
      )}
      <div className="grid gap-6 lg:grid-cols-2">
        <UsersCard />
        <Card title="Job queue">
          {jobs.isLoading ? <Loading /> : (
            <Table>
              <thead><tr><th>#</th><th>Type</th><th>Status</th><th>Attempts</th><th>Created</th></tr></thead>
              <tbody>{jobs.data?.map((j) => (
                <tr key={j.id} title={j.last_error ?? undefined}><td>{j.id}</td><td>{humanize(j.type)}</td>
                  <td><Badge tone={j.status === "succeeded" ? "green" : j.status === "failed" ? "red" : "sky"}>{j.status}</Badge></td>
                  <td className="text-xs text-slate-500">{j.attempts} attempt{j.attempts === 1 ? "" : "s"}</td>
                  <td className="whitespace-nowrap">{dateTime(j.created_at)}</td></tr>
              ))}</tbody>
            </Table>
          )}
        </Card>
      </div>
      <Card title="Audit trail" actions={audit.data && (
        <Badge tone={audit.data.chain.valid ? "green" : "red"}>
          {audit.data.chain.valid ? `hash chain verified · ${audit.data.chain.events} events` : `chain broken at #${audit.data.chain.broken_at}`}
        </Badge>
      )}>
        <p className="mb-3 text-xs text-slate-500">Every promotion, rejection, rollback and user change, hash-chained so tampering is detectable.</p>
        {audit.isLoading ? <Loading /> : (
          <Table>
            <thead><tr><th>#</th><th>When</th><th>Who</th><th>Action</th><th>Details</th><th>Hash</th></tr></thead>
            <tbody>{audit.data?.events.map((e) => (
              <tr key={e.id}><td>{e.id}</td><td className="whitespace-nowrap">{dateTime(e.ts)}</td><td>{e.actor}</td>
                <td><code className="text-xs">{e.action}</code></td>
                <td className="max-w-xs truncate text-xs text-slate-500">{Object.keys(e.data).length ? JSON.stringify(e.data) : "—"}</td>
                <td><code className="text-xs text-slate-400">{e.hash}</code></td></tr>
            ))}</tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function UsersCard() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const [form, setForm] = useState({ email: "", password: "", role: "engineer" as Role });
  const create = useMutation({
    mutationFn: () => api("/users", { body: form }),
    onSuccess: () => { setForm({ email: "", password: "", role: "engineer" }); void qc.invalidateQueries({ queryKey: ["users"] }); },
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<User> }) => api(`/users/${id}`, { method: "PATCH", body }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["users"] }),
  });
  return (
    <Card title="Users">
      <form onSubmit={(e: FormEvent) => { e.preventDefault(); create.mutate(); }} className="mb-4 grid gap-2 sm:grid-cols-4">
        <Field label="Email"><Input type="email" required value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
        <Field label="Password"><Input type="password" required minLength={12} autoComplete="new-password" value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <Field label="Role"><Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value as Role })}>
          {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</Select></Field>
        <div className="flex items-end"><Button type="submit" busy={create.isPending} className="w-full">Add user</Button></div>
      </form>
      <ErrorBox error={create.error ?? update.error} />
      {users.isLoading ? <Loading /> : (
        <Table>
          <thead><tr><th>Email</th><th>Role</th><th>Status</th><th /></tr></thead>
          <tbody>{users.data?.map((u) => (
            <tr key={u.id}><td>{u.email}</td>
              <td><Select aria-label={`Role for ${u.email}`} value={u.role} disabled={u.id === me?.id} className="w-28"
                onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value as Role } })}>
                {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}</Select></td>
              <td>{u.is_active ? "active" : "deactivated"}</td>
              <td>{u.id !== me?.id && <Button variant="secondary" onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>
                {u.is_active ? "Deactivate" : "Reactivate"}</Button>}</td></tr>
          ))}</tbody>
        </Table>
      )}
    </Card>
  );
}
