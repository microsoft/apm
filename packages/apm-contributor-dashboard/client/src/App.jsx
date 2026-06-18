import { createSignal, Show } from "solid-js";
import Navbar from "./components/Navbar";
import TabBar from "./components/TabBar";
import IssuesTab from "./components/issues/IssuesTab";
import PrsTab from "./components/prs/PrsTab";
import Toast from "./components/Toast";
import { issueResource, refetchIssues } from "./stores/issues";
import { prResource, refetchPrs } from "./stores/prs";

export default function App() {
  const [activeTab, setActiveTab] = createSignal("issues");

  const tabs = [
    { id: "issues", label: "Issues", count: () => issueResource()?.issues?.length || 0 },
    { id: "prs", label: "Pull Requests", count: () => prResource()?.prs?.length || 0 },
  ];

  function handleRefresh() {
    refetchIssues();
    refetchPrs();
  }

  const lastUpdated = () => issueResource()?.lastUpdated || prResource()?.lastUpdated;

  return (
    <>
      <Navbar onRefresh={handleRefresh} />
      <div class="subtitle">
        <span class="live-dot"></span>
        {lastUpdated() ? `Live -- last fetched ${lastUpdated()} (auto-refresh 30s)` : "Connecting to GitHub..."}
      </div>
      <Show when={issueResource()?.error}>
        <div class="error-bar">{issueResource().error}</div>
      </Show>
      <TabBar tabs={tabs} active={activeTab} onSwitch={setActiveTab} />
      <Show when={activeTab() === "issues"}>
        <IssuesTab />
      </Show>
      <Show when={activeTab() === "prs"}>
        <PrsTab />
      </Show>
      <Toast />
    </>
  );
}
