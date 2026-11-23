// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';
import starlightLlmsTxt from 'starlight-llms-txt';
import starlightLinksValidator from 'starlight-links-validator';
import mermaid from 'astro-mermaid';
import { unified } from '@astrojs/markdown-remark';

// https://astro.build/config
export default defineConfig({
	site: 'https://microsoft.github.io',
	base: '/apm/',
	// The default Satteri processor does not run remark/rehype plugins, so
	// astro-mermaid diagrams never rendered. `unified` runs them; gfm and
	// smartypants match the previous defaults.
	markdown: {
		processor: unified({ gfm: true, smartypants: true }),
	},
	trailingSlash: 'always',
	prefetch: {
		prefetchAll: true,
		defaultStrategy: 'viewport',
	},
	// Astro applies the `base` prefix automatically to redirect SOURCE
	// keys, but NOT to redirect DESTINATIONS -- destinations are emitted
	// as literal strings into the redirect HTML's meta refresh / canonical
	// link. Every destination below MUST therefore start with `/apm/` to
	// land on the actual deployed URL on https://microsoft.github.io/apm/.
	// Source keys MUST NOT include `/apm` -- the base is added at build
	// time and the source would otherwise resolve under `/apm/apm/`.
	// Astro applies the `base` prefix to redirect SOURCE keys but NOT to
	// DESTINATIONS, so every destination starts with `/apm/` and source keys
	// never include `/apm`. Every retired slug redirects to its canonical task
	// page (2026-11 docs redesign); chains are flattened.
	redirects: {
		'/guides/private-registries': '/apm/reference/registries/',
		'/enterprise/teams': '/apm/enterprise/',
		'/enterprise/governance': '/apm/enterprise/security/',
		'/enterprise/governance-overview': '/apm/enterprise/',
		'/enterprise/security-and-supply-chain': '/apm/enterprise/security/',
		'/enterprise/apm-policy-getting-started': '/apm/enterprise/apm-policy/',
		'/introduction/what-is-apm': '/apm/concepts/how-apm-works/',
		'/introduction/why-apm': '/apm/concepts/agent-supply-chain/',
		'/introduction/how-it-works': '/apm/concepts/how-apm-works/',
		'/introduction/key-concepts': '/apm/concepts/glossary/',
		'/introduction/anatomy-of-an-apm-package': '/apm/producer/create-a-package/',
		'/getting-started/quick-start': '/apm/quickstart/',
		'/guides/dependencies': '/apm/consumer/install-packages/',
		'/guides/skills': '/apm/producer/write-primitives/',
		'/guides/prompts': '/apm/producer/write-primitives/',
		'/guides/agent-workflows': '/apm/producer/write-primitives/',
		'/guides/compilation': '/apm/consumer/choose-harnesses/',
		'/guides/dev-only-primitives': '/apm/producer/write-primitives/',
		'/guides/package-relative-links': '/apm/producer/write-primitives/',
		'/guides/marketplaces': '/apm/consumer/installing-from-marketplaces/',
		'/guides/marketplace-authoring': '/apm/producer/publish-to-a-marketplace/',
		'/guides/plugins': '/apm/producer/build-a-plugin/',
		'/guides/mcp-servers': '/apm/consumer/install-mcp-servers/',
		'/guides/pack-distribute': '/apm/producer/',
		'/guides/private-packages': '/apm/getting-started/authentication/',
		'/guides/org-packages': '/apm/getting-started/authentication/',
		'/guides/ci-policy-setup': '/apm/enterprise/enforce-in-ci/',
		'/guides/drift-detection': '/apm/enterprise/drift-detection/',
		'/reference/cli-commands': '/apm/reference/cli/install/',
		'/concepts/what-is-apm': '/apm/concepts/how-apm-works/',
		'/concepts/the-three-promises': '/apm/concepts/agent-supply-chain/',
		'/concepts/lifecycle': '/apm/concepts/how-apm-works/',
		'/concepts/primitives-and-targets': '/apm/concepts/how-apm-works/',
		'/concepts/package-anatomy': '/apm/producer/create-a-package/',
		'/consumer/authentication': '/apm/getting-started/authentication/',
		'/consumer/choosing-an-mcp-registry': '/apm/consumer/install-mcp-servers/',
		'/consumer/copilot-agent-plugins': '/apm/consumer/install-plugins/',
		'/consumer/deploy-a-bundle': '/apm/consumer/install-packages/',
		'/consumer/drift-and-secure-by-default': '/apm/enterprise/security/',
		'/consumer/governance-on-the-consumer-ramp': '/apm/enterprise/',
		'/consumer/install-lsp-servers': '/apm/consumer/install-mcp-servers/',
		'/consumer/manage-dependencies': '/apm/consumer/install-packages/',
		'/consumer/private-and-org-packages': '/apm/getting-started/authentication/',
		'/consumer/run-scripts': '/apm/reference/cli/run/',
		'/producer/author-primitives': '/apm/producer/write-primitives/',
		'/producer/author-primitives/skills': '/apm/producer/write-primitives/',
		'/producer/author-primitives/prompts': '/apm/producer/write-primitives/',
		'/producer/author-primitives/instructions-and-agents': '/apm/producer/write-primitives/',
		'/producer/author-primitives/hooks-and-commands': '/apm/producer/write-primitives/',
		'/producer/author-primitives/mcp-as-primitive': '/apm/producer/write-primitives/',
		'/producer/compile': '/apm/consumer/choose-harnesses/',
		'/producer/package-relative-links': '/apm/producer/write-primitives/',
		'/producer/preview-and-validate': '/apm/producer/test-packages/',
		'/producer/releasing-from-any-ci': '/apm/producer/release-a-version/',
		'/producer/repo-shapes': '/apm/producer/publish-to-a-marketplace/',
		'/producer/source-vs-installed': '/apm/concepts/how-apm-works/',
		'/producer/versioning-strategies': '/apm/producer/release-a-version/',
		'/producer/pack-a-bundle': '/apm/producer/build-a-plugin/',
		'/enterprise/adoption-playbook': '/apm/enterprise/',
		'/enterprise/governance-guide': '/apm/enterprise/security/',
		'/enterprise/github-rulesets': '/apm/enterprise/enforce-in-ci/',
		'/enterprise/lifecycle-scripts': '/apm/enterprise/control-executables/',
		'/enterprise/making-the-case': '/apm/concepts/agent-supply-chain/',
		'/enterprise/policy-pilot': '/apm/enterprise/',
		'/enterprise/policy-reference': '/apm/reference/policy-schema/',
		'/guides/operating-installed-context': '/apm/concepts/how-apm-works/',
		'/guides/registries': '/apm/reference/registries/',
		'/integrations/ci-cd': '/apm/consumer/run-in-ci/',
		'/integrations/gh-aw': '/apm/consumer/run-in-ci/',
		'/integrations/github-rulesets': '/apm/enterprise/enforce-in-ci/',
		'/integrations/ide-tool-integration': '/apm/consumer/choose-harnesses/',
		'/integrations/runtime-compatibility': '/apm/reference/targets-matrix/',
		'/getting-started/first-package': '/apm/producer/create-a-package/',
		'/getting-started/migration': '/apm/getting-started/coming-from/',
		'/reference/examples': '/apm/quickstart/',
		'/reference/common-errors': '/apm/troubleshooting/common-errors/',
		'/troubleshooting/compile-zero-output-warning': '/apm/troubleshooting/common-errors/',
		'/troubleshooting/policy-debugging': '/apm/enterprise/apm-policy/',
		'/troubleshooting/migration': '/apm/getting-started/coming-from/',
		// Stable OpenAPM shortlinks. Versioned URLs (/spec/v0.1) are immortal
		// citation targets; /spec and /spec/latest alias the latest ratified version.
		'/spec': '/apm/specs/openapm-v01/',
		'/spec/latest': '/apm/specs/openapm-v01/',
		'/spec/v0.1': '/apm/specs/openapm-v01/',
	},
	integrations: [
		sitemap(),
		mermaid(),
		starlight({
			title: 'Agent Package Manager',
			description: 'The dependency manager for the agent supply chain: declare, lock and govern the skills, plugins, MCP servers and hooks your coding agents load.',
			favicon: '/favicon.svg',
			editLink: {
				baseUrl: 'https://github.com/microsoft/apm/edit/main/docs/',
			},
			lastUpdated: true,
			components: {
				Footer: './src/components/Footer.astro',
			},
			head: [
				{
					tag: 'meta',
					attrs: { name: 'theme-color', content: '#1d4ed8' },
				},
				{
					tag: 'meta',
					attrs: { property: 'og:type', content: 'website' },
				},
				{
					tag: 'meta',
					attrs: { name: 'twitter:card', content: 'summary_large_image' },
				},
			],
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/microsoft/apm' },
			],
			tableOfContents: {
				minHeadingLevel: 2,
				maxHeadingLevel: 4,
			},
			pagination: true,
			customCss: ['./src/styles/custom.css'],
			expressiveCode: {
				themes: ['github-dark', 'github-light'],
				styleOverrides: {
					borderRadius: '0.5rem',
					borderWidth: '1px',
					codeFontSize: '0.875rem',
					codeLineHeight: '1.5',
					frames: {
						shadowColor: 'transparent',
					},
				},
				frames: {
					showCopyToClipboardButton: true,
				},
			},
			plugins: [
				starlightLinksValidator({
					errorOnRelativeLinks: false,
					errorOnLocalLinks: true,
				}),
				starlightLlmsTxt({
					description: 'APM (Agent Package Manager) is the open-source dependency manager for the agent supply chain. It declares the skills, instructions, prompts, agents, hooks, plugins and MCP servers coding agents load in apm.yml, locks them in apm.lock.yaml, and checks them against an organization policy.',
					exclude: ['contributing/**'],
					customSets: [
						{
							label: 'Get started',
							description: 'Install APM, the five-minute quickstart, how APM works and why the agent supply chain matters.',
							paths: ['quickstart', 'getting-started/**', 'concepts/**', 'troubleshooting/**'],
						},
						{
							label: 'Use skills, plugins and MCP',
							description: 'Install, update and verify agent skills, plugins and MCP servers in a project.',
							paths: ['consumer/**'],
						},
						{
							label: 'Publish packages',
							description: 'Author, test, release and publish APM packages and plugins.',
							paths: ['producer/**'],
						},
						{
							label: 'Govern your organization',
							description: 'Org policy, pull request gates, executables, private marketplaces and the security model.',
							paths: ['enterprise/**'],
						},
						{
							label: 'CLI reference',
							description: 'Per-command reference for the apm CLI.',
							paths: ['reference/cli/**'],
						},
					],
				}),
			],
			sidebar: [
				{
					label: 'Get started',
					items: [
						{ label: 'Install APM', slug: 'getting-started/installation' },
						{ label: 'Quickstart', slug: 'quickstart' },
						{ label: 'How APM works', slug: 'concepts/how-apm-works' },
						{ label: 'The agent supply chain', slug: 'concepts/agent-supply-chain' },
						{ label: 'Coming from another tool', slug: 'getting-started/coming-from' },
						{ label: 'Troubleshoot', slug: 'troubleshooting' },
					],
				},
				{
					label: 'Use skills, plugins and MCP',
					items: [
						{ label: 'Overview', slug: 'consumer' },
						{ label: 'Add and remove packages', slug: 'consumer/install-packages' },
						{ label: 'Install from a marketplace', slug: 'consumer/installing-from-marketplaces' },
						{ label: 'Install plugins', slug: 'consumer/install-plugins' },
						{ label: 'Add MCP and LSP servers', slug: 'consumer/install-mcp-servers' },
						{ label: 'Choose your harnesses', slug: 'consumer/choose-harnesses' },
						{ label: 'Approve hooks and MCP servers', slug: 'consumer/approve-executables' },
						{ label: 'Update dependencies', slug: 'consumer/update-and-refresh' },
						{ label: 'Run in CI', slug: 'consumer/run-in-ci' },
						{ label: 'Use private sources', slug: 'getting-started/authentication' },
					],
				},
				{
					label: 'Publish packages',
					items: [
						{ label: 'Overview', slug: 'producer' },
						{ label: 'Create a package', slug: 'producer/create-a-package' },
						{ label: 'Write skills, instructions and hooks', slug: 'producer/write-primitives' },
						{ label: 'Test a package locally', slug: 'producer/test-packages' },
						{ label: 'Release a version', slug: 'producer/release-a-version' },
						{ label: 'Build a plugin', slug: 'producer/build-a-plugin' },
						{ label: 'Publish to a marketplace', slug: 'producer/publish-to-a-marketplace' },
					],
				},
				{
					label: 'Govern your organization',
					items: [
						{ label: 'Overview', slug: 'enterprise' },
						{ label: 'Security model', slug: 'enterprise/security' },
						{ label: 'Set an org policy', slug: 'enterprise/apm-policy' },
						{ label: 'Gate pull requests', slug: 'enterprise/enforce-in-ci' },
						{ label: 'Control executables', slug: 'enterprise/control-executables' },
						{ label: "Build your organization's marketplace", slug: 'enterprise/org-marketplace' },
						{ label: 'Install through a proxy or offline', slug: 'enterprise/registry-proxy' },
						{ label: 'Inventory and drift', slug: 'enterprise/drift-detection' },
					],
				},
				{ label: 'Integrations A-Z', slug: 'integrations' },
				{
					label: 'Reference',
					collapsed: true,
					items: [
						{ label: 'Overview', slug: 'reference' },
						{
							label: 'CLI commands',
							collapsed: true,
							items: [{ autogenerate: { directory: 'reference/cli' } }],
						},
						{ label: 'apm.yml', slug: 'reference/manifest-schema' },
						{ label: 'apm.lock.yaml', slug: 'reference/lockfile-spec' },
						{ label: 'apm-policy.yml', slug: 'reference/policy-schema' },
						{ label: 'Targets matrix', slug: 'reference/targets-matrix' },
						{ label: 'Primitive types', slug: 'reference/primitive-types' },
						{ label: 'Package types', slug: 'reference/package-types' },
						{ label: 'Audit checks', slug: 'reference/baseline-checks' },
						{ label: 'MCP variables', slug: 'reference/mcp-variable-compatibility' },
						{ label: 'Environment variables', slug: 'reference/environment-variables' },
						{ label: 'Error messages', slug: 'troubleshooting/common-errors' },
						{ label: 'Install failures', slug: 'troubleshooting/install-failures' },
						{ label: 'TLS and proxies', slug: 'troubleshooting/ssl-issues' },
						{ label: 'Glossary', slug: 'concepts/glossary' },
						{
							label: 'Experimental features',
							collapsed: true,
							items: [
								{ label: 'Overview', slug: 'reference/experimental' },
								{ label: 'Package registries', slug: 'reference/registries' },
								{ label: 'Registry HTTP API', slug: 'reference/registry-http-api' },
								{ label: 'External scanners', slug: 'integrations/external-scanners' },
								{ label: 'Copilot app workflows', slug: 'integrations/copilot-app' },
								{ label: 'Microsoft 365 Copilot Cowork', slug: 'integrations/copilot-cowork' },
								{ label: 'Canvas extensions', slug: 'integrations/canvas' },
								{ label: 'OpenClaw', slug: 'integrations/openclaw' },
								{ label: 'Hermes Agent', slug: 'integrations/hermes' },
							],
						},
					],
				},
				{
					label: 'Specification',
					collapsed: true,
					items: [
						// Starlight strips dots from slugs: openapm-v0.1.md -> /specs/openapm-v01/.
						{ label: 'OpenAPM v0.1', slug: 'specs/openapm-v01' },
						{ label: 'Conformance', slug: 'specs/conformance' },
					],
				},
				{
					label: 'About',
					collapsed: true,
					items: [
						{ label: 'How APM compares', slug: 'about/compared' },
						{ label: 'Organizations using APM', slug: 'about/adopters' },
						{
							label: 'Contributing',
							collapsed: true,
							items: [{ autogenerate: { directory: 'contributing' } }],
						},
					],
				},
			],
		}),
	],
});
