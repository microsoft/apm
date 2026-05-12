// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import sitemap from '@astrojs/sitemap';
import starlightLlmsTxt from 'starlight-llms-txt';
import starlightLinksValidator from 'starlight-links-validator';
import mermaid from 'astro-mermaid';

// https://astro.build/config
export default defineConfig({
	site: 'https://microsoft.github.io',
	base: '/apm/',
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
	redirects: {
		// Legacy enterprise slugs
		'/enterprise/teams': '/apm/enterprise/making-the-case',
		'/enterprise/governance': '/apm/enterprise/governance-guide',
		// Legacy intro section -> concepts
		'/introduction/what-is-apm': '/apm/concepts/what-is-apm',
		'/introduction/why-apm': '/apm/concepts/the-three-promises',
		'/introduction/how-it-works': '/apm/concepts/lifecycle',
		'/introduction/key-concepts': '/apm/concepts/glossary',
		'/introduction/anatomy-of-an-apm-package': '/apm/concepts/package-anatomy',
		// Legacy getting-started -> persona ramps
		'/getting-started/quick-start': '/apm/quickstart',
		'/getting-started/installation': '/apm/quickstart',
		'/getting-started/authentication': '/apm/consumer/authentication',
		'/getting-started/migration': '/apm/troubleshooting/migration',
		// Legacy guides -> consumer/producer ramps
		'/guides/dependencies': '/apm/consumer/manage-dependencies',
		'/guides/skills': '/apm/producer/author-primitives/skills',
		'/guides/prompts': '/apm/producer/author-primitives/prompts',
		'/guides/agent-workflows': '/apm/producer/author-primitives/instructions-and-agents',
		'/guides/compilation': '/apm/producer/compile',
		'/guides/dev-only-primitives': '/apm/producer/author-primitives',
		'/guides/package-relative-links': '/apm/producer/package-relative-links',
		'/guides/marketplaces': '/apm/consumer/private-and-org-packages',
		'/guides/marketplace-authoring': '/apm/producer/publish-to-a-marketplace',
		'/guides/plugins': '/apm/producer/author-primitives',
		'/guides/mcp-servers': '/apm/consumer/install-mcp-servers',
		'/guides/pack-distribute': '/apm/producer',
		'/guides/private-packages': '/apm/consumer/private-and-org-packages',
		'/guides/org-packages': '/apm/consumer/private-and-org-packages',
		'/guides/ci-policy-setup': '/apm/enterprise/enforce-in-ci',
		'/guides/drift-detection': '/apm/enterprise/drift-detection',
		// Legacy reference monolith -> per-command
		'/reference/cli-commands': '/apm/reference/cli/install',
	},
	integrations: [
		sitemap(),
		mermaid(),
		starlight({
			title: 'Agent Package Manager',
			description: 'An open-source dependency manager for AI agents. Declare skills, prompts, instructions, and tools in apm.yml -- install with one command.',
			favicon: '/favicon.svg',
			editLink: {
				baseUrl: 'https://github.com/microsoft/apm/edit/main/docs/',
			},
			lastUpdated: true,
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
					description: 'APM (Agent Package Manager) is an open-source dependency manager for AI agents. It lets you declare skills, prompts, instructions, agents, hooks, plugins, and MCP servers in a single apm.yml manifest, resolving transitive dependencies automatically.',
					exclude: ['contributing/**'],
					customSets: [
						{
							label: 'Consumer ramp',
							description: 'How to install and use APM packages in your project.',
							paths: ['quickstart', 'consumer/**'],
						},
						{
							label: 'Producer ramp',
							description: 'How to author, validate, and publish APM packages.',
							paths: ['producer/**', 'getting-started/first-package'],
						},
						{
							label: 'Enterprise ramp',
							description: 'Policy, audit, and CI gating for platform teams.',
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
					label: 'Start here',
					items: [
						{ label: 'Quickstart', slug: 'quickstart' },
						{ label: 'Your first package', slug: 'getting-started/first-package' },
					],
				},
				{
					label: 'Use a package (Consumer)',
					items: [
						{ label: 'Overview', slug: 'consumer' },
						{ label: 'Install packages', slug: 'consumer/install-packages' },
						{ label: 'Manage dependencies', slug: 'consumer/manage-dependencies' },
						{ label: 'Run scripts', slug: 'consumer/run-scripts' },
						{ label: 'Update and refresh', slug: 'consumer/update-and-refresh' },
						{ label: 'Install MCP servers', slug: 'consumer/install-mcp-servers' },
						{ label: 'Authentication', slug: 'consumer/authentication' },
						{ label: 'Private and org packages', slug: 'consumer/private-and-org-packages' },
						{ label: 'Deploy a local bundle', slug: 'consumer/deploy-a-bundle' },
						{ label: 'Drift and secure-by-default', slug: 'consumer/drift-and-secure-by-default' },
						{ label: 'Governance on the consumer ramp', slug: 'consumer/governance-on-the-consumer-ramp' },
					],
				},
				{
					label: 'Author a package (Producer)',
					items: [
						{ label: 'Overview', slug: 'producer' },
						{
							label: 'Author primitives',
							items: [
								{ label: 'Overview', slug: 'producer/author-primitives' },
								{ label: 'Skills', slug: 'producer/author-primitives/skills' },
								{ label: 'Prompts', slug: 'producer/author-primitives/prompts' },
								{ label: 'Instructions and agents', slug: 'producer/author-primitives/instructions-and-agents' },
								{ label: 'Hooks and commands', slug: 'producer/author-primitives/hooks-and-commands' },
								{ label: 'MCP as a primitive', slug: 'producer/author-primitives/mcp-as-primitive' },
							],
						},
						{ label: 'Compile your package', slug: 'producer/compile' },
						{ label: 'Preview and validate', slug: 'producer/preview-and-validate' },
						{ label: 'Pack a bundle', slug: 'producer/pack-a-bundle' },
						{ label: 'Publish to a marketplace', slug: 'producer/publish-to-a-marketplace' },
						{ label: 'Package-relative links', slug: 'producer/package-relative-links' },
					],
				},
				{
					label: 'Govern at scale (Enterprise)',
					items: [
						{ label: 'Overview', slug: 'enterprise' },
						{ label: 'Making the case', slug: 'enterprise/making-the-case' },
						{ label: 'Adoption playbook', slug: 'enterprise/adoption-playbook' },
						{ label: 'Governance overview', slug: 'enterprise/governance-overview' },
						{ label: 'Governance guide', slug: 'enterprise/governance-guide' },
						{ label: 'Policy: getting started', slug: 'enterprise/apm-policy-getting-started' },
						{ label: 'Policy pilot', slug: 'enterprise/policy-pilot' },
						{ label: 'Policy files', slug: 'enterprise/apm-policy' },
						{ label: 'Policy reference', slug: 'enterprise/policy-reference' },
						{ label: 'Enforce in CI', slug: 'enterprise/enforce-in-ci' },
						{ label: 'Security model', slug: 'enterprise/security' },
						{ label: 'Security and supply chain', slug: 'enterprise/security-and-supply-chain' },
						{ label: 'Drift detection', slug: 'enterprise/drift-detection' },
						{ label: 'Registry proxy and air-gapped', slug: 'enterprise/registry-proxy' },
						{ label: 'GitHub rulesets', slug: 'enterprise/github-rulesets' },
					],
				},
				{
					label: 'Integrations',
					items: [
						{ label: 'IDE and tool integration', slug: 'integrations/ide-tool-integration' },
						{ label: 'CI/CD pipelines', slug: 'integrations/ci-cd' },
						{ label: 'GitHub Agentic Workflows', slug: 'integrations/gh-aw' },
						{ label: 'Microsoft 365 Copilot Cowork (Experimental)', slug: 'integrations/copilot-cowork' },
						{ label: 'AI runtime compatibility', slug: 'integrations/runtime-compatibility' },
						{ label: 'GitHub rulesets', slug: 'integrations/github-rulesets' },
					],
				},
				{
					label: 'CLI reference',
					items: [
						{ label: 'Overview', slug: 'reference' },
						{
							label: 'Commands',
							autogenerate: { directory: 'reference/cli' },
						},
					],
				},
				{
					label: 'Schemas and specs',
					items: [
						{ label: 'Manifest schema', slug: 'reference/manifest-schema' },
						{ label: 'Lockfile spec', slug: 'reference/lockfile-spec' },
						{ label: 'Policy schema', slug: 'reference/policy-schema' },
						{ label: 'Targets matrix', slug: 'reference/targets-matrix' },
						{ label: 'Primitive types', slug: 'reference/primitive-types' },
						{ label: 'Package types', slug: 'reference/package-types' },
						{ label: 'Baseline checks', slug: 'reference/baseline-checks' },
						{ label: 'Environment variables', slug: 'reference/environment-variables' },
						{ label: 'Examples', slug: 'reference/examples' },
						{ label: 'Experimental', slug: 'reference/experimental' },
					],
				},
				{
					label: 'Concepts',
					items: [
						{ label: 'What is APM?', slug: 'concepts/what-is-apm' },
						{ label: 'The three promises', slug: 'concepts/the-three-promises' },
						{ label: 'Lifecycle', slug: 'concepts/lifecycle' },
						{ label: 'Primitives and targets', slug: 'concepts/primitives-and-targets' },
						{ label: 'Package anatomy', slug: 'concepts/package-anatomy' },
						{ label: 'Glossary', slug: 'concepts/glossary' },
					],
				},
				{
					label: 'Troubleshooting',
					items: [
						{ label: 'Overview', slug: 'troubleshooting' },
						{ label: 'Common errors', slug: 'troubleshooting/common-errors' },
						{ label: 'Install failures', slug: 'troubleshooting/install-failures' },
						{ label: 'Compile produced no output', slug: 'troubleshooting/compile-zero-output-warning' },
						{ label: 'Policy debugging', slug: 'troubleshooting/policy-debugging' },
						{ label: 'SSL / TLS issues', slug: 'troubleshooting/ssl-issues' },
						{ label: 'Migration paths', slug: 'troubleshooting/migration' },
					],
				},
				{
					label: 'Contributing',
					autogenerate: { directory: 'contributing' },
				},
			],
		}),
	],
});
