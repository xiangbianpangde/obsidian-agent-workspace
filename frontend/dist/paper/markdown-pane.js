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
    this._enforceEgressBoundary();
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

  /**
   * Apply the zero-egress boundary to rendered content (ADR-009).
   *
   * A translated paper routinely cites external links and remote images.
   * Rendering them as-is means simply opening a paper silently contacts
   * third-party servers, which is exactly what the paper subsystem promises not
   * to do.
   *
   *   - remote images are replaced with a placeholder that requires a click;
   *   - links keep their text but are marked so the host can confirm first.
   *
   * Local images are left alone: they resolve through the same-origin asset
   * endpoint and never leave the machine.
   */
  _enforceEgressBoundary() {
    const blocked = [];

    this.host.querySelectorAll('img').forEach((img) => {
      const src = img.getAttribute('src') || '';
      if (!src || this._isSameOrigin(src)) return;
      const placeholder = document.createElement('button');
      placeholder.type = 'button';
      placeholder.className = 'paper-external-image';
      placeholder.textContent = '外部图片已阻止 — 点击后加载';
      placeholder.dataset.externalSrc = src;
      placeholder.addEventListener('click', () => {
        const real = document.createElement('img');
        real.src = placeholder.dataset.externalSrc;
        real.alt = img.alt || '外部图片';
        placeholder.replaceWith(real);
      });
      img.replaceWith(placeholder);
      blocked.push(src);
    });

    this.host.querySelectorAll('a[href]').forEach((anchor) => {
      const href = anchor.getAttribute('href') || '';
      if (!href || href.startsWith('#') || this._isSameOrigin(href)) return;
      anchor.dataset.externalHref = href;
      anchor.setAttribute('rel', 'noopener noreferrer nofollow');
      anchor.classList.add('paper-external-link');
      blocked.push(href);
    });

    if (blocked.length) {
      this._emit('egressblocked', { count: blocked.length });
    }
  }

  _isSameOrigin(url) {
    if (!url) return true;
    if (url.startsWith('/') || url.startsWith('./') || url.startsWith('../')) return true;
    if (url.startsWith('data:') || url.startsWith('blob:')) return true;
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch {
      return false;
    }
  }

  /** @returns {Array<{id: string, level: number, title: string}>} */
  getOutline() {
    return [...this.headings];
  }

  /**
   * Scroll to a heading by its text.
   *
   * Annotation jump used to call this without it existing, so every Markdown
   * jump silently did nothing. Matching on the collected heading text keeps the
   * anchor structural rather than depending on a DOM selector that a re-render
   * would invalidate.
   *
   * @param {string} title heading text to scroll to
   * @returns {boolean} whether a matching heading was found
   */
  scrollToHeading(title) {
    const wanted = String(title || '').trim();
    if (!wanted) return false;
    const heading =
      this.headings.find((h) => h.title === wanted) ||
      this.headings.find((h) => h.title.includes(wanted) || wanted.includes(h.title));
    if (!heading) return false;
    const node = this.host.querySelector(`#${heading.id}`);
    if (!node) return false;
    node.scrollIntoView({ block: 'start', behavior: 'smooth' });
    this._emit('headingjumped', { id: heading.id, title: heading.title });
    return true;
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
   * Context and block fingerprint are captured because a Markdown anchor must
   * survive re-rendering; a DOM selector alone would break the moment KaTeX or
   * a table changes the node structure (ADR-008). Headings move between
   * revisions and offsets shift with any edit, so a fingerprint of the
   * containing block is recorded alongside the heading path.
   * @returns {{exact: string, prefix: string, suffix: string, headingPath: string[], blockFingerprint: string|null, textPosition: {start:number,end:number}|null}|null}
   */
  getSelection() {
    const sel = window.getSelection();
    const text = sel ? String(sel).trim() : '';
    if (!text) return null;
    if (!this.host.contains(sel.anchorNode)) return null;

    let prefix = '';
    let suffix = '';
    let blockFingerprint = null;
    let textPosition = null;
    try {
      const range = sel.getRangeAt(0);
      const node = range.startContainer;
      const full = node.nodeValue || '';
      prefix = full.slice(Math.max(0, range.startOffset - 48), range.startOffset);
      suffix = full.slice(range.endOffset, range.endOffset + 48);

      const element =
        node.nodeType === 1 ? node : node.parentElement;
      const block = element?.closest('p, li, td, th, blockquote, pre, h1, h2, h3, h4, h5, h6');
      if (block && full) {
        // A cheap content fingerprint of the containing block: it survives
        // re-rendering as long as the block's text is unchanged, which is
        // exactly the condition under which the offsets stay meaningful.
        blockFingerprint = fingerprint(block.textContent || '');
        const before = full.slice(0, range.startOffset);
        const blockStart = (block.textContent || '').indexOf(full.slice(0, 1));
        const offset = blockStart >= 0 ? blockStart : 0;
        textPosition = {
          start: offset + before.length,
          end: offset + before.length + String(sel).length,
        };
      }
    } catch {
      /* context is best effort */
    }

    return {
      exact: text,
      prefix,
      suffix,
      headingPath: this.currentHeadingPath(),
      blockFingerprint,
      textPosition,
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

/**
 * Stable short fingerprint of a text block.
 *
 * Uses length plus a rolling hash so it is cheap and stable across renders; the
 * point is to detect that the block changed, not to be cryptographic.
 */
function fingerprint(text) {
  const value = String(text || '');
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return `md${value.length.toString(36)}-${(hash >>> 0).toString(36)}`;
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
