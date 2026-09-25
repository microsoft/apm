import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { STORE } from "./config.mjs";
import { Store } from "./store.mjs";
import { startServer } from "./server.mjs";
import { defaultView, projectView } from "./scope.mjs";

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const round = process.argv[2] || "initial";
if (!["initial", "confirm", "primer-initial", "primer-confirm"].includes(round)) throw new Error("Use initial, confirm, primer-initial or primer-confirm.");
const base = join(STORE, "browser-runs");
await mkdir(base, { recursive: true });
const root = await mkdtemp(join(base, `${round}-`));
const screenshots = join(STORE, "screenshots");
await mkdir(screenshots, { recursive: true });
const snapshotFile = join(STORE, "repos/microsoft/apm/snapshot.json");
const snapshot = JSON.parse(await readFile(snapshotFile, "utf8")).data;
const store = new Store({ root });
store.snapshot = snapshot;
store.view = { ...defaultView(), selectedId: "microsoft/apm/issue/3015" };
const expected = projectView(store.state().items, store.view);
const server = await startServer(store);
const browser = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  "--headless=new", "--disable-gpu", "--remote-debugging-port=0",
  `--user-data-dir=${join(root, "profile")}`, "--no-first-run", "--no-default-browser-check",
  "--disable-background-networking", "--disable-component-update", "--disable-sync",
  "--disable-extensions", "about:blank",
], { stdio: ["ignore", "ignore", "ignore"] });
let ws;
const pending = new Map();
let next = 0;
let sessionId;
const exceptions = [];
const observations = { round, exceptions };
try {
  let portData;
  for (let i = 0; i < 100; i++) {
    try { portData = await readFile(join(root, "profile", "DevToolsActivePort"), "utf8"); break; }
    catch { await sleep(100); }
  }
  if (!portData) throw new Error("Installed Chrome did not expose DevTools.");
  const [port, path] = portData.trim().split("\n");
  ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
  await new Promise((resolve, reject) => { ws.addEventListener("open", resolve, { once: true }); ws.addEventListener("error", reject, { once: true }); });
  ws.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject, timer } = pending.get(message.id);
      clearTimeout(timer); pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
    }
    if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails.text);
  });
  const send = (method, params = {}, session = sessionId) => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 20000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params, ...(session ? { sessionId: session } : {}) }));
  });
  const target = await send("Target.createTarget", { url: "about:blank" }, null);
  const attached = await send("Target.attachToTarget", { targetId: target.targetId, flatten: true }, null);
  sessionId = attached.sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  const evaluate = async expression => {
    const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: server.url });
  for (let i = 0; i < 100; i++) {
    if (await evaluate("document.querySelector('#decision h2')?.textContent.includes('prune')")) break;
    await sleep(100);
  }
  const desktop = await evaluate(`({
    width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
    rows:document.querySelectorAll('.work-row').length,
    title:document.querySelector('#decision h2')?.textContent,
    recommendation:document.querySelector('.recommendation h3')?.textContent,
    horizons:[...document.querySelectorAll('[data-horizon]')].map(e=>e.textContent),
    badLinks:[...document.querySelectorAll('a')].filter(a=>a.protocol==='javascript:').length,
    font:getComputedStyle(document.body).fontFamily,
    bodyText:document.querySelector('#decision').textContent,
    queueRight:document.querySelector('.queue-pane').getBoundingClientRect().right,
    decisionLeft:document.querySelector('#decision').getBoundingClientRect().left
  })`);
  observations.desktop = { ...desktop, bodyText: undefined };
  assert.equal(desktop.scrollWidth, desktop.width);
  assert.ok(desktop.rows > 0);
  assert.ok(desktop.horizons.length === 5);
  assert.equal(desktop.badLinks, 0);
  assert.ok(desktop.decisionLeft >= desktop.queueRight - 1);
  assert.match(desktop.bodyText, /What it does not do/);
  assert.match(desktop.bodyText, /Execution agent/);
  const screenshot = async name => {
    const image = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(join(screenshots, `${round}-${name}.png`), Buffer.from(image.data, "base64"));
  };
  await screenshot("desktop");
  const themeViews = [];
  observations.themeViews = themeViews;
  if (round.startsWith("primer-")) {
    for (const theme of ["light", "dark"]) {
      await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: theme === "light" ? "dark" : "light" }] });
      await evaluate(`document.documentElement.dataset.colorMode=${JSON.stringify(theme)}`);
      for (const width of [1440, 390]) {
        await send("Emulation.setDeviceMetricsOverride", { width, height: 1100, deviceScaleFactor: 1, mobile: width === 390 });
        await evaluate("window.scrollTo(0,0)");
        await sleep(100);
        const metrics = await evaluate(`(() => {
          const blend=(a,b)=>[...a.slice(0,3).map((c,i)=>c*a[3]+b[i]*(1-a[3])),1];
          const canvas=document.createElement('canvas'); canvas.width=canvas.height=1;
          const ctx=canvas.getContext('2d',{willReadFrequently:true});
          const rgba=c=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=c;ctx.fillRect(0,0,1,1);const p=[...ctx.getImageData(0,0,1,1).data];return [p[0],p[1],p[2],p[3]/255]};
          const background=e=>{const path=[];for(;e;e=e.parentElement)path.unshift(e);return path.reduce((bg,n)=>blend(rgba(getComputedStyle(n).backgroundColor),bg),[255,255,255,1])};
          const lum=c=>c.slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
          const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)/(Math.min(lum(a),lum(b))+.05);
          const pairs=['.decision h2','.recommendation h3','.recommendation .read-only','.recommendation .tradeoff','.state-label[data-state="open"]','.queue-help','.scope-basis','.selection-meta','.row-context','.row-linked','.count','a','.freshness-stale'];
          const contrast=pairs.flatMap(selector=>{const e=document.querySelector(selector);if(!e)return [];const bg=background(e),fg=blend(rgba(getComputedStyle(e).color),bg);return [{selector,ratio:ratio(fg,bg),color:getComputedStyle(e).color,background:bg.slice(0,3)}]});
          const text=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT),all=[];let node;
          while(node=text.nextNode()){const e=node.parentElement;if(!node.textContent.trim()||!e.getClientRects().length||['SCRIPT','STYLE','OPTION'].includes(e.tagName))continue;
            const style=getComputedStyle(e);if(style.visibility!=='visible')continue;const bg=background(e),fg=blend(rgba(style.color),bg);
            all.push({text:node.textContent.trim().slice(0,70),ratio:ratio(fg,bg)});
          }
          const input=document.querySelector('#search'),placeholder=getComputedStyle(input,'::placeholder');
          all.push({text:'Search placeholder',ratio:ratio(blend(rgba(placeholder.color),background(input)),background(input))});
          const focus=['#scope','[data-horizon="Now"]','.work-row','.queue > summary'].map(selector=>{
            const e=document.querySelector(selector);e.focus({preventScroll:true});const style=getComputedStyle(e);
            return {selector,visible:e.matches(':focus-visible'),width:style.outlineWidth,style:style.outlineStyle,ratio:ratio(rgba(style.outlineColor),background(e.parentElement))};
          });
          const queue=document.querySelector('.queue-pane').getBoundingClientRect(),decision=document.querySelector('#decision').getBoundingClientRect();
          return {width:innerWidth,scrollWidth:document.documentElement.scrollWidth,theme:document.documentElement.dataset.colorMode,contrast,focus,
            allTextCount:all.length,minTextContrast:Math.min(...all.map(t=>t.ratio)),lowTextContrast:all.filter(t=>t.ratio<4.5),
            queueRight:queue.right,decisionLeft:decision.left,queueBottom:queue.bottom,decisionTop:decision.top,
            headline:document.querySelector('#scope-heading')?.textContent,total:document.querySelector('#scope-total')?.textContent,
            queueTotal:document.querySelector('#queue-total').textContent,
            queueSum:[...document.querySelectorAll('.queue > summary .count')].reduce((sum,e)=>sum+Number(e.textContent),0),
            horizonCounts:Object.fromEntries([...document.querySelectorAll('[data-horizon]')].map(e=>[e.dataset.horizon,Number(e.querySelector('.count').textContent)])),
            horizonAll:document.querySelector('[data-horizon="All"]')?.textContent,
            bg:getComputedStyle(document.body).backgroundColor,osDark:matchMedia('(prefers-color-scheme:dark)').matches};
        })()`);
        themeViews.push(metrics);
        await screenshot(`${theme}-${width === 390 ? "narrow" : "desktop"}`);
        if (width === 390) {
          await evaluate("document.querySelector('#decision').scrollIntoView()");
          await screenshot(`${theme}-narrow-decision`);
        }
      }
    }
    await evaluate("document.documentElement.dataset.colorMode='light'");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
    await evaluate("window.scrollTo(0,0)");
  }
  await evaluate(`document.querySelector('[data-horizon="Now"]').focus(); document.querySelector('[data-horizon="Now"]').click()`);
  await sleep(200);
  const focusAfterHorizon = await evaluate("document.activeElement?.dataset.horizon || document.activeElement?.tagName");
  await evaluate(`document.querySelector('#search').value='3015'; document.querySelector('#search').dispatchEvent(new Event('input',{bubbles:true}))`);
  await sleep(500);
  const filtered = await evaluate(`({count:document.querySelectorAll('.work-row').length,title:document.querySelector('#decision h2')?.textContent,selected:document.querySelector('.work-row[aria-pressed="true"]')?.dataset.select})`);
  assert.ok(filtered.count >= 1);
  assert.equal(filtered.selected, "microsoft/apm/issue/3015");
  await evaluate(`document.querySelector('.work-row[data-select="microsoft/apm/issue/3015"]').focus()`);
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r", unmodifiedText: "\r" });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  for (let i = 0; i < 200; i++) {
    if (await evaluate("document.activeElement?.id === 'decision'")) break;
    await sleep(100);
  }
  const keyboardSelect = await evaluate("document.activeElement?.id");
  const keyboardState = await evaluate(`({active:document.activeElement?.outerHTML?.slice(0,400),status:document.querySelector('#status').textContent,error:document.querySelector('#error').textContent,busy:document.querySelector('#decision').getAttribute('aria-busy')})`);
  const scopeChecks = [];
  observations.scopeChecks = scopeChecks;
  if (round.startsWith("primer-")) {
    const beforeEmptySelection = store.state().view.selectedId;
    await evaluate(`document.querySelector('#search').value='no-such-issue-filter';document.querySelector('#search').dispatchEvent(new Event('input',{bubbles:true}))`);
    await sleep(500);
    const empty = await evaluate(`({total:document.querySelector('#scope-total').textContent,selection:document.querySelector('.work-row[aria-pressed="true"]')?.dataset.select||null,title:document.querySelector('#decision h2').textContent,notice:document.querySelector('#view-notice').textContent})`);
    assert.equal(Number(empty.total), 0);
    assert.equal(empty.selection, null);
    assert.match(empty.title, /No matching/);
    if (beforeEmptySelection) assert.match(empty.notice, /No matching/);
    scopeChecks.push({ scope: "empty-filter", ...empty });
    for (const scope of ["roadmap-history", "repository-prs", "roadmap-open"]) {
      await evaluate(`document.querySelector('#scope').value=${JSON.stringify(scope)};document.querySelector('#scope').dispatchEvent(new Event('change',{bubbles:true}))`);
      await sleep(350);
      const actual = await evaluate(`({total:Number(document.querySelector('#scope-total').textContent),headline:document.querySelector('#scope-heading').textContent,search:document.querySelector('#search').value,
        horizon:document.querySelector('[data-horizon][aria-pressed="true"]').dataset.horizon,notice:document.querySelector('#view-notice').textContent,
        selected:document.querySelector('.work-row[aria-pressed="true"]')?.dataset.select||null,linkedSelectButtons:document.querySelectorAll('.related-row [data-select]').length,
        queueSum:[...document.querySelectorAll('.queue > summary .count')].reduce((sum,e)=>sum+Number(e.textContent),0)})`);
      const projected = projectView(store.state().items, { ...defaultView(), scope });
      assert.equal(actual.total, projected.filteredTotal);
      assert.equal(actual.queueSum, actual.total);
      assert.equal(actual.search, "");
      assert.equal(actual.horizon, "All");
      assert.equal(actual.linkedSelectButtons, 0);
      assert.ok(projected.visible.some(item => item.id === store.state().view.selectedId));
      scopeChecks.push({ scope, ...actual });
    }
    const hostTokens = { "--background-color-default": "#f9fafb", "--text-color-default": "#24292f", "--true-color-blue": "#0550ae", "--true-color-blue-muted": "#ddf4ff", "--color-focus-outline": "#0550ae" };
    const host = await evaluate(`(() => {const root=document.documentElement;for(const [name,value] of Object.entries(${JSON.stringify(hostTokens)}))root.style.setProperty(name,value);
      const result={background:getComputedStyle(document.body).backgroundColor,color:getComputedStyle(document.body).color,link:getComputedStyle(document.querySelector('#project-link')).color,recommendation:getComputedStyle(document.querySelector('.recommendation')).backgroundColor};
      for(const name of ${JSON.stringify(Object.keys(hostTokens))})root.style.removeProperty(name);return result;})()`);
    assert.deepEqual(host, { background: "rgb(249, 250, 251)", color: "rgb(36, 41, 47)", link: "rgb(5, 80, 174)", recommendation: "rgb(221, 244, 255)" });
    scopeChecks.push({ hostTokenOverride: host });
  }
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 1100, deviceScaleFactor: 1, mobile: true });
  await evaluate("window.scrollTo(0,0)");
  await sleep(200);
  const narrow = await evaluate(`({
    width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
    queueBottom:document.querySelector('.queue-pane').getBoundingClientRect().bottom,
    decisionTop:document.querySelector('#decision').getBoundingClientRect().top,
    buttonWidth:document.querySelector('#refresh').getBoundingClientRect().width
  })`);
  assert.equal(narrow.scrollWidth, narrow.width);
  assert.ok(narrow.decisionTop >= narrow.queueBottom - 1);
  await screenshot("narrow");
  await evaluate("document.querySelector('#decision').scrollIntoView()");
  await screenshot("narrow-decision");
  assert.deepEqual(exceptions, []);
  const report = { round, desktop: { ...desktop, bodyText: undefined }, narrow, themeViews, scopeChecks,
    expectedScope: { total: expected.universeTotal, horizons: expected.horizonCounts, snapshotObservedAt: snapshot.sources.roadmap.observedAt },
    focusAfterHorizon, keyboardSelect, keyboardState, exceptions, screenshots };
  await writeFile(join(STORE, `browser-${round}.json`), JSON.stringify(report, null, 2));
  Object.assign(observations, report);
  process.stdout.write(JSON.stringify(report, null, 2) + "\n");
  assert.equal(keyboardSelect, "decision");
  assert.equal(focusAfterHorizon, "Now");
  for (const metrics of themeViews) {
    assert.equal(metrics.width, metrics.scrollWidth);
    assert.match(metrics.horizonAll, /^All horizons/);
    assert.deepEqual(metrics.horizonCounts, expected.horizonCounts);
    assert.equal(Number(metrics.total), expected.universeTotal);
    assert.equal(metrics.queueSum, expected.universeTotal);
    assert.equal(metrics.osDark, metrics.theme === "light");
    assert.equal(metrics.bg, metrics.theme === "light" ? "rgb(255, 255, 255)" : "rgb(13, 17, 23)");
    assert.deepEqual(metrics.lowTextContrast, []);
    if (metrics.width === 390) assert.ok(metrics.decisionTop >= metrics.queueBottom - 1);
    else assert.ok(metrics.decisionLeft >= metrics.queueRight - 1);
    for (const contrast of metrics.contrast) assert.ok(contrast.ratio >= 4.5, `${metrics.theme} ${contrast.selector}: ${contrast.ratio}`);
    for (const focus of metrics.focus) {
      assert.equal(focus.visible, true);
      assert.equal(focus.style, "solid");
      assert.ok(Number.parseFloat(focus.width) >= 2);
      assert.ok(focus.ratio >= 3, `${metrics.theme} focus ${focus.selector}: ${focus.ratio}`);
    }
  }
} catch (error) {
  await writeFile(join(STORE, `browser-${round}.json`), JSON.stringify({ ...observations, error: error.message }, null, 2));
  throw error;
} finally {
  for (const entry of pending.values()) clearTimeout(entry.timer);
  ws?.close();
  browser.kill("SIGTERM");
  await new Promise(resolve => { if (browser.exitCode !== null) resolve(); else browser.once("exit", resolve); });
  await server.close();
  await rm(root, { recursive: true, force: true });
}
