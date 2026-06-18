import { createResource } from "solid-js";

async function fetchPermissions() {
  const res = await fetch("/api/permissions");
  if (!res.ok) return { pull: true, triage: false, push: false, maintain: false, admin: false };
  return res.json();
}

const [permissionsResource] = createResource(fetchPermissions);

// Convenience accessors (safe defaults: deny when unknown)
export function canWrite() {
  const p = permissionsResource();
  return p?.push || p?.maintain || p?.admin || false;
}

export function canTriage() {
  const p = permissionsResource();
  return p?.triage || p?.push || p?.maintain || p?.admin || false;
}

export { permissionsResource };
