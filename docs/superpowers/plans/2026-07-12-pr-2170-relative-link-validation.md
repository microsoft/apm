# PR 2170 Relative Link Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the remaining relative links and make the docs build reject future relative-link depth errors.

**Architecture:** Astro builds the canonical output. A dependency-free Node post-build checker resolves relative `href` values from generated page URLs and verifies their targets exist under `dist/`.

**Tech Stack:** Astro, Node.js standard library, npm, Markdown, git

---

### Task 1: Fix the three missed links

**Files:**
- Modify: `docs/src/content/docs/enterprise/registry-proxy.md:281`
- Modify: `docs/src/content/docs/enterprise/security.md:51`
- Modify: `docs/src/content/docs/reference/environment-variables.md:45`

- [ ] **Step 1: Change each target**

Replace:

```markdown
../troubleshooting/ssl-issues/
```

with:

```markdown
../../troubleshooting/ssl-issues/
```

- [ ] **Step 2: Build once to establish the current validator gap**

```bash
cd docs
npm ci
npm run build
```

Expected: PASS. This confirms the existing validator does not protect relative
destinations by itself.

### Task 2: Add the generated-output checker

**Files:**
- Create: `docs/scripts/check-relative-links.mjs`
- Modify: `docs/package.json`

- [ ] **Step 1: Add the checker**

```javascript
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';

const dist = resolve('dist');
const failures = [];

function walk(directory) {
	const files = [];
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) files.push(...walk(path));
		else if (entry.name.endsWith('.html')) files.push(path);
	}
	return files;
}

function isIgnored(href) {
	return /^(?:#|\/|https?:|mailto:|tel:|data:|javascript:)/i.test(href);
}

function destinationExists(pathname) {
	const decoded = decodeURIComponent(pathname).replace(/^\/apm\//, '');
	const candidate = join(dist, ...decoded.split('/'));
	return (
		existsSync(candidate) ||
		existsSync(`${candidate}.html`) ||
		existsSync(join(candidate, 'index.html'))
	);
}

for (const file of walk(dist)) {
	const html = readFileSync(file, 'utf8');
	const pagePath = relative(dist, file).split(sep).join('/').replace(/index\.html$/, '');
	const pageUrl = new URL(pagePath, 'https://docs.invalid/apm/');
	for (const match of html.matchAll(/\bhref=(["'])(.*?)\1/g)) {
		const href = match[2];
		if (isIgnored(href)) continue;
		const target = new URL(href, pageUrl);
		if (!target.pathname.startsWith('/apm/') || !destinationExists(target.pathname)) {
			failures.push(`${relative(dist, file)}: ${href} -> ${target.pathname}`);
		}
	}
}

if (failures.length) {
	console.error('[x] Broken relative documentation links:');
	for (const failure of failures) console.error(failure);
	process.exit(1);
}

console.log('[+] Relative documentation links are valid');
```

- [ ] **Step 2: Wire it after Astro**

Change the package script to:

```json
"build": "astro build && node scripts/check-relative-links.mjs"
```

- [ ] **Step 3: Run the build**

```bash
cd docs
npm run build
```

Expected: Astro succeeds and the checker prints
`[+] Relative documentation links are valid`.

- [ ] **Step 4: Mutation-break one corrected link**

Revert `enterprise/security.md` to `../troubleshooting/ssl-issues/`, run
`npm run build`, and verify the checker exits 1 and names the resolved missing
destination. Restore the link and rerun to green.

- [ ] **Step 5: Commit and push with contributor credit**

```bash
git add docs/package.json docs/scripts/check-relative-links.mjs \
  docs/src/content/docs/enterprise/registry-proxy.md \
  docs/src/content/docs/enterprise/security.md \
  docs/src/content/docs/reference/environment-variables.md
git commit -m "test(docs): validate relative link destinations" \
  -m "Co-authored-by: Jason Tame <jason.tame@tillo.io>" \
  -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>" \
  -m "Copilot-Session: 7955c89b-a997-42aa-9c45-ef4c7fe4b1e7"
git remote get-url contributor >/dev/null 2>&1 \
  || git remote add contributor https://github.com/JasonTame/apm.git
git push contributor HEAD:fix/docs-relative-link-depths
```
