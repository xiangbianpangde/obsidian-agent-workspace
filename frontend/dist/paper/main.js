/**
 * Paper Workbench entry point.
 *
 * Mounted by the host `index.html` via a single module script tag. Keeping this
 * subsystem in its own ES modules is deliberate: the host page is already a
 * 200 KB single file, and folding a PDF bridge, an annotation layer and a
 * four-pane layout into it would make any change a whole-application
 * regression risk (ADR-009).
 */

import { api } from './api.js';
import { PaperLayout } from './layout.js';
import { PdfBridge, PDFJS_VERSION } from './pdf-bridge.js';
import { MarkdownPane } from './markdown-pane.js';
import { NoteEditor } from './note-pane.js';
import {
  ANCHOR_SCHEMA_VERSION,
  AnnotationList,
  makeMarkdownAnchor,
  makePdfAnchor,
  validateAnchor,
} from './annotations.js';
import { assembleAiContext } from './ai-context.js';
import { WorkspaceStateTracker } from './workspace-state.js';
import { ACTIVITY, TabCoordinator } from './tab-sync.js';

const STATUS_LABELS = {
  UNREAD: '未看',
  READING: '正在看',
  COMPLETED: '看完',
};

const ROLE_LABELS = {
  ORIGINAL_PDF: '原文 PDF',
  SUPPLEMENTAL_PDF: '附加 PDF',
  TRANSLATION_FULL: '全文翻译',
  TRANSLATION_GUIDE: '翻译导读',
  EXTRACTED_MARKDOWN: '解析 Markdown',
  OTHER_MARKDOWN: '其他 Markdown',
};

export class PaperWorkbench {
  constructor(root) {
    this.root = root;
    this.layout = null;
    this.pdf = null;
    this.markdown = null;
    this.note = null;
    this.annotations = null;
    this.papers = [];
    this.selected = null;
    this.sources = [];
    this.activeSourceId = null;
    this.statusFilter = null;
  }

  async init() {
    this.root.innerHTML = SHELL_HTML;

    this.el = {
      paperList: this.root.querySelector('[data-role="paper-list"]'),
      paperCount: this.root.querySelector('[data-role="paper-count"]'),
      statusFilter: this.root.querySelector('[data-role="status-filter"]'),
      title: this.root.querySelector('[data-role="paper-title"]'),
      meta: this.root.querySelector('[data-role="paper-meta"]'),
      sourceTabs: this.root.querySelector('[data-role="source-tabs"]'),
      pdfHost: this.root.querySelector('[data-role="pdf-host"]'),
      markdownHost: this.root.querySelector('[data-role="markdown-host"]'),
      noteHost: this.root.querySelector('[data-role="note-host"]'),
      annHost: this.root.querySelector('[data-role="annotation-host"]'),
      btnLeft: this.root.querySelector('[data-role="toggle-left"]'),
      btnRight: this.root.querySelector('[data-role="toggle-right"]'),
      emptyState: this.root.querySelector('[data-role="empty-state"]'),
      viewerVersion: this.root.querySelector('[data-role="pdfjs-version"]'),
      statusButton: this.root.querySelector('[data-role="status-button"]'),
    };

    this.layout = new PaperLayout(this.root.querySelector('[data-role="grid"]'));
    this.pdf = new PdfBridge(this.el.pdfHost);
    this.markdown = new MarkdownPane(this.el.markdownHost);
    this.note = new NoteEditor(this.el.noteHost, {
      // The closures read this.selected at call time, so they always address
      // the paper that is selected now; the editor pins its own epoch to stop
      // a late response from a previous paper mutating live state.
      load: () => api.getNote(this.selected.paper_id),
      save: (content, hash) => api.saveNote(this.selected.paper_id, content, hash),
      create: (content) => api.createNote(this.selected.paper_id, content),
    });
    this.note.mount();

    this.workspaceState = new WorkspaceStateTracker({
      load: () => api.getWorkspaceState(this.selected.paper_id),
      save: (state, expectedVersion) =>
        api.saveWorkspaceState(
          this.selected.paper_id,
          {
            active_pane: state.active_pane || 'PDF',
            active_pdf_source_id: state.active_pdf_source_id ?? null,
            active_markdown_source_id: state.active_markdown_source_id ?? null,
            source_positions: state.source_positions || {},
            note_cursor_start: state.note_cursor_start ?? null,
            note_cursor_end: state.note_cursor_end ?? null,
            note_content_sha256: state.note_content_sha256 ?? null,
          },
          expectedVersion,
        ),
    });
    this.workspaceState.bind();

    // Cross-tab awareness. The server stays the authority on whether a write
    // is allowed; this only lets a tab learn sooner that another tab has
    // moved the same paper forward.
    this.tabs = new TabCoordinator();
    this.tabs.on(ACTIVITY.NOTE_SAVED, (info) => this._onRemoteNoteSave(info));
    this.tabs.on(ACTIVITY.ANNOTATION_CHANGED, (info) => this._onRemoteAnnotationChange(info));
    this.note.addEventListener('saved', (event) => {
      this.tabs.announce(ACTIVITY.NOTE_SAVED, {
        paperId: this.selected?.paper_id ?? null,
        hash: event.detail?.hash ?? null,
      });
    });
    this.annotations = new AnnotationList(this.el.annHost, {
      list: () => api.listAnnotations(this.selected.paper_id),
      onJump: (item) => this.jumpToAnnotation(item),
    });
    this.annotations.mount();

    this.el.viewerVersion.textContent = `PDF.js ${PDFJS_VERSION}`;
    this.el.btnLeft.addEventListener('click', () => this.layout.toggle('left'));
    this.el.btnRight.addEventListener('click', () => this.layout.toggle('right'));
    this.el.statusFilter.addEventListener('change', () => {
      this.statusFilter = this.el.statusFilter.value || null;
      this.loadPapers();
    });
    this.el.statusButton.addEventListener('click', () => this.advanceStatus());

    // Selection availability drives the annotation button; the panes do not
    // know about the list, so the workbench is the one place that joins them.
    this.annotations.addEventListener('addrequest', () => this.createAnnotationFromSelection());
    document.addEventListener('selectionchange', () => this._refreshSelectionState());
    this.el.markdownHost.addEventListener('mouseup', () => this._refreshSelectionState());

    // Position capture. Scroll and page events only touch memory; the tracker
    // owns the debounce and the flush policy.
    this.pdf.onPageChanged(({ pageIndex }) => {
      const source = this.sources.find((s) => s.source_id === this.activeSourceId);
      if (!source) return;
      this.workspaceState.notePdfPosition(source.source_id, {
        pageIndex,
        offsetRatio: 0,
        sourceVersion: source.source_version,
      });
    });
    this.el.markdownHost.addEventListener('scroll', () => this._captureMarkdownPosition());
    this.pdf.container.addEventListener('paperpdfscroll', () => this._capturePdfScroll());

    this.root.addEventListener('layoutresize', () => {
      if (this.pdf?.iframe) this.pdf.iframe.style.height = '100%';
    });

    await this.loadPapers();
    this._selectFirst();
  }

  async loadPapers() {
    try {
      const opts = {};
      if (this.statusFilter === 'AMBIGUOUS') {
        opts.bindingState = 'AMBIGUOUS';
      } else if (this.statusFilter) {
        opts.status = this.statusFilter;
      }
      const data = await api.listPapers(opts);
      this.papers = data.papers || [];
      this._renderPaperList();
    } catch (error) {
      this.el.paperList.innerHTML = `<p class="paper-error">无法加载论文列表：${escapeHtml(error.message)}</p>`;
    }
  }

  _renderPaperList() {
    this.el.paperCount.textContent = String(this.papers.length);
    if (!this.papers.length) {
      this.el.paperList.innerHTML = '<p class="paper-muted">没有符合条件的论文</p>';
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const paper of this.papers) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'paper-item';
      button.dataset.paperId = paper.paper_id;
      if (this.selected && this.selected.paper_id === paper.paper_id) {
        button.classList.add('is-active');
      }
      const isAmbiguous = paper.binding_state === 'AMBIGUOUS';
      const statusLabel = isAmbiguous
        ? '待确认'
        : (STATUS_LABELS[paper.status] || paper.status);
      const statusAttr = isAmbiguous ? 'AMBIGUOUS' : paper.status;

      button.innerHTML = `
        <span class="paper-item-title">${escapeHtml(paper.title || paper.display_title || '未命名')}</span>
        <span class="paper-item-meta">
          <span class="paper-status" data-status="${escapeHtml(statusAttr)}">${escapeHtml(statusLabel)}</span>
          ${paper.source_count ? `<span class="paper-badge">${paper.source_count} 来源</span>` : ''}
        </span>`;
      button.addEventListener('click', () => this.selectPaper(paper.paper_id));
      fragment.appendChild(button);
    }
    this.el.paperList.replaceChildren(fragment);
  }

  _selectFirst() {
    if (this.papers.length) this.selectPaper(this.papers[0].paper_id);
  }

  async selectPaper(paperId) {
    // Persist the outgoing paper's note and reading position before switching.
    await Promise.all([this.note.flush(), this.workspaceState.flush()]);
    try {
      const [paper, sources] = await Promise.all([
        api.getPaper(paperId),
        api.listSources(paperId),
      ]);
      this.selected = paper;
      this.sources = sources.sources || [];
      this.activeSourceId = null;

      this.el.emptyState.hidden = true;
      this.el.title.textContent = paper.title || paper.display_title || '未命名';
      this._renderMeta();
      this._renderPaperList();
      this._renderStatusButton();

      if (paper.binding_state === 'AMBIGUOUS') {
        this._renderAmbiguityResolutionPanel();
        return;
      }

      if (this._resolutionHost) {
        this._resolutionHost.style.display = 'none';
      }

      this._renderSources();

      // Mark READING only once a readable source actually loads, never on a
      // mere list click (ADR-007).
      const primary = this.sources.find((s) => s.media_kind === 'PDF');
      if (primary) {
        await this.openSource(primary.source_id);
        await this.markOpened();
      } else {
        const md = this.sources.find((s) => s.media_kind === 'MARKDOWN');
        if (md) {
          await this.openSource(md.source_id);
          await this.markOpened();
        }
      }

      await Promise.all([this.note.loadFor(paperId), this.workspaceState.loadFor(paperId)]);
      await this._restorePosition();
    } catch (error) {
      this.el.title.textContent = '加载失败';
      this.el.meta.textContent = error.message;
    }
  }

  _captureMarkdownPosition() {
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    if (!source || source.media_kind !== 'MARKDOWN') return;
    this.workspaceState.noteMarkdownPosition(source.source_id, {
      headingPath: this.markdown.currentHeadingPath(),
      scrollRatio: this.markdown.getScrollRatio(),
      sourceVersion: source.source_version,
    });
  }

  _capturePdfScroll() {
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    if (!source || source.media_kind !== 'PDF') return;
    this.workspaceState.notePdfPosition(source.source_id, {
      pageIndex: this.pdf.getCurrentPage(),
      offsetRatio: 0,
      sourceVersion: source.source_version,
    });
  }

  /**
   * Restore the recorded position for the active source.
   *
   * When the source revision changed, only the coarse position (page or
   * heading) is restored: the fine offset pointed into different bytes.
   */
  async _restorePosition() {
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    if (!source) return;
    const kind = source.media_kind === 'PDF' ? 'PDF' : 'MARKDOWN';
    const target = this.workspaceState.resolveRestore(
      source.source_id,
      source.source_version,
      kind
    );
    if (target.restored === 'none') return;

    if (kind === 'PDF' && Number.isFinite(target.pageIndex)) {
      this.pdf.goToPage(target.pageIndex);
    } else if (kind === 'MARKDOWN' && Number.isFinite(target.scrollRatio)) {
      this.markdown.scrollToRatio(target.scrollRatio);
    }
    this._emitRestoreNotice(target.restored);
  }

  _emitRestoreNotice(restored) {
    if (restored !== 'coarse') return;
    this.el.meta.dataset.restore = 'coarse';
    this.el.meta.title = '来源已更新，仅恢复到页/章节级位置';
  }

  _renderMeta() {
    const paper = this.selected;
    if (!paper) return;
    const isAmbiguous = paper.binding_state === 'AMBIGUOUS';
    const statusText = isAmbiguous
      ? '待人工确认'
      : (STATUS_LABELS[paper.status] || paper.status);
    const parts = [
      statusText,
      paper.category_relpath,
      `${this.sources.length} 个${isAmbiguous ? '候选' : ''}来源`,
    ].filter(Boolean);
    this.el.meta.textContent = parts.join(' · ');
  }

  _renderStatusButton() {
    const paper = this.selected;
    if (!paper || !this.el.statusButton) return;
    if (paper.binding_state === 'AMBIGUOUS') {
      this.el.statusButton.hidden = true;
      return;
    }
    this.el.statusButton.hidden = false;
    const next = { UNREAD: 'READING', READING: 'COMPLETED', COMPLETED: 'READING' }[paper.status];
    const nextLabel = { UNREAD: '开始阅读', READING: '标记看完', COMPLETED: '重新阅读' }[paper.status];
    this.el.statusButton.textContent = nextLabel;
    this.el.statusButton.dataset.next = next;
  }

  _renderAmbiguityResolutionPanel() {
    this.el.sourceTabs.innerHTML = '';
    if (this.pdf && typeof this.pdf.hide === 'function') {
      this.pdf.hide();
    }
    if (this.el.statusButton) this.el.statusButton.hidden = true;
    if (this.note) {
      if (typeof this.note.clear === 'function') {
        this.note.clear('待确认论文暂无笔记');
      } else if (typeof this.note._setText === 'function') {
        this.note._setText('');
        if (typeof this.note._setState === 'function') {
          this.note._setState('待确认论文暂无笔记');
        }
      }
    }
    this.el.markdownHost.innerHTML = '<p class="paper-muted" style="padding:24px;">当前论文待确认来源绑定。请在左侧面板选择角色与首要主文件并确认采纳。</p>';

    let reasonDesc = '该论文夹存在多个候选来源或未检测到 PDF 原文，请确认文件角色和主文件。';
    if (this.selected.ambiguity_reason === 'NO_PDF_MARKDOWN_ONLY') {
      reasonDesc = '未在该论文夹中检测到 PDF 原文。系统共发现了以下候选 Markdown 文档，请确认各文档的角色与首要主阅读文件以完成采纳。';
    } else if (this.selected.ambiguity_reason === 'MULTIPLE_PDFS') {
      reasonDesc = '该论文夹中包含多个候选 PDF 原文。请指定首要阅读的原文 PDF，其余文件可作为附加材料或忽略。';
    }

    const sources = this.sources || [];
    const panelHtml = `
      <div class="paper-resolution-panel">
        <div class="paper-resolution-banner">
          <span class="paper-resolution-badge">待人工确认</span>
          <h3>来源绑定消歧与采纳</h3>
          <p class="paper-resolution-desc">${escapeHtml(reasonDesc)}</p>
        </div>

        <form class="paper-resolution-form" id="resolution-form">
          <table class="paper-resolution-table">
            <thead>
              <tr>
                <th style="width: 48px; text-align: center;">主文件</th>
                <th style="min-width: 120px;">候选文件名</th>
                <th style="width: 120px;">分配角色</th>
                <th style="width: 50px; text-align: right;">大小</th>
              </tr>
            </thead>
            <tbody>
              ${sources.map((s, idx) => {
                const isPdf = s.media_kind === 'PDF';
                return `
                <tr>
                  <td style="text-align: center;">
                    <input type="radio" name="primary_choice" value="${escapeHtml(s.rel_path)}" ${idx === 0 ? 'checked' : ''} />
                  </td>
                  <td>
                    <span class="paper-resolution-filename">${escapeHtml(s.rel_path)}</span>
                  </td>
                  <td>
                    <select class="paper-select paper-resolution-role" data-relpath="${escapeHtml(s.rel_path)}">
                      ${isPdf ? `
                        <option value="ORIGINAL_PDF" ${s.role === 'ORIGINAL_PDF' ? 'selected' : ''}>原文 PDF</option>
                        <option value="SUPPLEMENTAL_PDF" ${s.role === 'SUPPLEMENTAL_PDF' ? 'selected' : ''}>附加 PDF</option>
                      ` : `
                        <option value="TRANSLATION_FULL" ${s.role === 'TRANSLATION_FULL' || idx === 0 ? 'selected' : ''}>全文翻译</option>
                        <option value="TRANSLATION_GUIDE" ${s.role === 'TRANSLATION_GUIDE' ? 'selected' : ''}>翻译导读</option>
                        <option value="EXTRACTED_MARKDOWN" ${s.role === 'EXTRACTED_MARKDOWN' ? 'selected' : ''}>解析 Markdown</option>
                        <option value="OTHER_MARKDOWN" ${s.role === 'OTHER_MARKDOWN' ? 'selected' : ''}>其他 Markdown</option>
                      `}
                      <option value="IGNORE">忽略（不绑定）</option>
                    </select>
                  </td>
                  <td class="paper-resolution-size" style="text-align: right;">
                    ${s.size_bytes ? `${Math.round(s.size_bytes / 1024)} KB` : '-'}
                  </td>
                </tr>
                `;
              }).join('')}
            </tbody>
          </table>

          <div class="paper-resolution-actions">
            <button type="submit" class="paper-resolve-submit-btn">确认绑定并采纳论文</button>
            <span class="paper-resolution-msg"></span>
          </div>
        </form>
      </div>
    `;

    if (!this._resolutionHost) {
      this._resolutionHost = document.createElement('div');
      this._resolutionHost.className = 'paper-resolution-host';
      this.el.pdfHost.appendChild(this._resolutionHost);
    }
    this._resolutionHost.style.display = 'block';
    this._resolutionHost.innerHTML = panelHtml;

    const form = this._resolutionHost.querySelector('#resolution-form');
    const msgEl = this._resolutionHost.querySelector('.paper-resolution-msg');
    const submitBtn = this._resolutionHost.querySelector('.paper-resolve-submit-btn');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      msgEl.className = 'paper-resolution-msg';
      msgEl.textContent = '';
      submitBtn.disabled = true;

      const checkedRadio = form.querySelector('input[name="primary_choice"]:checked');
      const primaryPath = checkedRadio ? checkedRadio.value : null;

      const roleSelects = form.querySelectorAll('.paper-resolution-role');
      const payloadSources = [];

      for (const sel of roleSelects) {
        const relPath = sel.dataset.relpath;
        const role = sel.value;
        if (role === 'IGNORE') continue;

        payloadSources.push({
          rel_path: relPath,
          role: role,
          is_primary: relPath === primaryPath,
          active: true,
        });
      }

      if (!payloadSources.length) {
        msgEl.className = 'paper-resolution-msg is-error';
        msgEl.textContent = '至少需要绑定一个文件！';
        submitBtn.disabled = false;
        return;
      }

      if (!payloadSources.some((s) => s.is_primary)) {
        payloadSources[0].is_primary = true;
      }

      try {
        msgEl.textContent = '正在采纳并固化 Manifest...';
        await api.resolvePaper(this.selected.paper_id, { sources: payloadSources });
        msgEl.className = 'paper-resolution-msg is-ok';
        msgEl.textContent = '采纳成功！';
        if (this._resolutionHost) this._resolutionHost.style.display = 'none';
        await this.loadPapers();
        await this.selectPaper(this.selected.paper_id);
      } catch (err) {
        msgEl.className = 'paper-resolution-msg is-error';
        msgEl.textContent = `采纳失败: ${err.message}`;
        submitBtn.disabled = false;
      }
    });
  }

  /** UNREAD -> READING fires on a successful source load, not on a list click. */
  async markOpened() {
    if (!this.selected) return;
    if (this.selected.status === 'COMPLETED') {
      // Opening a finished paper must not downgrade it.
      return;
    }
    if (this.selected.status !== 'UNREAD') return;
    try {
      await api.setStatus(this.selected.paper_id, 'READING', 'first_load');
      this.selected.status = 'READING';
      this._renderMeta();
      this._renderStatusButton();
      this._renderPaperList();
    } catch (error) {
      console.warn('状态更新失败:', error.message);
    }
  }

  /** COMPLETED only ever comes from an explicit user action. */
  async advanceStatus() {
    if (!this.selected) return;
    const next = this.el.statusButton.dataset.next;
    if (!next) return;
    try {
      const result = await api.setStatus(this.selected.paper_id, next, 'user_action');
      this.selected.status = result.status;
      this._renderMeta();
      this._renderStatusButton();
      this._renderPaperList();
    } catch (error) {
      this.el.meta.textContent = `状态更新失败：${error.message}`;
    }
  }

  /**
   * Another tab saved the same note.
   *
   * If this tab has no unsaved work, quietly adopt the newer hash so the next
   * save succeeds. If it does have a draft, warn instead of reloading — the
   * user's in-progress text must never be replaced behind their back.
   */
  _onRemoteNoteSave(info) {
    if (!this.selected || info.paperId !== this.selected.paper_id) return;
    if (!this.note) return;
    if (this.note.hasUnsavedWork()) {
      this.el.meta.textContent = '另一标签页保存了此笔记，你的草稿仍保留';
      this.el.meta.dataset.remoteEdit = 'true';
      return;
    }
    this.note.loadFor(this.selected.paper_id);
    this.el.meta.textContent = '已同步另一标签页的改动';
  }

  /** Another tab changed annotations for this paper. */
  _onRemoteAnnotationChange(info) {
    if (!this.selected || info.paperId !== this.selected.paper_id) return;
    this.annotations.load();
  }

  _refreshSelectionState() {
    const available = !!(this.pdf?.getSelection() || this.markdown?.getSelection());
    this.annotations?.setSelectionAvailable(available);
  }

  /** Record an annotation from whatever the user currently has selected. */
  async createAnnotationFromSelection() {
    if (!this.selected) return;
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    if (!source) return;

    const isPdf = source.media_kind === 'PDF';
    const selection = isPdf ? this.pdf.getSelection() : this.markdown.getSelection();
    if (!selection?.exact) {
      this.el.meta.textContent = '请先选中一段文本再添加标注';
      return;
    }

    const anchor = isPdf
      ? makePdfAnchor({
          pageIndex: selection.pageIndex,
          selectedText: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
          selectionRect: selection.selectionRect,
          pageWidth: selection.pageWidth,
          pageHeight: selection.pageHeight,
        })
      : makeMarkdownAnchor({
          headingPath: selection.headingPath,
          selectedText: selection.exact,
          prefix: selection.prefix,
          suffix: selection.suffix,
          blockFingerprint: selection.blockFingerprint,
          textPosition: selection.textPosition,
        });

    // A resolvable locator is mandatory from anchor schema version 2: an
    // anchor that validates but cannot be re-resolved is worse than none.
    const check = validateAnchor(anchor);
    if (!check.ok) {
      this.el.meta.textContent = `无法生成可用锚点：${check.reason}`;
      return;
    }
    anchor.schema_version = ANCHOR_SCHEMA_VERSION;

    try {
      await api.createAnnotation(this.selected.paper_id, {
        source_id: source.source_id,
        kind: 'HIGHLIGHT',
        anchor,
        selected_text: selection.exact,
        source_version: source.source_version,
      });
      await this.annotations.load();
      this._refreshSelectionState();
      this.tabs.announce(ACTIVITY.ANNOTATION_CHANGED, {
        paperId: this.selected.paper_id,
      });
    } catch (error) {
      this.el.meta.textContent = `标注保存失败：${error.message}`;
    }
  }

  /** Jump to an annotation. P0 supports this without repainting highlights. */
  jumpToAnnotation(item) {
    if (item.anchor_type === 'PDF_TEXT') {
      const page = Number(item.page_index);
      if (Number.isFinite(page)) this.pdf.goToPage(page);
      return;
    }
    if (item.anchor_type === 'MARKDOWN_TEXT') {
      const path = item.heading_path;
      const target = Array.isArray(path) && path.length ? path[path.length - 1] : null;
      if (!target) return;
      const jumped = this.markdown.scrollToHeading(target);
      if (!jumped) {
        this.el.meta.textContent = `未找到对应章节：${target}`;
      }
    }
  }

  /**
   * Build the P1 context envelope locally.
   *
   * Exposed on the instance so the contract can be exercised now; nothing here
   * calls a model or touches the network (ADR-009).
   */
  buildAiContext() {
    if (!this.selected) return null;
    const source = this.sources.find((s) => s.source_id === this.activeSourceId);
    const pdfSelection = this.pdf.getSelection();
    const mdSelection = this.markdown.getSelection();
    const selection = pdfSelection || mdSelection || null;

    return assembleAiContext({
      paper: this.selected,
      focus: source
        ? {
            pane: source.media_kind === 'PDF' ? 'PDF' : 'MARKDOWN',
            sourceId: source.source_id,
            sourceVersion: source.source_version,
            locator:
              source.media_kind === 'PDF'
                ? { page_index: this.pdf.getCurrentPage() }
                : { heading_path: this.markdown.currentHeadingPath() },
            selection,
          }
        : null,
      sourceRefs: this.sources.map((s) => ({
        sourceId: s.source_id,
        role: s.role,
        sha256: s.sha256,
        sourceVersion: s.source_version,
      })),
      note: this.selected.note_id
        ? { noteId: this.selected.note_id, sha256: null, content: this.note.getText() }
        : null,
    });
  }

  _renderSources() {
    const fragment = document.createDocumentFragment();
    for (const source of this.sources) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.className = 'paper-source-tab';
      tab.dataset.sourceId = source.source_id;
      if (source.source_id === this.activeSourceId) tab.classList.add('is-active');
      tab.textContent = ROLE_LABELS[source.role] || source.role;
      tab.title = source.rel_path;
      tab.addEventListener('click', () => this.openSource(source.source_id));
      fragment.appendChild(tab);
    }
    this.el.sourceTabs.replaceChildren(fragment);
  }

  async openSource(sourceId) {
    const source = this.sources.find((s) => s.source_id === sourceId);
    if (!source) return;
    // Persist where we were before moving to a different source.
    await this.workspaceState.flush();
    this.activeSourceId = sourceId;
    this._renderSources();
    this.workspaceState.noteActive({
      pane: source.media_kind === 'PDF' ? 'PDF' : 'MARKDOWN',
      pdfSourceId: source.media_kind === 'PDF' ? sourceId : undefined,
      markdownSourceId: source.media_kind === 'MARKDOWN' ? sourceId : undefined,
    });

    if (source.media_kind === 'PDF') {
      const url = api.sourceContentUrl(sourceId, source.source_version);
      await this.pdf.open(url, {
        sourceId,
        sourceVersion: source.source_version,
      });
      this.el.markdownHost.innerHTML =
        '<p class="paper-muted">切换到翻译来源以查看对照阅读</p>';
    } else {
      try {
        const text = await api.sourceText(sourceId);
        this.markdown.setContent(text, { sourceId });
      } catch (error) {
        this.el.markdownHost.innerHTML = `<p class="paper-error">译文加载失败：${escapeHtml(error.message)}</p>`;
      }
    }
    this._refreshSelectionState();
  }
}

const SHELL_HTML = `
<div class="paper-workbench">
  <header class="paper-toolbar">
    <button type="button" class="paper-icon-btn" data-role="toggle-left" title="折叠/展开论文目录" aria-label="折叠论文目录">☰</button>
    <span class="paper-toolbar-title">论文工作台</span>
    <select class="paper-select" data-role="status-filter" aria-label="按阅读状态筛选">
      <option value="">全部状态</option>
      <option value="UNREAD">未看</option>
      <option value="READING">正在看</option>
      <option value="COMPLETED">看完</option>
      <option value="AMBIGUOUS">待确认</option>
    </select>
    <span class="paper-spacer"></span>
    <button type="button" class="paper-status-btn" data-role="status-button">开始阅读</button>
    <span class="paper-version" data-role="pdfjs-version"></span>
    <button type="button" class="paper-icon-btn" data-role="toggle-right" title="折叠/展开笔记栏" aria-label="折叠笔记栏">≡</button>
  </header>

  <div class="paper-grid" data-role="grid">
    <aside class="paper-pane paper-sidebar">
      <div class="paper-pane-head">
        <span>论文目录</span>
        <span class="paper-count" data-role="paper-count">0</span>
      </div>
      <div class="paper-pane-body" data-role="paper-list"></div>
    </aside>

    <div class="paper-splitter" data-splitter="left" role="separator" tabindex="0" aria-label="调整目录宽度"></div>

    <section class="paper-pane paper-reading">
      <div class="paper-pane-head">
        <span data-role="paper-title">未选择论文</span>
        <span class="paper-paper-meta" data-role="paper-meta"></span>
      </div>
      <div class="paper-tabs" data-role="source-tabs"></div>
      <div class="paper-pane-body paper-pdf-host" data-role="pdf-host"></div>
    </section>

    <div class="paper-splitter" data-splitter="center" role="separator" tabindex="0" aria-label="调整原文与翻译比例"></div>

    <section class="paper-pane paper-translation">
      <div class="paper-pane-head"><span>翻译对照</span></div>
      <div class="paper-pane-body" data-role="markdown-host">
        <p class="paper-muted">选择一篇论文以开始阅读</p>
      </div>
    </section>

    <div class="paper-splitter" data-splitter="right" role="separator" tabindex="0" aria-label="调整笔记栏宽度"></div>

    <aside class="paper-pane paper-notes">
      <div class="paper-pane-head"><span>阅读笔记</span></div>
      <div class="paper-pane-body paper-note-host" data-role="note-host"></div>
      <div class="paper-pane-head paper-ann-head"><span>标注</span></div>
      <div class="paper-pane-body paper-ann-host" data-role="annotation-host"></div>
    </aside>
  </div>

  <div class="paper-empty" data-role="empty-state">
    <p>从左侧选择一篇论文开始阅读</p>
  </div>
</div>`;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[ch]);
}

/** Boot when the host page provides a mount point. */
export function mountPaperWorkbench(elementId = 'workspace-paper') {
  const root = document.getElementById(elementId);
  if (!root) return null;
  const workbench = new PaperWorkbench(root);
  workbench.init().catch((error) => {
    console.error('论文工作台初始化失败:', error);
    root.innerHTML = `<p class="paper-error">论文工作台初始化失败：${escapeHtml(error.message)}</p>`;
  });
  return workbench;
}

window.PaperWorkbench = { mountPaperWorkbench, PaperWorkbench, PdfBridge, PDFJS_VERSION };
