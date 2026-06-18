import { createSignal, createMemo, Show } from "solid-js";
import { issueResource, refetchIssues } from "../../stores/issues";
import StatsCards from "../StatsCards";
import IssueTable from "./IssueTable";
import IssueDetail from "./IssueDetail";
import Pagination from "../Pagination";

const prioOrder = { P0: 0, P1: 1, P2: 2, P3: 3 };
const typeOrder = { bug: 0, feature: 1, chore: 2 };

function defaultSort(a, b) {
  const p = (prioOrder[a.priority] ?? 9) - (prioOrder[b.priority] ?? 9);
  if (p !== 0) return p;
  const t = (typeOrder[a.type] ?? 9) - (typeOrder[b.type] ?? 9);
  if (t !== 0) return t;
  return a.number - b.number;
}

export default function IssuesTab() {
  const [page, setPage] = createSignal(0);
  const [pageSize, setPageSize] = createSignal(25);
  const [filters, setFilters] = createSignal({});
  const [sortCol, setSortCol] = createSignal(null);
  const [sortAsc, setSortAsc] = createSignal(true);
  const [detailIssue, setDetailIssue] = createSignal(null);

  const issues = () => issueResource()?.issues || [];

  const filtered = createMemo(() => {
    const f = filters();
    return issues().filter(i => {
      for (const [key, val] of Object.entries(f)) {
        if (key === "priority" && i.priority !== val) return false;
        if (key === "type" && i.type !== val) return false;
        if (key === "author" && i.author !== val) return false;
        if (key === "status" && i.status !== val) return false;
        if (key === "prStatus") {
          const ps = i.pr ? i.pr.prStatus : "none";
          if (ps !== val) return false;
        }
      }
      return true;
    });
  });

  const sorted = createMemo(() => {
    const col = sortCol();
    const items = [...filtered()];
    if (!col) return items.sort(defaultSort);
    const dir = sortAsc() ? 1 : -1;
    return items.sort((a, b) => {
      if (col === "priority") return dir * ((prioOrder[a.priority] ?? 9) - (prioOrder[b.priority] ?? 9));
      if (col === "type") return dir * ((typeOrder[a.type] ?? 9) - (typeOrder[b.type] ?? 9));
      if (col === "number") return dir * (a.number - b.number);
      const va = (a[col] || "").toString().toLowerCase();
      const vb = (b[col] || "").toString().toLowerCase();
      return dir * va.localeCompare(vb);
    });
  });

  const paged = createMemo(() => {
    const start = page() * pageSize();
    return sorted().slice(start, start + pageSize());
  });

  const stats = [
    { label: "Open", color: undefined, value: () => issues().length },
    { label: "Bugs", color: "#f85149", value: () => issues().filter(i => i.type === "bug").length },
    { label: "Features", color: "#3fb950", value: () => issues().filter(i => i.type === "feature").length },
    { label: "Chores", color: "#8b949e", value: () => issues().filter(i => i.type === "chore").length },
    { label: "P0/P1", color: "#d29922", value: () => issues().filter(i => i.priority === "P0" || i.priority === "P1").length },
    { label: "With PR", color: "#3fb950", value: () => issues().filter(i => i.pr).length },
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

  function clearFilter(key) {
    setFilters(f => {
      const next = { ...f };
      delete next[key];
      return next;
    });
    setPage(0);
  }

  function clearFilters() { setFilters({}); setPage(0); }

  return (
    <>
      <StatsCards id="issueStats" cards={stats} />
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
      <Show when={!issueResource.loading || issues().length > 0} fallback={
        <div class="loading-state">
          <div class="spinner"></div>
          <p>Fetching issues from microsoft/apm...</p>
        </div>
      }>
        <Show when={issues().length > 0} fallback={<div class="empty">No open issues found.</div>}>
          <IssueTable issues={paged()} onFilter={toggleFilter} onSort={toggleSort} sortCol={sortCol} sortAsc={sortAsc} onDetail={(issue) => setDetailIssue(issue)} />
          <Pagination
            page={page}
            pageSize={pageSize}
            total={() => filtered().length}
            onPageChange={setPage}
            onPageSizeChange={(s) => { setPageSize(s); setPage(0); }}
          />
        </Show>
      </Show>
      <Show when={detailIssue() !== null}>
        <IssueDetail issue={detailIssue()} onClose={() => setDetailIssue(null)} />
      </Show>
    </>
  );
}
