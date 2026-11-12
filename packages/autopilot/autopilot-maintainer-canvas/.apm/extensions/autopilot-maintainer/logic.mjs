export const REPO = "microsoft/apm";

export const LABEL = {
    accepted: "status/accepted",
    deferred: "status/deferred",
    needsTriage: "status/needs-triage",
    needsDesign: "status/needs-design",
    advisory: "triage/recommended",
    advisoryLegacy: "status/triaged",
    panelReview: "panel-review",
};

const FORBIDDEN_GH_FLAGS = [
    "--add-assignee",
    "--remove-assignee",
    "--add-reviewer",
    "--remove-reviewer",
    "merge",
    "ready",
];

export function labelNames(labels) {
    return (labels || []).map((entry) => {
        if (typeof entry === "string") return entry;
        return entry && typeof entry.name === "string" ? entry.name : "";
    }).filter(Boolean);
}

export function hasLabel(names, label) {
    return names.includes(label);
}

export function parseItemNumber(value) {
    const number = typeof value === "number" ? value : Number(value);
    if (!Number.isInteger(number) || number < 1 || number > 999_999_999) {
        return null;
    }
    return number;
}

function pendingLane(accepted, triageRan) {
    if (accepted) return null;
    return triageRan ? "decide" : "new";
}

function classifyLanes({ accepted, deferred, triageRan }) {
    if (deferred) return ["deferred"];
    if (accepted) return ["accepted"];
    const pending = pendingLane(false, triageRan);
    return pending ? [pending] : [];
}

export function classifyIssue(raw) {
    const labels = labelNames(raw.labels);
    const accepted = hasLabel(labels, LABEL.accepted);
    const deferred = hasLabel(labels, LABEL.deferred);
    const triageRan = hasLabel(labels, LABEL.advisory) || hasLabel(labels, LABEL.advisoryLegacy);
    return {
        kind: "issue",
        number: raw.number,
        title: raw.title || "",
        url: raw.url || "",
        author: raw.author?.login || raw.author || "",
        labels,
        accepted,
        deferred,
        advisory: triageRan,
        needsTriage: hasLabel(labels, LABEL.needsTriage),
        needsDesign: hasLabel(labels, LABEL.needsDesign),
        panelReview: false,
        isDraft: false,
        triageRan,
        triageConclusion: raw.triageConclusion || null,
        linkedPrs: [],
        hasOpenPr: false,
        lanes: classifyLanes({ accepted, deferred, triageRan }),
    };
}

export function closingIssueNumbers(raw) {
    const fromApi = (raw.closingIssuesReferences || [])
        .map((entry) => (entry && typeof entry.number === "number" ? entry.number : Number(entry && entry.number)))
        .filter((number) => Number.isInteger(number) && number > 0);
    const fromBody = [];
    const body = String(raw.body || "");
    const pattern = /\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)/gi;
    let match = pattern.exec(body);
    while (match) {
        const number = Number(match[1]);
        if (Number.isInteger(number) && number > 0) fromBody.push(number);
        match = pattern.exec(body);
    }
    return [...new Set([...fromApi, ...fromBody])];
}

export function attachLinkedPrs(issues, prs) {
    const byIssue = new Map();
    for (const pr of prs || []) {
        for (const number of pr.closesIssues || []) {
            if (!byIssue.has(number)) byIssue.set(number, []);
            byIssue.get(number).push({
                number: pr.number,
                title: pr.title || "",
                url: pr.url || "",
                isDraft: Boolean(pr.isDraft),
                accepted: Boolean(pr.accepted),
                deferred: Boolean(pr.deferred),
                panelReview: Boolean(pr.panelReview),
                triageRan: Boolean(pr.triageRan),
                triageConclusion: pr.triageConclusion || null,
                labels: pr.labels || [],
            });
        }
    }
    for (const issue of issues || []) {
        const linkedPrs = byIssue.get(issue.number) || [];
        issue.linkedPrs = linkedPrs;
        issue.hasOpenPr = linkedPrs.length > 0;
    }
    return issues;
}

export function canSpawnIssueDelivery(item) {
    return Boolean(item && item.kind === "issue" && item.accepted && !item.hasOpenPr);
}

export function prClosesListedIssue(pr, issues) {
    const listed = new Set((issues || []).map((issue) => issue.number));
    return (pr.closesIssues || []).some((number) => listed.has(number));
}

export function boardItems(issues, prs) {
    const standalone = (prs || []).filter((pr) => !prClosesListedIssue(pr, issues));
    return [...(issues || []), ...standalone];
}

export function classifyPr(raw) {
    const labels = labelNames(raw.labels);
    const accepted = hasLabel(labels, LABEL.accepted);
    const deferred = hasLabel(labels, LABEL.deferred);
    const panelReview = hasLabel(labels, LABEL.panelReview);
    const isDraft = Boolean(raw.isDraft);
    const triageRan = hasLabel(labels, LABEL.advisory) || hasLabel(labels, LABEL.advisoryLegacy);
    const lanes = classifyLanes({ accepted, deferred, triageRan });
    return {
        kind: "pr",
        number: raw.number,
        title: raw.title || "",
        url: raw.url || "",
        author: raw.author?.login || raw.author || "",
        labels,
        accepted,
        deferred,
        advisory: triageRan,
        needsTriage: hasLabel(labels, LABEL.needsTriage),
        needsDesign: hasLabel(labels, LABEL.needsDesign),
        panelReview,
        isDraft,
        triageRan,
        triageConclusion: raw.triageConclusion || null,
        closesIssues: closingIssueNumbers(raw),
        lanes,
    };
}

export const ISSUE_TRIAGE_CONCLUSIONS = [
    "accept",
    "needs-design",
    "decline-with-reason",
    "duplicate-of",
    "defer-later",
    "auto-handle",
];

export const PR_TRIAGE_CONCLUSIONS = [
    "ready-for-review",
    "needs-design",
    "needs-issue",
    "duplicate-of",
    "decline-with-reason",
    "auto-handle",
];

function firstAllowedToken(text, allowed) {
    const lower = String(text || "").toLowerCase();
    return allowed.find((token) => lower.includes(token)) || null;
}

export function parseTriageConclusion(body) {
    const text = String(body || "");
    const prBlock = text.match(/## PR triage recommendation\s*\r?\n+([\s\S]{0,240})/i);
    if (prBlock) {
        return { ran: true, conclusion: firstAllowedToken(prBlock[1], PR_TRIAGE_CONCLUSIONS) };
    }
    const issueBlock = text.match(/## Triage recommendation\s*\r?\n+([\s\S]{0,240})/i);
    if (issueBlock) {
        return { ran: true, conclusion: firstAllowedToken(issueBlock[1], ISSUE_TRIAGE_CONCLUSIONS) };
    }
    if (/apm-(?:pr-)?triage-advisory:/.test(text)) {
        return { ran: true, conclusion: null };
    }
    return null;
}

export function latestTriageAdvice(comments) {
    const bodies = (comments || []).map((entry) => {
        if (typeof entry === "string") return entry;
        return entry && typeof entry.body === "string" ? entry.body : "";
    });
    for (let index = bodies.length - 1; index >= 0; index -= 1) {
        const parsed = parseTriageConclusion(bodies[index]);
        if (parsed) return parsed;
    }
    return null;
}

export function applyTriageAdvice(item, comments) {
    const advice = latestTriageAdvice(comments);
    const triageRan = Boolean(item.advisory || item.triageRan || advice);
    const next = {
        ...item,
        triageRan,
        triageConclusion: advice && advice.conclusion ? advice.conclusion : item.triageConclusion || null,
    };
    if (next.deferred) {
        next.lanes = ["deferred"];
    } else if (!next.accepted) {
        const rest = (next.lanes || []).filter((lane) => lane !== "new" && lane !== "decide" && lane !== "deferred");
        next.lanes = [triageRan ? "decide" : "new", ...rest];
    }
    return next;
}

function schedulerCard(spec) {
    return {
        skill: spec.skill,
        mode: "run",
        subject: REPO,
        path: spec.path,
        intent: spec.intent,
        origin: "actor-session",
        write: "off",
        worker_write: "on",
        repo: REPO,
        fanout_limit: 2,
        invocation: "actor-session",
        queue: spec.queue,
        compose: "never",
        ...spec.extra,
    };
}

function workerCard(spec, number) {
    const idKey = spec.itemKind === "pr" ? "pr" : "issue";
    return {
        skill: spec.skill,
        mode: "run",
        subject: `${REPO}#${number}`,
        path: spec.path,
        intent: spec.intent,
        origin: "actor-session",
        write: "on",
        repo: REPO,
        [idKey]: number,
        invocation: "actor-session",
        compose: "never",
        ...spec.extra,
    };
}

export function formatActivationCard(fields) {
    return Object.entries(fields)
        .filter(([, value]) => value != null && value !== "")
        .map(([key, value]) => `${key}: ${String(value).replace(/`/g, "")}`)
        .join("\n");
}

export const SPAWN_CATALOG = {
    "sweep-issue-triage": {
        id: "sweep-issue-triage",
        role: "scheduler",
        skill: "autopilot-issue-triage-scheduler",
        sessionName: "Issue triage scheduler",
        target: "scheduler:issue-triage",
        needsNumber: false,
        requiresConfirm: false,
        card: schedulerCard({
            skill: "autopilot-issue-triage-scheduler",
            path: "triage",
            intent: "select issues and fan out triage workers",
            queue: "queue-all",
            extra: { worker_comment_via: "autopilot-comment" },
        }),
    },
    "sweep-issue-delivery": {
        id: "sweep-issue-delivery",
        role: "scheduler",
        skill: "autopilot-issue-delivery-scheduler",
        sessionName: "Issue delivery scheduler",
        target: "scheduler:issue-delivery",
        needsNumber: false,
        requiresConfirm: false,
        card: schedulerCard({
            skill: "autopilot-issue-delivery-scheduler",
            path: "delivery",
            intent: "select accepted issues and fan out delivery workers",
            queue: "status/accepted",
            extra: { triage_recommended: "not-authorization" },
        }),
    },
    "sweep-pr-triage": {
        id: "sweep-pr-triage",
        role: "scheduler",
        skill: "autopilot-pr-triage-scheduler",
        sessionName: "PR triage scheduler",
        target: "scheduler:pr-triage",
        needsNumber: false,
        requiresConfirm: false,
        card: schedulerCard({
            skill: "autopilot-pr-triage-scheduler",
            path: "triage",
            intent: "select PRs and fan out triage workers",
            queue: "queue-open",
            extra: { worker_comment_via: "autopilot-comment" },
        }),
    },
    "sweep-pr-review": {
        id: "sweep-pr-review",
        role: "scheduler",
        skill: "autopilot-pr-review-scheduler",
        sessionName: "PR review scheduler",
        target: "scheduler:pr-review",
        needsNumber: false,
        requiresConfirm: false,
        card: schedulerCard({
            skill: "autopilot-pr-review-scheduler",
            path: "review",
            intent: "select accepted review PRs and fan out review workers",
            queue: "panel-review-union-accepted",
            extra: { drop_unaccepted: "yes", compose_merge: "never" },
        }),
    },
    "worker-issue-triage": {
        id: "worker-issue-triage",
        role: "worker",
        skill: "autopilot-issue-triage-worker",
        itemKind: "issue",
        needsNumber: true,
        requiresConfirm: false,
        sessionNameFor: (number) => `Issue triage #${number}`,
        targetFor: (number) => `worker:issue-triage:${number}`,
        cardFor: (number) => workerCard({
            skill: "autopilot-issue-triage-worker",
            itemKind: "issue",
            path: "triage",
            intent: "advise one already-selected issue",
            extra: { json: "off", debug: "off", comment_via: "autopilot-comment" },
        }, number),
    },
    "worker-issue-delivery": {
        id: "worker-issue-delivery",
        role: "worker",
        skill: "autopilot-issue-delivery-worker",
        itemKind: "issue",
        needsNumber: true,
        requiresConfirm: false,
        sessionNameFor: (number) => `Issue delivery #${number}`,
        targetFor: (number) => `worker:issue-delivery:${number}`,
        cardFor: (number) => workerCard({
            skill: "autopilot-issue-delivery-worker",
            itemKind: "issue",
            path: "delivery",
            intent: "implement one already-selected issue",
            extra: { queue_signal: "not-permission" },
        }, number),
    },
    "worker-pr-triage": {
        id: "worker-pr-triage",
        role: "worker",
        skill: "autopilot-pr-triage-worker",
        itemKind: "pr",
        needsNumber: true,
        requiresConfirm: false,
        sessionNameFor: (number) => `PR triage #${number}`,
        targetFor: (number) => `worker:pr-triage:${number}`,
        cardFor: (number) => workerCard({
            skill: "autopilot-pr-triage-worker",
            itemKind: "pr",
            path: "triage",
            intent: "advise one already-selected PR",
            extra: { json: "off", debug: "off", comment_via: "autopilot-comment" },
        }, number),
    },
    "worker-pr-review": {
        id: "worker-pr-review",
        role: "worker",
        skill: "autopilot-pr-review-worker",
        itemKind: "pr",
        needsNumber: true,
        requiresConfirm: false,
        sessionNameFor: (number) => `PR review #${number}`,
        targetFor: (number) => `worker:pr-review:${number}`,
        cardFor: (number) => workerCard({
            skill: "autopilot-pr-review-worker",
            itemKind: "pr",
            path: "review",
            intent: "advise one already-selected PR",
            extra: {
                debug: "off",
                invocation_mode: "session-review",
                compose_merge: "never",
                "panel-mode": "full",
            },
        }, number),
    },
    "worker-pr-merge": {
        id: "worker-pr-merge",
        role: "worker",
        skill: "autopilot-pr-merge-worker",
        itemKind: "pr",
        needsNumber: true,
        requiresConfirm: true,
        sessionNameFor: (number) => `PR merge #${number}`,
        targetFor: (number) => `worker:pr-merge:${number}`,
        kickoffMode: "plan",
        cardFor: (number) => workerCard({
            skill: "autopilot-pr-merge-worker",
            itemKind: "pr",
            path: "merge",
            intent: "drive one already-selected PR",
            extra: {
                invocation_mode: "composed-implementation-review",
                summoned_by_name: "yes",
                request_implementer_reviewer: "no",
                auto_merge: "no",
                plan_first: "yes",
            },
        }, number),
    },
};

export function resolveSpawn(actionId, number) {
    const spec = SPAWN_CATALOG[actionId];
    if (!spec) {
        throw new Error(`unknown spawn action: ${actionId}`);
    }
    if (spec.needsNumber) {
        const parsed = parseItemNumber(number);
        if (parsed == null) {
            throw new Error("spawn requires a valid issue or PR number");
        }
        const card = spec.cardFor(parsed);
        return {
            ...spec,
            number: parsed,
            sessionName: spec.sessionNameFor(parsed),
            target: spec.targetFor(parsed),
            card,
            kickoff: formatActivationCard(card),
        };
    }
    const card = spec.card;
    return {
        ...spec,
        number: null,
        sessionName: spec.sessionName,
        target: spec.target,
        card,
        kickoff: formatActivationCard(card),
    };
}

export function occupancyKey(target) {
    return String(target || "");
}

const FAMILY_ORDER = [
    "issue-triage",
    "issue-delivery",
    "pr-triage",
    "pr-review",
    "pr-merge",
];

export function occupancyFamily(target) {
    const parts = String(target || "").split(":").filter(Boolean);
    if (parts[0] === "scheduler") {
        return parts.slice(1).join(":") || "unknown";
    }
    if (parts[0] === "worker") {
        if (parts.length >= 3) {
            return parts.slice(1, -1).join(":");
        }
        return parts.slice(1).join(":") || "unknown";
    }
    return String(target || "unknown");
}

export function groupOccupancy(rows) {
    const groups = new Map();
    for (const row of rows || []) {
        const family = occupancyFamily(row.target);
        if (!groups.has(family)) {
            groups.set(family, { family, scheduler: null, workers: [] });
        }
        const group = groups.get(family);
        if (String(row.target || "").startsWith("scheduler:")) {
            group.scheduler = row;
        } else {
            group.workers.push(row);
        }
    }
    return [...groups.values()].sort((left, right) => {
        const leftIndex = FAMILY_ORDER.indexOf(left.family);
        const rightIndex = FAMILY_ORDER.indexOf(right.family);
        return (leftIndex === -1 ? 99 : leftIndex) - (rightIndex === -1 ? 99 : rightIndex);
    });
}

export function planLabelMutation(input) {
    const kind = input.kind === "pr" ? "pr" : input.kind === "issue" ? "issue" : null;
    if (!kind) {
        throw new Error("kind must be issue or pr");
    }
    const number = parseItemNumber(input.number);
    if (number == null) {
        throw new Error("invalid item number");
    }
    const current = new Set(labelNames(input.currentLabels));
    const action = input.action;
    const add = [];
    const remove = [];

    if (action === "accept") {
        if (!current.has(LABEL.accepted)) add.push(LABEL.accepted);
        if (current.has(LABEL.deferred)) remove.push(LABEL.deferred);
        if (current.has(LABEL.needsTriage)) remove.push(LABEL.needsTriage);
    } else if (action === "defer") {
        if (current.has(LABEL.accepted) && input.confirmClearAccepted !== true) {
            throw new Error("defer of an accepted item requires confirmClearAccepted");
        }
        if (!current.has(LABEL.deferred)) add.push(LABEL.deferred);
        if (current.has(LABEL.accepted)) remove.push(LABEL.accepted);
    } else if (action === "panel-review") {
        if (kind !== "pr") {
            throw new Error("panel-review applies to pull requests only");
        }
        if (current.has(LABEL.panelReview)) {
            remove.push(LABEL.panelReview);
            add.push(LABEL.panelReview);
        } else {
            add.push(LABEL.panelReview);
        }
    } else {
        throw new Error(`unsupported label action: ${action}`);
    }

    return { kind, number, add, remove, repo: input.repo || REPO };
}

export function applyLabelPlan(item, plan) {
    if (!item) return null;
    const remove = new Set(plan.remove || []);
    const labels = (item.labels || []).filter((name) => !remove.has(name));
    for (const name of plan.add || []) {
        if (!labels.includes(name)) labels.push(name);
    }
    const next = item.kind === "pr"
        ? classifyPr({ ...item, labels })
        : classifyIssue({ ...item, labels });
    if (item.kind === "issue") {
        next.linkedPrs = item.linkedPrs || [];
        next.hasOpenPr = Boolean(item.hasOpenPr);
    }
    next.triageConclusion = item.triageConclusion || next.triageConclusion;
    return next;
}

export function replaceBoardItem(items, patched) {
    if (!patched || !Array.isArray(items)) return items;
    const index = items.findIndex((row) => row.number === patched.number);
    if (index >= 0) items[index] = patched;
    return items;
}

export function patchLinkedPullRequests(issues, patchedPr) {
    if (!patchedPr || patchedPr.kind !== "pr") return issues;
    for (const issue of issues || []) {
        const linked = issue.linkedPrs || [];
        const index = linked.findIndex((pr) => pr.number === patchedPr.number);
        if (index < 0) continue;
        linked[index] = {
            ...linked[index],
            labels: patchedPr.labels,
            accepted: patchedPr.accepted,
            deferred: patchedPr.deferred,
            panelReview: patchedPr.panelReview,
            triageRan: patchedPr.triageRan,
            triageConclusion: patchedPr.triageConclusion,
        };
    }
    return issues;
}

export function ghLabelArgs(plan) {
    const command = plan.kind === "pr" ? "pr" : "issue";
    const args = [command, "edit", String(plan.number), "--repo", plan.repo];
    for (const label of plan.remove) {
        args.push("--remove-label", label);
    }
    for (const label of plan.add) {
        args.push("--add-label", label);
    }
    assertSafeGhArgs(args);
    return args;
}

export function assertSafeGhArgs(args) {
    if (!Array.isArray(args) || args.length < 2) {
        throw new Error("invalid gh args");
    }
    const joined = args.join(" ");
    for (const flag of FORBIDDEN_GH_FLAGS) {
        if (args.includes(flag) || joined.includes(` ${flag} `)) {
            throw new Error(`forbidden gh flag: ${flag}`);
        }
    }
    const head = args[0];
    if (head !== "issue" && head !== "pr" && head !== "api") {
        throw new Error(`forbidden gh command: ${head}`);
    }
    if (head === "pr" && args[1] === "merge") {
        throw new Error("forbidden gh command: pr merge");
    }
    if (args[1] !== "edit" && args[1] !== "list" && head !== "api") {
        throw new Error(`forbidden gh subcommand: ${args[1]}`);
    }
}

export function buildSpawnPrompt(resolved, canvasId) {
    const parent = formatActivationCard({
        action: "spawn-isolated-session",
        tool: "create_session",
        coordinate_with_creator: "false",
        detached: "false",
        kickoff_mode: resolved.kickoffMode === "plan" ? "plan" : "interactive",
        do_not_run_here: "yes",
        skill: resolved.skill,
        session_name: resolved.sessionName,
        occupancy_canvas: canvasId,
        occupancy_action: "report_occupancy",
        occupancy_target: resolved.target,
        occupancy_busy: "true",
        skip_if_busy_named: resolved.sessionName,
        forbid: "assign, request-reviewers, merge, labels",
    });
    const kickoff = formatActivationCard(resolved.card)
        .split("\n")
        .map((line) => `  ${line}`)
        .join("\n");
    return `${parent}\n\nkickoff:\n${kickoff}`;
}

export function occupancyActivity(row) {
    if (!row) return "idle";
    if (row.pending) return "pending";
    const raw = String(row.activity || "").toLowerCase();
    if (raw === "idle") return "idle";
    if (raw === "working" || raw === "busy") return "working";
    if (raw === "pending") return "pending";
    return row.busy === false ? "idle" : "working";
}

export function occupancyBlocksSpawn(row) {
    const activity = occupancyActivity(row);
    return activity === "working" || activity === "pending";
}

const SESSION_FAMILY = {
    "Issue triage": "issue-triage",
    "Issue delivery": "issue-delivery",
    "PR triage": "pr-triage",
    "PR review": "pr-review",
    "PR merge": "pr-merge",
};

const SESSION_NAME_RE = /^(Issue triage|Issue delivery|PR triage|PR review|PR merge)(?: scheduler| #(\d+))$/;

export function occupancyFromSessionName(name) {
    const trimmed = String(name || "").trim();
    const match = SESSION_NAME_RE.exec(trimmed);
    if (!match) return null;
    const family = SESSION_FAMILY[match[1]];
    if (!family) return null;
    if (match[2]) {
        return { target: `worker:${family}:${match[2]}`, name: trimmed, kind: "worker" };
    }
    return { target: `scheduler:${family}`, name: trimmed, kind: "scheduler" };
}

export function occupancyRecord(input = {}) {
    return {
        busy: input.busy !== false,
        activity: occupancyActivity(input),
        name: String(input.name || ""),
        skill: String(input.skill || ""),
        sessionId: input.sessionId || input.session_id || null,
        creatorSessionId: input.creatorSessionId || input.creator_session_id || null,
        pending: Boolean(input.pending),
    };
}

export function occupancyRowsFromCopilotSessions(sessions) {
    const rows = [];
    for (const session of sessions || []) {
        if (session.archivedAt) continue;
        const parsed = occupancyFromSessionName(session.name);
        if (!parsed) continue;
        const working = Boolean(session.isRunning);
        rows.push({
            target: parsed.target,
            ...occupancyRecord({
                busy: true,
                activity: working ? "working" : "idle",
                name: parsed.name,
                skill: "",
                sessionId: session.sessionId || null,
                creatorSessionId: session.creatorSessionId || session.creator_session_id || null,
                pending: false,
            }),
        });
    }
    return rows;
}

export function mergeLiveOccupancy(occupancy, liveRows) {
    const liveTargets = new Set((liveRows || []).map((row) => row.target));
    for (const target of [...occupancy.keys()]) {
        if (!liveTargets.has(target)) occupancy.delete(target);
    }
    for (const row of liveRows || []) {
        const current = occupancy.get(row.target) || {};
        occupancy.set(row.target, occupancyRecord({
            ...current,
            ...row,
            pending: false,
            skill: row.skill || current.skill || "",
            name: row.name || current.name || "",
            sessionId: row.sessionId || current.sessionId || null,
            creatorSessionId: row.creatorSessionId || current.creatorSessionId || null,
        }));
    }
    return occupancy;
}

export function sessionTableRows(groups, options = {}) {
    const canvasSessionId = String(options.canvasSessionId || "");
    const byId = new Map();
    for (const group of groups || []) {
        if (group.scheduler?.sessionId) byId.set(group.scheduler.sessionId, group.scheduler);
        for (const worker of group.workers || []) {
            if (worker.sessionId) byId.set(worker.sessionId, worker);
        }
    }
    function parentName(row, familyParent) {
        const creator = row.creatorSessionId || row.creator_session_id || "";
        if (creator && canvasSessionId && creator === canvasSessionId) return "this canvas";
        if (creator && byId.has(creator)) {
            const parent = byId.get(creator);
            return parent.name || parent.target || creator;
        }
        if (familyParent && row !== familyParent) return familyParent.name || familyParent.target || "";
        if (creator) return creator.slice(0, 8);
        return "";
    }
    const rows = [];
    for (const group of groups || []) {
        if (group.scheduler) {
            rows.push({
                ...group.scheduler,
                role: "scheduler",
                child: false,
                parentName: parentName(group.scheduler, null),
                childCount: (group.workers || []).length,
            });
        }
        for (const worker of group.workers || []) {
            rows.push({
                ...worker,
                role: "worker",
                child: Boolean(group.scheduler),
                parentName: parentName(worker, group.scheduler),
                childCount: 0,
            });
        }
    }
    return rows;
}

export function assertSpawnAllowed(resolved, occupancy, options = {}) {
    if (resolved.requiresConfirm && options.confirm !== true) {
        throw new Error("merge-worker spawn requires explicit confirm");
    }
    const key = occupancyKey(resolved.target);
    const current = occupancy instanceof Map ? occupancy.get(key) : occupancy?.[key];
    if (occupancyBlocksSpawn(current)) {
        const name = current.name || resolved.sessionName;
        throw new Error(`refused: ${name} is already working for ${resolved.target}`);
    }
    return key;
}
