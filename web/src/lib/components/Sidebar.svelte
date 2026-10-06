<script>
  import { filters, toggleTag, toggleModel, toggleResolution, clearFilters, userTags, setTagMode, setPeriod, periodLabel, monthLabel } from '$lib/state.js';
  import Collapsible from './Collapsible.svelte';

  let { facets = { tags: [], models: [] }, onbrowse = () => {}, onmanagetags = () => {} } = $props();

  const topTags = $derived((facets.tags || []).slice(0, 12));
  // Grok Imagine collections (read-only "grok:<name>" tags), most-used first.
  // A selected collection with no items in the current scope still lists (count 0) so it
  // can be deselected here; the Prompt tags section leaves grok:* chips to this one.
  const grokTags = $derived.by(() => {
    const list = [...(facets.grok_tags || [])];
    for (const t of $filters.tags) {
      if (t.startsWith('grok:') && !list.some((x) => x.name === t)) list.push({ name: t, count: 0 });
    }
    return list;
  });
  const nonGrokSelected = $derived($filters.tags.filter((t) => !t.startsWith('grok:')));
  const grokLabel = (name) => String(name).replace(/^grok:/, '');

  // Date panel: quick ranges + years -> months that actually have items (facets.months,
  // newest first, counted in this browser's local time).
  const QUICK = [{ id: 'all', label: 'All time' }, { id: 'last30', label: 'Last 30 days' }, { id: 'last60', label: 'Last 60 days' }];
  const years = $derived.by(() => {
    const out = [];
    for (const m of facets.months || []) {
      const y = String(m.month).slice(0, 4);
      let row = out.find((r) => r.year === y);
      if (!row) { row = { year: y, count: 0, months: [] }; out.push(row); }
      row.count += m.count;
      row.months.push(m);
    }
    return out;
  });
  const pickedMonth = $derived(String($filters.period || '').startsWith('m:') ? $filters.period.slice(2) : '');
  let openYears = $state({});
  // The year holding the picked month opens itself; otherwise the newest year starts open.
  const yearOpen = (y, i) => {
    if (openYears[y] !== undefined) return openYears[y];
    if (pickedMonth) return pickedMonth.startsWith(y);
    return i === 0;
  };
  const otherPeriod = $derived(!QUICK.some((q) => q.id === $filters.period) && !pickedMonth);
  // Hand-assigned tags, counted within the current scope (facets.user_tags); a tag with no
  // match in scope still lists (count 0) so it stays one click away.
  const myTags = $derived.by(() => {
    const inScope = new Map((facets.user_tags || []).map((t) => [t.name, t.count]));
    return ($userTags || []).map((t) => ({ ...t, scoped: inScope.get(t.name) || 0 }))
      .sort((a, b) => b.scoped - a.scoped || a.name.localeCompare(b.name));
  });
</script>

<!-- Filters only. Media type lives in the grid header (high-frequency, applies everywhere) and
     Playlists moved to the Library page — so the sidebar is now a single-purpose: refine the grid.
     The wrapper (desktop <aside> or mobile drawer) owns width, scrolling and visibility. -->
<div class="p-4">
  <Collapsible title="Date" count={$filters.period !== 'all' ? '•' : ''}>
    <div class="flex flex-wrap gap-1.5">
      {#each QUICK as qd (qd.id)}
        {@const on = $filters.period === qd.id}
        <button type="button" aria-pressed={on}
          class="rounded-full border px-2.5 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
          onclick={() => setPeriod(qd.id)}>{qd.label}</button>
      {/each}
      {#if otherPeriod}
        <button type="button" class="rounded-full border border-transparent bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-[var(--on-accent)]"
          title="Clear the date filter" onclick={() => setPeriod('all')}>{periodLabel($filters.period)} ✕</button>
      {/if}
    </div>
    {#if years.length}
      <div class="mt-2.5 flex flex-col gap-1">
        {#each years as y, i (y.year)}
          {@const isOpen = yearOpen(y.year, i)}
          <button type="button" class="flex items-center justify-between rounded-lg px-1.5 py-1 text-left text-sm font-semibold transition hover:bg-[var(--surface-2)]"
            aria-expanded={isOpen} onclick={() => (openYears = { ...openYears, [y.year]: !isOpen })}>
            <span class="flex items-center gap-1.5">
              <svg viewBox="0 0 24 24" class="h-3 w-3 transition-transform {isOpen ? 'rotate-90' : ''}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
              {y.year}
            </span>
            <span class="text-xs font-normal text-muted">{y.count.toLocaleString()}</span>
          </button>
          {#if isOpen}
            <div class="mb-1 grid grid-cols-3 gap-1 pl-4">
              {#each y.months as m (m.month)}
                {@const on = pickedMonth === m.month}
                <button type="button" aria-pressed={on} title={`${monthLabel(m.month)} — ${m.count.toLocaleString()} items`}
                  class="flex flex-col items-center rounded-lg border px-1 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                  onclick={() => setPeriod(on ? 'all' : `m:${m.month}`)}>
                  <span>{monthLabel(m.month, 'short')}</span>
                  <span class="text-[10px] font-normal opacity-70">{m.count.toLocaleString()}</span>
                </button>
              {/each}
            </div>
          {/if}
        {/each}
      </div>
    {/if}
  </Collapsible>

  {#if facets.resolutions?.length}
    <Collapsible title="Resolution" count={facets.resolutions.length} open={false}>
      <div class="flex flex-wrap gap-1.5">
        {#each facets.resolutions as r (`${r.height}-${r.orientation}`)}
          {@const key = `${r.height}-${r.orientation}`}
          <button type="button"
            class="rounded-full border px-2.5 py-1 text-xs font-semibold capitalize transition {$filters.resolutions.includes(key) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
            onclick={() => toggleResolution(key)}>{r.height}p {r.orientation} <span class="opacity-70">{r.count}</span></button>
        {/each}
      </div>
    </Collapsible>
  {/if}

  <Collapsible title="My tags" count={myTags.length}>
    {#if myTags.length}
      <div class="flex flex-wrap gap-1.5">
        {#each myTags as t (t.name)}
          {@const on = $filters.tags.includes(t.name)}
          <button class="inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-[var(--chip-line)] bg-[var(--chip-bg)] text-[var(--chip-ink)] hover:border-[var(--accent)]'}"
            title={t.name} onclick={() => toggleTag(t.name)}>
            {#if t.color}<span class="h-2 w-2 shrink-0 rounded-full" style="background:{t.color}"></span>{/if}
            <span class="truncate">{t.name}</span> <span class="shrink-0 opacity-70">{on ? '✕' : t.scoped}</span>
          </button>
        {/each}
      </div>
    {:else}
      <p class="text-xs text-muted">No tags yet — add them from an item's info panel or select items and use Tag….</p>
    {/if}
    <div class="mt-2.5 flex items-center justify-between gap-2">
      <button class="text-xs font-semibold text-[var(--accent)] hover:underline" onclick={onmanagetags}>Manage tags →</button>
      {#if $filters.tags.length > 1}
        <div class="inline-flex rounded-md border border-line p-0.5 text-[11px] font-semibold" role="group" aria-label="Match selected tags">
          <button class="rounded px-2 py-0.5 {$filters.tagMode !== 'all' ? 'bg-[var(--surface-2)]' : 'text-muted'}" title="Items with ANY selected tag" onclick={() => setTagMode('any')}>Any</button>
          <button class="rounded px-2 py-0.5 {$filters.tagMode === 'all' ? 'bg-[var(--surface-2)]' : 'text-muted'}" title="Items with ALL selected tags" onclick={() => setTagMode('all')}>All</button>
        </div>
      {/if}
    </div>
  </Collapsible>

  <Collapsible title="Prompt tags">
    {#if nonGrokSelected.length}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each nonGrokSelected as t (t)}
          <button class="inline-flex max-w-full items-center gap-1 rounded-full bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-[var(--on-accent)]" title={t} onclick={() => toggleTag(t)}><span class="truncate">{t}</span> ✕</button>
        {/each}
      </div>
    {/if}
    <div class="flex flex-wrap gap-1.5">
      {#each topTags as t (t.name)}
        {#if !$filters.tags.includes(t.name)}
          <button class="inline-flex max-w-full items-center gap-1 rounded-full border border-line px-2.5 py-1 text-xs hover:border-[var(--accent)]" title={t.name} onclick={() => toggleTag(t.name)}><span class="truncate">{t.name}</span> <span class="shrink-0 opacity-70">{t.count}</span></button>
        {/if}
      {/each}
    </div>
    <button class="mt-2.5 text-xs font-semibold text-[var(--accent)] hover:underline" onclick={onbrowse}>Browse all {facets.tags?.length || 0} tags →</button>
  </Collapsible>

  {#if grokTags.length}
    <Collapsible title="Grok collections" count={grokTags.length}>
      <div class="flex flex-wrap gap-1.5">
        {#each grokTags as t (t.name)}
          {@const on = $filters.tags.includes(t.name)}
          <button type="button" title={`Grok collection “${grokLabel(t.name)}” (from grok.com — read-only)`}
            class="inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-dashed border-line hover:border-[var(--accent)]'}"
            onclick={() => toggleTag(t.name)}>
            <span class="truncate">{grokLabel(t.name)}</span> <span class="shrink-0 opacity-70">{on ? '✕' : t.count}</span>
          </button>
        {/each}
      </div>
    </Collapsible>
  {/if}

  {#if facets.models?.length}
    <Collapsible title="Models" count={facets.models.length}>
      <div class="flex flex-col gap-1">
        {#each facets.models as m (m.name)}
          <button type="button"
            class="flex items-center justify-between rounded-lg border px-2.5 py-1.5 text-left text-sm {$filters.models.includes(m.name) ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}"
            onclick={() => toggleModel(m.name)}>
            <span class="truncate">{m.name}</span><span class="ml-2 text-xs opacity-70">{m.count}</span>
          </button>
        {/each}
      </div>
    </Collapsible>
  {/if}

  <button type="button" class="w-full rounded-lg border border-line py-2 text-sm font-semibold transition hover:border-[var(--accent)]" onclick={clearFilters}>Clear filters</button>
</div>
