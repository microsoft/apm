import { createSignal, Show } from "solid-js";

export default function Toast() {
  let timeout;
  const [message, setMessage] = createSignal("");
  const [visible, setVisible] = createSignal(false);

  function show(msg) {
    setMessage(msg);
    setVisible(true);
    clearTimeout(timeout);
    timeout = setTimeout(() => setVisible(false), 3000);
  }

  // Expose show via window for imperative use
  window.__toast = show;

  return (
    <div class={`toast ${visible() ? "show" : ""}`}>{message()}</div>
  );
}

export function showToast(msg) {
  if (window.__toast) window.__toast(msg);
}
