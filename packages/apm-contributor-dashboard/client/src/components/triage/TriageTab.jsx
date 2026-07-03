import { createSignal, createMemo, Show } from "solid-js";
import { triageResource, refetchTriage } from "../../stores/triage";
import StatsCards from "../StatsCards";
import TriageTable from "./TriageTable";
import TriageDetail from "./TriageDetail";
import Pagination from "../Pagination";

function decisionKey(decision) {
  if (!decision) return "";
  if (decision.startsWith("accept")) return "accept";
  if (decision.startsWith("needs-design")) return "needs-design";
  if (decision.startsWith("decline")) return "decline";
  if (decision.startsWith("defer")) return "defer";
  if (decision.startsWith("auto-handle")) return "auto-handle";
  if (decision.startsWith("duplicate")) return "duplicate";
  return decision;
}

export default function TriageTab() {
  const [page, setPage] = createSignal(0);
  const [pageSize, setPageSize] = createSignal(25);
  const [filters, setFilters] = createSignal({});
  const [sortCol, setSortCol] = createSignal(null);
  const [sortAsc, setSortAsc] = createSignal(true);
  const [detailItem, setDetailItem] = createSignal(null);

  const items = () => triageResource()?.items || [];

  const filtered = createMemo(() => {
    const f = filters();
    return items().filter(item => {
      for (const [key, val] of Object.entries(f)) {
        if (key === "decision" && !item.decision?.startsWith(val)) return false;
        if (key === "priority" && item.priority !== val) return false;
        if (key === "type" && item.type !== val) return false;
      }
      return true;
    });
  });

  const sorted = createMemo(() => {
    const col = sortCol();
    const dir = sortAsc() ? 1 : -1;
    const copy = [...filtered()];
    if (!col) return copy;
    return copy.sort((a, b) => {
      if (col === "number") return dir * (a.number - b.number);
      const va = (a[col] || "").toLowerCase();
      const vb = (b[col] || "").toLowerCase();
      return dir * va.localeCompare(vb);
    });
  });

  const paged = createMemo(() => {
    const start = page() * pageSize();
    return sorted().slice(start, start + pageSize());
  });

  const stats = [
    { label: "Total", value: () => items().length },
    { label: "Accept", color: "#3fb950", value: () => items().filter(i => decisionKey(i.decision) === "accept").length },
    { label: "Needs Design", color: "#bc8cff", value: () => items().filter(i => decisionKey(i.decision) === "needs-design").length },
    { label: "Decline", color: "#f85149", value: () => items().filter(i => decisionKey(i.decision) === "decline").length },
    { label: "Defer", color: "#8b949e", value: () => items().filter(i => decisionKey(i.decision) === "defer").length },
    { label: "Auto-handle", color: "#58a6ff", value: () => items().filter(i => decisionKey(i.decision) === "auto-handle").length },
  ];

  function toggleFilter(key, val) {
    setFilters(f => {
      const next = { ...f };
      if (next[key] === val) delete next[key];
      else next[key] = val;
      return next;
    });
    setPage(0);
  }

  function toggleSort(col) {
    if (sortCol() === col) {
      if (!sortAsc()) { setSortCol(null); setSortAsc(true); }
      else setSortAsc(false);
    } else {
      setSortCol(col);
      setSortAsc(true);
    }
    setPage(0);
  }

  function clearFilters() { setFilters({}); setPage(0); }

  const isLoading = () => triageResource.loading;

  return (
    <>
      <StatsCards id="triageStats" cards={stats} />
      <Show when={Object.keys(filters()).length > 0}>
        <div class="filter-bar">
          <span class="filter-label">Filters:</span>
          {Object.entries(filters()).map(([k, v]) => (
            <span class="filter-chip" onClick={() => toggleFilter(k, v)}>
              {k}: {v} <span class="x">&times;</span>
            </span>
          ))}
          <button class="btn-clear-filters" onClick={clearFilters}>Clear all</button>
        </div>
      </Show>
      <Show when={triageResource()?.error}>
        <div class="error-bar">{triageResource().error}</div>
      </Show>
      <Show when={!isLoading() || items().length > 0} fallback={
        <div class="loading-state">
          <div class="spinner"></div>
          <p>Fetching triage data from microsoft/apm...</p>
        </div>
      }>
        <Show when={items().length > 0} fallback={
          <div class="empty">
            <p>No triaged issues found.</p>
            <p style={{ "font-size": "12px", "margin-top": "8px" }}>
              Triage comments use a <code>triage-decision</code> JSON block posted by the apm-triage-panel agent.
            </p>
          </div>
        }>
          <TriageTable
            items={paged()}
            onFilter={toggleFilter}
            onSort={toggleSort}
            sortCol={sortCol}
            sortAsc={sortAsc}
            onDetail={(item) => setDetailItem(item)}
          />
          <Pagination
            page={page}
            pageSize={pageSize}
            total={() => filtered().length}
            onPageChange={setPage}
            onPageSizeChange={(s) => { setPageSize(s); setPage(0); }}
          />
        </Show>
      </Show>
      <Show when={detailItem() !== null}>
        <TriageDetail item={detailItem()} onClose={() => setDetailItem(null)} />
      </Show>
    </>
  );
}
