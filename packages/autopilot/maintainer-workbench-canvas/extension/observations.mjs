const time = value => Date.parse(value || "") || 0;
const changedAt = record => time(record?.data?.updatedAt || record?.updatedAt);
const readAt = record => Math.max(time(record?.observedAt), time(record?.attemptedAt));

export function newerObservation(a, b) {
  if (!a) return b;
  if (!b) return a;
  if (changedAt(a) !== changedAt(b)) return changedAt(a) > changedAt(b) ? a : b;
  return readAt(a) > readAt(b) ? a : b;
}

function mergePull(a, b) {
  const winner = newerObservation(a, b);
  const head = winner?.data?.headRefOid;
  const candidates = [a, b].filter(r => r?.data?.evidence?.head === head);
  if (candidates.length < 2) return winner;
  const checks = candidates.reduce((x, y) => time(x.data.evidence.observedAt) >= time(y.data.evidence.observedAt) ? x : y).data.evidence;
  const runs = candidates.reduce((x, y) => time(x.data.evidence.runs?.observedAt) >= time(y.data.evidence.runs?.observedAt) ? x : y).data.evidence.runs;
  return { ...winner, data: { ...winner.data, evidence: { ...checks, runs } } };
}

function mergeRecords(current = {}, incoming = {}, choose = newerObservation) {
  const result = { ...current };
  for (const [key, record] of Object.entries(incoming)) result[key] = choose(result[key], record);
  return result;
}

// A selected read can finish during portfolio assembly. Never roll that observation back.
export function mergeObservations(current, incoming, portfolio = false) {
  const result = portfolio ? { ...incoming } : { ...current };
  for (const key of ["details", "pulls", "discussions"]) result[key] = mergeRecords(current[key], incoming[key], key === "pulls" ? mergePull : newerObservation);
  if (incoming.lifecycle) {
    const latest = time(current.lifecycle?.actor?.observedAt) > time(incoming.lifecycle.actor?.observedAt) ? current.lifecycle : incoming.lifecycle;
    result.lifecycle = { ...(portfolio ? incoming.lifecycle : current.lifecycle),
      actor: latest.actor, trustedSha: latest.trustedSha,
      issues: mergeRecords(current.lifecycle?.issues, incoming.lifecycle.issues) };
    if (!portfolio) {
      result.lifecycle.observedAt = current.lifecycle?.observedAt;
      result.lifecycle.complete = current.lifecycle?.complete;
    }
  }
  return result;
}

export const CHECK_INTERVAL = 10000;
export const isoNow = () => new Date().toISOString();
