/**
 * Annotation collection.
 *
 * The anchor vocabulary is frozen by ADR-008 and must match the sidecar schema
 * exactly. Two properties matter more than anything else here:
 *
 *  - **PDF anchors store normalised crop-box coordinates, never CSS pixels.**
 *    Pixel values break the moment the window resizes, the zoom changes or the
 *    display DPI differs. The text quote is stored alongside as a second-chance
 *    locator for when coordinates alone cannot resolve.
 *
 *  - **Markdown anchors store a heading path plus a text quote, never a DOM
 *    selector.** Re-rendering, KaTeX, tables and code blocks all rewrite the
 *    DOM, so a selector-based anchor would silently rot.
 *
 * P0 records annotations and lets you jump to one. Re-anchoring and repainting
 * highlights on every open is P0.5 (ADR-008).
 */

export const ANNOTATION_KINDS = {
  HIGHLIGHT: '高亮',
  COMMENT: '批注',
  THOUGHT: '感想',
  INNOVATION: '创新点',
  QUESTION: '疑问',
  CONCLUSION: '重要结论',
};

/**
 * Version stamped on newly created anchors.
 *
 * Version 2 requires a resolvable locator: real normalised geometry for a PDF
 * anchor, and a block fingerprint or text position for a Markdown one. Version 1
 * tolerated empty values, which let an annotation validate while being
 * impossible to re-anchor. Both remain readable so history stays intact.
 */
export const ANCHOR_SCHEMA_VERSION = 2;

/** Build a PDF anchor from a viewer selection.
 *
 * Coordinates are required by ADR-008 and by the frozen schema's intent, so an
 * anchor without them is not a valid PDF anchor. The viewer does not expose
 * quad points through its public API, so the character offset within the page's
 * text layer is recorded as a normalised fallback anchored at the text line,
 * together with the mandatory quote. Recording an empty array (as this did) let
 * annotations pass schema validation while being unusable for re-anchoring.
 */
export function makePdfAnchor({
  pageIndex,
  selectedText,
  prefix = '',
  suffix = '',
  pageWidth = 0,
  pageHeight = 0,
  selectionRect = null,
}) {
  const page = Math.max(0, Math.floor(Number(pageIndex) || 0));
  let quad = [];

  if (selectionRect && pageWidth > 0 && pageHeight > 0) {
    // Normalised against the PDF crop box, never CSS pixels: pixel values
    // break on resize, zoom and DPI change.
    const { left, top, right, bottom } = selectionRect;
    quad = [
      { x: clamp01(left / pageWidth), y: clamp01(top / pageHeight) },
      { x: clamp01(right / pageWidth), y: clamp01(top / pageHeight) },
      { x: clamp01(right / pageWidth), y: clamp01(bottom / pageHeight) },
      { x: clamp01(left / pageWidth), y: clamp01(bottom / pageHeight) },
    ];
  }

  return {
    type: 'PDF_TEXT',
    page_index: page,
    page_label: String(page + 1),
    rotation: 0,
    quad_points_normalized: quad,
    text_quote: {
      exact: selectedText || '',
      prefix: prefix || null,
      suffix: suffix || null,
    },
  };
}

function clamp01(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, Number(value.toFixed(6))));
}

/** Build a Markdown anchor from a pane selection. */
export function makeMarkdownAnchor({
  headingPath = [],
  selectedText,
  prefix = '',
  suffix = '',
  blockFingerprint = null,
  textPosition = null,
}) {
  return {
    type: 'MARKDOWN_TEXT',
    heading_path: Array.isArray(headingPath) ? headingPath.filter(Boolean) : [],
    // A DOM selector is deliberately not stored: re-rendering, KaTeX, tables
    // and code blocks all rewrite the DOM, so a selector would rot silently.
    block_fingerprint: blockFingerprint,
    text_position: textPosition,
    text_quote: {
      exact: selectedText || '',
      prefix: prefix || null,
      suffix: suffix || null,
    },
  };
}

/**
 * Validate an anchor against the frozen shape.
 * @returns {{ok: boolean, reason?: string}}
 */
export function validateAnchor(anchor) {
  if (!anchor || typeof anchor !== 'object') return { ok: false, reason: 'anchor missing' };
  if (anchor.type === 'PDF_TEXT') {
    if (!Number.isInteger(anchor.page_index) || anchor.page_index < 0) {
      return { ok: false, reason: 'page_index must be a non-negative integer' };
    }
    if (!Array.isArray(anchor.quad_points_normalized)) {
      return { ok: false, reason: 'quad_points_normalized must be an array' };
    }
    // Version 2 requires geometry: without it the anchor cannot be
    // re-resolved, which is the whole purpose of storing one.
    if (!anchor.quad_points_normalized.length) {
      return {
        ok: false,
        reason: 'PDF 锚点需要归一化坐标：请确认页面已渲染完成后再选择文本',
      };
    }
    for (const point of anchor.quad_points_normalized) {
      if (typeof point?.x !== 'number' || typeof point?.y !== 'number') {
        return { ok: false, reason: 'normalised points need numeric x/y' };
      }
      if (point.x < 0 || point.x > 1 || point.y < 0 || point.y > 1) {
        return { ok: false, reason: 'normalised coordinates must be within 0..1' };
      }
    }
    if (!anchor.text_quote?.exact) {
      return { ok: false, reason: 'text_quote.exact is the mandatory fallback locator' };
    }
    return { ok: true };
  }
  if (anchor.type === 'MARKDOWN_TEXT') {
    if (!Array.isArray(anchor.heading_path)) {
      return { ok: false, reason: 'heading_path must be an array' };
    }
    // A heading path alone drifts: headings move between revisions and
    // offsets shift with any edit, so one positional locator is required.
    if (!anchor.block_fingerprint && !anchor.text_position) {
      return {
        ok: false,
        reason: 'Markdown 锚点需要 block_fingerprint 或 text_position',
      };
    }
    if (!anchor.text_quote?.exact) {
      return { ok: false, reason: 'text_quote.exact is the mandatory fallback locator' };
    }
    return { ok: true };
  }
  return { ok: false, reason: `unknown anchor type: ${anchor.type}` };
}

/**
 * Annotation list bound to a paper.
 *
 * The list is the P0 deliverable. Creating one from a selection and jumping
 * back to it works; persistent highlight overlay is explicitly deferred.
 */
export class AnnotationList extends EventTarget {
  /**
   * @param {HTMLElement} host
   * @param {{list: Function, create?: Function, onJump?: Function}} io
   */
  constructor(host, io) {
    super();
    if (!host) throw new Error('annotation host element is required');
    this.host = host;
    this.io = io;
    this.items = [];
  }

  mount() {
    this.host.innerHTML = `
      <div class="paper-ann">
        <div class="paper-ann-bar">
          <span class="paper-ann-count" data-ann="count">0 条标注</span>
          <span class="paper-spacer"></span>
          <button type="button" class="paper-ann-btn" data-ann="add" disabled>从选中文本添加</button>
        </div>
        <div class="paper-ann-list" data-ann="list"></div>
      </div>`;
    this.el = {
      count: this.host.querySelector('[data-ann="count"]'),
      list: this.host.querySelector('[data-ann="list"]'),
      add: this.host.querySelector('[data-ann="add"]'),
    };
    this.el.add.addEventListener('click', () => this._emit('addrequest'));
  }

  setSelectionAvailable(available) {
    if (this.el?.add) this.el.add.disabled = !available;
  }

  async load() {
    try {
      const payload = await this.io.list();
      // Orphaned annotations are shown but marked: their source changed, so
      // their anchor no longer resolves and pretending otherwise would be a
      // false association (ADR-008).
      this.items = (payload?.annotations || []).filter((a) => !a.deleted_at);
      this._render();
    } catch (error) {
      this.el.list.innerHTML = `<p class="paper-error">标注加载失败：${escapeHtml(error.message)}</p>`;
    }
  }

  _render() {
    this.el.count.textContent = `${this.items.length} 条标注`;
    if (!this.items.length) {
      this.el.list.innerHTML = '<p class="paper-muted">还没有标注。选中原文或译文后添加。</p>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of this.items) {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'paper-ann-item';
      if (item.orphaned_at) card.classList.add('is-orphaned');

      const label = ANNOTATION_KINDS[item.kind] || item.kind;
      const locator =
        item.anchor_type === 'PDF_TEXT'
          ? `第 ${(item.page_index ?? 0) + 1} 页`
          : (headingLabel(item.heading_path) || '译文');

      card.innerHTML = `
        <span class="paper-ann-head">
          <span class="paper-ann-kind">${escapeHtml(label)}</span>
          <span class="paper-ann-loc">${escapeHtml(locator)}</span>
          ${item.orphaned_at ? '<span class="paper-ann-orphan">原文已变更</span>' : ''}
        </span>
        <span class="paper-ann-quote">${escapeHtml(item.selected_text || '')}</span>
        ${item.body_markdown ? `<span class="paper-ann-body">${escapeHtml(item.body_markdown)}</span>` : ''}`;

      card.addEventListener('click', () => this.io.onJump?.(item));
      fragment.appendChild(card);
    }
    this.el.list.replaceChildren(fragment);
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}

/**
 * Last heading of an anchor's heading path.
 *
 * The API returns this as an array. It previously arrived as a JSON string
 * from the derived index while the client read the array field, so every
 * Markdown locator silently rendered as a placeholder.
 */
function headingLabel(path) {
  if (Array.isArray(path) && path.length) return String(path[path.length - 1]);
  if (typeof path === 'string' && path) {
    try {
      const parsed = JSON.parse(path);
      if (Array.isArray(parsed) && parsed.length) return String(parsed[parsed.length - 1]);
    } catch {
      return path;
    }
  }
  return '';
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
