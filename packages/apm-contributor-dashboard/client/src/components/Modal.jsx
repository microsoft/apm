import { Show } from "solid-js";

export default function Modal(props) {
  return (
    <div class={`modal-overlay ${props.open() ? "open" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) props.onClose(); }}>
      <div class="modal">
        <div class="modal-header">
          <h2>{props.title()}</h2>
          <Show when={props.onRefresh}>
            <button class="modal-refresh" onClick={props.onRefresh}>Refresh</button>
          </Show>
          <button class="modal-close" onClick={props.onClose}>&times;</button>
        </div>
        <div class="modal-body">
          {props.children}
        </div>
        <Show when={props.footer}>
          <div class="modal-footer">
            {props.footer}
          </div>
        </Show>
      </div>
    </div>
  );
}
