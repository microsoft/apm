<details>
<summary>MCP Gateway</summary>

- ✓ **startup** MCPG Gateway version: v0.3.19
- ✓ **startup** Starting MCPG with config: stdin, listen: 0.0.0.0:8080, log-dir: /tmp/gh-aw/mcp-logs/
- ✓ **startup** WASM compilation cache directory: /tmp/gh-aw/mcp-logs/wazero-cache
- ✓ **startup** Loaded 2 MCP server(s): [github safeoutputs]
- ✓ **startup** Guards sink server ID logging enrichment disabled (no sink server IDs configured)
- ✓ **startup** OpenTelemetry tracing disabled (no OTLP endpoint configured)
- 🔍 rpc **safeoutputs**→`tools/list`
- 🔍 rpc **safeoutputs**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"tools":[{"description":"WRITE-ONCE: do NOT call this tool with empty or placeholder arguments to probe or discover its schema — the required `body` field is listed in this schema; if you are not ready to post a real comment, call `noop` instead. Adds a comment to an existing GitHub issue, pull request, or discussion. Use this to provide feedback, answer questions, or add information to an existing conversation. For creating new items, use create_issue, create_discussion,...`
- ✓ **backend**
  ```
  Successfully connected to MCP backend server, command=docker
  ```
- 🔍 rpc **github**→`tools/list`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"tools":[{"annotations":{"readOnlyHint":true,"title":"Get commit details"},"description":"Get details for a commit from a GitHub repository","inputSchema":{"properties":{"include_diff":{"default":true,"description":"Whether to include file diffs and stats in the response. Default is true.","type":"boolean"},"owner":{"description":"Repository owner","type":"string"},"page":{"description":"Page number for pagination (min 1)","minimum":1,"type":"number"},"perPage":{"descriptio...`
- ✓ **startup** Starting MCPG in ROUTED mode on 0.0.0.0:8080
- ✓ **startup** Routes: /mcp/<server> where <server> is one of: [github safeoutputs]
- ✓ **startup** TLS not configured — listening on http://0.0.0.0:8080 (set --tls-cert/--tls-key to enable)
- ✓ **backend**
  ```
  Successfully connected to MCP backend server, command=docker
  ```
- 🔍 rpc **github**→`tools/call` `search_repositories`
  
  ```json
  {"params":{"arguments":{"perPage":10,"query":"repo:microsoft/apm"},"name":"search_repositories"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"total_count\":1,\"incomplete_results\":false,\"items\":[{\"id\":1059472549,\"name\":\"apm\",\"full_name\":\"microsoft/apm\",\"description\":\"Agent Package Manager\",\"html_url\":\"https://github.com/microsoft/apm\",\"language\":\"Python\",\"stargazers_count\":2939,\"forks_count\":243,\"open_issues_count\":84,\"updated_at\":\"2026-06-19T08:42:37Z\",\"created_at\":\"2025-09-18T13:45:22Z\",\"topics\":[\"ai-agents\",\"claude-code\",\"codex-...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\"docs/src/content/docs/enterprise/lifecycle-hooks.md\",\"status\":\"added\",\"additions\":245,\"changes\":245,\"patch\":\"@@ -0,0 +1,245 @@\\n+---\\n+title: \\\"Lifecycle Hooks\\\"\\n+description: \\\"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\\\"\\n+sidebar:\\n+  order: 12\\n+---\\n+\\n+APM supports **lifecycle hooks** -- custom actions that fire automatically\\n+at key moment...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_reviews","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"id\":4504742740,\"state\":\"COMMENTED\",\"body\":\"## Pull request overview\\n\\nAdds a lifecycle hooks framework to APM so installs/updates/uninstalls can emit structured events to user/admin-defined “command” and “http” hooks, plus a new `apm hooks` CLI group and accompanying enterprise documentation.\\n\\n**Changes:**\\n- Introduces hook discovery + event models + runner (`lifecycle_hooks.py`) and executors with stdout/stderr...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/docs/src/content/docs/enterprise/lifecycle-hooks.md b/docs/src/content/docs/enterprise/lifecycle-hooks.md\nnew file mode 100644\nindex 000000000..e61efa0f4\n--- /dev/null\n+++ b/docs/src/content/docs/enterprise/lifecycle-hooks.md\n@@ -0,0 +1,245 @@\n+---\n+title: \"Lifecycle Hooks\"\n+description: \"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\"\n+sidebar:\n+  order: 12\n+---\n+\n+A...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_review_comments","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"review_threads\":[{\"is_resolved\":false,\"is_outdated\":false,\"is_collapsed\":false,\"comments\":[{\"body\":\"`pre-install` hooks are fired before confirming the install pipeline can even be imported. If `run_install_pipeline` import fails (missing optional extras), hooks still run even though no install can proceed, which is surprising for analytics/automation hooks.\",\"path\":\"src/apm_cli/install/service.py\",\"line\":71,\"author\"...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\"docs/src/content/docs/enterprise/lifecycle-hooks.md\",\"status\":\"added\",\"additions\":245,\"changes\":245,\"patch\":\"@@ -0,0 +1,245 @@\\n+---\\n+title: \\\"Lifecycle Hooks\\\"\\n+description: \\\"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\\\"\\n+sidebar:\\n+  order: 12\\n+---\\n+\\n+APM supports **lifecycle hooks** -- custom actions that fire automatically\\n+at key moment...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/docs/src/content/docs/enterprise/lifecycle-hooks.md b/docs/src/content/docs/enterprise/lifecycle-hooks.md\nnew file mode 100644\nindex 000000000..e61efa0f4\n--- /dev/null\n+++ b/docs/src/content/docs/enterprise/lifecycle-hooks.md\n@@ -0,0 +1,245 @@\n+---\n+title: \"Lifecycle Hooks\"\n+description: \"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\"\n+sidebar:\n+  order: 12\n+---\n+\n+A...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/docs/src/content/docs/enterprise/lifecycle-hooks.md b/docs/src/content/docs/enterprise/lifecycle-hooks.md\nnew file mode 100644\nindex 000000000..e61efa0f4\n--- /dev/null\n+++ b/docs/src/content/docs/enterprise/lifecycle-hooks.md\n@@ -0,0 +1,245 @@\n+---\n+title: \"Lifecycle Hooks\"\n+description: \"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\"\n+sidebar:\n+  order: 12\n+---\n+\n+A...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","pullNumber":1798,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\"docs/src/content/docs/enterprise/lifecycle-hooks.md\",\"status\":\"added\",\"additions\":245,\"changes\":245,\"patch\":\"@@ -0,0 +1,245 @@\\n+---\\n+title: \\\"Lifecycle Hooks\\\"\\n+description: \\\"Run custom actions (shell commands, HTTP webhooks) at install, update, and uninstall time.\\\"\\n+sidebar:\\n+  order: 12\\n+---\\n+\\n+APM supports **lifecycle hooks** -- custom actions that fire automatically\\n+at key moment...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1798","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1798,\"title\":\"feat: add general-purpose lifecycle hooks framework (#1529)\",\"body\":\"## TL;DR\\n\\nAdds a Copilot CLI-aligned lifecycle hooks framework that fires events on install, update, and uninstall -- enabling enterprise analytics, CI integrations, and custom automation via standalone JSON hook files.\\n\\n## Problem (WHY)\\n\\nEnterprise users need opt-in analytics and extensibility around package lifecycle events (i...`
- ✓ **shutdown** Shutting down gateway...

</details>
