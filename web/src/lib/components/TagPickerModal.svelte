<script>
  // Bulk-tag the current selection. Each row shows how many of the selected items carry
  // that tag ("on 3 of 5"); clicking a row applies it to ALL of them (or, when every item
  // already has it, removes it from all). Typing a new name offers to create + apply it.
  // Changes save immediately — Done just closes.
  import { onMount } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import SearchField from './SearchField.svelte';
  import { userTags, tagEdits, userTagsOf, tagItems, loadUserTags } from '$lib/state.js';
  import { mediaByIds } from '$lib/api.js';

  let { ids = [], onclose = () => {}, onmanage = null } = $props();
  let q = $state('');
  let busy = $state(false);
  let loaded = $state(null); // fresh records for the selection (user_tags snapshot)

  const selected = $derived((ids || []).map(String).filter(Boolean));
  onMount(async () => {
    loadUserTags();
    loaded = await mediaByIds(selected);
  });

  const records = $derived(loaded ?? selected.map((id) => ({ id, user_tags: [] })));
  const counts = $derived.by(() => {
    const m = new Map();
    for (const r of records) for (const t of userTagsOf(r, $tagEdits)) m.set(t, (m.get(t) || 0) + 1);
    return m;
  });
  const total = $derived(records.length);
  const rows = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    const list = ($userTags || []).filter((t) => !needle || t.name.toLowerCase().includes(needle));
    // Tags already on some of the selection float to the top, then by name.
    return list.slice().sort((a, b) => (counts.get(b.name) || 0) - (counts.get(a.name) || 0) || a.name.localeCompare(b.name));
  });
  const canCreate = $derived(!!q.trim() && !($userTags || []).some((t) => t.name.toLowerCase() === q.trim().toLowerCase()));

  async function apply(name, remove = false) {
    if (busy || !selected.length) return;
    busy = true;
    await tagItems(selected, remove ? { remove: [name] } : { add: [name] });
    busy = false;
  }
  function toggle(name) {
    apply(name, (counts.get(name) || 0) >= total && total > 0);
  }
  async function create() {
    const name = q.trim();
    if (!name) return;
    q = '';
    await apply(name);
  }
</script>

<Modal {onclose} ariaLabel="Tag selected items" panelClass="panel flex max-h-[80dvh] w-full max-w-md flex-col overflow-hidden rounded-card">
  <div class="border-b border-line p-4">
    <div class="mb-3 flex items-baseline justify-between gap-2">
      <h2 class="text-lg font-bold">Tag {selected.length} item{selected.length === 1 ? '' : 's'}</h2>
      {#if onmanage}<button type="button" class="text-xs font-semibold text-[var(--accent)] hover:underline" onclick={onmanage}>Manage tags…</button>{/if}
    </div>
    <form onsubmit={(e) => { e.preventDefault(); if (canCreate) create(); else if (rows.length === 1) toggle(rows[0].name); }}>
      <SearchField bind:value={q} placeholder="Find or create a tag…" ariaLabel="Find or create a tag"
        inputClass="w-full rounded-full border border-line bg-[var(--surface-2)] py-2 pl-4 pr-10 text-sm outline-none" />
    </form>
  </div>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    {#if canCreate}
      <button type="button" class="mb-1 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm font-semibold text-[var(--accent)] hover:bg-[var(--surface-2)]"
        disabled={busy} onclick={create}>+ Create “{q.trim()}” and tag {selected.length === 1 ? 'it' : `all ${selected.length}`}</button>
    {/if}
    {#each rows as t (t.name)}
      {@const c = counts.get(t.name) || 0}
      {@const st = c === 0 ? 'none' : c >= total ? 'all' : 'some'}
      <div class="flex items-center gap-2 rounded-lg px-2 hover:bg-[var(--surface-2)]">
        <button type="button" class="flex min-w-0 flex-1 items-center gap-2 py-2 text-left text-sm" disabled={busy} onclick={() => toggle(t.name)}
          title={st === 'all' ? `Remove “${t.name}” from all` : `Add “${t.name}” to all`}>
          <span class="grid h-4 w-4 shrink-0 place-items-center rounded border text-[10px] font-bold leading-none {st === 'none' ? 'border-line' : 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]'}">{st === 'all' ? '✓' : st === 'some' ? '–' : ''}</span>
          {#if t.color}<span class="h-2 w-2 shrink-0 rounded-full" style="background:{t.color}"></span>{/if}
          <span class="truncate">{t.name}</span>
          <span class="ml-auto shrink-0 text-xs text-muted">{c ? `on ${c} of ${total}` : `${t.count} in library`}</span>
        </button>
        {#if st === 'some'}
          <button type="button" class="shrink-0 rounded px-1.5 py-0.5 text-xs text-muted hover:text-[var(--ink)]" disabled={busy}
            title={`Remove “${t.name}” from the ${c} that have it`} onclick={() => apply(t.name, true)}>Remove</button>
        {/if}
      </div>
    {/each}
    {#if !rows.length && !canCreate}
      <p class="p-3 text-sm text-muted">{q ? `No tags match “${q}”.` : 'No tags yet — type a name to create one.'}</p>
    {/if}
  </div>
  <div class="flex justify-end border-t border-line p-3">
    <Button onclick={onclose}>Done</Button>
  </div>
</Modal>
