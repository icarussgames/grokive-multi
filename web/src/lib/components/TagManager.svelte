<script>
  // Manage hand-assigned tags: every tag with its item count, filter, rename (renaming
  // onto an existing name merges the two), recolor, delete, and "show" (filter the
  // library by it). Every change saves immediately.
  import { onMount } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import SearchField from './SearchField.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import { userTags, loadUserTags, renameUserTag, deleteUserTag, setUserTagColor } from '$lib/state.js';

  let { onclose = () => {}, onshow = () => {} } = $props();
  let q = $state('');
  let editing = $state(''); // tag being renamed
  let draft = $state('');
  let confirming = $state(null); // tag pending delete
  let busy = $state(false);

  onMount(loadUserTags);

  const rows = $derived(($userTags || []).filter((t) => !q.trim() || t.name.toLowerCase().includes(q.trim().toLowerCase())));
  const mergeTarget = $derived.by(() => {
    const key = draft.trim().toLowerCase();
    if (!editing || !key || key === editing.toLowerCase()) return null;
    return ($userTags || []).find((t) => t.name.toLowerCase() === key) || null;
  });

  function startRename(t) {
    editing = t.name;
    draft = t.name;
  }
  async function commitRename() {
    const from = editing;
    const to = draft.trim();
    if (!from || busy) return;
    if (!to || to === from) { editing = ''; return; }
    busy = true;
    await renameUserTag(from, to);
    busy = false;
    editing = '';
  }
  async function recolor(t, color) {
    if (busy) return;
    busy = true;
    await setUserTagColor(t.name, color);
    busy = false;
  }
  async function doDelete() {
    const name = confirming?.name;
    confirming = null;
    if (!name) return;
    busy = true;
    await deleteUserTag(name);
    busy = false;
  }
  // Focus the rename field as it mounts.
  const focus = (el) => { el.focus(); el.select(); };
</script>

<Modal {onclose} ariaLabel="Manage tags" closeOnEscape={!confirming && !editing}
  panelClass="panel flex max-h-[85dvh] w-full max-w-xl flex-col overflow-hidden rounded-card">
  <div class="flex items-center gap-3 border-b border-line p-4">
    <h2 class="shrink-0 text-lg font-bold">My tags <span class="text-sm font-normal text-muted">{($userTags || []).length}</span></h2>
    <SearchField bind:value={q} placeholder="Filter tags…" ariaLabel="Filter tags" wrapperClass="flex-1"
      inputClass="w-full rounded-full border border-line bg-[var(--surface-2)] py-2 pl-4 pr-10 text-sm outline-none" />
  </div>
  <div class="min-h-0 flex-1 overflow-auto p-2">
    {#each rows as t (t.name)}
      <div class="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-[var(--surface-2)]">
        <label class="relative h-5 w-5 shrink-0 cursor-pointer rounded-full border border-line" title="Tag color"
          style={t.color ? `background:${t.color}` : ''}>
          <input type="color" class="absolute inset-0 h-full w-full cursor-pointer opacity-0" value={t.color || '#8b5cf6'}
            aria-label={`Color for ${t.name}`} onchange={(e) => recolor(t, e.currentTarget.value)} />
        </label>
        {#if editing === t.name}
          <form class="flex min-w-0 flex-1 flex-col" onsubmit={(e) => { e.preventDefault(); commitRename(); }}>
            <input use:focus bind:value={draft} maxlength="64" aria-label={`Rename ${t.name}`}
              onkeydown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); editing = ''; } }}
              class="w-full rounded-md border border-line bg-[var(--surface-2)] px-2 py-1 text-sm outline-none focus:border-[var(--accent)]" />
            {#if mergeTarget}<span class="mt-1 text-xs text-muted">Merges into existing “{mergeTarget.name}” ({mergeTarget.count}).</span>{/if}
          </form>
          <Button size="md" class="!px-3 !py-1 text-sm" disabled={busy} onclick={commitRename}>{mergeTarget ? 'Merge' : 'Save'}</Button>
          <Button variant="secondary" class="!px-3 !py-1 text-sm" onclick={() => (editing = '')}>Cancel</Button>
        {:else}
          <button type="button" class="min-w-0 flex-1 truncate text-left text-sm font-semibold hover:underline" title={`Show media tagged “${t.name}”`}
            onclick={() => { onshow(t.name); onclose(); }}>{t.name}</button>
          <span class="shrink-0 text-xs text-muted">{t.count} item{t.count === 1 ? '' : 's'}</span>
          {#if t.color}
            <button type="button" class="shrink-0 text-xs text-muted hover:text-[var(--ink)]" disabled={busy} onclick={() => recolor(t, '')} title="Clear color">No color</button>
          {/if}
          <button type="button" class="shrink-0 rounded px-2 py-0.5 text-xs font-semibold hover:bg-[var(--surface-solid)]" onclick={() => startRename(t)}>Rename</button>
          <button type="button" class="shrink-0 rounded px-2 py-0.5 text-xs font-semibold text-[var(--danger-ink)] hover:bg-[var(--surface-solid)]" onclick={() => (confirming = t)}>Delete</button>
        {/if}
      </div>
    {/each}
    {#if !rows.length}
      <p class="p-3 text-sm text-muted">{q ? `No tags match “${q}”.` : 'No tags yet. Add them from an item’s info panel, or select items and use Tag….'}</p>
    {/if}
  </div>
  <div class="flex justify-end border-t border-line p-3">
    <Button onclick={onclose}>Done</Button>
  </div>
</Modal>

{#if confirming}
  <ConfirmDialog title={`Delete tag “${confirming.name}”?`}
    message={`It's removed from ${confirming.count} item${confirming.count === 1 ? '' : 's'}. The media themselves are untouched.`}
    onconfirm={doDelete} oncancel={() => (confirming = null)} />
{/if}
