# Vault Knowledge Governance

This file is the canonical operational contract for **Claude Code, Codex, Hermes, future AI-secretary services, and human maintainers** working in this Obsidian Vault.

## Source of truth and scope

| Area | Role | Default agent permission |
| --- | --- | --- |
| `03. 🟤 表达 Present/个人 Wiki/` | Formal, user-curated and reusable knowledge | **Signed broker only**; create/replace requires normalized dry-run, exact Secure Enclave human signature, and single-use nonce |
| `01. 🟣 采集 Grasp/Wiki 研究草稿/` | AI/research evidence and unverified drafts | **Read/dry-run only while draft apply is policy-paused** |
| `01. 🟣 采集 Grasp/任务/秘书inbox/` | One-line capture buffer and per-entry triage state (not formal knowledge) | **Controlled local writes only** via `./tools/secretary-capture-inbox` and `./tools/secretary-inbox-triage` |
| `01. 🟣 采集 Grasp/任务/秘书记忆/` | Secretary observation/decision ledger (not formal Wiki or diary) | **Controlled local write only** via `./tools/secretary-memory-review`; stable keep requires its exact content-bound confirmation token |
| `01. 🟣 采集 Grasp/任务/每日任务/` | Mechanical daily plan files | Read by scan tools; **writes only with user confirmation** (outline→plan expansion), never via Wiki broker |
| `01. 🟣 采集 Grasp/任务/每日任务/.state/` and dated `修订/rNNNN/` | Versioned daily-workflow state, review and correction events | **Controlled local writes only** via `./tools/secretary-daily-workflow`; exact preview token required |
| Other visible folders | Existing knowledge, projects, journals, study notes, media | Read-only unless the user explicitly asks otherwise |
| `.obsidian/`, `.claudian/`, `copilot/`, `.trash/`, attachments, credentials | Configuration, sessions, private material, binary assets | Never read for indexing or write through Wiki workflows |

### Write surfaces (do not mix)

| Surface | Tool | Must NOT use |
| --- | --- | --- |
| Formal Wiki | `/usr/local/bin/wiki-write-broker` via `wiki_formal_dry_run` → `approval_request_user_auth(domain=wiki_formal)` → `wiki_formal_commit_signed` | Unsigned broker calls; direct `Write`/`Edit`/Bash; raw signer invocation; credential input; proposal mutation or signature replay |
| Wiki 研究草稿 | Read/dry-run only while draft apply is policy-paused (atomic draft capability is installed; `--apply` stays paused until human acceptance of root-owned enforcement) | Formal signed-write grant; direct writes |
| 秘书 inbox 一句话捕获 | `./tools/secretary-capture-inbox` | Wiki broker (inbox is not a Wiki page) |
| 秘书 inbox 逐条 triage | `./tools/secretary-inbox-triage` | Rewriting raw capture Markdown; daily-plan or Wiki writes |
| 秘书记忆观察/决策 | `./tools/secretary-memory-review` | Formal Wiki/diary writes; keep without the exact preview token |
| 每日计划文件 | User-confirmed plan expansion into `每日任务/` | Wiki broker; silent bulk rewrite |
| 计划修订 / Day Do / 纠错状态 | `./tools/secretary-daily-workflow` only; append revisions/events after exact preview confirmation | Direct edits, Wiki broker, cross-operation tokens |
| 点名日记的计划联动块 | `./tools/secretary-daily-workflow link-diary` only; exact date/path and exact before/after token required | Broad diary edits, frontmatter rewrites, unqualified basename links |
| 日记正文人工认证追加 | `approval_file_dry_run` → 用户确认精确 diff → `approval_file_authenticated_dry_run(preview_handle)` → 原生差异确认 + Touch ID/系统密码 → `approval_file_commit_authenticated(approval_handle)`；在此受治理 MCP 路径中，完整 proposal/签名不进入模型上下文 | AI 调用原始签名器、接收密码、构造或转运 proposal/签名、拒绝后重试、复用句柄、直接 Write/Edit/Bash、覆盖已有字节、批量日记、frontmatter/标题/顺序重写 |
| Diaries / `.obsidian` / credentials | 日记默认只读；用户明确提出日记任务时只用受限 reader 读取精确日期段落。只有上一行的人机签名 broker 可追加正文；`.obsidian`/credentials 永不读取 | Direct diary mutation, broad scans, indexing, persistence, or any automated path outside the bounded reader or signed broker |

The active Vault is the Markdown source of truth. The former web Wiki at `/Volumes/项目/research-output/personal-wiki/` is a read-only legacy backup and must not be treated as a second write target.

## Content model

### Required frontmatter for formal Wiki pages

```yaml
---
type: index | knowledge | method | project
状态: 进行中 | 已完成 | 已归档
创建时间: '[[YYYY-MM-DD]]'
更新时间: '[[YYYY-MM-DD]]'
链接:
  - '[[上级 MOC]]'
tags:
  - 主题
来源:
  - 可定位来源
verification: 未验证 | 已核对 | 自己实践过
---
```

- `状态` is a single scalar, never a list.
- Formal pages need a clear title, a personal summary, a source, an MOC link, and at least one substantive relationship.
- `链接` represents belonging; body wikilinks represent semantic or evidentiary relationships.
- A page is eligible for `已完成` only when its key claims are verified and it is not a raw AI output.

### Draft packages

Draft imports live at `01. 🟣 采集 Grasp/Wiki 研究草稿/<slug>/`. They preserve raw source references, import date/tool, verification state, and candidate conclusions. Do not overwrite a prior package; add a new dated package or ask before merging.

## Required workflow

1. **Inspect** the relevant formal Wiki page and cited evidence before proposing changes.
2. **Classify** input as formal knowledge, a draft/reference, a project log, or private material.
3. **Normalize proposals** with `wiki_formal_dry_run`; preserve the returned exact target, full content, unified diff, source references, hashes, expiry, and nonce without modification.
4. **Validate** frontmatter, source presence, links, one status value, and absence of credentials.
5. **Promote only with exact human approval**: the human reviews the displayed diff and authorizes it through Secure Enclave/Touch ID. Only the resulting signature may be passed to `wiki_formal_commit_signed`; merge, delete, rename, bulk edit, and unrelated-note changes remain unsupported or require a separate explicit workflow.

For knowledge questions, query `03. 🟤 表达 Present/个人 Wiki/` first. Read cited drafts or existing notes only if the formal page does not answer the question or needs verification.

## Hard safety rules

- Never copy, expose, index, or log API keys, Tokens, passwords, certificates, private diary content, Copilot conversations, or plugin configuration.
- Do not write to daily journals, raw notes, existing project logs, templates, Bases, `.obsidian`, `.claudian`, `copilot`, attachments, or any directory outside the two Wiki boundaries without explicit user authorization.
- Do not use broad shell access as a substitute for the controlled Wiki writer.
- Do not silently rewrite raw evidence; retain source provenance and uncertainty.
- Before reporting completion, run the Wiki validation command and state its actual result.

## Tool and service integration

- **Formal Wiki signed mutation enabled**: root-owned broker v3 may create or replace one formal Markdown page only after proposal normalization, pinned P-256 public-key verification, unexpired content/diff-bound approval, compare-and-swap base verification, and single-use nonce consumption. `verify-grant` is shadow-only and never writes or consumes the nonce. Unsigned calls, direct `Write`/`Edit`/Bash, raw signer invocation, credential input, proposal mutation/replay, delete/rename, and scope widening remain prohibited. AI may only request one OS-mediated decision for the exact broker proposal through `approval_request_user_auth`; the tool never commits. `Wiki 研究草稿/` remains under its separate paused rule.
- **Capture and triage (not Wiki)**: `./tools/secretary-capture-inbox` may append raw captures under `任务/秘书inbox/`; `./tools/secretary-inbox-triage` may write only per-entry JSON under its `.state/` directory. Triage never rewrites raw capture Markdown and task conversion is preview-only. Never promote inbox lines into formal Wiki without human confirmation and a formal page write via broker.
- **Memory review (not Wiki)**: `./tools/secretary-memory-review` may append observations/contradictions and apply keep/discard/defer decisions only in `任务/秘书记忆/memory-ledger.json`. A keep first returns a content-bound preview token; only the exact token may atomically add the stable record and audit entry. It never writes formal Wiki pages or diaries.
- **Daily scan (read-only)**: `./tools/secretary-daily-scan` never writes.
- **Daily organize preview (read-only plan surface)**: `./tools/secretary-daily-organize --date YYYY-MM-DD` reads only the exact today/tomorrow Day Planner/Day Do sections and runs real workflow previews; it never consumes a confirmation token or writes plans, diaries, Wiki, or memory. Preview approval records may be created in the controlled daily `.state/` ledger and must not be treated as an applied plan.
- **Governed daily workflow (not Wiki)**: `./tools/secretary-daily-workflow` may write only dated plan revisions and their `.state/` record after an exact operation-bound preview token. `day-do-review` and `correct-state` append derived events and never change diary/Wiki/memory. `link-diary` is the managed-block exception: it requires a user-named exact `YYYY-MM-DD.md`, rechecks the before hash, and changes only its explicit `ai-secretary:day-planner` block.
- **Memory tool routing**: `memory_propose` is only a read-only duplicate/conflict check and requires the complete sourced candidate envelope (`content`, `kind`, `activation`, `scope`, `source`, `clientId`, `idempotencyKey`); it never creates a Touch ID proposal. Ordinary approved additions use `memory_remember`. Only correct/forget/revoke/confirm-disputed use `approval_memory_dry_run` → `approval_request_user_auth(domain=protected_action)` → the matching unified-memory action with `{proposal, signature}` as `approvalGrant`. Follow safe `reasonCode` feedback once; never retry unchanged rejected input or invoke a nonexistent `approval-broker memory_propose` CLI route.
- **Human-signed protected actions**: direct diary `Write`/`Edit`/Bash remains denied. File writes use opaque, process-local, single-use handles: preview returns only an exact diff and `preview_handle`; after explicit user confirmation, authentication displays that exact diff in a trusted macOS window before Touch ID and returns only `approval_handle`; commit consumes it. In this governed MCP path, complete file proposals and signatures never enter model context. Memory correction/forget/revoke/disputed confirmation continues to use exact broker proposals with `approval_request_user_auth(domain=protected_action)`. Never call a raw signer, accept credentials, retry denial/failure, or reuse a handle/signature. Protected-note replacement is unavailable unless the exact path exists in the root-owned allowlist.
- **Research import**: `./tools/research-import-to-wiki-draft` performs a bounded, symlink-safe, secret-scanned dry-run. Its former per-file apply path was removed because it could leave a half-package; the installed broker backend now has atomic draft batch capability, so `--apply` is fail-closed via a **client-side policy kill-switch** (`.wiki-broker/draft-policy.json`, default `paused`) until a human accepts atomic-batch governance and root-owned enforcement. Never bypass this with direct/per-file or direct `write-batch` calls, and never run `research publish-wiki` against this Vault.
- **Time query (read-only utility, no side effect)**: `./tools/now` returns the current local/UTC/ISO/epoch in a single shell-out (use `./tools/now -j` for one-line JSON). Use it for log attribution, plan anchoring, and any "what time is it" reasoning instead of multi-call `date` chains or the session-start `currentDate` snapshot. macOS BSD `date`-compatible; vault-resident copy of `~/.local/bin/now`, so it works without depending on the host `PATH`. Read-only — never writes, never substitutes for any Wiki or diary write surface.
- Promotion, merge, delete, rename, and bulk edits still require explicit user confirmation.
- Secrets (MiniMax etc.) stay in Hermes env/secrets only — never in notes, skills, or tools source.
- The detailed Wiki procedure lives in `.claude/skills/wiki-maintenance/SKILL.md`; Hermes secretary procedure in `.hermes/skills/ai-secretary/SKILL.md`.

## Human-only operations

Ask before performing any of the following:

- Promote, merge, delete, rename, or bulk-edit notes.
- Change the controlled writer configuration, macOS service account, sandbox profile, or permissions.
- Modify `.obsidian`, `.claudian`, `.claude/settings.json`, `.codex`, global Claude/Codex settings, MCP servers, credentials, or sync configuration.
- Alter existing templates, Bases, daily plans, journals, project logs, or attachments.

## Entry points

| Client | Adapter | Notes |
| --- | --- | --- |
| Any / human | this `AGENTS.md` | Canonical contract |
| Claude Code | `CLAUDE.md` + `.claude/skills/wiki-maintenance` + `.claude/agents/wiki-maintainer.md` | Vault-local settings via `.claude/settings.json` → `.wiki-broker/config/claude-settings.json` |
| Codex | `.codex/AGENTS.md` + `.codex/agents/wiki-maintainer.toml` + `.agents/skills/wiki-maintenance` symlink | Same skill source as Claude |
| Hermes | `.hermes/AGENTS.md` | Points back here; no second rule set |
| Claudian | `.claudian/claudian-settings.json` + `.claude/skills/ai-secretary/` | 主要 AI 秘书运行时；Claude `safeMode=default`, `loadUserSettings=false`; 仅启用白名单 MCP；正式 Wiki 只经签名 broker；日记默认受限只读，精确追加必须经独立人工签名 broker |

- Formal Wiki: [[03. 🟤 表达 Present/个人 Wiki/00. 个人 Wiki索引|个人 Wiki索引]]
- Draft intake: [[01. 🟣 采集 Grasp/Wiki 研究草稿/README|Wiki 研究草稿]]
- Human-facing guide: [[03. 🟤 表达 Present/个人 Wiki/README|个人 Wiki README]]
- Controlled writer: `tools/README.md` · bulk validate: `./tools/wiki-validate-all`
- AI secretary tools: `./tools/secretary-daily-context`, `./tools/secretary-daily-organize`, `./tools/secretary-daily-scan`, `./tools/secretary-daily-workflow`, `./tools/secretary-minimax-normalize`, `./tools/secretary-capture-inbox`, `./tools/secretary-inbox-triage`, `./tools/secretary-memory-review`, `./tools/secretary-recovery-plan`, `./tools/research-import-to-wiki-draft`, `./tools/secretary-plan-expand`, `./tools/secretary-outlook-pilot`
- Templates: `01. 🟣 采集 Grasp/任务/秘书模板/`；23:00 scan via Hermes cron `vault-daily-scan-2300`
- Hermes skill: `.hermes/skills/ai-secretary` (also linked under `~/.hermes/skills/`)
- Desktop: `~/Desktop/启动AI秘书.command`
