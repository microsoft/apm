import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm, readdir } from "node:fs/promises";
import { join } from "node:path";
import { STORE } from "./config.mjs";
import { now } from "./fixtures.mjs";
import { responsiveStore, deferred, clone } from "./responsive-fixtures.mjs";
import { Explanations } from "./explanations.mjs";
import { startServer } from "./server.mjs";

const round = process.argv[2];
if (!["initial", "confirm"].includes(round)) throw new Error("Use initial or confirm.");
const browserBinary = process.argv[3] === "edge" ? "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" : "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const base = join(STORE, "browser-runs"), screenshots = join(STORE, "screenshots");
await mkdir(base, { recursive: true }); await mkdir(screenshots, { recursive: true });
const root = await mkdtemp(join(base, "responsive-fixture-"));
const f = responsiveStore(root), { store, held, calls, approvalGate } = f;
const portfolio = deferred(), sending = deferred(), relays = [];
const originalSnapshot = clone(store.snapshot);
store.github.portfolio = async () => { await portfolio.promise; return originalSnapshot; };
for (const number of [1, 3, 4]) held.set(number, deferred());
store.view.selectedId = "microsoft/apm/issue/4";
store.explanations = new Explanations(store, { timeout: 90000, send: async options => {
  relays.push(options); await sending.promise; return "fixture-narrative-message";
} });
const server = await startServer(store, { instanceId: "responsive-fixture" });
const browser = spawn(browserBinary, [
  "--headless=new", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${join(root, "profile")}`,
  "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-sync", "--disable-extensions", "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
let stderr = "", exit = null;
browser.stderr.on("data", data => { stderr = (stderr + data).slice(-5000); });
browser.once("exit", (code, signal) => { exit = { code, signal }; });
const report = { round, fixtureOnly: true, themeViews: [], interactions: [], exceptions: [], screenshots: [] };
let ws, sessionId, next = 0;
const pending = new Map();
try {
  let portData;
  for (let i = 0; i < 600; i++) {
    try { portData = await readFile(join(root, "profile", "DevToolsActivePort"), "utf8"); break; }
    catch (error) { if (error.code !== "ENOENT") throw error; if (exit) break; await sleep(100); }
  }
  if (!portData) throw new Error(`Browser startup failed before rendering; pid=${browser.pid}, exit=${JSON.stringify(exit)}, profile=${JSON.stringify(await readdir(join(root, "profile")).catch(() => []))}, stderr=${stderr}`);
  const [port, path] = portData.trim().split("\n");
  ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
  await new Promise((resolve, reject) => { ws.addEventListener("open", resolve, { once: true }); ws.addEventListener("error", reject, { once: true }); });
  ws.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject, timer } = pending.get(message.id);
      clearTimeout(timer); pending.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result);
    }
    if (message.method === "Runtime.exceptionThrown") report.exceptions.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
  });
  const send = (method, params = {}, session = sessionId) => new Promise((resolve, reject) => {
    const id = ++next, timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 20000);
    pending.set(id, { resolve, reject, timer }); ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
  });
  const target = await send("Target.createTarget", { url: "about:blank" }, null);
  sessionId = (await send("Target.attachToTarget", { targetId: target.targetId, flatten: true }, null)).sessionId;
  await send("Page.enable"); await send("Runtime.enable");
  const evaluate = async expression => {
    const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const wait = async expression => {
    for (let i = 0; i < 300; i++) { if (await evaluate(expression)) return; await sleep(100); }
    throw new Error(`UI did not settle: ${expression}`);
  };
  const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const screenshot = async name => {
    const file = `responsive-${round}-${name}.png`;
    const image = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(join(screenshots, file), Buffer.from(image.data, "base64")); report.screenshots.push(file);
  };
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: server.url });
  await wait(`document.querySelector('.selected-progress')?.textContent.includes('Updating this item')`);
  const refreshing = store.refresh();
  await click('[data-select="microsoft/apm/issue/1"]');
  const a = await evaluate(`({title:document.querySelector('#decision h2').textContent,id:document.querySelector('.work-row[aria-pressed="true"]').dataset.select,loading:document.querySelector('.selected-progress').textContent})`);
  assert.match(a.title, /Preserve nested skill/); assert.equal(a.id, "microsoft/apm/issue/1");
  await click('[data-select="microsoft/apm/issue/3"]');
  const b = await evaluate(`({title:document.querySelector('#decision h2').textContent,id:document.querySelector('.work-row[aria-pressed="true"]').dataset.select,loading:document.querySelector('.selected-progress').textContent,busy:document.querySelector('#decision').getAttribute('aria-busy')})`);
  assert.match(b.title, /Planned without verified scope/); assert.equal(b.id, "microsoft/apm/issue/3"); assert.equal(b.busy, null);
  assert.equal(store.refreshing, true);
  report.interactions.push({ immediateHeldSelectionA: a, immediateHeldSelectionB: b, portfolioStillHeld: true });
  await screenshot("held-selection");
  held.get(4).resolve(); await store.waitSelected("microsoft/apm/issue/4");
  held.get(3).resolve(); await store.waitSelected("microsoft/apm/issue/3");
  await wait(`document.querySelector('.explanation-state')?.textContent.includes('Sending to this conversation')`);
  await screenshot("sending-explanation");
  sending.resolve();
  await wait(`document.querySelector('.explanation-state')?.textContent.includes("Waiting for this conversation's agent")`);
  const request = store.explanations.requests.find(r => r.targetId.endsWith("/3"));
  const claim = await store.explanations.claim({ id: request.id, revision: request.revision });
  await wait(`document.querySelector('.explanation-state')?.textContent.includes('Writing the explanation')`);
  await evaluate(`document.querySelector('[data-disclosure="evidence"]').open=true;document.querySelector('[data-disclosure="evidence"] summary').focus({preventScroll:true});window.scrollTo(0,250)`);
  const scrollBefore = await evaluate("scrollY");
  held.get(1).resolve(); portfolio.resolve(); await refreshing; await store.waitSelected("microsoft/apm/issue/1");
  await sleep(2400);
  assert.equal(await evaluate("document.querySelector('.work-row[aria-pressed=\"true\"]').dataset.select"), "microsoft/apm/issue/3");
  assert.equal(await evaluate("document.activeElement.textContent"), "Technical checks and source records");
  assert.equal(await evaluate("document.querySelector('[data-disclosure=\"evidence\"]').open"), true);
  assert.equal(await evaluate("scrollY"), scrollBefore);
  const result = packet => ({ title: "[Fixture] Understand the intended change", problem: "A fixture cleanup removes an instruction file that should remain installed.",
    example: "Imagine removing one unused skill while keeping a separate skill that is still needed.", exampleKind: "illustrative",
    impact: "The retained skill must continue to work.", tradeoff: "Remove leftovers without deleting retained instructions.",
    decisionQuestion: { state: "resolved", text: "Scope is agreed. Review whether the change keeps the intended files." },
    citations: [packet.selected.url] });
  await store.explanations.report({ id: claim.id, revision: claim.revision, claimToken: claim.claimToken, contentKey: claim.contentKey, status: "completed", result: result(claim.packet) });
  await wait("!!document.querySelector('.plain-context')");
  report.interactions.push({ sendingWaitingGeneratingCompleted: true, lateResponseSelectionStable: true, disclosureFocusScrollStable: true });
  await click('[data-select="microsoft/apm/issue/4"]');
  await wait(`document.querySelector('.explanation-state')?.textContent.includes("Waiting for this conversation's agent")`);
  const second = store.explanations.requests.find(r => r.targetId.endsWith("/4"));
  const secondClaim = await store.explanations.claim({ id: second.id, revision: second.revision });
  await store.explanations.report({ id: secondClaim.id, revision: secondClaim.revision, claimToken: secondClaim.claimToken,
    contentKey: secondClaim.contentKey, status: "completed", result: result(secondClaim.packet) });
  await wait("!!document.querySelector('.plain-context')");
  for (const theme of ["light", "dark"]) {
    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" }, { name: "prefers-reduced-motion", value: "reduce" }] });
    await evaluate(`document.documentElement.dataset.colorMode=${JSON.stringify(theme)}`);
    for (const width of [1440, 390]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 1100, deviceScaleFactor: 1, mobile: width === 390 });
      await evaluate(`document.body.classList.toggle('detail-open',innerWidth<680);window.scrollTo(0,0)`);
      await screenshot(`${theme}-${width}`);
      const metrics = await evaluate(`(() => {
        const blend=(a,b)=>[...a.slice(0,3).map((c,i)=>c*a[3]+b[i]*(1-a[3])),1];
        const c=document.createElement('canvas');c.width=c.height=1;const ctx=c.getContext('2d',{willReadFrequently:true});
        const rgba=x=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=x;ctx.fillRect(0,0,1,1);const p=[...ctx.getImageData(0,0,1,1).data];return [p[0],p[1],p[2],p[3]/255]};
        const bg=e=>{const path=[];for(;e;e=e.parentElement)path.unshift(e);return path.reduce((b,n)=>blend(rgba(getComputedStyle(n).backgroundColor),b),[255,255,255,1])};
        const lum=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
        const contrast=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
        const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),texts=[];let n;
        while(n=walker.nextNode()){const e=n.parentElement;if(!n.textContent.trim()||!e.getClientRects().length||!e.checkVisibility({visibilityProperty:true})||['SCRIPT','STYLE','OPTION'].includes(e.tagName)||e.closest('[disabled]'))continue;
          let hidden=false;for(let p=e;p;p=p.parentElement)if(p.matches('details:not([open])')&&!p.querySelector(':scope > summary')?.contains(e)){hidden=true;break;}if(hidden)continue;
          const s=getComputedStyle(e),b=bg(e);texts.push({text:n.textContent.trim().slice(0,70),ratio:contrast(blend(rgba(s.color),b),b)});}
        const el=document.querySelector('[data-primary]');el.focus({preventScroll:true});const s=getComputedStyle(el);
        return {theme:document.documentElement.dataset.colorMode,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
          bg:getComputedStyle(document.body).backgroundColor,minContrast:Math.min(...texts.map(t=>t.ratio)),lowContrast:texts.filter(t=>t.ratio<4.5),
          primaryCount:document.querySelectorAll('#decision [data-primary]').length,primary:el.textContent,
          focus:{visible:el.matches(':focus-visible'),width:s.outlineWidth,ratio:contrast(rgba(s.outlineColor),bg(el.parentElement))},
          timer:document.querySelector('#update-schedule').textContent,readableProgressPx:parseFloat(getComputedStyle(document.querySelector('.workflow-approval')||document.querySelector('.decision')).fontSize),
          reducedMotion:matchMedia('(prefers-reduced-motion:reduce)').matches};
      })()`);
      report.themeViews.push(metrics);
    }
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 1100, deviceScaleFactor: 1, mobile: true });
  await click('[data-primary-effect="approve-workflows"]');
  await wait("!!document.querySelector('[data-approval-ack]')");
  assert.equal(await evaluate("document.querySelector('[data-confirm-workflows]').disabled"), true);
  assert.equal(calls.approvals.length, 0);
  assert.match(await evaluate("document.querySelector('.workflow-approval').textContent"), /fixture-maintainer.*commit/s);
  await screenshot("narrow-confirmation");
  await click("[data-approval-ack]");
  await sleep(2400);
  assert.equal(await evaluate("document.querySelector('[data-approval-ack]').checked"), true);
  await click("[data-confirm-workflows]");
  await wait(`document.querySelector('.workflow-approval')?.textContent.includes('approving')`);
  assert.equal(await evaluate("!!document.querySelector('[data-confirm-workflows]')"), false);
  approvalGate.resolve(); await Promise.all([...store.workflowApprovals.tasks.values()]);
  await wait(`document.querySelector('.compact-runs')?.textContent.includes('Queued')`);
  await screenshot("queued-receipts");
  f.setRunState("in_progress");
  await wait(`document.querySelector('.compact-runs')?.textContent.includes('Running')`);
  f.setRunState("completed", "success");
  await wait(`document.querySelector('.compact-runs')?.textContent.includes('Completed: success')`);
  await wait(`document.querySelector('.primary-action')?.textContent.includes('for your review')`);
  assert.deepEqual(calls.approvals, [101, 102]);
  assert.equal(store.explanations.requests.filter(r => r.targetId.endsWith("/4")).length, 1);
  assert.equal(await evaluate("document.querySelector('.plain-context p').textContent"), result(secondClaim.packet).problem);
  await screenshot("completed-workflows");
  report.interactions.push({ explicitPreviewBeforePermission: true, acknowledgementSurvivesPoll: true, mockApprovals: calls.approvals,
    queuedRunningSuccessObserved: true, noNarrativeRestartOnCI: true, narrowOverflow: await evaluate("document.documentElement.scrollWidth>innerWidth") });
  await click("#back-to-list");
  await wait("document.activeElement.id==='search'");
  assert.equal(store.state().followingApproval, null);
  report.interactions.push({ narrowBackEndsFollow: true });
  // A hidden page receives no new local polling or GitHub read demand.
  await evaluate(`window.__fixtureHidden=true;Object.defineProperty(document,'hidden',{configurable:true,get:()=>window.__fixtureHidden});document.dispatchEvent(new Event('visibilitychange'))`);
  await sleep(2500);
  const pullsBefore = calls.pulls;
  await sleep(10500);
  assert.equal(calls.pulls, pullsBefore);
  await evaluate(`window.__fixtureHidden=false;document.dispatchEvent(new Event('visibilitychange'))`);
  await sleep(800);
  assert.ok(calls.pulls > pullsBefore);
  report.interactions.push({ hiddenTabStopsDemand: true, visibleReturnChecksImmediately: true });
  assert.deepEqual(report.exceptions, []);
  for (const m of report.themeViews) {
    assert.equal(m.width, m.scrollWidth); assert.deepEqual(m.lowContrast, []);
    assert.equal(m.primaryCount, 1); assert.match(m.primary, /Review workflow approval/);
    assert.equal(m.focus.visible, true); assert.ok(parseFloat(m.focus.width) >= 2); assert.ok(m.focus.ratio >= 3);
    assert.match(m.timer, /Auto-update every 4 min.*Last portfolio check/); assert.equal(m.reducedMotion, true);
  }
  assert.equal(report.interactions.find(i => i.mockApprovals).narrowOverflow, false);
  report.result = "passed";
} catch (error) {
  report.result = "failed"; report.error = error.stack;
  throw error;
} finally {
  for (const gate of held.values()) gate.resolve();
  sending.resolve(); portfolio.resolve(); approvalGate.resolve();
  await Promise.all([...store.selectedTasks.values(), ...store.checkTasks.values(), ...store.workflowApprovals.tasks.values()]);
  await store.pending; await store.writeQueue;
  report.browser = { binary: browserBinary, pid: browser.pid, exit, stderr };
  await writeFile(join(STORE, `browser-responsive-${round}.json`), JSON.stringify(report, null, 2));
  for (const entry of pending.values()) clearTimeout(entry.timer);
  ws?.close();
  if (!exit) {
    browser.kill("SIGTERM");
    await new Promise(resolve => { browser.once("exit", resolve); setTimeout(resolve, 5000).unref(); });
  }
  if (!exit) browser.kill("SIGKILL");
  await server.close(); await rm(root, { recursive: true, force: true });
  process.stdout.write(`${report.result}: ${report.themeViews.length} theme/viewport observations; ${report.interactions.length} interaction groups; ${report.exceptions.length} runtime exceptions\n`);
}
