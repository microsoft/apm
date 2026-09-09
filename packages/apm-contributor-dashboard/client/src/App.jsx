import { createSignal, Show, onMount } from "solid-js";
import Navbar from "./components/Navbar";
import TabBar from "./components/TabBar";
import IssuesTab from "./components/issues/IssuesTab";
import PrsTab from "./components/prs/PrsTab";
import TriageTab from "./components/triage/TriageTab";
import Toast from "./components/Toast";
import { issueResource, refetchIssues } from "./stores/issues";
import { prResource, refetchPrs } from "./stores/prs";
import { triageResource, refetchTriage, activateTriage } from "./stores/triage";
import { refreshData } from "./services/api";
import { formatRefreshInterval, refreshIntervalMs } from "./config";

export default function App() {
  const [activeTab, setActiveTab] = createSignal("issues");

  const tabs = [
    { id: "triage", label: "Triaged Issues", count: () => triageResource()?.items?.length || 0 },
    { id: "issues", label: "Issues", count: () => issueResource()?.issues?.length || 0 },
    { id: "prs", label: "Pull Requests", count: () => prResource()?.prs?.length || 0 },
  ];

  onMount(() => {
    if (activeTab() === "triage") activateTriage();
  });

  function handleTabSwitch(id) {
    if (id === "triage") activateTriage();
    setActiveTab(id);
  }

  async function handleRefresh() {
    try {
      await refreshData();
    } finally {
      refetchIssues();
      refetchPrs();
      if (activeTab() === "triage") refetchTriage();
    }
  }

  const lastUpdated = () => issueResource()?.lastUpdated || prResource()?.lastUpdated;

  return (
    <>
      <Navbar onRefresh={handleRefresh} />
      <div class="subtitle">
        <span class="live-dot"></span>
        {lastUpdated() ? `Live -- last fetched ${lastUpdated()} (auto-refresh ${formatRefreshInterval(refreshIntervalMs)})` : "Connecting to GitHub..."}
      </div>
      <Show when={issueResource()?.error}>
        <div class="error-bar">{issueResource().error}</div>
      </Show>
      <TabBar tabs={tabs} active={activeTab} onSwitch={handleTabSwitch} />
      <Show when={activeTab() === "issues"}>
        <IssuesTab />
      </Show>
      <Show when={activeTab() === "prs"}>
        <PrsTab />
      </Show>
      <Show when={activeTab() === "triage"}>
        <TriageTab />
      </Show>
      <Toast />
    </>
  );
}
