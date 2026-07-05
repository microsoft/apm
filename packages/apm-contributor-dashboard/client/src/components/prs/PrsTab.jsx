import { createSignal, createMemo, Show } from "solid-js";
import { prResource, refetchPrs } from "../../stores/prs";
import { runPanel, rerunCi } from "../../services/api";
import { showToast } from "../Toast";
import StatsCards from "../StatsCards";
import PrTable from "./PrTable";
import PrDetail from "./PrDetail";
import Pagination from "../Pagination";

export default function PrsTab() {
  const [page, setPage] = createSignal(0);
  const [pageSize, setPageSize] = createSignal(25);
  const [filters, setFilters] = createSignal({});
  const [detailPr, setDetailPr] = createSignal(null);
  const [selectedPrs, setSelectedPrs] = createSignal(new Set());

  const prs = () => prResource()?.prs || [];

  const filtered = createMemo(() => {
    const f = filters();
    return prs().filter(pr => {
      for (const [key, val] of Object.entries(f)) {
        if (key === "author" && pr.author !== val) return false;
        if (key === "pipeline" && pr.pipeline?.status !== val) return false;
        if (key === "panel" && pr.panel?.status !== val) return false;
        if (key === "prstatus" && pr.prStatus !== val) return false;
      }
      return true;
    });
  });

  const paged = createMemo(() => {
    const start = page() * pageSize();
    return filtered().slice(start, start + pageSize());
  });

  const stats = [
    { label: "Open PRs", color: undefined, value: () => prs().length },
    { label: "Ready", color: "#3fb950", value: () => prs().filter(p => p.prStatus === "ready-to-merge").length },
    { label: "Review Pending", color: "#58a6ff", value: () => prs().filter(p => p.prStatus === "review-pending").length },
    { label: "CI Green", color: "#3fb950", value: () => prs().filter(p => p.pipeline?.status === "green").length },
    { label: "CI Failing", color: "#f85149", value: () => prs().filter(p => p.pipeline?.status === "red").length },
    { label: "Panel", color: "#bc8cff", value: () => prs().filter(p => p.panel?.status !== "none").length },
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

  function clearFilters() { setFilters({}); setPage(0); }

  function toggleSelect(n) {
    setSelectedPrs(s => {
      const next = new Set(s);
      if (next.has(n)) next.delete(n); else next.add(n);
      return next;
    });
  }

  function togglePage(numbers) {
    setSelectedPrs(s => {
      const next = new Set(s);
      const allSelected = numbers.every(n => next.has(n));
      if (allSelected) numbers.forEach(n => next.delete(n));
      else numbers.forEach(n => next.add(n));
      return next;
    });
  }

  function clearSelection() { setSelectedPrs(new Set()); }

  async function runBulkCi() {
    const nums = [...selectedPrs()];
    showToast(`Re-running CI on ${nums.length} PR${nums.length > 1 ? "s" : ""}...`);
    await Promise.all(nums.map(n => rerunCi(n).catch(() => {})));
    showToast("CI re-run triggered for selected PRs");
  }

  async function runBulkPanel() {
    const nums = [...selectedPrs()];
    showToast(`Triggering panel on ${nums.length} PR${nums.length > 1 ? "s" : ""}...`);
    await Promise.all(nums.map(n => runPanel(n).catch(() => {})));
    showToast("Panel review triggered for selected PRs");
  }

  return (
    <>
      <StatsCards id="prStats" cards={stats} />
      <Show when={selectedPrs().size > 0}>
        <div class="bulk-action-bar">
          <span class="bulk-count">{selectedPrs().size} PR{selectedPrs().size > 1 ? "s" : ""} selected</span>
          <button class="btn btn-sm" onClick={runBulkCi}>Run CI</button>
          <button class="btn btn-sm btn-purple" onClick={runBulkPanel}>Run Panel</button>
          <button class="btn-clear-filters" onClick={clearSelection}>Clear</button>
        </div>
      </Show>
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
      <Show when={!prResource.loading || prs().length > 0} fallback={
        <div class="loading-state">
          <div class="spinner"></div>
          <p>Fetching pull requests from microsoft/apm...</p>
        </div>
      }>
        <Show when={prs().length > 0} fallback={<div class="empty">No open pull requests found.</div>}>
          <PrTable
            prs={paged()}
            selected={selectedPrs}
            onToggle={toggleSelect}
            onTogglePage={togglePage}
            onFilter={toggleFilter}
            onDetail={(pr) => setDetailPr(pr)}
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
      <Show when={detailPr() !== null}>
        <PrDetail pr={detailPr()} onClose={() => setDetailPr(null)} />
      </Show>
    </>
  );
}
