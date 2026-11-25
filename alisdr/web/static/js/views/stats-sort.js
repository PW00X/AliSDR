import { h, mount } from '../core.js';

// Keep a separate sort selection for each table when a statistics view refreshes.
export function statisticsSorter() {
  const selections = new Map();
  const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
  return function enable(root) {
    for (const table of root.querySelectorAll('table')) {
      const headers = Array.from(table.tHead.rows[0].cells);
      const key = headers.map(th => th.textContent).join('|');
      const tbody = table.tBodies[0];
      function apply() {
        const choice = selections.get(key);
        if (!choice) return;
        const { column, direction } = choice;
        headers.forEach((th, i) => {
          if (i === column) th.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending');
          else th.removeAttribute('aria-sort');
        });
        const rows = Array.from(tbody.rows);
        const value = row => {
          const cell = row.cells[column];
          if (!cell.hasAttribute('data-number')) return cell.textContent.trim();
          return cell.dataset.number === '' ? null : Number(cell.dataset.number);
        };
        rows.sort((left, right) => {
          const a = value(left), b = value(right);
          if (a == null || b == null) return Number(a == null) - Number(b == null);
          return direction * (typeof a === 'number' ? a - b : collator.compare(a, b));
        });
        tbody.replaceChildren(...rows);
      }
      headers.forEach((th, column) => {
        const label = th.textContent;
        if (!label) return;
        mount(th, h('button.stats-sort', { type: 'button', onclick: () => {
          const previous = selections.get(key);
          selections.set(key, { column, direction: previous?.column === column ? -previous.direction : 1 });
          apply();
        } }, label));
      });
      apply();
    }
  };
}

export const numericCell = value => ({ 'data-number': value ?? '' });
