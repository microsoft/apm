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
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"total_count\":1,\"incomplete_results\":false,\"items\":[{\"id\":1059472549,\"name\":\"apm\",\"full_name\":\"microsoft/apm\",\"description\":\"Agent Package Manager\",\"html_url\":\"https://github.com/microsoft/apm\",\"language\":\"Python\",\"stargazers_count\":2916,\"forks_count\":241,\"open_issues_count\":87,\"updated_at\":\"2026-06-17T14:27:33Z\",\"created_at\":\"2025-09-18T13:45:22Z\",\"topics\":[\"ai-agents\",\"claude-code\",\"codex-...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":50,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":50,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_files","owner":"microsoft","perPage":100,"pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"filename\":\".gitignore\",\"status\":\"modified\",\"additions\":3,\"changes\":3,\"patch\":\"@@ -86,3 +86,6 @@ server.pid\\n build/apm-*/\\n copilot-scratch/\\n .local/\\n+\\n+# APM-deployed canvas extensions (auto-generated by apm install)\\n+.github/extensions/\"},{\"filename\":\"apm.lock.yaml\",\"status\":\"modified\",\"additions\":8,\"deletions\":2,\"changes\":10,\"patch\":\"@@ -1,6 +1,6 @@\\n lockfile_version: '1'\\n-generated_at: '...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"dir\",\"size\":0,\"name\":\".apm\",\"path\":\"packages/apm-contributor-dashboard/.apm\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/trees/[REDACTED]\",\"html_url\":\"https://github.com/microsoft/apm/tree/[REDACTED]/packages/apm-contributor-dashboard/.apm\"},{\"type\":\"di...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"dir\",\"size\":0,\"name\":\".apm\",\"path\":\"packages/apm-contributor-dashboard/.apm\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/trees/[REDACTED]\",\"html_url\":\"https://github.com/microsoft/apm/tree/[REDACTED]/packages/apm-contributor-dashboard/.apm\"},{\"type\":\"di...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get","owner":"microsoft","pullNumber":"1825","repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"number\":1825,\"title\":\"feat: add APM Contributor Dashboard canvas package\",\"body\":\"## Description\\n\\nAdds a Copilot CLI canvas extension as a first-party APM package that provides a live\\ncontributor dashboard for any GitHub repository. The dashboard gives maintainers and\\ncontributors a single interactive panel to triage issues, review PRs, run CI actions,\\nand draft comments collaboratively with Copilot -- all without leavi...`
- 🔍 rpc **github**→`tools/call` `pull_request_read`
  
  ```json
  {"params":{"arguments":{"method":"get_diff","owner":"microsoft","pullNumber":1825,"repo":"apm"},"name":"pull_request_read"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"diff --git a/.gitignore b/.gitignore\nindex e7a922796..c55897be4 100644\n--- a/.gitignore\n+++ b/.gitignore\n@@ -86,3 +86,6 @@ server.pid\n build/apm-*/\n copilot-scratch/\n .local/\n+\n+# APM-deployed canvas extensions (auto-generated by apm install)\n+.github/extensions/\ndiff --git a/apm.lock.yaml b/apm.lock.yaml\nindex 641936a9a..608038a5e 100644\n--- a/apm.lock.yaml\n+++ b/apm.lock.yaml\n@@ -1,6 +1,6 @@\n lockfile_version: '1'\n-genera...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/tests/server.test.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extension: issue-monitor\n// Live dashboard monitoring APM issue triage inbox and session status\n// Fetches issues in real-time from GitHub via `gh` CLI\n\nimport { createServe...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/logic.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/apm.yml","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extracted HTTP request handler with dependency injection for testability.\n// Dependencies are passed via the `deps` object so tests can substitute mocks.\n//\n// Usage in ...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/tests/server.test.mjs","mimeType":"text/plain; charset=utf-8","text":"// Unit tests for server-handler.mjs with fully mocked dependencies.\n// No live server needed -- no side effects on the Copilot harness or GitHub.\n// Run: node --test tes...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extension: issue-monitor\n// Live dashboard monitoring APM issue triage inbox and session status\n// Fetches issues in real-time from GitHub via `gh` CLI\n\nimport { createServe...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/logic.mjs","mimeType":"text/plain; charset=utf-8","text":"// Pure business logic extracted for testability\n// Used by extension.mjs and test suite\n\nexport function classifyIssue(raw) {\n    const labels = (raw.labels || []).map((l) =\u003e...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/apm.yml","mimeType":"text/plain; charset=utf-8","text":"name: apm-contributor-dashboard\nversion: 0.1.0\ndescription: \u003e\n  Live contributor dashboard canvas for GitHub repositories.\n  Tracks issues, PRs, CI pipelines, and panel reviews in a single\n  interactive pan...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extracted HTTP request handler with dependency injection for testability.\n// Dependencies are passed via the `deps` object so tests can substitute mocks.\n//\n// Usage in ...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/README.md","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/apm.yml","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/README.md","mimeType":"text/plain; charset=utf-8","text":"# APM Contributor Dashboard\n\nA live GitHub Copilot CLI canvas extension that provides an interactive\ncontributor dashboard for the microsoft/apm repository. It monitors\nissues, pull requests, CI pipelines, and ...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/apm.yml","mimeType":"text/plain; charset=utf-8","text":"name: apm-contributor-dashboard\nversion: 0.1.0\ndescription: \u003e\n  Live contributor dashboard canvas for GitHub repositories.\n  Tracks issues, PRs, CI pipelines, and panel reviews in a single\n  interactive pan...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"dir\",\"size\":0,\"name\":\"extensions\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/trees/[REDACTED]\",\"html_url\":\"https://github.com/microsoft/apm/tree/[REDACTED]/packages/apm-contributor-dash...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"file\",\"size\":27,\"name\":\".gitignore\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/blobs/[REDACTED]\",\"html_url\":\"https://github.com/microso...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"file\",\"size\":27,\"name\":\".gitignore\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/blobs/[REDACTED]\",\"html_url\":\"https://github.com/microso...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/extension.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extension: issue-monitor\n// Live dashboard monitoring APM issue triage inbox and session status\n// Fetches issues in real-time from GitHub via `gh` CLI\n\nimport { createServe...`
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/server-handler.mjs","mimeType":"text/plain; charset=utf-8","text":"// Extracted HTTP request handler with dependency injection for testability.\n// Dependencies are passed via the `deps` object so tests can substitute mocks.\n//\n// Usage in ...`
- 🔍 rpc **github**←`resp`
  
  ```json
  {"id":1,"result":{"content":[{"text":"successfully downloaded text file (SHA: [REDACTED])","type":"text"},{"resource":{"mimeType":"text/plain; charset=utf-8","text":"dist/\nclient/node_modules/\n","uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/.gitignore"},"type":"resource"}]}}
  ```
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"file\",\"size\":30048,\"name\":\"App.css\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/App.css\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/App.css?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/blobs/[REDACTED]\",\"html_url\":\"https://gi...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/stores","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"file\",\"size\":436,\"name\":\"issues.js\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/stores/issues.js\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/stores/issues.js?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/blobs/[REDACTED]\",\"html_...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/tests","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"[{\"type\":\"file\",\"size\":9745,\"name\":\"api-service.test.mjs\",\"path\":\"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/tests/api-service.test.mjs\",\"sha\":\"[REDACTED]\",\"url\":\"https://api.github.com/repos/microsoft/apm/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/tests/api-service.test.mjs?ref=[REDACTED]\",\"git_url\":\"https://api.github.com/repos/microsoft/apm/git/blobs/[REDACTED]...`
- 🔍 rpc **github**→`tools/call` `get_file_contents`
  
  ```json
  {"params":{"arguments":{"owner":"microsoft","path":"packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/stores/permissions.js","ref":"refs/pull/1825/head","repo":"apm"},"name":"get_file_contents"}}
  ```
- 🔍 rpc **github**←`resp` `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"successfully downloaded text file (SHA: [REDACTED])"},{"type":"resource","resource":{"uri":"repo://microsoft/apm/sha/[REDACTED]/contents/packages/apm-contributor-dashboard/.apm/extensions/issue-monitor/client/src/stores/permissions.js","mimeType":"text/plain; charset=utf-8","text":"import { createResource } from \"solid-js\";\n\nasync function fetchPermissions() {\n  const res = await fetch(\"/api/permissions\");\n  if (!res.ok) return { pu...`
- 🔍 rpc **safeoutputs**→`tools/call` `noop`
  
  ```json
  {"params":{"arguments":{"message":"CEO synthesis complete for PR #1825. Returning JSON to orchestrator -- no GitHub write action taken."},"name":"noop"}}
  ```
- 🔍 rpc **safeoutputs**←`resp`
  
  ```json
  {"id":1,"result":{"content":[{"text":"{\"result\":\"success\"}","type":"text"}]}}
  ```
- 🔍 rpc **safeoutputs**→`tools/call` `{"jsonrpc":"2.0","method":"tools/call","params":{"arguments":{"body":"## APM Review Panel: `needs_rework`\n\n\u003e Adds APM's first first-party canvas package -- a Copilot contributor dashboard with 186 JS tests and a sound security core -- but ships non-functional on apm install because dist/ is gitignored; rework needed on three fronts.\n\ncc @sergio-sisternes-epam @danielmeppiel -- a fresh advisory pass is ready for your review.\n\nThis PR ships APM's most significant first-party proof-point to date: a ...`
- 🔍 rpc **safeoutputs**←`resp`
  
  ```json
  {"id":1,"result":{"content":[{"text":"{\"result\":\"success\",\"temporary_id\":\"aw_QqOi0qXh\",\"comment\":\"#aw_QqOi0qXh\"}","type":"text"}]}}
  ```
- 🔍 rpc **safeoutputs**→`tools/call` `remove_labels`
  
  ```json
  {"params":{"arguments":{"item_number":1825,"labels":["panel-review","panel-approved","panel-rejected"]},"name":"remove_labels"}}
  ```
- 🔍 rpc **safeoutputs**←`resp`
  
  ```json
  {"id":1,"result":{"content":[{"text":"{\"result\":\"success\"}","type":"text"}]}}
  ```
- ✓ **shutdown** Shutting down gateway...

</details>
