// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import starlightLlmsTxt from 'starlight-llms-txt';
import starlightLinksValidator from 'starlight-links-validator';
import mermaid from 'astro-mermaid';

// https://astro.build/config
export default defineConfig({
	site: 'https://microsoft.github.io',
	base: '/apm/',
	integrations: [
		mermaid(),
		starlight({
			title: 'Agent Package Manager',
			description: 'An open-source, community-driven dependency manager for AI agents. Declare skills, prompts, instructions, and tools in apm.yml — install with one command.',
			favicon: '/favicon.svg',
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
				}),
			],
			sidebar: [
				{
					label: 'Understanding APM',
					items: [
						{ label: 'What is APM?', slug: 'introduction/what-is-apm' },
						{ label: 'Why APM?', slug: 'introduction/why-apm' },
						{ label: 'How It Works', slug: 'introduction/how-it-works' },
						{ label: 'Key Concepts', slug: 'introduction/key-concepts' },
					],
				},
				{
					label: 'Getting Started',
					items: [
						{ label: 'Installation', slug: 'getting-started/installation' },
						{ label: 'Quick Start', slug: 'getting-started/quick-start' },
						{ label: 'Your First Package', slug: 'getting-started/first-package' },
						{ label: 'Authentication', slug: 'getting-started/authentication' },
						{ label: 'Existing Projects', slug: 'getting-started/migration' },
					],
				},
				{
					label: 'Guides',
					items: [
						{ label: 'Compilation & Optimization', slug: 'guides/compilation' },
						{ label: 'Skills', slug: 'guides/skills' },
						{ label: 'Prompts', slug: 'guides/prompts' },
						{ label: 'Plugins', slug: 'guides/plugins' },
						{ label: 'MCP Servers', slug: 'guides/mcp-servers' },
						{ label: 'Dependencies & Lockfile', slug: 'guides/dependencies' },
						{ label: 'Pack & Distribute', slug: 'guides/pack-distribute' },
						{ label: 'Private Packages', slug: 'guides/private-packages' },
						{ label: 'Org-Wide Packages', slug: 'guides/org-packages' },
						{ label: 'Marketplaces', slug: 'guides/marketplaces' },
						{ label: 'CI Policy Enforcement', slug: 'guides/ci-policy-setup' },
						{ label: 'Agent Workflows (Experimental)', slug: 'guides/agent-workflows' },
					],
				},
				{
					label: 'Enterprise',
					items: [
						{ label: 'APM for Teams', slug: 'enterprise/teams' },
						{ label: 'Governance & Compliance', slug: 'enterprise/governance' },
						{ label: 'Policy Reference', slug: 'enterprise/policy-reference' },
						{ label: 'Security Model', slug: 'enterprise/security' },
						{ label: 'Adoption Playbook', slug: 'enterprise/adoption-playbook' },
						{ label: 'Making the Case', slug: 'enterprise/making-the-case' },
					],
				},
				{
					label: 'Integrations',
					items: [
						{ label: 'CI/CD Pipelines', slug: 'integrations/ci-cd' },
						{ label: 'GitHub Agentic Workflows', slug: 'integrations/gh-aw' },
						{ label: 'IDE & Tool Integration', slug: 'integrations/ide-tool-integration' },
						{ label: 'AI Runtime Compatibility', slug: 'integrations/runtime-compatibility' },
						{ label: 'GitHub Rulesets', slug: 'integrations/github-rulesets' },
					],
				},
				{
					label: 'Reference',
					autogenerate: { directory: 'reference' },
				},
				{
					label: 'Contributing',
					autogenerate: { directory: 'contributing' },
				},
			],
		}),
	],
});
