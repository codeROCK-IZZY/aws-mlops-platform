import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import type { MonitoringReport, Promotion, Run } from "../api/types";
import { GateTable, PrAucChart, runTitle, StatusBadge } from "../components/pipeline";
import { PendingCard } from "../pages/ModelsPage";

const wrap = (ui: ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);

const promo: Promotion = {
  id: "p1", version: "4", from_version: "3", action: "promote", status: "pending", requested_by: "pipeline:drift",
  decided_by: null, decided_at: null, note: null, created_at: "2026-11-01T10:00:00Z", pipeline_run_id: "r1",
  gate_report: {
    passed: true,
    candidate: { pr_auc: 0.772, recall: 0.81, precision: 0.52, ece: 0.012 },
    champion: { pr_auc: 0.704, recall: 0.74, precision: 0.31, ece: 0.03 },
    checks: [
      { name: "no_regression", passed: true, value: 0.772, limit: 0.684, detail: "PR-AUC 0.772 vs champion 0.704 on the same month" },
      { name: "slice_recall", passed: true, value: 0.66, limit: 0.5, detail: "weakest slice motor: recall 0.66 (min 0.50)" },
    ],
  },
};

describe("pipeline components", () => {
  it("never shows status by colour alone", () => {
    render(<><StatusBadge status="alert" /><StatusBadge status="succeeded" /></>);
    expect(screen.getByText("▲ alert")).toBeInTheDocument();
    expect(screen.getByText("✓ succeeded")).toBeInTheDocument();
  });

  it("lists every gate with its outcome", () => {
    render(<GateTable checks={[{ name: "min_quality", passed: false, value: 0.4, limit: 0.55, detail: "PR-AUC 0.400 (floor 0.55)" }]} />);
    expect(screen.getByText("min quality")).toBeInTheDocument();
    expect(screen.getByText("failed", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/floor 0.55/)).toBeInTheDocument();
  });

  it("names runs in plain language", () => {
    const base: Omit<Run, "kind" | "params" | "model_version"> = {
      id: "1", trigger: "manual", status: "succeeded", steps: [], result: null, created_by: "x", created_at: "", started_at: null, finished_at: null,
    };
    expect(runTitle({ ...base, kind: "ingest", params: { month: "2026-09", scenario: "sensor_drift+fleet_mix" }, model_version: null }))
      .toBe("Ingest 2026-09 · sensor_drift + fleet_mix");
    expect(runTitle({ ...base, kind: "train", params: { holdout_month: "2026-08" }, model_version: "3" })).toBe("Train → holdout 2026-08 · v3");
  });

  it("describes the chart for screen readers and shows a reading on focus", async () => {
    const r = (month: string, pr: number, status: "ok" | "alert"): MonitoringReport => ({
      id: month, month, model_version: "1", status, created_at: "", retrain_run_id: null,
      drift: { status, features: {}, max_feature_psi: { feature: "x", psi: 0 }, drifted_features: [] },
      performance: { pr_auc: pr, roc_auc: 0.9, brier: 0.05, ece: 0.01, precision: 0.5, recall: 0.8, alerts: 10 },
    });
    render(<PrAucChart reports={[r("2026-06", 0.804, "ok"), r("2026-07", 0.724, "alert")]} floor={0.55} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/2026-06: 0.80, 2026-07: 0.72/);
    await userEvent.tab();
    await userEvent.tab();
    expect(screen.getByText("2026-07 · champion v1")).toBeInTheDocument();
  });
});

describe("PendingCard", () => {
  afterEach(() => vi.restoreAllMocks());

  it("compares candidate and champion on the same month and lets an approver decide", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ...promo, status: "approved" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const onDone = vi.fn();
    wrap(<PendingCard promo={promo} canDecide onDone={onDone} />);
    expect(screen.getByText("Promote v4 to replace v3?")).toBeInTheDocument();
    expect(screen.getByText("0.772")).toBeInTheDocument();
    expect(screen.getByText("0.704")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Decision note"), "looks good");
    await userEvent.click(screen.getByRole("button", { name: "Approve & deploy" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(String(url)).toContain("/promotions/p1/approve");
    expect(JSON.parse(String(init?.body))).toEqual({ note: "looks good" });
  });

  it("does not offer the decision to non-approvers", () => {
    wrap(<PendingCard promo={promo} canDecide={false} onDone={() => {}} />);
    expect(screen.queryByRole("button", { name: "Approve & deploy" })).toBeNull();
    expect(screen.getByText("An approver decides on promotions.")).toBeInTheDocument();
  });
});
