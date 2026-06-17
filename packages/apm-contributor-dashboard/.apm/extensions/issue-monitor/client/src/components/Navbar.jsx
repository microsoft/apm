import { permissionsResource } from "../stores/permissions";

export default function Navbar(props) {
  const accessLevel = () => {
    const p = permissionsResource();
    if (!p) return null;
    if (p.admin) return "admin";
    if (p.maintain) return "maintain";
    if (p.push) return "write";
    if (p.triage) return "triage";
    return "read";
  };

  return (
    <nav class="navbar">
      <div class="navbar-left">
        <svg class="navbar-logo" viewBox="0 0 23 23" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="10" height="10" fill="#f25022"/>
          <rect x="12" y="1" width="10" height="10" fill="#7fba00"/>
          <rect x="1" y="12" width="10" height="10" fill="#00a4ef"/>
          <rect x="12" y="12" width="10" height="10" fill="#ffb900"/>
        </svg>
        <div class="navbar-divider"></div>
        <div>
          <div class="navbar-title">APM Contributor Dashboard</div>
        </div>
      </div>
      <div class="navbar-right">
        {accessLevel() && <span class={`access-badge access-${accessLevel()}`}>{accessLevel()}</span>}
        <a class="navbar-repo" href="https://github.com/microsoft/apm" target="_blank">
          <svg width="14" height="14" viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
          microsoft/apm
        </a>
        <button class="btn-refresh" onClick={props.onRefresh}>Refresh</button>
      </div>
    </nav>
  );
}
