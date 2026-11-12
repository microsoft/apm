<!--
Load ONLY at synthesis time. Fill one advisory comment, under 250 lines,
ASCII only. Direct invocation returns this without posting.
Default `debug: off` posts only the HTML receipt plus the Suggested
PR comment body. `debug: on` posts this filled template prefixed with
`[i] Skill debug is on.` Both paths go through `autopilot-comment`,
which appends the AI disclaimer. Never post with `gh pr comment`.
-->

<!-- apm-pr-triage-advisory:v1 target=pr#<N> watermark=<latest-comment-id-or-updated-at> -->
## PR triage recommendation

<ready-for-review | needs-design | needs-issue | duplicate-of | decline-with-reason | auto-handle>

**Recommendation only, not merge or scope approval.** A responsible
human maintainer must approve. Labels, automated advice, and
silence are not approval.

## Linked issue

<issue number and status, or "none -- community PR without an issue">

## Proposed classification

<Only useful type/area/theme labels from the label contract, or none.
Existing human choices stay unchanged; name conflicts rather than replacing them.>

## Suggested next action

<One concrete step: discuss design, file an issue, wait for human
review, or close as duplicate. Do not invite merge from this
recommendation alone.>

## Suggested PR comment

```markdown
<When no linked status/accepted issue, thank the author and invite
them to open an issue for maintainer review and acceptance first.
Link CONTRIBUTING.md. Do not imply the project has accepted,
assigned, or merged this work. Do not contradict CODEOWNERS.

Example:
Thank you for contributing this pull request. APM starts with an
issue, not an implementation
(https://github.com/microsoft/apm/blob/main/CONTRIBUTING.md).
Please open an issue describing the user problem so a maintainer
can review and accept the scope first. This PR is labelled
status/deferred until that happens.>
```
