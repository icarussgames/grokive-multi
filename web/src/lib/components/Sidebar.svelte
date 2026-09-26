<script>
  import { filters, toggleTag, toggleModel, toggleResolution, clearFilters, userTags, setTagMode } from '$lib/state.js';
  import Collapsible from './Collapsible.svelte';

  let { facets = { tags: [], models: [] }, onbrowse = () => {}, onmanagetags = () => {} } = $props();

  const topTags = $derived((facets.tags || []).slice(0, 12));
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
    {#if $filters.tags.length}
      <div class="mb-2 flex flex-wrap gap-1.5">
        {#each $filters.tags as t (t)}
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
