<script>
  // Hand-assigned tags on ONE media item (the Lightbox info panel): removable chips, an
  // input with autocomplete over existing tags (Enter / comma adds, Backspace on an empty
  // field removes the last), and one-click "suggested" chips lifted from the item's
  // prompt-derived tags. Clicking a chip's name browses the library by that tag.
  import { userTags, tagEdits, userTagsOf, tagItems } from '$lib/state.js';

  let { item, onbrowse = () => {} } = $props();
  let text = $state('');
  let open = $state(false);
  let hi = $state(0);
  let busy = $state(false);
  let navigated = $state(false); // arrowed into the suggestion list — Enter takes that row

  const mine = $derived(userTagsOf(item, $tagEdits));
  const mineKeys = $derived(new Set(mine.map((t) => t.toLowerCase())));
  const colorOf = $derived(new Map(($userTags || []).map((t) => [t.name, t.color])));
  const matches = $derived.by(() => {
    const q = text.trim().toLowerCase();
    const pool = ($userTags || []).filter((t) => !mineKeys.has(t.name.toLowerCase()));
    const hits = q ? pool.filter((t) => t.name.toLowerCase().includes(q)) : pool;
    // Prefix matches first, then most-used.
    return hits
      .slice()
      .sort((a, b) => (q ? (b.name.toLowerCase().startsWith(q) - a.name.toLowerCase().startsWith(q)) : 0) || b.count - a.count)
      .slice(0, 8);
  });
  const exact = $derived(matches.some((t) => t.name.toLowerCase() === text.trim().toLowerCase()));
  // Prompt tags can be whole spoken lines; only short ones make sensible labels.
  const suggestions = $derived((item?.tags || []).filter((t) => t.length <= 32 && !mineKeys.has(t.toLowerCase())).slice(0, 6));

  async function add(names) {
    const list = names.map((n) => String(n).trim()).filter(Boolean);
    if (!list.length || !item || busy) return;
    busy = true;
    await tagItems([item.id], { add: list });
    busy = false;
  }
  async function remove(name) {
    if (!item || busy) return;
    busy = true;
    await tagItems([item.id], { remove: [name] });
    busy = false;
  }
  function commit() {
    // Enter takes the arrowed-to suggestion; otherwise the typed text itself (commas
    // split several names; the server reuses an existing tag's spelling, any case).
    const chosen = open && navigated && matches[hi] ? matches[hi].name : text;
    const names = String(chosen).split(',');
    text = '';
    hi = 0;
    navigated = false;
    add(names);
  }
  function onkeydown(e) {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (text.trim() || (navigated && matches[hi])) commit();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (open) hi = navigated ? Math.min(hi + 1, Math.max(0, matches.length - 1)) : 0;
      open = true;
      navigated = true;
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      hi = Math.max(0, hi - 1);
    } else if (e.key === 'Backspace' && !text && mine.length) {
      remove(mine[mine.length - 1]);
    } else if (e.key === 'Escape') {
      if (open && text) { open = false; } else e.currentTarget.blur();
    } else {
      open = true;
      hi = 0;
      navigated = false;
    }
  }
</script>

<div class="mb-3">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-xs font-bold uppercase tracking-wide text-muted">My tags</span>
    {#each mine as tag (tag)}
      {@const c = colorOf.get(tag)}
      <span class="inline-flex max-w-full items-center gap-1 rounded-full border border-[var(--chip-line)] bg-[var(--chip-bg)] py-0.5 pl-2.5 pr-1 text-xs font-semibold text-[var(--chip-ink)]">
        {#if c}<span class="h-2 w-2 shrink-0 rounded-full" style="background:{c}"></span>{/if}
        <button type="button" class="truncate hover:underline" title={`Show all media tagged “${tag}”`} onclick={() => onbrowse(tag)}>{tag}</button>
        <button type="button" class="grid h-4 w-4 shrink-0 place-items-center rounded-full opacity-70 hover:bg-[var(--surface-2)] hover:opacity-100"
          aria-label={`Remove tag ${tag}`} title="Remove tag" disabled={busy} onclick={() => remove(tag)}>✕</button>
      </span>
    {/each}
    <div class="relative min-w-[9rem] flex-1">
      <input data-tag-input type="text" bind:value={text} {onkeydown} maxlength="64"
        onfocus={() => (open = true)} onblur={() => setTimeout(() => (open = false), 120)}
        placeholder={mine.length ? 'Add tag…' : 'Add a tag…'} aria-label="Add tag"
        class="w-full rounded-full border border-line bg-[var(--surface-2)] px-3 py-1 text-xs outline-none focus:border-[var(--accent)]" />
      {#if open && (matches.length || (text.trim() && !exact))}
        <ul class="panel absolute left-0 right-0 top-full z-10 mt-1 max-h-56 overflow-auto rounded-lg py-1 text-xs shadow-lg" role="listbox">
          {#each matches as t, k (t.name)}
            <li>
              <button type="button" role="option" aria-selected={k === hi}
                class="flex w-full items-center gap-2 px-3 py-1.5 text-left {k === hi ? 'bg-[var(--surface-2)]' : ''}"
                onmousedown={(e) => { e.preventDefault(); text = ''; add([t.name]); }}>
                {#if t.color}<span class="h-2 w-2 shrink-0 rounded-full" style="background:{t.color}"></span>{/if}
                <span class="truncate">{t.name}</span><span class="ml-auto opacity-60">{t.count}</span>
              </button>
            </li>
          {/each}
          {#if text.trim() && !exact}
            <li>
              <button type="button" class="w-full px-3 py-1.5 text-left text-[var(--accent)]"
                onmousedown={(e) => { e.preventDefault(); const n = text; text = ''; add(n.split(',')); }}>Create “{text.trim()}”</button>
            </li>
          {/if}
        </ul>
      {/if}
    </div>
  </div>
  {#if suggestions.length}
    <div class="mt-2 flex flex-wrap items-center gap-1.5">
      <span class="text-[11px] text-muted">Suggested:</span>
      {#each suggestions as s (s)}
        <button type="button" class="max-w-full truncate rounded-full border border-dashed border-line px-2 py-0.5 text-[11px] text-muted transition hover:border-[var(--accent)] hover:text-[var(--ink)]"
          title={`Add “${s}” as one of your tags`} disabled={busy} onclick={() => add([s])}>+ {s}</button>
      {/each}
    </div>
  {/if}
</div>
