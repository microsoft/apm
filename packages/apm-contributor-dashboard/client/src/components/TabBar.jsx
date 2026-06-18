import { For } from "solid-js";

export default function TabBar(props) {
  return (
    <div class="tab-bar">
      <For each={props.tabs}>
        {(tab) => (
          <button
            class={`tab-btn ${props.active() === tab.id ? "active" : ""}`}
            onClick={() => props.onSwitch(tab.id)}
          >
            {tab.label}
            <span class="tab-count">{tab.count()}</span>
          </button>
        )}
      </For>
    </div>
  );
}
