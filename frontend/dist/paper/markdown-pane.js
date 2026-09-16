/**
 * Translation / Markdown reading pane.
 *
 * Rendering goes through `window.WorkspaceMarkdown`, the host page's existing
 * pipeline (marked + KaTeX + wikilinks + DOMPurify). Duplicating that pipeline
 * inside this module would mean two sanitisation rulesets drifting apart, so
 * the module reuses it and only single-sources the sanitisation decision.
 *
 * The raw Markdown is preserved: this pane is a *view*, and the note editor
 * writes the original text back, so nothing here may convert the document into
 * an irreversible rich-text form.
 */

const PHASE_LABELS = {
  TRANSLATION_FULL: '全文翻译',
  TRANSLATION_GUIDE: '翻译导读',
  EXTRACTED_MARKDOWN: '解析 Markdown',
  OTHER_MARKDOWN: '其他 Markdown',
};

export class MarkdownPane {
  /**
   * @param {HTMLElement} host
   */
  constructor(host) {
    if (!host) throw new Error('markdown host element is required');
    this.host = host;
    this.raw = '';
    this.sourceId = null;
    this.headings = [];
    this._scrollRatio = 0;
  }

  /** True when the host page's renderer is available. */
  static rendererAvailable() {
    return typeof window !== 'undefined' && !!window.WorkspaceMarkdown?.render;
  }

  /**
   * Render Markdown text.
   * @param {string} text raw markdown
   * @param {{sourceId?: string, restoreRatio?: number}} [opts]
   */
  setContent(text, opts = {}) {
    this.raw = String(text ?? '');
    this.sourceId = opts.sourceId ?? null;

    if (!MarkdownPane.rendererAvailable()) {
      // Fail loudly rather than injecting unsanitised HTML as a fallback.
      this.host.innerHTML =
        '<p class="paper-error">Markdown 渲染器不可用（宿主页未提供 WorkspaceMarkdown）。</p>';
      this._emit('rendererror', { reason: 'renderer-unavailable' });
      return;
    }

    const { html, error } = window.WorkspaceMarkdown.render(this.raw);
    if (error) {
      this.host.innerHTML = `<p class="paper-error">渲染失败：${escapeHtml(error)}</p>`;
      this._emit('rendererror', { reason: error });
      return;
    }

    this.host.innerHTML = `<article class="paper-md-body">${html}</article>`;
    this._collectHeadings();
    this._decorateHeadings();

    if (typeof opts.restoreRatio === 'number') {
      this.scrollToRatio(opts.restoreRatio);
    }

    this._emit('rendered', {
      sourceId: this.sourceId,
      headings: this.headings.map((h) => h.title),
    });
  }

  /**
   * Collect headings into a navigable outline.
   *
   * Heading path is also the Markdown anchor's structural locator (ADR-008),
   * so it must be derived from the rendered document rather than from a DOM
   * selector that a re-render would invalidate.
   */
  _collectHeadings() {
    this.headings = [];
    const nodes = this.host.querySelectorAll('h1, h2, h3, h4, h5, h6');
    nodes.forEach((node, index) => {
      const id = `paper-md-h-${index}`;
      node.id = id;
      this.headings.push({
        id,
        level: Number(node.tagName.substring(1)),
        title: node.textContent.trim(),
      });
    });
  }

  _decorateHeadings() {
    // Nothing visual yet; headings carry ids so the annotation list can jump
    // to them in P0.5 without a re-render invalidating the anchor.
  }

  /** @returns {Array<{id: string, level: number, title: string}>} */
  getOutline() {
    return [...this.headings];
  }

  /** Current heading path, used as the Markdown anchor's structural locator. */
  currentHeadingPath() {
    const body = this.host.querySelector('.paper-md-body');
    if (!body) return [];
    const scrollTop = this.host.scrollTop;
    const path = [];
    for (const heading of this.headings) {
      const node = this.host.querySelector(`#${heading.id}`);
      if (!node) continue;
      const offset = node.offsetTop - body.offsetTop;
      if (offset <= scrollTop + 40) {
        // Keep an ancestor chain: drop deeper entries when a sibling appears.
        while (path.length && path[path.length - 1].level >= heading.level) path.pop();
        path.push(heading);
      }
    }
    return path.map((h) => h.title);
  }

  /** @returns {number} 0..1 */
  getScrollRatio() {
    const max = this.host.scrollHeight - this.host.clientHeight;
    return max > 0 ? this.host.scrollTop / max : 0;
  }

  scrollToRatio(ratio) {
    const max = this.host.scrollHeight - this.host.clientHeight;
    this.host.scrollTop = Math.max(0, Math.min(1, Number(ratio) || 0)) * max;
  }

  /**
   * Selected text inside the pane, with surrounding context.
   *
   * Context is captured because a Markdown anchor must survive re-rendering;
   * a DOM selector alone would break the moment KaTeX or a table changes the
   * node structure (ADR-008).
   * @returns {{exact: string, prefix: string, suffix: string, headingPath: string[]}|null}
   */
  getSelection() {
    const sel = window.getSelection();
    const text = sel ? String(sel).trim() : '';
    if (!text) return null;
    if (!this.host.contains(sel.anchorNode)) return null;

    let prefix = '';
    let suffix = '';
    try {
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      const full = node.nodeValue || '';
      prefix = full.slice(Math.max(0, range.startOffset - 48), range.startOffset);
      suffix = full.slice(range.endOffset, range.endOffset + 48);
    } catch {
      /* context is best effort */
    }

    return {
      exact: text,
      prefix,
      suffix,
      headingPath: this.currentHeadingPath(),
    };
  }

  clear() {
    this.raw = '';
    this.host.innerHTML = '<p class="paper-muted">选择一篇论文以开始对照阅读</p>';
    this.headings = [];
  }

  _emit(type, detail) {
    this.host.dispatchEvent(new CustomEvent(type, { detail, bubbles: true }));
  }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

export { PHASE_LABELS };
