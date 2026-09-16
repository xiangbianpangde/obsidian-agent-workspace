/**
 * Note editor with a serialised autosave queue.
 *
 * Two constraints shape this module.
 *
 * 1. **Serialised saves.** Autosave fires on a debounce, but responses can
 *    return out of order — if save B resolves before save A, a naive client
 *    would leave the older hash installed and the next save would 409 against
 *    its own content. Every save therefore carries a monotonically increasing
 *    sequence, and the client ignores any response older than the newest one
 *    it has already accepted.
 *
 * 2. **Conflicts must not destroy work.** A 409 means the file changed
 *    elsewhere (typically Obsidian). The editor stops autosaving, keeps the
 *    local draft in the textarea, and surfaces the choice — it never
 *    auto-overwrites and never silently discards the draft.
 */

const DEBOUNCE_MS = 750;
/** Longest a dirty buffer may sit unsaved while the user keeps typing. */
const MAX_INTERVAL_MS = 5000;

export class NoteEditor {
  /**
   * @param {HTMLElement} host
   * @param {{load: Function, save: Function, create: Function}} io
   */
  constructor(host, io) {
    if (!host) throw new Error('note host element is required');
    this.host = host;
    this.io = io;

    this.hash = null;
    this.noteId = null;
    this.dirty = false;
    this.saving = false;
    this.conflict = false;
    this._saveSeq = 0;
    this._acceptedSeq = 0;
    this._debounceTimer = null;
    this._maxTimer = null;
    this._pending = false;
    this._lastLoadedText = '';
  }

  mount() {
    this.host.innerHTML = `
      <div class="paper-note">
        <div class="paper-note-bar">
          <span class="paper-note-state" data-note="state">未保存</span>
          <span class="paper-spacer"></span>
          <button type="button" class="paper-note-btn" data-note="save">保存</button>
        </div>
        <div class="paper-note-conflict" data-note="conflict" hidden>
          <p>此笔记已在别处被修改（可能是在 Obsidian 中）。自动保存已停止，你的草稿仍保留在这里。</p>
          <div class="paper-note-conflict-actions">
            <button type="button" data-note="reload">放弃草稿并重新加载</button>
            <button type="button" data-note="copy">复制我的草稿</button>
            <button type="button" data-note="dismiss">继续编辑（稍后处理）</button>
          </div>
        </div>
        <textarea class="paper-note-editor" data-note="editor"
                  spellcheck="false" placeholder="记录你的阅读笔记…"></textarea>
      </div>`;

    this.el = {
      state: this.host.querySelector('[data-note="state"]'),
      editor: this.host.querySelector('[data-note="editor"]'),
      save: this.host.querySelector('[data-note="save"]'),
      conflict: this.host.querySelector('[data-note="conflict"]'),
      reload: this.host.querySelector('[data-note="reload"]'),
      copy: this.host.querySelector('[data-note="copy"]'),
      dismiss: this.host.querySelector('[data-note="dismiss"]'),
    };

    this.el.editor.addEventListener('input', () => this._onInput());
    this.el.save.addEventListener('click', () => this.flush());
    this.el.reload.addEventListener('click', () => this.reloadFromRemote());
    this.el.copy.addEventListener('click', () => this._copyDraft());
    this.el.dismiss.addEventListener('click', () => {
      this.el.conflict.hidden = true;
    });
  }

  /** Load a note into the editor. Absent notes are legal (ADR-006). */
  async load() {
    try {
      const payload = await this.io.load();
      if (!payload?.exists) {
        this.hash = null;
        this.noteId = null;
        this._setText('');
        this._setState('尚无笔记，开始输入即创建');
        return { exists: false };
      }
      this.hash = payload.note.hash;
      this.noteId = payload.note.note_id;
      this._setText(payload.note.content);
      this._setState('已保存');
      this.conflict = false;
      this.el.conflict.hidden = true;
      return { exists: true };
    } catch (error) {
      this._setState(`加载失败：${error.message}`);
      return { exists: false, error };
    }
  }

  _setText(text) {
    this.el.editor.value = text ?? '';
    this._lastLoadedText = this.el.editor.value;
    this.dirty = false;
  }

  _onInput() {
    if (this.conflict) return;
    this.dirty = true;
    this._setState('编辑中…');

    clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => this.flush(), DEBOUNCE_MS);

    // A user who types continuously would otherwise never trip the debounce.
    if (!this._maxTimer) {
      this._maxTimer = setTimeout(() => {
        this._maxTimer = null;
        this.flush();
      }, MAX_INTERVAL_MS);
    }
  }

  /** Save now. Safe to call repeatedly; concurrent calls coalesce. */
  async flush() {
    clearTimeout(this._debounceTimer);
    if (this.conflict) return { ok: false, reason: 'conflict' };
    if (!this.dirty) return { ok: true, reason: 'clean' };
    if (this.saving) {
      this._pending = true;
      return { ok: false, reason: 'in-flight' };
    }

    const text = this.el.editor.value;
    const seq = ++this._saveSeq;
    this.saving = true;
    this._setState('保存中…');

    try {
      let result;
      if (this.hash === null) {
        // First write creates the note; the response carries the initial hash.
        result = await this.io.create(text);
        this.hash = result.hash;
        this.noteId = result.note_id;
      } else {
        result = await this.io.save(text, this.hash);
        this.hash = result.new_hash;
      }

      // Ignore a response that a newer save has already superseded.
      if (seq < this._acceptedSeq) {
        return { ok: false, reason: 'superseded' };
      }
      this._acceptedSeq = seq;
      this._lastLoadedText = text;
      this.dirty = this.el.editor.value !== text;
      this._setState(this.dirty ? '有未保存改动' : '已保存');
      this._emit('saved', { hash: this.hash, seq });
      return { ok: true, hash: this.hash };
    } catch (error) {
      if (error.status === 409) {
        this.conflict = true;
        this.el.conflict.hidden = false;
        this._setState('冲突：已在别处修改');
        this._emit('conflict', { message: error.message });
        return { ok: false, reason: 'conflict' };
      }
      this._setState(`保存失败：${error.message}`);
      this._emit('saveerror', { message: error.message });
      return { ok: false, reason: 'error', error };
    } finally {
      this.saving = false;
      if (this._pending) {
        this._pending = false;
        if (!this.conflict) setTimeout(() => this.flush(), 50);
      }
    }
  }

  /** Discard the local draft and take the remote copy. */
  async reloadFromRemote() {
    this.conflict = false;
    this.el.conflict.hidden = true;
    this.dirty = false;
    return this.load();
  }

  async _copyDraft() {
    const text = this.el.editor.value;
    try {
      await navigator.clipboard.writeText(text);
      this._setState('草稿已复制到剪贴板');
    } catch {
      // Clipboard may be blocked; selecting the text still lets the user copy.
      this.el.editor.select();
      this._setState('草稿已选中，请手动复制');
    }
  }

  /** Current text, for the AI context assembler and for switching papers. */
  getText() {
    return this.el?.editor?.value ?? '';
  }

  hasUnsavedWork() {
    return this.dirty && this.el?.editor?.value !== this._lastLoadedText;
  }

  _setState(text) {
    if (this.el?.state) this.el.state.textContent = text;
  }

  _emit(type, detail) {
    this.host.dispatchEvent(new CustomEvent(type, { detail, bubbles: true }));
  }

  dispose() {
    clearTimeout(this._debounceTimer);
    clearTimeout(this._maxTimer);
  }
}

export { DEBOUNCE_MS as NOTE_DEBOUNCE_MS, MAX_INTERVAL_MS as NOTE_MAX_INTERVAL_MS };
