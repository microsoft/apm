import { For } from "solid-js";

export default function StatsCards(props) {
  return (
    <div class="stats" id={props.id}>
      <For each={props.cards}>
        {(card) => (
          <div class="stat-card">
            <div class="num" style={card.color ? { color: card.color } : {}}>{card.value()}</div>
            <div class="label">{card.label}</div>
          </div>
        )}
      </For>
    </div>
  );
}
