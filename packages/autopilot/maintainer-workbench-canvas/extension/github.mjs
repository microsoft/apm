import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { MAX_PAGES, REPO, sanitize, pool } from "./config.mjs";

const exec = promisify(execFile);
const PAGE = "pageInfo{hasNextPage endCursor} totalCount";
const COMMON = `number title url state body updatedAt createdAt author{login}
  assignees(first:100){nodes{login} ${PAGE}}
  labels(first:100){nodes{name} ${PAGE}} repository{nameWithOwner}`;
const ISSUE = `${COMMON} stateReason closedAt`;
const PR = `${COMMON} isDraft headRefOid baseRefName mergedAt mergedBy{login}
  reviewDecision mergeable mergeStateStatus autoMergeRequest{enabledAt mergeMethod enabledBy{login}}
  reviewRequests(first:100){nodes{requestedReviewer{__typename ...on User{login} ...on Team{name slug}}} ${PAGE}}
  closingIssuesReferences(first:100){nodes{number url repository{nameWithOwner}} ${PAGE}}`;
const CHECKS = `commits(last:1){nodes{commit{oid statusCheckRollup{
  contexts(first:100,after:$cursor){${PAGE} nodes{__typename
    ...on CheckRun{name status conclusion detailsUrl startedAt completedAt}
    ...on StatusContext{context state targetUrl createdAt}}}
}}}}`;
const QUERIES = Object.freeze({
  projects: `query($cursor:String){organization(login:"microsoft"){projectsV2(first:100,after:$cursor,query:"APM"){${PAGE} nodes{id number title url closed updatedAt}}}}`,
  project: `query($number:Int!,$cursor:String){organization(login:"microsoft"){projectV2(number:$number){
    id number title url updatedAt items(first:100,after:$cursor){${PAGE} nodes{id updatedAt isArchived
      fieldValues(first:100){${PAGE} nodes{...on ProjectV2ItemFieldSingleSelectValue{name field{...on ProjectV2SingleSelectField{name}}}}}
      content{__typename ...on Issue{${ISSUE}} ...on PullRequest{${PR}} ...on DraftIssue{title}}}
  }}}}`,
  issues: `query($cursor:String){repository(owner:"microsoft",name:"apm"){issues(first:100,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){${PAGE} nodes{${ISSUE}}}}}`,
  prs: `query($cursor:String){repository(owner:"microsoft",name:"apm"){pullRequests(first:20,after:$cursor,states:OPEN,orderBy:{field:UPDATED_AT,direction:DESC}){${PAGE} nodes{${PR}}}}}`,
  issue: `query($number:Int!){repository(owner:"microsoft",name:"apm"){issue(number:$number){${ISSUE}}}}`,
  pr: `query($number:Int!,$cursor:String){repository(owner:"microsoft",name:"apm"){pullRequest(number:$number){${PR} ${CHECKS}}}}`,
});

export function pullRecord(data, previous) {
  const history = [...(previous?.history || [])];
  const old = previous?.data?.evidence;
  if (old && old.head !== data.headRefOid && !history.some(e => e.head === old.head)) history.unshift(old);
  return { data, status: "current", observedAt: new Date().toISOString(), history: history.slice(0, 3) };
}

export class Github {
  constructor(run = async (args) => {
    try {
      const { stdout } = await exec("gh", args, {
        timeout: 60000, maxBuffer: 24 * 1024 * 1024,
        env: { ...process.env, GH_PROMPT_DISABLED: "1", GH_PAGER: "cat" },
      });
      return JSON.parse(stdout);
    } catch (error) {
      // Never store stderr: CLI auth failures can contain sensitive environment details.
      throw new Error(`GitHub read failed (${error.killed ? "timeout" : "API or account access"}). Retry refresh or check gh access outside the canvas.`);
    }
  }) {
    this.run = run;
  }

  async query(key, variables = {}) {
    if (!Object.hasOwn(QUERIES, key)) throw new Error("Unsupported read query.");
    const args = ["api", "graphql", "-f", `query=${QUERIES[key]}`];
    for (const [name, value] of Object.entries(variables)) {
      if (!["cursor", "number"].includes(name)) throw new Error("Unsupported query variable.");
      if (name === "number" && (!Number.isSafeInteger(value) || value < 1)) throw new Error("Invalid item number.");
      if (name === "cursor" && value !== null && typeof value !== "string") throw new Error("Invalid cursor.");
      if (value !== null && value !== undefined) args.push("-F", `${name}=${value}`);
    }
    const response = await this.run(args);
    if (response.errors?.length || !response.data) throw new Error("GitHub returned incomplete query data; previous evidence retained.");
    return sanitize(response.data);
  }

  async connection(key, pick, variables = {}) {
    const nodes = [];
    let cursor = null;
    let total = null;
    const cursors = new Set();
    for (let page = 0; page < MAX_PAGES; page++) {
      const connection = pick(await this.query(key, { ...variables, cursor }));
      if (!connection || !Array.isArray(connection.nodes) || !connection.pageInfo) throw new Error("GitHub connection is unavailable.");
      nodes.push(...connection.nodes.filter(Boolean));
      total = connection.totalCount ?? total;
      if (!connection.pageInfo.hasNextPage) return { nodes, total, complete: true, pages: page + 1 };
      cursor = connection.pageInfo.endCursor;
      if (!cursor || cursors.has(cursor)) throw new Error("GitHub pagination did not advance; collection is incomplete.");
      cursors.add(cursor);
    }
    return { nodes, total, complete: false, pages: MAX_PAGES, limitation: "Pagination safety limit reached." };
  }

  async rest(kind, numberOrSha, page = 1) {
    if (!Number.isInteger(page) || page < 1 || page > MAX_PAGES) throw new Error("Invalid page.");
    const number = Number(numberOrSha);
    const validNumber = Number.isSafeInteger(number) && number > 0 && number < 1e9;
    let path;
    if (["comments", "timeline"].includes(kind) && validNumber) {
      path = `repos/${REPO}/issues/${number}/${kind}?per_page=100&page=${page}`;
    } else if (kind === "runs" && /^[0-9a-f]{40}$/.test(numberOrSha)) {
      path = `repos/${REPO}/actions/runs?head_sha=${numberOrSha}&per_page=100&page=${page}`;
    } else {
      throw new Error("Unsupported read endpoint.");
    }
    return sanitize(await this.run(["api", "--method", "GET", path]));
  }

  async restPages(kind, value) {
    const nodes = [];
    let total = null;
    for (let page = 1; page <= MAX_PAGES; page++) {
      const data = await this.rest(kind, value, page);
      const batch = kind === "runs" ? data.workflow_runs : data;
      if (!Array.isArray(batch)) throw new Error("GitHub returned an invalid read response.");
      total = data.total_count ?? total;
      nodes.push(...batch);
      if (batch.length < 100 || (total !== null && nodes.length >= total)) {
        return { nodes, total: total ?? nodes.length, pages: page, complete: true };
      }
    }
    return { nodes, total, pages: MAX_PAGES, complete: false };
  }

  async roadmap() {
    const discovery = await this.connection("projects", d => d.organization?.projectsV2);
    if (!discovery.complete) throw new Error("Project discovery was incomplete; cannot resolve APM Roadmap.");
    const matches = discovery.nodes.filter(p => p.title === "APM Roadmap" && !p.closed);
    if (matches.length !== 1) throw new Error("APM Roadmap is unavailable or ambiguous for the current account.");
    const project = matches[0];
    const items = await this.connection("project", d => d.organization?.projectV2?.items, { number: project.number });
    const fieldsPartial = items.nodes.some(n => n.fieldValues?.pageInfo?.hasNextPage);
    return { project, ...items, complete: items.complete && !fieldsPartial,
      limitation: fieldsPartial ? "Some project field values are not fully paginated." : items.limitation };
  }

  async pull(number) {
    let metadata;
    const checks = await this.connection("pr", d => {
      const pr = d.repository?.pullRequest;
      if (!pr) throw new Error("Pull request is unavailable.");
      if (metadata && metadata.headRefOid !== pr.headRefOid) throw new Error("PR head changed during pagination. Refresh again.");
      metadata = pr;
      const commit = pr.commits?.nodes?.[0]?.commit;
      if (commit && commit.oid !== pr.headRefOid) throw new Error("Check revision does not match the PR head.");
      return commit?.statusCheckRollup?.contexts ?? { nodes: [], totalCount: 0, pageInfo: { hasNextPage: false } };
    }, { number });
    const head = metadata.headRefOid;
    let runs;
    try {
      runs = { ...await this.restPages("runs", head), observedAt: new Date().toISOString() };
    } catch (error) {
      runs = { nodes: [], complete: false, error: error.message, observedAt: null };
    }
    // Re-read the head after separate workflow queries; never attach old evidence to a new head.
    const final = await this.query("pr", { number });
    if (final.repository?.pullRequest?.headRefOid !== head) throw new Error("PR head changed while gathering evidence. Refresh again.");
    return {
      ...metadata, commits: undefined,
      evidence: { head, observedAt: new Date().toISOString(), checks, runs },
    };
  }

  async selectedDiscussion(number) {
    const comments = await this.restPages("comments", number);
    return { discussion: comments.nodes.map(c => ({ author: c.user?.login, body: c.body, url: c.html_url,
      updatedAt: c.updated_at, association: c.author_association, type: c.user?.type })),
      complete: comments.complete, observedAt: new Date().toISOString() };
  }

  async selectedIssue(number) {
    const [issue, comments, timeline] = await Promise.all([
      this.query("issue", { number }).then(d => d.repository?.issue),
      this.restPages("comments", number),
      this.restPages("timeline", number),
    ]);
    if (!issue) throw new Error("Issue is unavailable.");
    return {
      ...issue,
      discussion: comments.nodes.map(c => ({ author: c.user?.login, body: c.body, url: c.html_url, updatedAt: c.updated_at, association: c.author_association })),
      timelineReferences: timeline.nodes.filter(e => e.event === "cross-referenced" && e.source?.issue?.pull_request)
        .map(e => ({ number: e.source.issue.number, url: e.source.issue.html_url, observedAt: e.created_at })),
      detailCoverage: { comments: comments.complete, timeline: timeline.complete },
      detailObservedAt: new Date().toISOString(),
    };
  }

  async portfolio(previous = {}) {
    const snapshot = { ...previous, sources: { ...previous.sources }, attemptedAt: new Date().toISOString() };
    const source = async (key, fn) => {
      try {
        const value = await fn();
        snapshot[key] = value;
        snapshot.sources[key] = {
          observedAt: new Date().toISOString(), attemptedAt: snapshot.attemptedAt,
          status: value.complete === false ? "partial" : "current",
          count: value.nodes.length, total: value.total, pages: value.pages,
          error: value.limitation || null,
        };
      } catch (error) {
        snapshot.sources[key] = {
          ...previous.sources?.[key], attemptedAt: snapshot.attemptedAt,
          status: previous[key] ? "stale" : "error", error: error.message,
        };
      }
    };
    await Promise.all([
      source("roadmap", () => this.roadmap()),
      source("issues", () => this.connection("issues", d => d.repository?.issues)),
      source("prs", () => this.connection("prs", d => d.repository?.pullRequests)),
    ]);
    const old = previous.pulls || {};
    snapshot.pulls = { ...old };
    const observed = [...(snapshot.prs?.nodes || [])];
    const seen = new Set(observed.map(pr => pr.number));
    for (const record of Object.values(old)) {
      if (record.data?.state === "OPEN" && !seen.has(record.data.number)) {
        observed.push(record.data);
        seen.add(record.data.number);
      }
    }
    // Explicit historical example from the approved brief; not a complete closed-PR feed.
    if (!seen.has(3061) && !old[3061]?.data) observed.push({ number: 3061 });
    await pool(observed, async pr => {
      try {
        snapshot.pulls[pr.number] = pullRecord(await this.pull(pr.number), old[pr.number]);
      } catch (error) {
        snapshot.pulls[pr.number] = { ...old[pr.number], status: old[pr.number] ? "stale" : "error", error: error.message, attemptedAt: new Date().toISOString() };
      }
    });
    snapshot.fetchedAt = new Date().toISOString();
    return snapshot;
  }
}
