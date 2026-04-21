---
title: "Prompts"
sidebar:
  order: 3
---

Prompts are the building blocks of APM -- focused, reusable AI instructions that accomplish specific tasks. They follow the `.prompt.md` convention and are distributed as shareable packages.

## How Prompts Work in APM

APM treats prompts as deployable artifacts:

1. **Prompts** (`.prompt.md` files) contain AI instructions with parameter placeholders
2. **Packages** bundle prompts for sharing via `apm publish` and `apm install`
3. **Deployment** places prompts into well-known directories (e.g., `.github/prompts/`) where tools like GitHub Copilot can discover and use them
4. **Compilation** resolves parameter placeholders, cross-file references, and link transforms at install time

```bash
# Deployment flow
apm install owner/my-prompt-package
  ↓
APM compiles .prompt.md files (parameter defaults, link resolution)
  ↓
Prompts land in .github/prompts/ for Copilot to discover
```

## What are Prompts?

A prompt is a single-purpose AI instruction stored in a `.prompt.md` file. Prompts are:
- **Focused**: Each prompt does one thing well
- **Reusable**: Can be used across multiple scripts
- **Parameterized**: Accept inputs to customize behavior
- **Testable**: Easy to run and validate independently

## Prompt File Structure

Prompts follow the VSCode `.prompt.md` convention with YAML frontmatter:

```markdown
---
description: Analyzes application logs to identify errors and patterns
author: DevOps Team
mcp:
  - logs-analyzer
input:
  - service_name
  - time_window
  - log_level
---

# Analyze Application Logs

You are a expert DevOps engineer analyzing application logs to identify issues and patterns.

## Context
- Service: ${input:service_name}
- Time window: ${input:time_window}
- Log level: ${input:log_level}

## Task
1. Retrieve logs for the specified service and time window
2. Identify any ERROR or FATAL level messages
3. Look for patterns in warnings that might indicate emerging issues
4. Summarize findings with:
   - Critical issues requiring immediate attention
   - Trends or patterns worth monitoring
   - Recommended next steps

## Output Format
Provide a structured summary with:
- **Status**: CRITICAL | WARNING | NORMAL
- **Issues Found**: List of specific problems
- **Patterns**: Recurring themes or trends
- **Recommendations**: Suggested actions
```

## Key Components

### YAML Frontmatter
- **description**: Clear explanation of what the prompt does
- **author**: Who created/maintains this prompt
- **mcp**: Required MCP servers for tool access
- **input**: Parameters the prompt expects

### Prompt Body
- **Clear instructions**: Tell the AI exactly what to do
- **Context section**: Provide relevant background information
- **Input references**: Use `${input:parameter_name}` for dynamic values
- **Output format**: Specify how results should be structured

## Input Parameters

Reference script inputs using the `${input:name}` syntax:

```markdown
## Analysis Target
- Service: ${input:service_name}
- Environment: ${input:environment}
- Start time: ${input:start_time}
```

## MCP servers in prompts

Prompts can declare MCP server dependencies in their frontmatter under the `mcp:` key (see the deployment-health-check example below). To add an MCP server to your project, see the [MCP Servers guide](../mcp-servers/).

## Writing Effective Prompts

### Be Specific
```markdown
# Good
Analyze the last 24 hours of application logs for service ${input:service_name}, 
focusing on ERROR and FATAL messages, and identify any patterns that might 
indicate performance degradation.

# Avoid
Look at some logs and tell me if there are problems.
```

### Structure Your Instructions
```markdown
## Task
1. First, do this specific thing
2. Then, analyze the results looking for X, Y, and Z
3. Finally, summarize findings in the specified format

## Success Criteria
- All ERROR messages are categorized
- Performance trends are identified
- Clear recommendations are provided
```

### Specify Output Format
```markdown
## Output Format
**Summary**: One-line status
**Critical Issues**: Numbered list of immediate concerns
**Recommendations**: Specific next steps with priority levels
```

## Example Prompts

### Code Review Prompt
```markdown
---
description: Reviews code changes for best practices and potential issues
author: Engineering Team
input:
  - pull_request_url
  - focus_areas
---

# Code Review Assistant

Review the code changes in pull request ${input:pull_request_url} with focus on ${input:focus_areas}.

## Review Criteria
1. **Security**: Check for potential vulnerabilities
2. **Performance**: Identify optimization opportunities  
3. **Maintainability**: Assess code clarity and structure
4. **Testing**: Evaluate test coverage and quality

## Output
Provide feedback in standard PR review format with:
- Specific line comments for issues
- Overall assessment score (1-10)
- Required changes vs suggestions
```

### Deployment Health Check
```markdown
---
description: Verifies deployment success and system health
author: Platform Team
mcp:
  - kubernetes-tools
  - monitoring-api
input:
  - service_name
  - deployment_version
---

# Deployment Health Check

Verify the successful deployment of ${input:service_name} version ${input:deployment_version}.

## Health Check Steps
1. Confirm pods are running and ready
2. Check service endpoints are responding
3. Verify metrics show normal operation
4. Test critical user flows

## Success Criteria
- All pods STATUS = Running
- Health endpoint returns 200
- Error rate < 1%
- Response time < 500ms
```

## Running Prompts

Prompts can be executed locally using APM's experimental agent workflow system.
Define scripts in your `apm.yml` or let APM auto-discover `.prompt.md` files as
runnable workflows.

See the [Agent Workflows guide](../agent-workflows/) for setup instructions,
runtime configuration, and execution examples.

## Best Practices

### 1. Single Responsibility
Each prompt should do one thing well. Break complex operations into multiple prompts.

### 2. Clear Naming
Use descriptive names that indicate the prompt's purpose:
- `analyze-performance-metrics.prompt.md`
- `create-incident-ticket.prompt.md`
- `validate-deployment-config.prompt.md`

### 3. Document Inputs
Always specify what inputs are required and their expected format:

```yaml
input:
  - service_name     # String: name of the service to analyze
  - time_window      # String: time range (e.g., "1h", "24h", "7d")
  - severity_level   # String: minimum log level ("ERROR", "WARN", "INFO")
```

### 4. Version Control
Keep prompts in version control alongside scripts. Use semantic versioning for breaking changes.

## Next Steps

- Learn about [Agent Workflows](../agent-workflows/) to run prompts locally with AI runtimes
- See [CLI Reference](../../reference/cli-commands/) for complete command documentation
- Check [Development Guide](../../contributing/development-guide/) for local development setup
