# Claude Code adapter for this Vault

Read and follow [`AGENTS.md`](AGENTS.md) before making any change. It is the canonical cross-agent governance contract.

## Default operating mode

- Answer knowledge questions from `03. 🟤 表达 Present/个人 Wiki/` first.
- Treat the rest of the Vault as read-only unless the user explicitly requests a change there.
- Use the `wiki-maintenance` skill for intake, validation, and promotion-candidate work.
- For a formal Wiki create/replace, use `wiki_formal_dry_run`, keep the exact proposal unchanged, call `approval_request_user_auth` with `domain=wiki_formal`, and only after the user approves in macOS pass the returned signature unchanged to `wiki_formal_commit_signed`. Never call the raw signer, accept credentials, retry denial, mutate the proposal, or reuse a signature. `Wiki 研究草稿/` mutation remains paused.
- One-line capture is **not** Wiki: use `./tools/secretary-capture-inbox` → `任务/秘书inbox/` only. Do not broker-write the inbox; do not capture-write formal Wiki.
- Memory routing: `memory_propose` requires `content`, `kind`, `activation`, `scope`, `source`, `clientId`, and `idempotencyKey`; it only checks duplicates/conflicts and never creates a Touch ID proposal. New approved memory uses `memory_remember`. Correct/forget/revoke/confirm-disputed use `approval_memory_dry_run` → `approval_request_user_auth` → matching unified-memory action with `{proposal, signature}` as `approvalGrant`. Correct safe `reasonCode` failures once; never repeat unchanged input or call `approval-broker memory_propose`.
- Never substitute unrestricted Bash, Write, Edit, or direct filesystem commands for governed paths. Diary direct mutation remains denied. Use only `approval_file_dry_run` → explicit confirmation of its exact diff → `approval_file_authenticated_dry_run(preview_handle)` → `approval_file_commit_authenticated(approval_handle)`. The trusted macOS window shows the exact diff before Touch ID; in this governed MCP path, complete proposals and signatures never enter model context. Never call the raw signer, accept credentials, retry denial/failure, or reuse a handle.
- Never use subagents for Vault maintenance unless the user explicitly requests them.

## Required confirmation

Ask before promotion, merging, deleting, bulk editing, renaming, configuration changes, or any write outside the two governed Wiki directories.
