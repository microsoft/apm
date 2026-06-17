// Pure business logic extracted for testability
// Used by extension.mjs and test suite

export function classifyIssue(raw) {
    const labels = (raw.labels || []).map((l) => l.name);
    let type = "chore";
    for (const l of labels) {
        if (/bug/i.test(l)) { type = "bug"; break; }
        if (/feature|enhancement/i.test(l)) { type = "feature"; break; }
    }
    let priority = "P2";
    for (const l of labels) {
        if (l.startsWith("priority/")) {
            const lvl = l.split("/")[1];
            if (/critical|0/.test(lvl)) priority = "P0";
            else if (/high|1/.test(lvl)) priority = "P1";
            else if (/low|3/.test(lvl)) priority = "P3";
        }
    }
    return {
        number: raw.number,
        title: raw.title.slice(0, 90),
        type,
        priority,
        author: raw.author?.login || "unknown",
        status: "available",
        url: raw.url,
        labels,
    };
}

export function classifyPrStatus(pr) {
    if (pr.isDraft) return "draft";
    const checks = (pr.statusCheckRollup || []);
    const hasFailing = checks.some(c => c.conclusion === "FAILURE" || c.conclusion === "ERROR" || c.conclusion === "CANCELLED");
    const hasPending = checks.some(c => c.status === "IN_PROGRESS" || c.status === "QUEUED" || c.status === "PENDING");
    if (hasFailing) return "ci-failing";
    const rd = pr.reviewDecision || "";
    if (rd === "CHANGES_REQUESTED") return "changes-requested";
    if (rd === "APPROVED" && !hasPending) return "ready-to-merge";
    if (rd === "APPROVED" && hasPending) return "ci-pending";
    if (hasPending) return "ci-pending";
    return "review-pending";
}

export function escapeHtml(str) {
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

export function matchPrsToIssues(issues, prs) {
    const issueNumbers = new Set(issues.map((i) => i.number));
    for (const pr of prs) {
        const text = (pr.title || "") + " " + (pr.body || "");
        for (const issueNum of issueNumbers) {
            const pattern = new RegExp("(?:^|\\W)#" + issueNum + "(?:\\W|$)");
            if (pattern.test(text)) {
                const issue = issues.find((i) => i.number === issueNum);
                if (issue && !issue.pr) {
                    issue.pr = {
                        number: pr.number,
                        url: pr.url,
                        state: pr.state,
                        prStatus: classifyPrStatus(pr),
                    };
                }
            }
        }
    }
    return issues;
}

export function classifyPipeline(checks, workflowRuns) {
    // Check workflow runs for action_required (awaiting approval) first
    if (workflowRuns && workflowRuns.length > 0) {
        const hasActionRequired = workflowRuns.some(r => r.conclusion === "action_required");
        if (hasActionRequired) return { status: "orange", label: "Awaiting Approval" };
    }
    if (!checks || checks.length === 0) return { status: "none", label: "No CI" };
    const hasFailing = checks.some(c =>
        c.conclusion === "FAILURE" || c.conclusion === "ERROR" || c.conclusion === "CANCELLED"
    );
    if (hasFailing) return { status: "red", label: "Failing" };
    const hasPending = checks.some(c =>
        c.status === "IN_PROGRESS" || c.status === "QUEUED" || c.status === "PENDING"
    );
    if (hasPending) return { status: "yellow", label: "Running" };
    return { status: "green", label: "Passing" };
}

export function classifyPanel(labels) {
    if (!labels || labels.length === 0) return { status: "none", label: "Not requested" };
    const names = labels.map(l => typeof l === "string" ? l : l.name || "");
    const hasPanel = names.some(n => n === "panel-review");
    const hasAccepted = names.some(n => n === "status/accepted");
    if (hasPanel && hasAccepted) return { status: "green", label: "Accepted" };
    if (hasPanel) return { status: "yellow", label: "Requested" };
    return { status: "none", label: "Not requested" };
}

export function parsePanelCounts(comments) {
    if (!comments || !Array.isArray(comments)) return null;
    for (const c of comments) {
        const body = typeof c === "string" ? c : (c.body || "");
        if (!body.includes("| B | R | N")) continue;
        let totalB = 0, totalR = 0, totalN = 0;
        const lines = body.split("\n");
        for (const line of lines) {
            // Match data rows: | Persona Name | 0 | 1 | 2 | ... |
            const m = line.match(/^\|\s*[A-Z][^|]+\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|/);
            if (m) {
                totalB += parseInt(m[1], 10);
                totalR += parseInt(m[2], 10);
                totalN += parseInt(m[3], 10);
            }
        }
        if (totalB + totalR + totalN > 0) return { b: totalB, r: totalR, n: totalN };
    }
    return null;
}

/**
 * Parse a full panel review from PR comments.
 * Returns structured data: verdict, personas, recommendation, deferred, folded, ciStatus, comment body.
 * Returns null if no panel review comment found.
 */
export function parsePanelReview(comments) {
    if (!comments || !Array.isArray(comments)) return null;
    // Find the LAST panel review comment (most recent run)
    let panelComment = null;
    for (const c of comments) {
        const body = typeof c === "string" ? c : (c.body || "");
        if (body.includes("APM Review Panel:") && body.includes("| B | R | N")) {
            panelComment = { body, author: c.author?.login || c.author || "unknown", createdAt: c.createdAt || "" };
        }
    }
    if (!panelComment) return null;
    const body = panelComment.body;

    // Extract verdict from header: ## APM Review Panel: `verdict`
    const verdictMatch = body.match(/APM Review Panel:\s*`([^`]+)`/);
    const verdict = verdictMatch ? verdictMatch[1] : "unknown";

    // Parse persona table
    const personas = [];
    const lines = body.split("\n");
    for (const line of lines) {
        const m = line.match(/^\|\s*([A-Z][^|]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|$/);
        if (m) {
            personas.push({
                name: m[1].trim(),
                b: parseInt(m[2], 10),
                r: parseInt(m[3], 10),
                n: parseInt(m[4], 10),
                takeaway: m[5].trim(),
            });
        }
    }

    // Totals
    let totalB = 0, totalR = 0, totalN = 0;
    for (const p of personas) { totalB += p.b; totalR += p.r; totalN += p.n; }

    // Extract sections by heading
    function extractSection(heading) {
        const re = new RegExp("^### " + heading + "\\s*$", "m");
        const idx = body.search(re);
        if (idx === -1) return "";
        const afterHeading = body.slice(idx).replace(re, "").trim();
        const nextHeading = afterHeading.search(/^### /m);
        return nextHeading === -1 ? afterHeading.trim() : afterHeading.slice(0, nextHeading).trim();
    }

    const recommendation = extractSection("Recommendation");
    const deferred = extractSection("Deferred \\(out-of-scope follow-ups\\)");
    const folded = extractSection("Folded in this run");
    const ciSection = extractSection("CI");
    const convergence = extractSection("Convergence");

    // Extract summary quote (the > blockquote after the verdict heading)
    const summaryMatch = body.match(/^>\s+(.+?)(?:\n\n|\n[^>])/ms);
    const summary = summaryMatch ? summaryMatch[1].replace(/\n>\s*/g, " ").trim() : "";

    return {
        verdict,
        summary,
        personas,
        totals: { b: totalB, r: totalR, n: totalN },
        recommendation,
        deferred,
        folded,
        ci: ciSection,
        convergence,
        author: panelComment.author,
        createdAt: panelComment.createdAt,
        rawBody: body,
    };
}

export function classifyPrForTable(raw) {
    const labels = (raw.labels || []).map(l => typeof l === "string" ? l : l.name || "");
    const pipeline = classifyPipeline(raw.statusCheckRollup, raw.workflowRuns);
    const panel = classifyPanel(raw.labels);
    const prStatus = classifyPrStatus(raw);
    const author = raw.author?.login || (typeof raw.author === "string" ? raw.author : "unknown");
    return {
        number: raw.number,
        title: (raw.title || "").slice(0, 90),
        author,
        url: raw.url,
        isDraft: !!raw.isDraft,
        reviewDecision: raw.reviewDecision || "",
        prStatus,
        pipeline,
        panel,
        panelCounts: raw.panelCounts || null,
        labels,
        branch: raw.headRefName || "",
    };
}
