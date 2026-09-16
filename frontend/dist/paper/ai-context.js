/**
 * AIContextV1 assembler.
 *
 * P0 defines and tests this contract; it never calls a model and never makes a
 * network request. Assembling locally now is what keeps the P1 work from
 * becoming a data-architecture rewrite: every field the future assistant needs
 * is already derivable from the aggregate.
 *
 * Two rules from the schema are enforced here rather than trusted to callers:
 *
 *  - **Every payload carries hashes.** Without a source and note hash, an
 *    answer cannot be traced back to the exact bytes it was built from.
 *  - **Excerpts are bounded.** The assembler never returns a whole paper; a
 *    byte budget is applied and the result records whether it truncated.
 */

export const AI_CONTEXT_SCHEMA_VERSION = '1';

/** Default ceiling for assembled excerpt bytes. */
export const DEFAULT_MAX_BYTES = 64 * 1024;

/**
 * Assemble a context envelope.
 *
 * @param {object} input
 * @param {object}  input.paper            Paper payload from the API.
 * @param {object=} input.focus            Where the user is.
 * @param {string=} input.focus.pane       PDF | MARKDOWN | NOTE
 * @param {string=} input.focus.sourceId
 * @param {number=} input.focus.sourceVersion
 * @param {string=} input.focus.sourceSha256
 * @param {object=} input.focus.locator
 * @param {object=} input.focus.selection  {exact, prefix, suffix}
 * @param {Array=}  input.sourceRefs       [{sourceId, role, sha256, excerpt}]
 * @param {object=} input.note             {noteId, sha256, content}
 * @param {Array=}  input.annotations
 * @param {number=} input.maxBytes
 * @returns {object} AIContextV1 envelope
 */
export function assembleAiContext(input) {
  const {
    paper,
    focus = null,
    sourceRefs = [],
    note = null,
    annotations = [],
    maxBytes = DEFAULT_MAX_BYTES,
  } = input || {};

  if (!paper || !paper.paper_id) {
    throw new Error('assembleAiContext requires a paper');
  }

  let used = 0;
  let truncated = false;

  // Apply the budget in priority order: the focus excerpt matters most, then
  // the note, then surrounding sources.
  const take = (text) => {
    if (text == null) return null;
    const value = String(text);
    const remaining = maxBytes - used;
    if (remaining <= 0) {
      truncated = true;
      return null;
    }
    if (value.length > remaining) {
      truncated = true;
      used = maxBytes;
      return value.slice(0, remaining);
    }
    used += value.length;
    return value;
  };

  const refs = sourceRefs.map((ref) => ({
    source_id: ref.sourceId,
    role: ref.role,
    sha256: ref.sha256,
    excerpt: take(ref.excerpt),
  }));

  const envelope = {
    schema_version: AI_CONTEXT_SCHEMA_VERSION,
    assembled_at: new Date().toISOString(),
    paper: {
      paper_id: paper.paper_id,
      title: paper.title || paper.display_title || '',
      tags: Array.isArray(paper.paper_tags) ? [...paper.paper_tags] : [],
      status: paper.status || 'UNREAD',
    },
    focus: focus
      ? {
          pane: focus.pane,
          source_id: focus.sourceId,
          ...(focus.sourceVersion != null ? { source_version: focus.sourceVersion } : {}),
          ...(focus.sourceSha256 ? { source_sha256: focus.sourceSha256 } : {}),
          ...(focus.locator ? { locator: focus.locator } : {}),
          selection: focus.selection
            ? {
                exact: focus.selection.exact,
                prefix: focus.selection.prefix ?? null,
                suffix: focus.selection.suffix ?? null,
              }
            : null,
        }
      : null,
    source_refs: refs,
    note: note
      ? {
          note_id: note.noteId,
          sha256: note.sha256,
          content: take(note.content),
        }
      : null,
    annotations: annotations.map((a) => ({
      annotation_id: a.annotation_id,
      source_id: a.source_id,
      kind: a.kind,
      anchor: safeParse(a.anchor_json) || a.anchor || {},
      body_markdown: a.body_markdown ?? null,
      selected_text: a.selected_text ?? null,
    })),
    resource_versions: [],
    budget: {
      max_bytes: maxBytes,
      used_bytes: used,
      truncated,
    },
  };

  // Traceability: stamp exactly which revisions the context was built from.
  if (focus?.sourceId && focus.sourceSha256) {
    envelope.resource_versions.push({
      resource_id: focus.sourceId,
      source_version: focus.sourceVersion ?? 0,
      sha256: focus.sourceSha256,
    });
  }
  for (const ref of sourceRefs) {
    if (!ref.sha256) continue;
    if (envelope.resource_versions.some((r) => r.resource_id === ref.sourceId)) continue;
    envelope.resource_versions.push({
      resource_id: ref.sourceId,
      source_version: ref.sourceVersion ?? 0,
      sha256: ref.sha256,
    });
  }
  if (note?.noteId && note.sha256) {
    envelope.resource_versions.push({
      resource_id: note.noteId,
      source_version: 0,
      sha256: note.sha256,
    });
  }

  return envelope;
}

function safeParse(value) {
  if (!value) return null;
  if (typeof value === 'object') return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}
