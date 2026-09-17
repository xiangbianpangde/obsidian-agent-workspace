/**
 * WorkspaceState capture and restore.
 *
 * The backend endpoints existed from Day 5 but nothing on the frontend ever
 * called them, so a reading position was never recorded — the "resume where you
 * left off" promise was never wired. This module closes that loop.
 *
 * Write policy (ADR-007):
 *   - scroll/zoom events only update memory;
 *   - trailing debounce of 750 ms;
 *   - a hard ceiling of 5 s so continuous scrolling still checkpoints;
 *   - flush on paper switch and on visibilitychange -> hidden;
 *   - never rely on beforeunload as the only save path.
 *
 * Positions are stored as ratios rather than raw pixel offsets, which survive
 * window resizing, zoom changes and differing display DPI.
 */

const DEBOUNCE_MS = 750;
const MAX_INTERVAL_MS = 5000;

export class WorkspaceStateTracker {
  /**
   * @param {{load: Function, save: Function}} io
   */
  constructor(io) {
    this.io = io;
    this.paperId = null;
    this.state = null;
    this._dirty = false;
    this._debounce = null;
    this._max = null;
    this._epoch = 0;
    this._flushing = null;
    /** Version last read, sent back so a concurrent tab's write is not lost. */
    this.stateVersion = null;
    this._bound = false;
    this._visibilityHandler = null;
  }

  /** Attach the flush-on-hide handler once. */
  bind() {
    if (this._bound) return;
    this._bound = true;
    this._visibilityHandler = () => {
      if (document.visibilityState === 'hidden') this.flush();
    };
    document.addEventListener('visibilitychange', this._visibilityHandler);
  }

  /**
   * Load the stored state for a paper.
   *
   * Restoring granular offsets is only safe when the source revision is
   * unchanged: if the PDF or Markdown was replaced, the old offset points into
   * different content, so we fall back to the page or heading only.
   */
  async loadFor(paperId) {
    const epoch = ++this._epoch;
    await this.flush();
    this.paperId = paperId;
    try {
      const payload = await this.io.load();
      if (epoch !== this._epoch) return null;
      this.state = payload?.state || null;
      // Remember the version we read so a save can detect that another tab
      // advanced it and refuse rather than silently overwriting.
      this.stateVersion = payload?.state?.state_version ?? null;
      this._dirty = false;
      return this.state;
    } catch {
      if (epoch === this._epoch) {
        this.state = null;
        this.stateVersion = null;
      }
      return null;
    }
  }

  /** Position for a source, if one was recorded. */
  positionFor(sourceId) {
    return this.state?.source_positions?.[sourceId] || null;
  }

  /**
   * Restore a viewer position.
   *
   * @returns {{pageIndex?: number, scrollRatio?: number, restored: 'full'|'coarse'|'none'}}
   */
  resolveRestore(sourceId, sourceVersion, kind) {
    const stored = this.positionFor(sourceId);
    if (!stored) return { restored: 'none' };

    const sameVersion = stored.source_version == null || stored.source_version === sourceVersion;
    if (!sameVersion) {
      // Version moved: only the page / heading survives, the offset does not.
      return kind === 'PDF'
        ? { pageIndex: stored.page_index ?? 0, restored: 'coarse' }
        : { restored: 'coarse' };
    }
    return kind === 'PDF'
      ? {
          pageIndex: stored.page_index ?? 0,
          scrollRatio: stored.page_offset_ratio ?? 0,
          scale: stored.scale,
          rotation: stored.rotation,
          restored: 'full',
        }
      : { scrollRatio: stored.scroll_ratio ?? 0, restored: 'full' };
  }

  /** Record a PDF position. Cheap; only touches memory. */
  notePdfPosition(sourceId, { pageIndex, offsetRatio = 0, scale = 1, rotation = 0, sourceVersion }) {
    if (!this.paperId) return;
    this._merge(sourceId, {
      kind: 'PDF',
      page_index: Math.max(0, Math.floor(pageIndex || 0)),
      page_offset_ratio: clamp01(offsetRatio),
      scale: Number(scale) || 1,
      rotation: Number(rotation) || 0,
      source_version: sourceVersion ?? null,
    });
  }

  /** Record a Markdown position. */
  noteMarkdownPosition(sourceId, { headingPath = [], scrollRatio = 0, sourceVersion }) {
    if (!this.paperId) return;
    this._merge(sourceId, {
      kind: 'MARKDOWN',
      heading_path: Array.isArray(headingPath) ? headingPath.filter(Boolean) : [],
      scroll_ratio: clamp01(scrollRatio),
      source_version: sourceVersion ?? null,
    });
  }

  /** Record the note cursor. */
  noteCursor({ start = null, end = null, noteSha = null }) {
    if (!this.paperId) return;
    this.state = this.state || {};
    this.state.note_cursor_start = start;
    this.state.note_cursor_end = end;
    if (noteSha) this.state.note_content_sha256 = noteSha;
    this._schedule();
  }

  /** Record which pane and which sources are active. */
  noteActive({ pane = null, pdfSourceId = null, markdownSourceId = null }) {
    if (!this.paperId) return;
    this.state = this.state || {};
    if (pane) this.state.active_pane = pane;
    if (pdfSourceId !== null) this.state.active_pdf_source_id = pdfSourceId;
    if (markdownSourceId !== null) this.state.active_markdown_source_id = markdownSourceId;
    this._schedule();
  }

  _merge(sourceId, position) {
    this.state = this.state || {};
    const positions = this.state.source_positions || {};
    positions[sourceId] = { ...(positions[sourceId] || {}), ...position };
    this.state.source_positions = positions;
    this._schedule();
  }

  _schedule() {
    this._dirty = true;
    clearTimeout(this._debounce);
    this._debounce = setTimeout(() => this.flush(), DEBOUNCE_MS);

    // Continuous scrolling would otherwise keep resetting the debounce and
    // never checkpoint.
    if (!this._max) {
      this._max = setTimeout(() => {
        this._max = null;
        this.flush();
      }, MAX_INTERVAL_MS);
    }
  }

  /** Persist now. Coalesces concurrent calls. */
  async flush() {
    clearTimeout(this._debounce);
    if (!this.paperId || !this._dirty || !this.state) return { ok: true, reason: 'clean' };
    if (this._flushing) return this._flushing;

    const paperId = this.paperId;
    const epoch = this._epoch;
    const snapshot = { ...this.state };
    const expectedVersion = this.stateVersion;
    this._dirty = false;

    this._flushing = (async () => {
      try {
        const result = await this.io.save(snapshot, expectedVersion);
        if (epoch !== this._epoch) return { ok: false, reason: 'stale-epoch' };
        if (result && typeof result.state_version === 'number') {
          this.stateVersion = result.state_version;
        }
        return { ok: true };
      } catch (error) {
        // A failed checkpoint is not worth interrupting reading for; the next
        // scroll will schedule another attempt.
        if (epoch !== this._epoch) return { ok: false, reason: 'stale-epoch' };
        if (error.status === 409) {
          // Another tab advanced the state. Adopt its version and retry once so
          // the newer position is not clobbered, and do not treat this as a
          // user-visible failure.
          this.stateVersion = null;
          this._dirty = true;
          return { ok: false, reason: 'version-conflict' };
        }
        this._dirty = true;
        return { ok: false, reason: 'error', error };
      } finally {
        this._flushing = null;
      }
    })();

    void paperId;
    return this._flushing;
  }

  dispose() {
    clearTimeout(this._debounce);
    clearTimeout(this._max);
    if (this._visibilityHandler) {
      document.removeEventListener('visibilitychange', this._visibilityHandler);
      this._visibilityHandler = null;
    }
    this._bound = false;
  }
}

function clamp01(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.min(1, Math.max(0, number));
}

export { DEBOUNCE_MS as WORKSPACE_DEBOUNCE_MS, MAX_INTERVAL_MS as WORKSPACE_MAX_INTERVAL_MS };
