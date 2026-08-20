#!/usr/bin/env node
// Dependency-free relative-link checker for the generated Astro/Starlight
// site under docs/dist. Walks every generated *.html page, extracts real
// <a href="..."> anchors (never raw markdown/code-fence text), resolves
// relative destinations against the page's own generated URL under the
// site's `/apm/` base, and verifies the target exists in dist as a file,
// a `<path>.html` file, or a `<path>/index.html` directory index.
//
// Usage: node docs/scripts/check-links.mjs [distDir] [base]
// Exits 0 when every relative link resolves, 1 otherwise (with diagnostics
// naming the source page, the original href, and the resolved destination).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DEFAULT_BASE = '/apm/';

// Matches an opening <a ...> tag and captures its href attribute value in
// either double or single quotes. Only matches real anchor elements -- text
// that merely *looks* like a link (e.g. inside a rendered <code> fence) has
// no href attribute on an <a> tag and is never captured.
const HREF_PATTERN = /<a\b[^>]*\shref\s*=\s*(["'])(.*?)\1[^>]*>/gi;

const IGNORED_SCHEME_PATTERN = /^(https?|mailto|tel|data|javascript):/i;

/** Extracts raw href attribute values from every <a> tag in an HTML string. */
export function extractHrefs(html) {
	const hrefs = [];
	let match = HREF_PATTERN.exec(html);
	while (match !== null) {
		hrefs.push(match[2]);
		match = HREF_PATTERN.exec(html);
	}
	return hrefs;
}

/**
 * True when an href is not a relative page/file destination the checker
 * should validate: empty values, in-page anchors, root-absolute links,
 * known non-filesystem schemes (http(s), mailto, tel, data, javascript),
 * and query-only values.
 */
export function shouldIgnoreHref(href) {
	if (!href) return true;
	if (href.startsWith('#')) return true;
	if (href.startsWith('/')) return true;
	if (href.startsWith('?')) return true;
	if (IGNORED_SCHEME_PATTERN.test(href)) return true;
	return false;
}

/** Strips a trailing query string and/or hash fragment for filesystem lookup. */
export function stripQueryAndHash(href) {
	return href.split(/[?#]/)[0];
}

/** Computes the site URL path (under `base`) a generated dist file is served at. */
export function pageUrlForDistFile(distDir, filePath, base) {
	const rel = path.relative(distDir, filePath).split(path.sep).join('/');
	let urlPath;
	if (rel === 'index.html') {
		urlPath = '';
	} else if (rel.endsWith('/index.html')) {
		urlPath = rel.slice(0, -'index.html'.length);
	} else if (rel.endsWith('.html')) {
		urlPath = rel.slice(0, -'.html'.length);
	} else {
		urlPath = rel;
	}
	const normalizedBase = base.endsWith('/') ? base : `${base}/`;
	return normalizedBase + urlPath.replace(/^\//, '');
}

/** Resolves a relative href against the page's own site URL path. */
export function resolveHref(href, pageUrlPath) {
	const origin = 'https://docs.invalid';
	const baseUrl = new URL(pageUrlPath, origin);
	const resolved = new URL(stripQueryAndHash(href), baseUrl);
	return resolved.pathname;
}

/** Checks whether a resolved site path exists in dist as a file/.html/index.html. */
export function targetExistsInDist(distDir, resolvedPathname, base) {
	const normalizedBase = base.endsWith('/') ? base : `${base}/`;
	if (!resolvedPathname.startsWith(normalizedBase)) return false;

	let rel = resolvedPathname.slice(normalizedBase.length);
	rel = rel.replace(/\/+$/, '');

	const candidates =
		rel === ''
			? [path.join(distDir, 'index.html')]
			: [
					path.join(distDir, rel),
					path.join(distDir, `${rel}.html`),
					path.join(distDir, rel, 'index.html'),
				];

	return candidates.some((candidate) => {
		try {
			return fs.statSync(candidate).isFile();
		} catch {
			return false;
		}
	});
}

/** Recursively lists every *.html file under a directory. */
function listHtmlFiles(dir) {
	const results = [];
	for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
		const fullPath = path.join(dir, entry.name);
		if (entry.isDirectory()) {
			results.push(...listHtmlFiles(fullPath));
		} else if (entry.isFile() && entry.name.endsWith('.html')) {
			results.push(fullPath);
		}
	}
	return results;
}

/**
 * Walks every generated HTML page under distDir and validates relative
 * <a href> destinations. Returns { errors, checked } where errors is an
 * array of { sourceFile, href, resolvedPath } for every unresolved link,
 * and checked is the count of relative links actually validated.
 */
export function findBrokenLinks(distDir, { base = DEFAULT_BASE } = {}) {
	const errors = [];
	let checked = 0;

	for (const filePath of listHtmlFiles(distDir)) {
		const html = fs.readFileSync(filePath, 'utf8');
		const pageUrlPath = pageUrlForDistFile(distDir, filePath, base);

		for (const href of extractHrefs(html)) {
			if (shouldIgnoreHref(href)) continue;

			checked += 1;
			const resolvedPath = resolveHref(href, pageUrlPath);
			if (!targetExistsInDist(distDir, resolvedPath, base)) {
				errors.push({
					sourceFile: path.relative(distDir, filePath),
					href,
					resolvedPath,
				});
			}
		}
	}

	return { errors, checked };
}

function isMainModule() {
	if (!process.argv[1]) return false;
	return fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
}

if (isMainModule()) {
	const distDir = path.resolve(process.argv[2] || path.join(process.cwd(), 'dist'));
	const base = process.argv[3] || DEFAULT_BASE;

	if (!fs.existsSync(distDir)) {
		console.error(`[x] Dist directory not found: ${distDir} (run \`astro build\` first)`);
		process.exit(1);
	}

	const { errors, checked } = findBrokenLinks(distDir, { base });

	if (errors.length > 0) {
		console.error(
			`[x] Found ${errors.length} broken relative link(s) across generated pages (checked ${checked} relative link(s)):\n`,
		);
		for (const error of errors) {
			console.error(`  page:     ${error.sourceFile}`);
			console.error(`  href:     ${error.href}`);
			console.error(`  resolved: ${error.resolvedPath} (not found in dist)\n`);
		}
		process.exit(1);
	}

	console.log(`[+] Checked ${checked} relative link(s) across generated pages. No broken relative links found.`);
}
