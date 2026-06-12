# Security Policy

## Supported versions

Pre-1.0, only the latest release is supported. Fixes land on `main` and are
cut as a new release; older tags will not be back-patched.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security problems.

Email **Avicennasis@gmail.com** with:

- A description of the issue.
- Steps to reproduce (or a proof-of-concept).
- The version or commit SHA you found it against.
- Any suggested mitigation if you have one.

Expect an acknowledgement within a week. This is a side-project — there is
no bug bounty and no SLA — but security issues are taken seriously and a
fix and disclosure will be coordinated with you.

## Security model notes

- **Credentials**: the Redmine API key is supplied via environment variables
  (or a configured secrets lookup); it is never written to the schema cache,
  never logged, and never echoed back in tool responses.
- **Filesystem access**: attachment upload/download is path-restricted to
  `REDMINE_MCP_ALLOWED_DIRECTORIES`; downloads validate byte count against
  the metadata `filesize` before writing.
- **Passthrough**: the generic `redmine_request` escape hatch is disabled by
  default and gated behind `REDMINE_MCP_ENABLE_PASSTHROUGH=true`; every
  passthrough response is flagged `validation_skipped: true`.
- **Read-only mode**: setting `REDMINE_MCP_READ_ONLY` blocks all write tools.

## Out of scope

- Issues in upstream dependencies (report upstream).
- Misconfiguration by consumers of this project (e.g. enabling passthrough
  or granting overly broad allowed directories).
- The security of the Redmine instance itself.
