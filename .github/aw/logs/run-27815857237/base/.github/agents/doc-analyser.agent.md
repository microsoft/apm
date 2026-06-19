---
description: 'Describe what this custom agent does and when to use it.'
tools: [agent/runSubagent]
handoffs: 
  - label: Analyze Documentation
    agent: doc-writer.agent.md
    prompt: Analyze the documentation of the application
    send: true
---


By using the `agent/runSubagent` tool, please dispatch one subAgent per main module of the application to


And then summarize the overall gap