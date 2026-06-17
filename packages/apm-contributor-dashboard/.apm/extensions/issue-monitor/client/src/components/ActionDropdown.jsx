import { createSignal, Show, onCleanup } from "solid-js";

export default function ActionDropdown(props) {
  const [open, setOpen] = createSignal(false);
  let ref;

  function handleDocClick(e) {
    if (ref && !ref.contains(e.target)) setOpen(false);
  }

  // Close on outside click
  document.addEventListener("click", handleDocClick);
  onCleanup(() => document.removeEventListener("click", handleDocClick));

  return (
    <div class="action-dropdown" ref={ref}>
      <button class="btn-details" onClick={() => props.onDetails()}>Details</button>
      <Show when={props.items && props.items.length > 0}>
        <button class="btn-dropdown" onClick={(e) => { e.stopPropagation(); setOpen(!open()); }}>...</button>
        <div class={`dropdown-menu ${open() ? "open" : ""}`}>
          {props.items.map(item => (
            <div class={`dropdown-item ${item.class || ""}`} onClick={() => { setOpen(false); item.action(); }}>
              {item.label}
            </div>
          ))}
        </div>
      </Show>
    </div>
  );
}
