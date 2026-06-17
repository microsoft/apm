import { createSignal, createMemo, Show } from "solid-js";
import { prResource, refetchPrs } from "../../stores/prs";
import StatsCards from "../StatsCards";
import PrTable from "./PrTable";
import PrDetail from "./PrDetail";
import Pagination from "../Pagination";

export default function PrsTab() {
  const [page, setPage] = createSignal(0);
  const [pageSize, setPageSize] = createSignal(25);
  const [filters, setFilters] = createSignal({});
  const [detailPr, setDetailPr] = createSignal(null);

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

  return (
    <>
      <StatsCards id="prStats" cards={stats} />
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
      <Show when={prs().length > 0} fallback={<div class="empty">Loading pull requests...</div>}>
        <PrTable prs={paged()} onFilter={toggleFilter} onDetail={(pr) => setDetailPr(pr)} />
        <Pagination
          page={page}
          pageSize={pageSize}
          total={() => filtered().length}
          onPageChange={setPage}
          onPageSizeChange={(s) => { setPageSize(s); setPage(0); }}
        />
      </Show>
      <Show when={detailPr() !== null}>
        <PrDetail pr={detailPr()} onClose={() => setDetailPr(null)} />
      </Show>
    </>
  );
}
