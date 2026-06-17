// Test suite for markdown utility functions
// Run: node --test tests/markdown.test.mjs

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { escapeHtml, renderMarkdown } from "../client/src/utils/markdown.js";

// ---------------------------------------------------------------------------
// escapeHtml
// ---------------------------------------------------------------------------

describe("escapeHtml", () => {
  // Null / undefined / empty ------------------------------------------------
  it("returns empty string for null", () => {
    assert.equal(escapeHtml(null), "");
  });

  it("returns empty string for undefined", () => {
    assert.equal(escapeHtml(undefined), "");
  });

  it("returns empty string for empty string", () => {
    assert.equal(escapeHtml(""), "");
  });

  // Individual special characters -------------------------------------------
  it("escapes & to &amp;", () => {
    assert.equal(escapeHtml("cats & dogs"), "cats &amp; dogs");
  });

  it("escapes < to &lt;", () => {
    assert.equal(escapeHtml("a < b"), "a &lt; b");
  });

  it("escapes > to &gt;", () => {
    assert.equal(escapeHtml("a > b"), "a &gt; b");
  });

  it('escapes " to &quot;', () => {
    assert.equal(escapeHtml('say "hello"'), "say &quot;hello&quot;");
  });

  // Multiple special characters in one string --------------------------------
  it("escapes all four special chars in one string", () => {
    assert.equal(
      escapeHtml('<a href="x">link & more</a>'),
      "&lt;a href=&quot;x&quot;&gt;link &amp; more&lt;/a&gt;",
    );
  });

  it("escapes repeated & characters", () => {
    assert.equal(escapeHtml("a & b & c"), "a &amp; b &amp; c");
  });

  it("escapes a raw HTML script tag", () => {
    assert.equal(
      escapeHtml("<script>alert(1)</script>"),
      "&lt;script&gt;alert(1)&lt;/script&gt;",
    );
  });

  // Normal text passthrough --------------------------------------------------
  it("passes through plain alphabetic text unchanged", () => {
    assert.equal(escapeHtml("hello world"), "hello world");
  });

  it("passes through numbers and punctuation unchanged", () => {
    assert.equal(escapeHtml("foo-bar_baz.123"), "foo-bar_baz.123");
  });

  it("passes through single-quote unchanged (not escaped)", () => {
    assert.equal(escapeHtml("it's fine"), "it's fine");
  });
});

// ---------------------------------------------------------------------------
// renderMarkdown
// ---------------------------------------------------------------------------

describe("renderMarkdown", () => {
  // -- null / empty ----------------------------------------------------------

  it("returns empty string for null", () => {
    assert.equal(renderMarkdown(null), "");
  });

  it("returns empty string for undefined", () => {
    assert.equal(renderMarkdown(undefined), "");
  });

  it("returns empty string for empty string", () => {
    assert.equal(renderMarkdown(""), "");
  });

  // -- plain text ------------------------------------------------------------

  it("wraps plain text in a paragraph", () => {
    assert.equal(renderMarkdown("hello"), '<p class="md-p">hello</p>');
  });

  // -- code blocks -----------------------------------------------------------

  it("renders a fenced code block with a language tag", () => {
    const result = renderMarkdown("```js\nconsole.log('hi')\n```");
    assert.ok(
      result.includes('<div class="md-pre"><code>console.log(\'hi\')</code></div>'),
      `unexpected result: ${result}`,
    );
  });

  it("renders a fenced code block without a language tag", () => {
    const result = renderMarkdown("```\nhello code\n```");
    assert.ok(
      result.includes('<div class="md-pre"><code>hello code</code></div>'),
      `unexpected result: ${result}`,
    );
  });

  it("trims leading/trailing whitespace inside a code block", () => {
    const result = renderMarkdown("```\n  trimmed  \n```");
    assert.ok(
      result.includes("<code>trimmed</code>"),
      `whitespace was not trimmed: ${result}`,
    );
  });

  it("preserves HTML-escaped characters inside code blocks", () => {
    const result = renderMarkdown("```\n<b>not bold</b>\n```");
    // escapeHtml runs first, so the angle brackets become entities
    assert.ok(result.includes("&lt;b&gt;not bold&lt;/b&gt;"), `got: ${result}`);
    assert.ok(!result.includes("<b>"), "raw <b> tag must not appear in code block");
  });

  // -- inline code -----------------------------------------------------------

  it("renders inline code in a md-code span", () => {
    assert.equal(
      renderMarkdown("`myVar`"),
      '<p class="md-p"><span class="md-code">myVar</span></p>',
    );
  });

  it("renders inline code with multiple backtick spans on the same line", () => {
    const result = renderMarkdown("`alpha` and `beta`");
    assert.ok(result.includes('<span class="md-code">alpha</span>'));
    assert.ok(result.includes('<span class="md-code">beta</span>'));
  });

  // -- images ----------------------------------------------------------------

  it("renders an image tag with alt and src", () => {
    const result = renderMarkdown("![my alt](https://example.com/img.png)");
    assert.ok(
      result.includes('<img class="md-img" alt="my alt" src="https://example.com/img.png" />'),
      `got: ${result}`,
    );
  });

  it("renders an image with empty alt text", () => {
    const result = renderMarkdown("![](https://example.com/img.png)");
    assert.ok(result.includes('alt=""'), `got: ${result}`);
  });

  it("does not render an image as a link", () => {
    // Image syntax has priority over link syntax
    const result = renderMarkdown("![alt](https://example.com)");
    assert.ok(result.includes("<img"), "should produce img");
    assert.ok(!result.includes("<a "), "should not produce anchor");
  });

  // -- links -----------------------------------------------------------------

  it("renders a link with target=_blank", () => {
    assert.equal(
      renderMarkdown("[click here](https://example.com)"),
      '<p class="md-p"><a class="md-link" href="https://example.com" target="_blank">click here</a></p>',
    );
  });

  it("renders a link with multi-word anchor text", () => {
    const result = renderMarkdown("[open the docs](https://docs.example.com)");
    assert.ok(result.includes(">open the docs</a>"), `got: ${result}`);
  });

  // -- headings --------------------------------------------------------------

  it("renders h1", () => {
    assert.ok(renderMarkdown("# Hello World").includes("<h1>Hello World</h1>"));
  });

  it("renders h2", () => {
    assert.ok(renderMarkdown("## Subheading").includes("<h2>Subheading</h2>"));
  });

  it("renders h3", () => {
    assert.ok(renderMarkdown("### Third level").includes("<h3>Third level</h3>"));
  });

  it("renders h4", () => {
    assert.ok(renderMarkdown("#### Fourth level").includes("<h4>Fourth level</h4>"));
  });

  it("does not treat ##### as h5 (not supported)", () => {
    // Implementation only handles h1-h4, so h5 should remain as raw text
    const result = renderMarkdown("##### Five");
    assert.ok(!result.includes("<h5>"), `h5 should not be rendered: ${result}`);
  });

  // -- horizontal rules ------------------------------------------------------

  it("renders --- as a horizontal rule", () => {
    assert.ok(renderMarkdown("---").includes('<hr class="md-hr" />'));
  });

  it("renders ---- (4 dashes) as a horizontal rule", () => {
    assert.ok(renderMarkdown("----").includes('<hr class="md-hr" />'));
  });

  it("renders ----- (5 dashes) as a horizontal rule", () => {
    assert.ok(renderMarkdown("-----").includes('<hr class="md-hr" />'));
  });

  // -- emphasis --------------------------------------------------------------

  it("renders **text** as bold", () => {
    assert.ok(renderMarkdown("**bold**").includes("<strong>bold</strong>"));
  });

  it("renders *text* as italic", () => {
    assert.ok(renderMarkdown("*italic*").includes("<em>italic</em>"));
  });

  it("renders ***text*** as bold + italic", () => {
    assert.ok(
      renderMarkdown("***bold italic***").includes("<strong><em>bold italic</em></strong>"),
    );
  });

  it("renders ~~text~~ as strikethrough", () => {
    assert.ok(renderMarkdown("~~strike~~").includes("<del>strike</del>"));
  });

  // -- blockquotes -----------------------------------------------------------

  it("renders > as a blockquote div", () => {
    // escapeHtml turns '>' into '&gt;'; the regex then matches '^&gt; '
    assert.ok(
      renderMarkdown("> quoted text").includes('<div class="md-quote">quoted text</div>'),
    );
  });

  it("preserves text content inside blockquotes", () => {
    const result = renderMarkdown("> hello world");
    assert.ok(result.includes("hello world"), `got: ${result}`);
  });

  // -- task lists ------------------------------------------------------------

  it("renders a checked task list item", () => {
    const result = renderMarkdown("- [x] done task");
    assert.ok(
      result.includes('<div class="md-task"><input type="checkbox" checked disabled />done task</div>'),
      `got: ${result}`,
    );
  });

  it("renders an unchecked task list item", () => {
    const result = renderMarkdown("- [ ] pending task");
    assert.ok(
      result.includes('<div class="md-task"><input type="checkbox" disabled />pending task</div>'),
      `got: ${result}`,
    );
  });

  it("does not render task list items as regular list items", () => {
    // Task-list regexes run before the generic `- item` regex
    const result = renderMarkdown("- [x] done");
    assert.ok(!result.includes('<li class="md-li">'), "task items should not become <li>");
  });

  // -- unordered lists -------------------------------------------------------

  it("renders a single unordered list item", () => {
    const result = renderMarkdown("- item one");
    assert.ok(result.includes('<ul class="md-ul">'), `missing ul: ${result}`);
    assert.ok(result.includes('<li class="md-li">item one</li>'), `missing li: ${result}`);
  });

  it("renders multiple unordered items inside a single ul", () => {
    const result = renderMarkdown("- alpha\n- beta\n- gamma");
    const ulCount = (result.match(/<ul/g) ?? []).length;
    assert.equal(ulCount, 1, `expected 1 ul, got ${ulCount}: ${result}`);
    assert.ok(result.includes('<li class="md-li">alpha</li>'));
    assert.ok(result.includes('<li class="md-li">beta</li>'));
    assert.ok(result.includes('<li class="md-li">gamma</li>'));
  });

  // -- ordered lists ---------------------------------------------------------

  it("renders a single ordered list item", () => {
    const result = renderMarkdown("1. first item");
    assert.ok(result.includes('<ol class="md-ol">'), `missing ol: ${result}`);
    assert.ok(result.includes('<li class="md-oli">first item</li>'), `missing li: ${result}`);
  });

  it("renders multiple ordered items inside a single ol", () => {
    const result = renderMarkdown("1. first\n2. second\n3. third");
    const olCount = (result.match(/<ol/g) ?? []).length;
    assert.equal(olCount, 1, `expected 1 ol, got ${olCount}: ${result}`);
    assert.ok(result.includes('<li class="md-oli">first</li>'));
    assert.ok(result.includes('<li class="md-oli">second</li>'));
    assert.ok(result.includes('<li class="md-oli">third</li>'));
  });

  // -- tables ----------------------------------------------------------------

  it("renders a table with thead and tbody", () => {
    const md = "| Name | Age |\n|------|-----|\n| Alice | 30 |";
    const result = renderMarkdown(md);
    assert.ok(result.includes('<table class="md-table">'), `missing table: ${result}`);
    assert.ok(result.includes("<thead>"), `missing thead: ${result}`);
    assert.ok(result.includes("<tbody>"), `missing tbody: ${result}`);
    assert.ok(result.includes("<th>Name</th>"), `missing Name th: ${result}`);
    assert.ok(result.includes("<th>Age</th>"), `missing Age th: ${result}`);
    assert.ok(result.includes("<td>Alice</td>"), `missing Alice td: ${result}`);
    assert.ok(result.includes("<td>30</td>"), `missing 30 td: ${result}`);
  });

  it("renders a two-row table body correctly", () => {
    const md = "| Col1 | Col2 |\n|------|------|\n| A | B |\n| C | D |";
    const result = renderMarkdown(md);
    assert.ok(result.includes("<td>A</td>"));
    assert.ok(result.includes("<td>B</td>"));
    assert.ok(result.includes("<td>C</td>"));
    assert.ok(result.includes("<td>D</td>"));
  });

  // -- paragraph breaks ------------------------------------------------------

  it("splits a double newline into two paragraphs", () => {
    const result = renderMarkdown("first\n\nsecond");
    // Should produce: <p ...>first</p><p ...>second</p>
    assert.ok(
      result.includes('</p><p class="md-p">'),
      `paragraph break missing: ${result}`,
    );
    assert.ok(result.startsWith('<p class="md-p">'));
    assert.ok(result.endsWith("</p>"));
  });

  it("produces separate paragraph elements for each block", () => {
    const result = renderMarkdown("one\n\ntwo\n\nthree");
    const pCount = (result.match(/<p /g) ?? []).length;
    assert.equal(pCount, 3, `expected 3 paragraphs, got ${pCount}: ${result}`);
  });

  // -- line breaks -----------------------------------------------------------

  it("converts a single newline to <br />", () => {
    const result = renderMarkdown("line one\nline two");
    assert.ok(result.includes("line one<br />line two"), `got: ${result}`);
  });

  it("converts multiple single newlines to multiple <br />", () => {
    const result = renderMarkdown("a\nb\nc");
    assert.ok(result.includes("a<br />b<br />c"), `got: ${result}`);
  });

  // -- XSS protection --------------------------------------------------------

  it("does not pass raw <script> tags through to output", () => {
    const result = renderMarkdown("<script>alert(1)</script>");
    assert.ok(!result.includes("<script>"), `raw <script> leaked: ${result}`);
    assert.ok(result.includes("&lt;script&gt;"), `expected escaped tag: ${result}`);
  });

  it("does not pass through inline event handlers", () => {
    const result = renderMarkdown('<img src="x" onerror="alert(1)">');
    assert.ok(!result.includes("<img src="), `raw img tag leaked: ${result}`);
    assert.ok(result.includes("&lt;img"), `expected escaped tag: ${result}`);
  });

  it("escapes & in plain text to &amp;", () => {
    const result = renderMarkdown("cats & dogs");
    assert.ok(result.includes("cats &amp; dogs"), `got: ${result}`);
  });

  it('escapes " in plain text to &quot;', () => {
    const result = renderMarkdown('say "hello"');
    assert.ok(result.includes("say &quot;hello&quot;"), `got: ${result}`);
  });
});
