// Tests for check-links.mjs -- run with: node --test docs/scripts/check-links.test.mjs
//
// Uses Node's built-in test runner (node:test) and creates throwaway dist
// fixtures under docs/scripts/.tmp-fixture-* (cleaned up after each test).
// No external dependencies.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import {
	extractHrefs,
	shouldIgnoreHref,
	stripQueryAndHash,
	findBrokenLinks,
} from './check-links.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CHECKER_PATH = path.join(__dirname, 'check-links.mjs');

/** Writes a fixture dist directory tree and returns its absolute path. */
function makeFixtureDist(files) {
	const dir = fs.mkdtempSync(path.join(__dirname, '.tmp-fixture-'));
	for (const [relPath, content] of Object.entries(files)) {
		const filePath = path.join(dir, relPath);
		fs.mkdirSync(path.dirname(filePath), { recursive: true });
		fs.writeFileSync(filePath, content, 'utf8');
	}
	return dir;
}

function cleanup(dir) {
	fs.rmSync(dir, { recursive: true, force: true });
}

test('extractHrefs finds anchor hrefs in both double- and single-quote styles', () => {
	const html = `<a href="../a/">Link A</a><a href='../b/'>Link B</a>`;
	assert.deepEqual(extractHrefs(html), ['../a/', '../b/']);
});

test('extractHrefs does not treat link-like text inside code fences as a real link', () => {
	// Rendered markdown code fences show sample link syntax as plain text
	// inside <pre><code>, never as an <a href> element.
	const html = `<pre><code class="language-md">[SSL](../troubleshooting/ssl-issues/)</code></pre>`;
	assert.deepEqual(extractHrefs(html), []);
});

test('shouldIgnoreHref ignores anchors, root-absolute links, known schemes, and query-only values', () => {
	assert.equal(shouldIgnoreHref('#section'), true);
	assert.equal(shouldIgnoreHref('/apm/foo/'), true);
	assert.equal(shouldIgnoreHref('https://example.com'), true);
	assert.equal(shouldIgnoreHref('http://example.com'), true);
	assert.equal(shouldIgnoreHref('mailto:hi@example.com'), true);
	assert.equal(shouldIgnoreHref('tel:+1234567890'), true);
	assert.equal(shouldIgnoreHref('data:text/plain,hi'), true);
	assert.equal(shouldIgnoreHref('javascript:void(0)'), true);
	assert.equal(shouldIgnoreHref('?foo=bar'), true);
	assert.equal(shouldIgnoreHref(''), true);
});

test('shouldIgnoreHref does not ignore relative page/file destinations', () => {
	assert.equal(shouldIgnoreHref('../sibling/'), false);
	assert.equal(shouldIgnoreHref('./sibling/'), false);
	assert.equal(shouldIgnoreHref('sibling/'), false);
	assert.equal(shouldIgnoreHref('../sibling.html'), false);
});

test('stripQueryAndHash removes query strings and hash fragments for filesystem lookup', () => {
	assert.equal(stripQueryAndHash('../a/b/?x=1'), '../a/b/');
	assert.equal(stripQueryAndHash('../a/b/#section'), '../a/b/');
	assert.equal(stripQueryAndHash('../a/b/?x=1#section'), '../a/b/');
	assert.equal(stripQueryAndHash('../a/b/'), '../a/b/');
});

test('findBrokenLinks passes for a valid sibling link and a valid parent link', () => {
	const dist = makeFixtureDist({
		'enterprise/registry-proxy/index.html': '<a href="../../troubleshooting/ssl-issues/">SSL</a>',
		'enterprise/security/index.html': '<a href="../drift-detection/">Drift</a>',
		'enterprise/drift-detection/index.html': '<p>ok</p>',
		'troubleshooting/ssl-issues/index.html': '<p>ok</p>',
	});
	try {
		const { errors, checked } = findBrokenLinks(dist, { base: '/apm/' });
		assert.deepEqual(errors, []);
		assert.ok(checked >= 2, 'expected at least two links to have been checked');
	} finally {
		cleanup(dist);
	}
});

test('findBrokenLinks resolves a link to a bare .html file target (no directory/index.html)', () => {
	const dist = makeFixtureDist({
		'a/index.html': '<a href="../b.html">B</a>',
		'b.html': '<p>ok</p>',
	});
	try {
		const { errors } = findBrokenLinks(dist, { base: '/apm/' });
		assert.deepEqual(errors, []);
	} finally {
		cleanup(dist);
	}
});

test('findBrokenLinks resolves an extensionless link against a sibling .html file', () => {
	const dist = makeFixtureDist({
		'a/index.html': '<a href="../b">B</a>',
		'b.html': '<p>ok</p>',
	});
	try {
		const { errors } = findBrokenLinks(dist, { base: '/apm/' });
		assert.deepEqual(errors, []);
	} finally {
		cleanup(dist);
	}
});

test('findBrokenLinks reports a missing target with actionable diagnostics', () => {
	const dist = makeFixtureDist({
		// One `../` too few: from /apm/enterprise/registry-proxy/ this only
		// climbs to /apm/enterprise/, landing on a path that doesn't exist.
		'enterprise/registry-proxy/index.html': '<a href="../troubleshooting/ssl-issues/">SSL</a>',
		'troubleshooting/ssl-issues/index.html': '<p>ok</p>',
	});
	try {
		const { errors } = findBrokenLinks(dist, { base: '/apm/' });
		assert.equal(errors.length, 1);
		const [error] = errors;
		assert.match(error.sourceFile, /enterprise[/\\]registry-proxy[/\\]index\.html$/);
		assert.equal(error.href, '../troubleshooting/ssl-issues/');
		assert.equal(error.resolvedPath, '/apm/enterprise/troubleshooting/ssl-issues/');
	} finally {
		cleanup(dist);
	}
});

test('findBrokenLinks ignores anchors, root-absolute, external, mailto, and query-only links', () => {
	const dist = makeFixtureDist({
		'index.html': [
			'<a href="#top">Top</a>',
			'<a href="/apm/enterprise/">Enterprise</a>',
			'<a href="https://github.com">GH</a>',
			'<a href="mailto:hi@example.com">Mail</a>',
			'<a href="?foo=bar">Query</a>',
		].join(''),
	});
	try {
		const { errors, checked } = findBrokenLinks(dist, { base: '/apm/' });
		assert.deepEqual(errors, []);
		assert.equal(checked, 0);
	} finally {
		cleanup(dist);
	}
});

test('findBrokenLinks strips query/hash before resolving and still catches a broken target', () => {
	const dist = makeFixtureDist({
		'enterprise/registry-proxy/index.html':
			'<a href="../troubleshooting/ssl-issues/?utm_source=x#top">SSL</a>',
	});
	try {
		const { errors } = findBrokenLinks(dist, { base: '/apm/' });
		assert.equal(errors.length, 1);
		assert.equal(errors[0].resolvedPath, '/apm/enterprise/troubleshooting/ssl-issues/');
	} finally {
		cleanup(dist);
	}
});

test('CLI exits 0 with no output errors when all links resolve', () => {
	const dist = makeFixtureDist({
		'enterprise/registry-proxy/index.html': '<a href="../../troubleshooting/ssl-issues/">SSL</a>',
		'troubleshooting/ssl-issues/index.html': '<p>ok</p>',
	});
	try {
		const output = execFileSync('node', [CHECKER_PATH, dist], { encoding: 'utf8' });
		assert.match(output, /No broken relative links/);
	} finally {
		cleanup(dist);
	}
});

test('CLI exits non-zero with actionable diagnostics when a link is broken', () => {
	const dist = makeFixtureDist({
		'enterprise/registry-proxy/index.html': '<a href="../troubleshooting/ssl-issues/">SSL</a>',
	});
	try {
		let threw = false;
		try {
			execFileSync('node', [CHECKER_PATH, dist], { encoding: 'utf8' });
		} catch (error) {
			threw = true;
			assert.notEqual(error.status, 0);
			const combined = `${error.stdout || ''}${error.stderr || ''}`;
			assert.match(combined, /registry-proxy/);
			assert.match(combined, /\.\.\/troubleshooting\/ssl-issues\//);
			assert.match(combined, /\/apm\/enterprise\/troubleshooting\/ssl-issues\//);
		}
		assert.ok(threw, 'expected CLI to exit with a non-zero status');
	} finally {
		cleanup(dist);
	}
});
