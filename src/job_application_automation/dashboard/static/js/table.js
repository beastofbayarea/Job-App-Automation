/**
 * A small sortable/filterable table controller.
 *
 * Every data table previously re-implemented its own filter loop and had no
 * sorting at all, so rows appeared in whatever order the JSON happened to use
 * — for the submission log that meant alphabetical-by-key, not newest first.
 */

import { escapeHtml } from './format.js';

/** Replace the table body with a single full-width message row. */
export function setTableMessage(tbody, columnCount, message, tone = 'muted') {
  if (!tbody) return;
  tbody.innerHTML = `
    <tr>
      <td colspan="${columnCount}" class="table-message table-message-${escapeHtml(tone)}">
        ${escapeHtml(message)}
      </td>
    </tr>
  `;
}

/** Render skeleton rows so layout is stable while the first fetch is in flight. */
export function setTableLoading(tbody, columnCount, rows = 5) {
  if (!tbody) return;
  const cells = `<td><span class="skeleton skeleton-text"></span></td>`.repeat(columnCount);
  tbody.innerHTML = `<tr class="skeleton-row" aria-hidden="true">${cells}</tr>`.repeat(rows);
}

export class DataTable {
  /**
   * @param {object} config
   * @param {HTMLTableElement} config.table
   * @param {Array<{key:string,label:string,sortable?:boolean,sortValue?:Function,render:Function,className?:string}>} config.columns
   * @param {string} [config.initialSort]      Column key to sort by on first render.
   * @param {boolean} [config.initialDesc]     Sort descending first.
   * @param {Function} [config.searchText]     row => string searched by the filter box.
   * @param {string} [config.emptyMessage]
   * @param {number} [config.pageSize]         Rows rendered before "show more".
   */
  constructor(config) {
    this.table = config.table;
    this.columns = config.columns;
    this.searchText = config.searchText || (() => '');
    this.emptyMessage = config.emptyMessage || 'No records to display.';
    this.pageSize = config.pageSize || 100;
    this.sortKey = config.initialSort || null;
    this.sortDesc = Boolean(config.initialDesc);
    this.query = '';
    this.rows = [];
    this.limit = this.pageSize;

    this.thead = this.table.querySelector('thead');
    this.tbody = this.table.querySelector('tbody');
    this.renderHead();
  }

  renderHead() {
    if (!this.thead) return;
    this.thead.innerHTML = `<tr>${this.columns
      .map((col) => {
        const sortable = col.sortable !== false;
        const isSorted = this.sortKey === col.key;
        const ariaSort = isSorted ? (this.sortDesc ? 'descending' : 'ascending') : 'none';
        const inner = sortable
          ? `<button type="button" class="th-sort" data-sort-key="${escapeHtml(col.key)}">
               ${escapeHtml(col.label)}
               <span class="sort-arrow" aria-hidden="true">${isSorted ? (this.sortDesc ? '▼' : '▲') : '↕'}</span>
             </button>`
          : escapeHtml(col.label);
        return `<th scope="col" aria-sort="${ariaSort}"${col.className ? ` class="${escapeHtml(col.className)}"` : ''}>${inner}</th>`;
      })
      .join('')}</tr>`;

    this.thead.querySelectorAll('[data-sort-key]').forEach((btn) => {
      btn.addEventListener('click', () => this.toggleSort(btn.dataset.sortKey));
    });
  }

  toggleSort(key) {
    if (this.sortKey === key) {
      this.sortDesc = !this.sortDesc;
    } else {
      this.sortKey = key;
      this.sortDesc = false;
    }
    this.renderHead();
    this.render();
  }

  setRows(rows) {
    this.rows = Array.isArray(rows) ? rows : [];
    this.limit = this.pageSize;
    this.render();
  }

  setQuery(query) {
    this.query = String(query || '').trim().toLowerCase();
    this.limit = this.pageSize;
    this.render();
  }

  showMore() {
    this.limit += this.pageSize;
    this.render();
  }

  visibleRows() {
    let rows = this.rows;
    if (this.query) {
      rows = rows.filter((row) => this.searchText(row).toLowerCase().includes(this.query));
    }
    const column = this.columns.find((col) => col.key === this.sortKey);
    if (column) {
      const accessor = column.sortValue || ((row) => row[column.key]);
      rows = [...rows].sort((a, b) => {
        const av = accessor(a);
        const bv = accessor(b);
        if (av === bv) return 0;
        if (av === null || av === undefined) return 1;
        if (bv === null || bv === undefined) return -1;
        const result = typeof av === 'number' && typeof bv === 'number'
          ? av - bv
          : String(av).localeCompare(String(bv), 'en', { numeric: true, sensitivity: 'base' });
        return this.sortDesc ? -result : result;
      });
    }
    return rows;
  }

  render() {
    if (!this.tbody) return;
    const rows = this.visibleRows();

    if (!this.rows.length) {
      setTableMessage(this.tbody, this.columns.length, this.emptyMessage);
      this.updateCount(0, 0);
      return;
    }
    if (!rows.length) {
      setTableMessage(this.tbody, this.columns.length, 'No records match the current filter.');
      this.updateCount(0, this.rows.length);
      return;
    }

    const page = rows.slice(0, this.limit);
    this.tbody.innerHTML = page
      .map((row) => `<tr>${this.columns.map((col) => `<td${col.className ? ` class="${escapeHtml(col.className)}"` : ''}>${col.render(row)}</td>`).join('')}</tr>`)
      .join('');

    this.updateCount(rows.length, this.rows.length, page.length);
  }

  /**
   * Publish how many rows are shown versus available.
   *
   * The jobs table used to slice to 100 rows with no indication, so 263 of 363
   * discovered jobs were simply invisible with no way to reach them.
   */
  updateCount(matching, total, shown = 0) {
    const countEl = document.querySelector(`[data-table-count="${this.table.id}"]`);
    if (countEl) {
      countEl.textContent = matching === total
        ? `Showing ${shown || matching} of ${total}`
        : `Showing ${shown || matching} of ${matching} matching (${total} total)`;
    }
    const moreBtn = document.querySelector(`[data-table-more="${this.table.id}"]`);
    if (moreBtn) moreBtn.hidden = shown >= matching;
  }
}
