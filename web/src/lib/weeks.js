// Local-time week buckets for gallery section headers.
//
// A week is Monday 00:00 → Sunday 23:59:59 in the browser's local timezone (same
// convention as the Date panel's tz_offset). Items without a parseable created_at
// land in an "Unknown date" bucket at the end.

const MS_DAY = 86400000;

/** Local midnight of the Monday that starts the week containing `d`. */
export function weekStartLocal(d) {
  const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  // getDay: Sun=0…Sat=6 → Mon=0…Sun=6
  const monOffset = (day.getDay() + 6) % 7;
  day.setDate(day.getDate() - monOffset);
  day.setHours(0, 0, 0, 0);
  return day;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

/** Stable key `YYYY-MM-DD` of the week's Monday (local). */
export function weekKey(createdAt) {
  const d = createdAt ? new Date(createdAt) : null;
  if (!d || Number.isNaN(d.getTime())) return 'unknown';
  const start = weekStartLocal(d);
  return `${start.getFullYear()}-${pad(start.getMonth() + 1)}-${pad(start.getDate())}`;
}

function fmtDay(d, { withYear = false, withMonth = true } = {}) {
  const opts = { day: 'numeric' };
  if (withMonth) opts.month = 'short';
  if (withYear) opts.year = 'numeric';
  return d.toLocaleDateString(undefined, opts);
}

/** "Sep 1 – Sep 7, 2026" / "Sep 29 – Oct 5, 2026" / "Unknown date". */
export function weekLabel(key) {
  if (key === 'unknown') return 'Unknown date';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
  if (!m) return key;
  const start = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const end = new Date(start.getTime() + 6 * MS_DAY);
  const sameMonth = start.getMonth() === end.getMonth();
  const sameYear = start.getFullYear() === end.getFullYear();
  const left = fmtDay(start, { withYear: false });
  const right = fmtDay(end, { withMonth: !sameMonth, withYear: false });
  const year = sameYear ? start.getFullYear() : `${start.getFullYear()} – ${end.getFullYear()}`;
  return `${left} – ${right}, ${year}`;
}

/**
 * Partition `items` into week groups. Preserves relative order within each week
 * (the API's sort). Weeks themselves are newest-first by default.
 * Returns [{ key, label, items }].
 */
export function groupItemsByWeek(items, { newestFirst = true } = {}) {
  const map = new Map();
  const order = [];
  for (const it of items || []) {
    const key = weekKey(it?.created_at);
    let g = map.get(key);
    if (!g) {
      g = { key, label: weekLabel(key), items: [] };
      map.set(key, g);
      order.push(key);
    }
    g.items.push(it);
  }
  // Sort week keys: unknown last; otherwise by Monday date.
  order.sort((a, b) => {
    if (a === 'unknown') return 1;
    if (b === 'unknown') return -1;
    return newestFirst ? b.localeCompare(a) : a.localeCompare(b);
  });
  return order.map((k) => map.get(k));
}

/** Date filters narrow enough that week headers help. */
export function shouldGroupByWeek(period) {
  const p = String(period || 'all');
  return p === 'last30' || p === 'last60' || p.startsWith('m:');
}
