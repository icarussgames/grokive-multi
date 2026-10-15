<script>
  // Archived Grok chats (normal text conversations backed up by grokchats.py during Sync).
  // Pinned filter rail (same layout as the library's), full-text search with highlighted
  // snippets, account / date / tag / archive filters, multi-select (Shift+click ranges) for
  // bulk Tag / Archive, and a reader for one conversation.
  import { onMount } from 'svelte';
  import { fetchChats, fetchChatMonths, fetchChat, fetchChatFacets, updateChatMeta } from '$lib/api.js';
  import { grokAccounts, accountLabel, loadGrokAccounts, periodLabel, monthLabel } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';
  import Collapsible from './Collapsible.svelte';
  import ChatTagModal from './ChatTagModal.svelte';

  const PAGE = 50;
  const QUICK = [
    { id: 'all', label: 'All time' }, { id: 'last7', label: '7 days' }, { id: 'last30', label: '30 days' },
    { id: 'month', label: 'This month' }, { id: 'year', label: 'This year' }
  ];
  let q = $state('');
  let qLive = $state('');
  let account = $state('all');
  let period = $state('all');
  let sort = $state('new');
  let archived = $state('active'); // active | archived | all
  let tags = $state([]);
  let tagMode = $state('any');
  let items = $state([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(false);
  let error = $state('');
  let hl = ['\u0001', '\u0002'];
  let months = $state([]);
  let facets = $state({ accounts: [], tags: [], archived: 0 });
  let openYears = $state({});
  let open = $state(null);
  let openErr = $state('');
  let selected = $state(new Set()); // "account/id"
  let anchor = null;
  let tagFor = $state(null); // chats passed to ChatTagModal
  let readerTag = $state('');
  let reqSeq = 0;
  let refreshTick = $state(0);

  const keyOf = (it) => `${it.account}/${it.id}`;
  const years = $derived.by(() => {
    const by = new Map();
    for (const m of months) {
      const y = m.month.slice(0, 4);
      if (!by.has(y)) by.set(y, { year: y, count: 0, months: [] });
      const e = by.get(y); e.count += m.count; e.months.push(m);
    }
    for (const e of by.values()) e.months.sort((a, b) => a.month.localeCompare(b.month));
    return [...by.values()];
  });
  const acctTotal = $derived(facets.accounts.reduce((n, a) => n + a.count, 0));
  const acctCount = $derived(Object.fromEntries(facets.accounts.map((a) => [a.id, a.count])));
  const accountList = $derived.by(() => {
    const ids = new Set([...$grokAccounts.map((a) => a.id), ...facets.accounts.map((a) => a.id)]);
    return [...ids].map((id) => ({ id, label: accountLabel(id, $grokAccounts), count: acctCount[id] || 0 }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
  });
  const selItems = $derived(items.filter((it) => selected.has(keyOf(it))));
  const periodText = $derived(period.startsWith('y:') ? period.slice(2) : periodLabel(period));
  const filtered = $derived(!!(q || account !== 'all' || period !== 'all' || tags.length || archived !== 'active'));

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function snippetHtml(s) {
    return esc(s).split(hl[0]).join('<mark class="chat-mark">').split(hl[1]).join('</mark>');
  }
  function highlightHtml(text, query) {
    const words = (query.replace(/#\S+/g, ' ').match(/\w+/gu) || []).filter((w) => w.length > 1);
    const safe = esc(text);
    if (!words.length) return safe;
    const re = new RegExp('(' + words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'giu');
    return safe.replace(re, '<mark class="chat-mark">$1</mark>');
  }
  function fmtDate(iso, withTime = false) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return withTime ? d.toLocaleString() : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function monthKey(iso) {
    const d = new Date(iso);
    if (!iso || isNaN(d)) return 'Unknown date';
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
  }
  const groups = $derived.by(() => {
    if (sort === 'title' || q) return [{ key: '', items }];
    const out = [];
    for (const it of items) {
      const k = monthKey(it.sort_time);
      if (!out.length || out[out.length - 1].key !== k) out.push({ key: k, items: [] });
      out[out.length - 1].items.push(it);
    }
    return out;
  });

  async function load(reset = true) {
    const seq = ++reqSeq;
    loading = true;
    error = '';
    const nextPage = reset ? 1 : page + 1;
    try {
      const res = await fetchChats({ q, account, period, sort, archived, tags, tagMode, page: nextPage, pageSize: PAGE });
      if (seq !== reqSeq) return;
      if (res.hl) hl = res.hl;
      items = reset ? res.items : [...items, ...res.items];
      total = res.total;
      page = nextPage;
      if (reset) { selected = new Set(); anchor = null; }
    } catch (e) {
      if (seq === reqSeq) error = String(e.message || e);
    } finally {
      if (seq === reqSeq) loading = false;
    }
  }
  async function loadFacets() {
    try { facets = await fetchChatFacets({ account, archived }); } catch {}
  }

  let debounce;
  function onSearchInput(v) {
    qLive = v;
    clearTimeout(debounce);
    debounce = setTimeout(() => { q = v.trim(); }, 250);
  }
  $effect(() => {
    void q; void account; void period; void sort; void archived; void tags; void tagMode; void refreshTick;
    load(true);
  });
  $effect(() => { void account; void archived; void refreshTick; loadFacets(); });
  $effect(() => {
    void account; void archived; void refreshTick;
    fetchChatMonths(account, archived).then((r) => (months = r.months || [])).catch(() => (months = []));
  });

  function refresh() { refreshTick++; }
  function toggleTag(name) {
    const low = name.toLowerCase();
    tags = tags.some((t) => t.toLowerCase() === low) ? tags.filter((t) => t.toLowerCase() !== low) : [...tags, name];
  }
  function clearAll() {
    qLive = ''; q = ''; account = 'all'; period = 'all'; tags = []; archived = 'active';
  }

  // Selection: click the checkbox (or Ctrl/Cmd-click a row) toggles; Shift+click selects the
  // range from the last clicked chat — same gesture as the library grid.
  function select(it, e) {
    const k = keyOf(it);
    const next = new Set(selected);
    const idx = items.findIndex((x) => keyOf(x) === k);
    if (e?.shiftKey && anchor != null) {
      const a = items.findIndex((x) => keyOf(x) === anchor);
      if (a >= 0) {
        const on = selected.has(anchor);
        const [lo, hi] = a < idx ? [a, idx] : [idx, a];
        for (let i = lo; i <= hi; i++) on ? next.add(keyOf(items[i])) : next.delete(keyOf(items[i]));
        selected = next;
        return;
      }
    }
    next.has(k) ? next.delete(k) : next.add(k);
    selected = next;
    anchor = k;
  }
  function rowClick(it, e) {
    if (e.shiftKey || e.ctrlKey || e.metaKey || selected.size) { e.preventDefault(); select(it, e); return; }
    openChat(it);
  }

  async function setArchived(list, value) {
    if (!list.length) return;
    try {
      await updateChatMeta(list.map(({ account: a, id }) => ({ account: a, id })), { archived: value });
      toast(`${value ? 'Archived' : 'Unarchived'} ${list.length} chat${list.length === 1 ? '' : 's'}`, { type: 'success' });
      if (open && list.some((c) => c.id === open.id && c.account === open.account)) open = { ...open, archived: value };
      refresh();
    } catch {
      toast("Couldn't save", { type: 'error' });
    }
  }
  async function readerTagEdit(patch) {
    try {
      await updateChatMeta([{ account: open.account, id: open.id }], patch);
      let t = [...(open.tags || [])];
      if (patch.add) for (const n of patch.add) if (!t.some((x) => x.toLowerCase() === n.toLowerCase())) t.push(n);
      if (patch.remove) t = t.filter((x) => !patch.remove.some((r) => r.toLowerCase() === x.toLowerCase()));
      open = { ...open, tags: t };
      refresh();
    } catch {
      toast("Couldn't save tags", { type: 'error' });
    }
  }

  async function openChat(it) {
    openErr = '';
    open = { id: it.id, account: it.account, title: it.title, tags: it.tags, archived: it.archived, messages: null };
    try {
      open = await fetchChat(it.account, it.id);
      window.scrollTo(0, 0);
      const target = it.snippets?.find((s) => s.msg_idx >= 0)?.msg_idx;
      if (target != null) setTimeout(() => document.getElementById(`chat-msg-${target}`)?.scrollIntoView({ block: 'center' }), 50);
    } catch (e) {
      openErr = String(e.message || e);
    }
  }
  async function copy(text, what = 'Message') {
    const ok = await copyText(text);
    toast(ok ? `${what} copied` : "Couldn't copy", { type: ok ? 'success' : 'error' });
  }
  function onKey(e) {
    if (e.key === 'Escape' && selected.size && !tagFor) selected = new Set();
  }
  onMount(() => {
    if (!$grokAccounts.length) loadGrokAccounts();
    return () => clearTimeout(debounce);
  });
  const chip = (on) => `rounded-full border px-2.5 py-1 text-xs font-semibold transition ${on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}`;
</script>

<svelte:window onkeydown={onKey} />

{#snippet panel(idp)}
  <div class="mb-4">
    <input type="search" class="w-full rounded-lg border border-line bg-transparent px-3 py-1.5 text-sm"
      placeholder={'Search chats…  "phrase"  #tag'} aria-label="Search chats"
      value={qLive} oninput={(e) => onSearchInput(e.currentTarget.value)} />
  </div>
  <Collapsible title="Show">
    <div class="flex flex-wrap gap-1.5" role="group" aria-label="Archive filter">
      <button type="button" class={chip(archived === 'active')} aria-pressed={archived === 'active'} onclick={() => (archived = 'active')}>Inbox</button>
      <button type="button" class={chip(archived === 'archived')} aria-pressed={archived === 'archived'} onclick={() => (archived = 'archived')}>Archived <span class="opacity-70">{facets.archived}</span></button>
      <button type="button" class={chip(archived === 'all')} aria-pressed={archived === 'all'} onclick={() => (archived = 'all')}>All</button>
    </div>
  </Collapsible>
  <Collapsible title="Account" count={accountList.length}>
    <label class="sr-only" for={`${idp}-acct`}>Account</label>
    <select id={`${idp}-acct`} class="w-full rounded-lg border border-line bg-transparent px-2 py-1.5 text-sm" bind:value={account}>
      <option value="all">All accounts ({acctTotal.toLocaleString()})</option>
      {#each accountList as a (a.id)}<option value={a.id}>{a.label} ({a.count.toLocaleString()})</option>{/each}
    </select>
  </Collapsible>
  <Collapsible title="Date">
    <div class="flex flex-wrap gap-1.5">
      {#each QUICK as p (p.id)}
        <button type="button" class={chip(period === p.id)} aria-pressed={period === p.id} onclick={() => (period = p.id)}>{p.label}</button>
      {/each}
      {#if !QUICK.some((p) => p.id === period)}
        <button type="button" class={chip(true)} title="Clear the date filter" onclick={() => (period = 'all')}>{periodText} ✕</button>
      {/if}
    </div>
    {#if years.length}
      <div class="mt-2.5 flex flex-col gap-1">
        {#each years as y, i (y.year)}
          {@const isOpen = openYears[y.year] ?? i === 0}
          <div class="flex items-center gap-1">
            <button type="button" class="flex flex-1 items-center justify-between rounded-lg px-1.5 py-1 text-left text-sm font-semibold transition hover:bg-[var(--surface-2)]"
              aria-expanded={isOpen} onclick={() => (openYears = { ...openYears, [y.year]: !isOpen })}>
              <span class="flex items-center gap-1.5">
                <svg viewBox="0 0 24 24" class="h-3 w-3 transition-transform {isOpen ? 'rotate-90' : ''}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
                {y.year}
              </span>
              <span class="text-xs font-normal text-muted">{y.count.toLocaleString()}</span>
            </button>
            <button type="button" class="rounded px-1.5 text-[10px] font-semibold {period === 'y:' + y.year ? 'text-[var(--accent)]' : 'text-muted'}" title="Whole year" onclick={() => (period = period === 'y:' + y.year ? 'all' : 'y:' + y.year)}>year</button>
          </div>
          {#if isOpen}
            <div class="mb-1 grid grid-cols-3 gap-1 pl-4">
              {#each y.months as m (m.month)}
                {@const on = period === 'm:' + m.month}
                <button type="button" aria-pressed={on} title={`${monthLabel(m.month)} — ${m.count} chats`}
                  class="flex flex-col items-center rounded-lg border px-1 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line hover:border-[var(--accent)]'}"
                  onclick={() => (period = on ? 'all' : 'm:' + m.month)}>
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
  <Collapsible title="Sort">
    <select class="w-full rounded-lg border border-line bg-transparent px-2 py-1.5 text-sm" aria-label="Sort" bind:value={sort}>
      <option value="new">Newest first</option>
      <option value="old">Oldest first</option>
      <option value="updated">Recently updated</option>
      <option value="title">Title A–Z</option>
    </select>
  </Collapsible>
  <Collapsible title="Tags" count={facets.tags.length}>
    {#if facets.tags.length}
      <div class="flex flex-wrap gap-1.5" data-testid="chat-tag-filter">
        {#each facets.tags as t (t.name)}
          {@const on = tags.some((x) => x.toLowerCase() === t.name.toLowerCase())}
          <button type="button" class="inline-flex max-w-full items-center gap-1 rounded-full border px-2.5 py-1 text-xs font-semibold transition {on ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-[var(--chip-line)] bg-[var(--chip-bg)] text-[var(--chip-ink)] hover:border-[var(--accent)]'}"
            title={t.name} onclick={() => toggleTag(t.name)}>
            <span class="truncate">{t.name}</span> <span class="shrink-0 opacity-70">{on ? '✕' : t.count}</span>
          </button>
        {/each}
      </div>
      {#if tags.length > 1}
        <div class="mt-2 flex gap-1.5 text-xs">
          <span class="text-muted">Match</span>
          <button type="button" class={chip(tagMode === 'any')} onclick={() => (tagMode = 'any')}>any</button>
          <button type="button" class={chip(tagMode === 'all')} onclick={() => (tagMode = 'all')}>all</button>
        </div>
      {/if}
    {:else}
      <p class="text-xs text-muted">No chat tags yet — select chats and use Tag…, or add one in a chat.</p>
    {/if}
  </Collapsible>
  {#if filtered}
    <button type="button" class="text-xs font-semibold text-[var(--accent)] hover:underline" onclick={clearAll}>Clear all filters</button>
  {/if}
{/snippet}

<div class="chats-layout -mb-3 -ml-3 -mt-3 flex sm:-mb-4 sm:-ml-4 sm:-mt-4">
  <aside class="filter-rail sticky hidden w-80 shrink-0 self-start overflow-y-auto overscroll-contain border-r border-line p-4 lg:block"
    style="top: var(--topbar-h, 56px); height: calc(100dvh - var(--topbar-h, 56px))" data-testid="chats-rail">
    {@render panel('rail')}
  </aside>

  <div class="min-w-0 flex-1 p-3 sm:p-4">
    {#if open}
      <section class="chat-reader mx-auto max-w-3xl" data-testid="chat-reader">
        <div class="mb-4 flex flex-wrap items-center gap-2">
          <button type="button" class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" onclick={() => (open = null)}>← Back to chats</button>
          <a class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" href={`https://grok.com/c/${encodeURIComponent(open.id)}`} target="_blank" rel="noopener noreferrer">Open on grok.com ↗</a>
          <button type="button" class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" onclick={() => setArchived([open], !open.archived)}>{open.archived ? 'Unarchive' : 'Archive'}</button>
        </div>
        <h2 class="text-xl font-bold">{open.title || 'Untitled chat'}{#if open.archived}<span class="ml-2 rounded-full border border-line px-2 py-0.5 align-middle text-xs font-semibold text-muted">archived</span>{/if}</h2>
        <p class="mb-2 text-xs text-muted">
          {accountLabel(open.account, $grokAccounts)} · created {fmtDate(open.create_time, true)}{#if open.modify_time} · updated {fmtDate(open.modify_time, true)}{/if}
          {#if open.messages} · {open.messages.length} messages{/if}
        </p>
        <div class="mb-4 flex flex-wrap items-center gap-1.5" data-testid="reader-tags">
          {#each open.tags || [] as t (t)}
            <span class="inline-flex items-center gap-1 rounded-full border border-[var(--chip-line)] bg-[var(--chip-bg)] px-2.5 py-0.5 text-xs font-semibold text-[var(--chip-ink)]">
              {t}<button type="button" aria-label={`Remove tag ${t}`} class="opacity-70 hover:opacity-100" onclick={() => readerTagEdit({ remove: [t] })}>✕</button>
            </span>
          {/each}
          <input type="text" class="w-32 rounded-full border border-line bg-transparent px-2.5 py-0.5 text-xs" placeholder="+ add tag" aria-label="Add tag"
            list="chat-tag-names" bind:value={readerTag}
            onkeydown={(e) => { if (e.key === 'Enter' && readerTag.trim()) { readerTagEdit({ add: [readerTag.trim()] }); readerTag = ''; } }} />
          <datalist id="chat-tag-names">{#each facets.tags as t (t.name)}<option value={t.name}></option>{/each}</datalist>
        </div>
        {#if openErr}<p class="text-sm text-red-500">{openErr}</p>{/if}
        {#if open.messages}
          <ol class="flex flex-col gap-3">
            {#each open.messages as m, i (m.id + ':' + i)}
              <li id={`chat-msg-${i}`} class="chat-msg rounded-xl border border-line p-3 {m.sender === 'user' ? 'chat-user' : ''}">
                <div class="mb-1 flex items-center gap-2 text-xs text-muted">
                  <span class="font-bold uppercase tracking-wide">{m.sender === 'user' ? 'You' : m.sender === 'assistant' ? 'Grok' : m.sender}</span>
                  {#if m.time}<span>{fmtDate(m.time, true)}</span>{/if}
                  {#if m.model}<span>· {m.model}</span>{/if}
                  <button type="button" class="ml-auto rounded border border-line px-2 py-0.5 font-semibold" title="Copy this message" onclick={() => copy(m.text, m.sender === 'user' ? 'Prompt' : 'Message')}>Copy</button>
                </div>
                <div class="chat-text whitespace-pre-wrap break-words text-sm">{@html highlightHtml(m.text, q)}</div>
                {#if m.attachments?.length}
                  <div class="mt-2 flex flex-wrap gap-2">
                    {#each m.attachments as a}
                      {#if a.url}
                        <a href={a.url} target="_blank" rel="noopener noreferrer" class="text-xs underline">{a.name || (a.kind === 'generated' ? 'generated image' : 'attachment')}</a>
                      {:else}
                        <span class="text-xs text-muted" title={a.ref}>📎 {a.name || 'attachment'}</span>
                      {/if}
                    {/each}
                  </div>
                {/if}
              </li>
            {/each}
          </ol>
        {:else if !openErr}
          <p class="text-sm text-muted">Loading…</p>
        {/if}
      </section>
    {:else}
      <section class="chats-view" data-testid="chats-view">
        <!-- Narrow screens: the rail is hidden, so the same panel sits above the list. -->
        <details class="mb-3 rounded-xl border border-line p-3 lg:hidden">
          <summary class="cursor-pointer text-sm font-semibold">Filters{filtered ? ' •' : ''}</summary>
          <div class="mt-3">{@render panel('drawer')}</div>
        </details>
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <h2 class="mr-1 text-lg font-bold">Grok chats</h2>
          <p class="text-sm text-muted" data-testid="chats-total">
            {total.toLocaleString()} {archived === 'archived' ? 'archived ' : ''}chat{total === 1 ? '' : 's'}{q ? ` matching “${q}”` : ''}{period !== 'all' ? ` · ${periodText}` : ''}{account !== 'all' ? ` · ${accountLabel(account, $grokAccounts)}` : ''}{tags.length ? ` · #${tags.join(tagMode === 'all' ? ' + #' : ' / #')}` : ''}
          </p>
          {#if items.length}
            <button type="button" class="ml-auto text-xs font-semibold text-[var(--accent)] hover:underline"
              onclick={() => (selected = selected.size === items.length ? new Set() : new Set(items.map(keyOf)))}>
              {selected.size === items.length ? 'Select none' : 'Select all loaded'}
            </button>
          {/if}
        </div>
        {#if selected.size}
          <div class="sticky z-20 mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-[var(--accent)] bg-[var(--surface,var(--bg))] p-2" style="top: calc(var(--topbar-h, 56px) + 0.5rem)" data-testid="chat-selbar">
            <span class="px-1 text-sm font-semibold">{selected.size} selected</span>
            <button type="button" class="rounded-lg border border-line px-3 py-1 text-sm font-semibold" onclick={() => (tagFor = selItems)}>Tag…</button>
            {#if selItems.some((c) => !c.archived)}
              <button type="button" class="rounded-lg border border-line px-3 py-1 text-sm font-semibold" onclick={() => setArchived(selItems.filter((c) => !c.archived), true)}>Archive</button>
            {/if}
            {#if selItems.some((c) => c.archived)}
              <button type="button" class="rounded-lg border border-line px-3 py-1 text-sm font-semibold" onclick={() => setArchived(selItems.filter((c) => c.archived), false)}>Unarchive</button>
            {/if}
            <button type="button" class="ml-auto text-xs font-semibold text-muted hover:underline" onclick={() => (selected = new Set())}>Clear (Esc)</button>
          </div>
        {/if}
        {#if error}<p class="text-sm text-red-500">{error}</p>{/if}
        {#if !loading && !items.length && !error}
          <p class="text-sm text-muted">{filtered ? 'No chats match.' : 'No chats archived yet — run Sync (chat backup is on by default in Config → Automation).'}</p>
        {/if}
        {#each groups as grp (grp.key)}
          {#if grp.key}<h3 class="mb-2 mt-4 text-sm font-bold text-muted">{grp.key === 'Unknown date' ? grp.key : monthLabel(grp.key)}</h3>{/if}
          <ul class="flex flex-col gap-2">
            {#each grp.items as it (keyOf(it))}
              {@const sel = selected.has(keyOf(it))}
              <li class="flex items-stretch gap-2">
                <button type="button" role="checkbox" aria-checked={sel} aria-label={`Select ${it.title || 'chat'}`}
                  class="chat-check flex w-7 shrink-0 items-start justify-center pt-3.5" onclick={(e) => select(it, e)}>
                  <span class="flex h-4 w-4 items-center justify-center rounded border text-[10px] {sel ? 'border-transparent bg-[var(--accent)] text-[var(--on-accent)]' : 'border-line'}">{sel ? '✓' : ''}</span>
                </button>
                <button type="button" class="chat-row min-w-0 flex-1 rounded-xl border p-3 text-left hover:border-[var(--accent)] {sel ? 'border-[var(--accent)]' : 'border-line'} {it.archived ? 'opacity-70' : ''}" onclick={(e) => rowClick(it, e)}>
                  <div class="flex flex-wrap items-baseline gap-2">
                    <span class="font-semibold">{it.title || 'Untitled chat'}</span>
                    {#if it.archived}<span class="rounded-full border border-line px-1.5 text-[10px] font-semibold text-muted">archived</span>{/if}
                    {#each it.tags || [] as t (t)}<span class="rounded-full border border-[var(--chip-line)] bg-[var(--chip-bg)] px-1.5 text-[10px] font-semibold text-[var(--chip-ink)]">{t}</span>{/each}
                    <span class="text-xs text-muted">{fmtDate(it.sort_time)} · {accountLabel(it.account, $grokAccounts)} · {it.n_messages} msg{#if it.hits} · {it.hits} hit{it.hits === 1 ? '' : 's'}{/if}</span>
                  </div>
                  {#if it.snippets?.length}
                    {#each it.snippets as s}
                      <p class="mt-1 text-sm text-muted"><span class="text-xs font-bold">{s.msg_idx < 0 ? 'title' : s.sender === 'user' ? 'you' : 'grok'}:</span> {@html snippetHtml(s.snippet)}</p>
                    {/each}
                  {:else if it.first_prompt}
                    <p class="mt-1 line-clamp-2 text-sm text-muted">{it.first_prompt}</p>
                  {/if}
                </button>
              </li>
            {/each}
          </ul>
        {/each}
        {#if items.length < total}
          <div class="my-4 text-center">
            <button type="button" class="rounded-lg border border-line px-4 py-1.5 text-sm font-semibold" disabled={loading} onclick={() => load(false)}>{loading ? 'Loading…' : 'Load more'}</button>
          </div>
        {/if}
      </section>
    {/if}
  </div>
</div>

{#if tagFor}
  <ChatTagModal chats={tagFor} allTags={facets.tags} onchange={refresh} onclose={() => { tagFor = null; refresh(); }} />
{/if}

<style>
  :global(.chat-mark) { background: color-mix(in srgb, var(--accent) 35%, transparent); color: inherit; border-radius: 2px; padding: 0 1px; }
  .chat-user { background: color-mix(in srgb, var(--accent) 7%, transparent); }
</style>
