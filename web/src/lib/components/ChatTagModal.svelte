<script>
  // Bulk-tag selected chats — same pattern as the library's TagPickerModal: each row shows
  // how many of the selection carry the tag; clicking applies to all (or removes from all
  // when every chat already has it). Changes save immediately.
  import { untrack } from 'svelte';
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import { updateChatMeta } from '$lib/api.js';
  import { toast } from '$lib/toast.js';

  let { chats = [], allTags = [], onclose = () => {}, onchange = () => {} } = $props();
  let q = $state('');
  let busy = $state(false);
  // Snapshot on open (the modal edits its own copy; parent refreshes on change).
  let local = $state(untrack(() => chats).map((c) => ({ account: c.account, id: c.id, tags: [...(c.tags || [])] })));

  const total = $derived(local.length);
  const counts = $derived.by(() => {
    const m = new Map();
    for (const c of local) for (const t of c.tags) m.set(t.toLowerCase(), (m.get(t.toLowerCase()) || 0) + 1);
    return m;
  });
  const names = $derived.by(() => {
    const seen = new Map();
    for (const t of allTags) seen.set(t.name.toLowerCase(), t.name);
    for (const c of local) for (const t of c.tags) if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
    return [...seen.values()];
  });
  const rows = $derived.by(() => {
    const n = q.trim().toLowerCase();
    return names.filter((t) => !n || t.toLowerCase().includes(n))
      .sort((a, b) => (counts.get(b.toLowerCase()) || 0) - (counts.get(a.toLowerCase()) || 0) || a.localeCompare(b));
  });
  const canCreate = $derived(!!q.trim() && !names.some((t) => t.toLowerCase() === q.trim().toLowerCase()));

  async function apply(name, remove) {
    if (busy || !total) return;
    busy = true;
    try {
      await updateChatMeta(local.map(({ account, id }) => ({ account, id })), remove ? { remove: [name] } : { add: [name] });
      for (const c of local) {
        const has = c.tags.some((t) => t.toLowerCase() === name.toLowerCase());
        if (remove) c.tags = c.tags.filter((t) => t.toLowerCase() !== name.toLowerCase());
        else if (!has) c.tags = [...c.tags, name];
      }
      onchange();
    } catch {
      toast("Couldn't save tags", { type: 'error' });
    } finally {
      busy = false;
    }
  }
  function create() {
    const name = q.trim();
    if (!name) return;
    q = '';
    apply(name, false);
  }
</script>

<Modal {onclose} ariaLabel="Tag selected chats" panelClass="panel flex max-h-[80dvh] w-full max-w-md flex-col overflow-hidden rounded-card">
  <div class="border-b border-line p-4">
    <h2 class="mb-3 text-lg font-bold">Tag {total} chat{total === 1 ? '' : 's'}</h2>
    <input type="text" class="w-full rounded-lg border border-line bg-transparent px-3 py-1.5 text-sm" placeholder="Find or create a tag…"
      aria-label="Find or create a tag" bind:value={q} onkeydown={(e) => { if (e.key === 'Enter' && canCreate) create(); }} />
  </div>
  <div class="flex-1 overflow-y-auto p-2" data-testid="chat-tag-rows">
    {#if canCreate}
      <button type="button" class="w-full rounded-lg px-3 py-2 text-left text-sm font-semibold text-[var(--accent)] hover:bg-[var(--surface-2)]" onclick={create}>+ Create “{q.trim()}”</button>
    {/if}
    {#each rows as name (name)}
      {@const n = counts.get(name.toLowerCase()) || 0}
      <button type="button" class="flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-[var(--surface-2)]"
        disabled={busy} onclick={() => apply(name, n >= total && total > 0)}>
        <span class="flex items-center gap-2"><span class="inline-block w-4 text-center">{n >= total && total ? '✓' : n ? '–' : ''}</span>{name}</span>
        <span class="text-xs text-muted">{n ? `on ${n} of ${total}` : ''}</span>
      </button>
    {:else}
      {#if !canCreate}<p class="p-3 text-sm text-muted">No tags yet — type a name to create one.</p>{/if}
    {/each}
  </div>
  <div class="flex justify-end border-t border-line p-3"><Button onclick={onclose}>Done</Button></div>
</Modal>
