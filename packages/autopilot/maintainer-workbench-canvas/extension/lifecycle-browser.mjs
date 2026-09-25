import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm, readdir } from "node:fs/promises";
import { join } from "node:path";
import { STORE } from "./config.mjs";
import { fixtureStore, scopeFields, now } from "./fixtures.mjs";
import { startServer } from "./server.mjs";
import { Explanations } from "./explanations.mjs";

const round = process.argv[2];
if (!["initial", "confirm"].includes(round)) throw new Error("Use initial or confirm.");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const base = join(STORE, "browser-runs"), screenshots = join(STORE, "screenshots");
await mkdir(base, { recursive: true }); await mkdir(screenshots, { recursive: true });
const root = await mkdtemp(join(base, "lifecycle-fixture-"));
const relays = [];
const store = fixtureStore(root, { send: async options => { relays.push(options.prompt); } });
store.view.selectedId = "microsoft/apm/issue/4";
const pr = store.snapshot.prs.nodes[0];
store.snapshot.pulls[20] = { status: "current", observedAt: now(), data: { ...pr, evidence: {
  head: pr.headRefOid, observedAt: now(),
  checks: { complete: true, nodes: [{ name: "CLA", conclusion: "SUCCESS", status: "COMPLETED" }, { name: "Advice", conclusion: "NEUTRAL", status: "COMPLETED" }] },
  runs: { complete: true, observedAt: now(), nodes: Array.from({ length: 6 }, (_, i) => ({ workflow_id: i + 1,
    name: `Workflow ${i + 1}`, head_sha: pr.headRefOid, status: "completed", conclusion: "action_required",
    html_url: `https://github.com/microsoft/apm/actions/runs/${100 + i}`, created_at: now() })) },
} } };
const explanationRelays = [], explanationErrors = [], inferenceJobs = [];
store.explanations = new Explanations(store, { send: async options => {
  explanationRelays.push(options);
  const id = /Request ID: ([\da-f-]+)/.exec(options.prompt)[1];
  inferenceJobs.push((async () => {
    await sleep(80);
    try {
      const r = store.explanations.inspect(id), c = await store.explanations.claim({ id, revision: r.revision });
      await store.explanations.report({ id, revision: c.revision, claimToken: c.claimToken, contentKey: c.contentKey, status: "completed",
        result: { title: "[Fixture] Keep the audit focused on intended files",
          problem: "The fixture audit scans saved conversations as though they were installed instructions. That wastes time and flags unrelated text.",
          example: "Imagine checking one installed skill but scanning years of saved conversations in the same folder.",
          exampleKind: "illustrative", impact: "People need useful findings about their installed instructions, not noise from unrelated files.",
          decisionQuestion: { state: "resolved", text: "The fixture scope is agreed. Decide whether the blocked workflows can run in GitHub, then review the contribution." },
          tradeoff: "Narrow the scan without missing the prompt fields people intentionally installed.",
          citations: [c.packet.selected.url] } });
    } catch (error) { explanationErrors.push(error.message); }
  })());
  return "fixture-context-message";
} });
const server = await startServer(store, { instanceId: "lifecycle-fixture" });
const browser = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  "--headless=new", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${join(root, "profile")}`,
  "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-sync", "--disable-extensions", "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
let browserStderr = "";
browser.stderr.on("data", data => { browserStderr = (browserStderr + data).slice(-4000); });
let ws, sessionId, next = 0;
const pending = new Map();
const report = { round, fixtureOnly: true, themeViews: [], interactions: [], exceptions: [], screenshots: [] };
try {
  let portData;
  for (let i = 0; i < 600; i++) {
    try { portData = await readFile(join(root, "profile", "DevToolsActivePort"), "utf8"); break; }
    catch (error) { if (error.code !== "ENOENT") throw error; if (browser.exitCode !== null) break; await sleep(100); }
  }
  if (!portData) throw new Error(`Chrome did not expose DevTools; pid=${browser.pid}, exit=${browser.exitCode}, profile=${JSON.stringify(await readdir(join(root, "profile")).catch(() => []))}: ${browserStderr}`);
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
    if (message.method === "Runtime.exceptionThrown") report.exceptions.push(message.params.exceptionDetails.text);
  });
  const send = (method, params = {}, session = sessionId) => new Promise((resolve, reject) => {
    const id = ++next, timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
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
    for (let i = 0; i < 200; i++) { if (await evaluate(expression)) return; await sleep(100); }
    throw new Error(`UI did not settle: ${expression}`);
  };
  const click = async selector => { await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`); await sleep(200); };
  const screenshot = async suffix => {
    const filename = `decision-${round}-${suffix}.png`;
    const image = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(join(screenshots, filename), Buffer.from(image.data, "base64")); report.screenshots.push(filename);
  };
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: server.url });
  await wait(`document.querySelector('#decision h2')?.textContent.includes('[Fixture]')`);
  await wait(`document.querySelector('#decision')?.getAttribute('aria-busy')!=='true'`);
  await wait(`document.querySelector('.plain-context')`);
  for (const theme of ["light", "dark"]) {
    await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" }] });
    await evaluate(`document.documentElement.dataset.colorMode=${JSON.stringify(theme)}`);
    for (const width of [1440, 390]) {
      await send("Emulation.setDeviceMetricsOverride", { width, height: 1100, deviceScaleFactor: 1, mobile: width === 390 });
      await evaluate("document.body.classList.remove('detail-open');window.scrollTo(0,0)");
      await sleep(100);
      await screenshot(`${theme}-${width === 390 ? "narrow-list" : "desktop"}`);
      if (width === 390) {
        await click(`.work-row[aria-pressed="true"]`);
        await wait("document.body.classList.contains('detail-open')");
        await evaluate("window.scrollTo(0,0)"); await screenshot(`${theme}-narrow-detail`);
      }
      const metrics = await evaluate(`(() => {
        const blend=(a,b)=>[...a.slice(0,3).map((c,i)=>c*a[3]+b[i]*(1-a[3])),1];
        const canvas=document.createElement('canvas');canvas.width=canvas.height=1;const ctx=canvas.getContext('2d',{willReadFrequently:true});
        const rgba=c=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=c;ctx.fillRect(0,0,1,1);const p=[...ctx.getImageData(0,0,1,1).data];return [p[0],p[1],p[2],p[3]/255]};
        const background=e=>{const path=[];for(;e;e=e.parentElement)path.unshift(e);return path.reduce((bg,n)=>blend(rgba(getComputedStyle(n).backgroundColor),bg),[255,255,255,1])};
        const lum=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
        const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
        const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),texts=[];let node;
        while(node=walker.nextNode()){const e=node.parentElement;if(!node.textContent.trim()||!e.getClientRects().length||!e.checkVisibility({visibilityProperty:true})||['SCRIPT','STYLE','OPTION'].includes(e.tagName)||e.closest('[disabled]'))continue;
          let hidden=false;for(let parent=e;parent;parent=parent.parentElement){if(parent.matches('details:not([open])')&&!parent.querySelector(':scope > summary')?.contains(e)){hidden=true;break;}}if(hidden)continue;
          const style=getComputedStyle(e);if(style.visibility!=='visible')continue;const bg=background(e);
          texts.push({text:node.textContent.trim().slice(0,70),ratio:ratio(blend(rgba(style.color),bg),bg)});}
        const input=document.querySelector('#search');if(input.getClientRects().length){const bg=background(input);texts.push({text:'Search placeholder',ratio:ratio(blend(rgba(getComputedStyle(input,'::placeholder').color),bg),bg)});}
        const focus=['[data-view="decisions"]',innerWidth<680?'#back-to-list':'.work-row','#decision','[data-primary]'].map(selector=>{
          const e=document.querySelector(selector);if(!e?.getClientRects().length)return null;e.focus({preventScroll:true});const s=getComputedStyle(e);
          return {selector,visible:e.matches(':focus-visible'),width:s.outlineWidth,style:s.outlineStyle,ratio:ratio(rgba(s.outlineColor),background(e.parentElement))};
        }).filter(Boolean);
        const q=document.querySelector('.queue-pane').getBoundingClientRect(),d=document.querySelector('#decision').getBoundingClientRect();
        return {theme:document.documentElement.dataset.colorMode,width:innerWidth,scrollWidth:document.documentElement.scrollWidth,osDark:matchMedia('(prefers-color-scheme:dark)').matches,
          bg:getComputedStyle(document.body).backgroundColor,allTextCount:texts.length,minTextContrast:Math.min(...texts.map(t=>t.ratio)),lowTextContrast:texts.filter(t=>t.ratio<4.5),focus,
          selected:document.querySelector('.work-row[aria-pressed="true"]')?.dataset.select,queueRight:q.right,decisionLeft:d.left,narrowFocused:document.body.classList.contains('detail-open'),
          title:document.querySelector('#decision h2').textContent,total:document.querySelector('#scope-total').textContent,planningHidden:document.querySelector('#planning-filter').hidden,
          badLinks:[...document.querySelectorAll('a')].filter(a=>a.protocol==='javascript:').length,
          primaryCount:document.querySelectorAll('#decision [data-primary]').length,
          primaryBottom:document.querySelector('[data-primary]').getBoundingClientRect().bottom,
          relatedBottom:document.querySelector('.compact-pr').getBoundingClientRect().bottom,
          actionBeforeProse:document.querySelector('[data-primary]').getBoundingClientRect().top<document.querySelector('.plain-context').getBoundingClientRect().top,
          visibleJargon:texts.filter(t=>/\\b(worker|session|driver|transport receipt)\\b/i.test(t.text)).map(t=>t.text)};
      })()`);
      report.themeViews.push(metrics);
    }
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await evaluate("document.documentElement.dataset.colorMode='light';document.body.classList.remove('detail-open')");
  await click('[data-view="roadmap"]');
  await wait("document.querySelector('#scope-heading').textContent==='Roadmap'");
  const roadmap = await evaluate(`({count:+document.querySelector('#scope-total').textContent,groups:[...document.querySelectorAll('.list-group')].map(e=>e.textContent),planning:!document.querySelector('#planning-filter').hidden,badges:document.querySelectorAll('[data-horizon] .count').length})`);
  assert.equal(roadmap.count, 4); assert.equal(roadmap.badges, 0); assert.equal(roadmap.planning, true);
  report.interactions.push({ roadmap });
  await evaluate("document.querySelector('[data-horizon=\"Unset\"]').focus();document.querySelector('[data-horizon=\"Unset\"]').click()");
  await wait("document.querySelector('#scope-total').textContent==='2'");
  assert.equal(await evaluate("document.activeElement.dataset.horizon"), "Unset");
  await click('[data-view="triage"]');
  await click('[data-batch="microsoft/apm/issue/1"]');
  await click("#triage-batch");
  assert.equal(await evaluate("document.activeElement.id"), "action-composer");
  assert.equal(await evaluate("document.querySelector('[name=mode]').value"), "preview");
  await click("#close-composer");
  await click('[data-view="decisions"]');
  await evaluate("document.querySelector('.work-row[data-select=\"microsoft/apm/issue/1\"]').focus()");
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await wait("document.activeElement.id==='decision'");
  await evaluate("document.querySelector('[data-disclosure=\"actions\"]').open=true");
  await click('[data-action="scope-accept"]');
  await evaluate(`for(const [key,value] of Object.entries(${JSON.stringify(scopeFields)})){const e=document.querySelector('[name="'+key+'"]');if(e)e.value=value}`);
  await click('[data-view="triage"]');
  assert.equal(await evaluate("document.querySelector('[name=scope]').value"), scopeFields.scope);
  await evaluate("document.querySelector('#action-composer').scrollIntoView();document.querySelector('#composer-form').requestSubmit()");
  await wait("!!document.querySelector('#confirm-request')");
  assert.match(await evaluate("document.querySelector('#exact-preview').textContent"), /Decision: approve/);
  await screenshot("scope-preview");
  await click("#confirm-request");
  assert.equal(relays.length, 0);
  assert.match(await evaluate("document.querySelector('#error').textContent"), /Review the exact effects/);
  await click("#confirm-effects"); await click("#confirm-request");
  assert.equal(relays.length, 1);
  const r = store.bridge.state().requests.at(-1);
  assert.equal(r.status, "awaiting-host");
  assert.match(await evaluate("document.querySelector('#request-feedback').textContent"), /received by transport/);
  const claim = await store.bridge.claim({ id: r.id, revision: r.revision });
  await store.bridge.report({ id: r.id, revision: claim.revision, claimToken: claim.claimToken, status: "completed", message: "Fixture roundtrip verified. No GitHub effects.",
    gate: { actor: "fixture-maintainer", tool: "fixture-confirmation", reference: "test-only", observedAt: now(), confirmed: true },
    receipts: ["comment-readback", "metadata"].map(step => ({ step, tool: "fixture-readback", reference: "mock-only", observedAt: now(), outcome: "verified" })) });
  await evaluate("document.querySelector('#request-panel').open=true;document.querySelector('#request-list').scrollIntoView()");
  await wait("document.querySelector('#request-list').textContent.includes('Fixture roundtrip verified')");
  report.interactions.push({ exactScopePreview: true, explicitCheckbox: true, acknowledgementNotRunning: true, mockParentRoundtrip: true, composerSurvivesViewUpdate: true });
  await screenshot("request-receipt");
  await click("#close-composer");
  await click('[data-view="delivery"]'); await click('[data-view="admission"]');
  await wait("document.querySelector('#scope-heading').textContent==='Contribution admission'");
  assert.equal(await evaluate("document.querySelector('#scope-total').textContent"), "1");
  await click('[data-view="history"]');
  assert.match(await evaluate("document.querySelector('#decision .selection-meta').textContent"), /Completed/);
  await click('[data-view="decisions"]');
  await evaluate("document.querySelector('#search').value='does-not-exist';document.querySelector('#search').dispatchEvent(new Event('input',{bubbles:true}))");
  await wait("document.querySelector('#scope-total').textContent==='0'");
  assert.match(await evaluate("document.querySelector('#decision h2').textContent"), /No matching/);
  await click('[data-view="decisions"]');
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 1100, deviceScaleFactor: 1, mobile: true });
  await click('.work-row[data-select="microsoft/apm/issue/1"]');
  await wait("document.body.classList.contains('detail-open')");
  await click("#back-to-list");
  assert.equal(await evaluate("document.activeElement.dataset.select"), "microsoft/apm/issue/1");
  assert.equal(await evaluate("document.body.classList.contains('detail-open')"), false);
  report.interactions.push({ admissionSeparate: true, history: true, emptyFilter: true, narrowReturnPreservesSelection: true, keyboardSelect: true, horizonFocus: true });
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await evaluate("document.querySelector('[data-disclosure=\"evidence\"]').open=true;document.querySelector('[data-disclosure=\"evidence\"] summary').focus()");
  await wait("!!document.querySelector('.plain-context')");
  store.bridge.feed = { observedAt: now(), complete: false };
  await sleep(16000);
  assert.equal(await evaluate("document.querySelector('[data-disclosure=\"evidence\"]').open"), true);
  assert.equal(await evaluate("document.activeElement.textContent"), "Technical checks and source records");
  assert.ok(explanationRelays.length > 0);
  assert.ok(explanationRelays.every(r => r.mode === "immediate"));
  const keys = store.explanations.requests.map(r => r.contentKey);
  assert.equal(new Set(keys).size, keys.length);
  assert.deepEqual(explanationErrors, []);
  report.interactions.push({ automaticSelectedExplanation: true, immediateMockParentReceipt: true, contextDeduplicated: true, disclosureAndFocusSurvivePolling: true, explanationRelays: explanationRelays.length });
  const host = await evaluate(`(() => {const root=document.documentElement;root.style.setProperty('--background-color-default','#f9fafb');root.style.setProperty('--text-color-default','#24292f');root.style.setProperty('--true-color-blue','#0550ae');
    const result={bg:getComputedStyle(document.body).backgroundColor,fg:getComputedStyle(document.body).color,link:getComputedStyle(document.querySelector('#project-link')).color};root.removeAttribute('style');return result;})()`);
  assert.deepEqual(host, { bg: "rgb(249, 250, 251)", fg: "rgb(36, 41, 47)", link: "rgb(5, 80, 174)" });
  report.hostTokenOverride = host;
  assert.deepEqual(report.exceptions, []);
  for (const metrics of report.themeViews) {
    assert.equal(metrics.width, metrics.scrollWidth);
    assert.equal(metrics.osDark, metrics.theme === "light");
    assert.equal(metrics.bg, metrics.theme === "light" ? "rgb(255, 255, 255)" : "rgb(13, 17, 23)");
    assert.deepEqual(metrics.lowTextContrast, []);
    assert.equal(metrics.primaryCount, 1);
    assert.ok(metrics.primaryBottom < 900); assert.ok(metrics.relatedBottom < 1000);
    assert.equal(metrics.actionBeforeProse, true);
    assert.deepEqual(metrics.visibleJargon, []);
    assert.equal(metrics.planningHidden, true); assert.equal(metrics.badLinks, 0);
    if (metrics.width === 390) assert.equal(metrics.narrowFocused, true);
    else assert.ok(metrics.decisionLeft >= metrics.queueRight - 1);
    for (const f of metrics.focus) { assert.equal(f.visible, true); assert.equal(f.style, "solid"); assert.ok(parseFloat(f.width) >= 2); assert.ok(f.ratio >= 3); }
  }
  report.result = "passed";
} catch (error) {
  report.result = "failed"; report.error = error.stack;
  throw error;
} finally {
  await Promise.all(inferenceJobs);
  await writeFile(join(STORE, `browser-decision-${round}.json`), JSON.stringify(report, null, 2));
  for (const entry of pending.values()) clearTimeout(entry.timer);
  ws?.close(); browser.kill("SIGTERM");
  await new Promise(resolve => browser.exitCode !== null ? resolve() : browser.once("exit", resolve));
  await server.close(); await rm(root, { recursive: true, force: true });
  process.stdout.write(`${report.result}: ${report.themeViews.length} theme/viewport observations; ${report.interactions.length} interaction groups; ${report.exceptions.length} runtime exceptions\n`);
}
