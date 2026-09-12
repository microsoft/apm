'use strict';

const MARKER = '<!-- apm-scope:v1 -->';
const RESET = '<!-- apm-governance-reset:2026-09-12:acceptance -->';
const REMITS = new Set(['project', 'registry-public-api']);

class EvidenceError extends Error {}

function requireValue(condition, message) {
  if (!condition) throw new EvidenceError(message);
}

function timestamp(value) {
  requireValue(typeof value === 'string' && Number.isFinite(Date.parse(value)),
    'Missing or invalid evidence timestamp');
  return Date.parse(value);
}

function readPolicy(markdown) {
  requireValue(typeof markdown === 'string', 'Governance policy unavailable');
  const blocks = [...markdown.matchAll(
    /<!-- apm-scope-roster:start -->([\s\S]*?)<!-- apm-scope-roster:end -->/g)];
  requireValue(blocks.length === 1, 'Expected one governance roster');
  const roster = new Map();
  for (const line of blocks[0][1].split('\n')) {
    if (!line.includes('https://github.com/')) continue;
    const cells = line.split('|').map(cell => cell.trim());
    requireValue(cells.length === 6, 'Malformed governance roster row');
    const link = /^\[[^\]]+\]\(https:\/\/github\.com\/([a-zA-Z0-9-]+)\)$/.exec(cells[1]);
    const remit = /^`([^`]+)`$/.exec(cells[4]);
    requireValue(link && remit && REMITS.has(remit[1]), 'Invalid governance identity or remit');
    const login = link[1].toLowerCase();
    requireValue(!roster.has(login), 'Duplicate governance identity');
    roster.set(login, remit[1]);
  }
  requireValue(roster.size > 0 && [...roster.values()].includes('project'),
    'Incomplete governance roster');
  const floors = [...markdown.matchAll(/<!-- apm-scope-reset-before:([^ ]+) -->/g)];
  requireValue(floors.length === 1, 'Expected one governance reset floor');
  return { roster, resetBefore: timestamp(floors[0][1]) };
}

function isResponsible(policy, user, area) {
  if (!user || user.type !== 'User' || typeof user.login !== 'string' || !REMITS.has(area)) {
    return false;
  }
  const remit = policy.roster.get(user.login.toLowerCase());
  return remit === 'project' || remit === area;
}

function parseRecord(body) {
  if (typeof body !== 'string' || !body.trimStart().startsWith(MARKER)) return null;
  const lines = body.trim().split(/\r?\n/);
  if (lines.shift() !== MARKER) return null;
  const record = {};
  const fields = new Set(['Decision', 'Area', 'Scope', 'Done when', 'Out of scope', 'Review contact', 'Reason']);
  for (const line of lines) {
    if (!line.trim()) continue;
    const match = /^([A-Za-z ]+): ([^\r\n]+)$/.exec(line);
    if (!match || !fields.has(match[1]) || Object.hasOwn(record, match[1])) return null;
    record[match[1]] = match[2].trim();
    if (!record[match[1]]) return null;
  }
  if (!REMITS.has(record.Area)) return null;
  if (record.Decision === 'withdraw') return record.Reason ? record : null;
  if (record.Decision !== 'approve' || !record.Scope || !record['Done when']
      || !/^@[a-zA-Z0-9-]+$/.test(record['Review contact'] || '')) return null;
  return record;
}

function commentReference(value, repository) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.origin !== 'https://github.com' || url.username || url.password || url.search) return null;
  const parts = url.pathname.split('/');
  const id = /^#issuecomment-([1-9]\d*)$/.exec(url.hash);
  if (parts.length !== 5 || parts.slice(1, 3).join('/').toLowerCase() !== repository.toLowerCase()
      || parts[3] !== 'issues' || !/^[1-9]\d*$/.test(parts[4]) || !id) return null;
  const issue = Number(parts[4]);
  const comment = Number(id[1]);
  return Number.isSafeInteger(issue) && Number.isSafeInteger(comment) ? { issue, comment } : null;
}

function evidence(state, reason, extra = {}) {
  return { state, reason, authorizes_implementation: false, ...extra };
}

function evaluateIssue({ policy, repository, issue, comments, approvalUrl, changed = false }) {
  requireValue(issue && Number.isSafeInteger(issue.number) && !issue.pull_request,
    'Scope target is not an issue');
  requireValue(Array.isArray(comments), 'Complete issue comment history required');
  if (changed) return evidence('needs-evidence', 'A decision or its context was edited/deleted; obtain fresh human confirmation.');
  if (issue.state !== 'open') {
    return evidence('manual-review', 'The linked issue is closed; a human must confirm any remaining scope.');
  }
  const nominated = approvalUrl ? commentReference(approvalUrl, repository) : null;
  if (approvalUrl && (!nominated || nominated.issue !== issue.number)) {
    return evidence('needs-evidence', 'Approval must reference a comment on this issue in the target repository.');
  }
  let resetBefore = policy.resetBefore;
  const withdrawals = [];
  const uncertain = [];
  const selected = nominated ? comments.find(comment => comment.id === nominated.comment) : null;
  const selectedArea = parseRecord(selected?.body)?.Area;
  for (const comment of comments) {
    requireValue(comment && Number.isSafeInteger(comment.id) && typeof comment.body === 'string'
      && comment.user && typeof comment.user.type === 'string', 'Incomplete issue comment');
    const body = comment.body;
    const record = parseRecord(body);
    if (body.includes(RESET) && isResponsible(policy, comment.user, 'project')) {
      resetBefore = Math.max(resetBefore, timestamp(comment.created_at));
    }
    const rosteredHuman = comment.user.type === 'User'
      && policy.roster.has(comment.user.login?.toLowerCase());
    if (!rosteredHuman) continue;
    if (record?.Decision === 'withdraw'
        && isResponsible(policy, comment.user, record.Area)
        && isResponsible(policy, comment.user, selectedArea || record.Area)) withdrawals.push(comment);
    if (body.includes(MARKER) && (!record || timestamp(comment.created_at) !== timestamp(comment.updated_at))) {
      uncertain.push(comment);
    }
  }
  const selectedTime = selected ? timestamp(selected.created_at) : 0;
  if (withdrawals.some(comment => timestamp(comment.created_at) >= selectedTime)) {
    return evidence('withdrawn', 'A responsible human withdrawal supersedes the nominated evidence.');
  }
  if (uncertain.some(comment => Math.max(timestamp(comment.created_at), timestamp(comment.updated_at)) >= selectedTime)) {
    return evidence('needs-evidence', 'A human decision record is malformed or edited; do not infer approval.');
  }
  if (!selected) {
    return evidence('needs-evidence', nominated
      ? 'The nominated approval comment is unavailable; no fallback to older approvals.'
      : 'Link an explicit human scope comment; labels, reviews and informal agreement are not evidence.');
  }
  if (selectedTime <= resetBefore) {
    return evidence('withdrawn', 'The acceptance reset supersedes this record; fresh human evidence is needed.');
  }
  const record = parseRecord(selected.body);
  if (!record || record.Decision !== 'approve' || !isResponsible(policy, selected.user, record.Area)
      || timestamp(selected.updated_at) !== selectedTime) {
    return evidence('needs-evidence', 'The nominated comment is not an unedited responsible-human approval record.');
  }
  const contact = record['Review contact'].slice(1).toLowerCase();
  if (!isResponsible(policy, { login: contact, type: 'User' }, record.Area)) {
    return evidence('needs-evidence', 'The review contact is outside the recorded approval remit.');
  }
  return evidence('record-present',
    'Record present, not permission. Confirm scope match, review capacity and current approval with a responsible human.',
    { issue: issue.number, comment: selected.id, area: record.Area,
      approver: selected.user.login, review_contact: contact,
      contact_confirmation_needed: contact !== selected.user.login.toLowerCase() });
}

module.exports = { MARKER, RESET, readPolicy, isResponsible, parseRecord, commentReference,
  evaluateIssue, evidence, requireValue, EvidenceError };
