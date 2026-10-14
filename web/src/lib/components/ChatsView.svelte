<script>
  // Archived Grok chats (normal text conversations backed up by grokchats.py during Sync).
  // List + full-text search (highlighted snippets) with account / Date (month-year) filters,
  // and a reader for one conversation.
  import { onMount } from 'svelte';
  import { fetchChats, fetchChatMonths, fetchChat } from '$lib/api.js';
  import { grokAccounts, accountLabel, PERIODS, periodLabel, monthLabel } from '$lib/state.js';
  import { copyText } from '$lib/clipboard.js';
  import { toast } from '$lib/toast.js';

  const PAGE = 50;
  let q = $state('');
  let qLive = $state('');
  let account = $state('all');
  let period = $state('all');
  let sort = $state('new');
  let items = $state([]);
  let total = $state(0);
  let page = $state(1);
  let loading = $state(false);
  let error = $state('');
  let hl = ['\u0001', '\u0002'];
  let months = $state([]);
  let dateOpen = $state(false);
  let pickYear = $state(String(new Date().getFullYear()));
  let open = $state(null); // loaded conversation doc
  let openErr = $state('');
  let reqSeq = 0;

  const years = $derived([...new Set(months.map((m) => m.month.slice(0, 4)))]);
  const monthCount = $derived(Object.fromEntries(months.map((m) => [m.month, m.count])));

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }
  function snippetHtml(s) {
    return esc(s).split(hl[0]).join('<mark class="chat-mark">').split(hl[1]).join('</mark>');
  }
  // Highlight the query words inside full message text (reader).
  function highlightHtml(text, query) {
    const words = (query.match(/\w+/gu) || []).filter((w) => w.length > 1);
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
      const res = await fetchChats({ q, account, period, sort, page: nextPage, pageSize: PAGE });
      if (seq !== reqSeq) return;
      if (res.hl) hl = res.hl;
      items = reset ? res.items : [...items, ...res.items];
      total = res.total;
      page = nextPage;
    } catch (e) {
      if (seq === reqSeq) error = String(e.message || e);
    } finally {
      if (seq === reqSeq) loading = false;
    }
  }
  async function loadMonths() {
    try { months = (await fetchChatMonths(account)).months || []; } catch { months = []; }
    if (years.length && !years.includes(pickYear)) pickYear = years[0];
  }

  let debounce;
  function onSearchInput(v) {
    qLive = v;
    clearTimeout(debounce);
    debounce = setTimeout(() => { q = v.trim(); }, 250);
  }
  // Refetch whenever a filter changes.
  $effect(() => {
    void q; void account; void period; void sort;
    load(true);
  });
  $effect(() => { void account; loadMonths(); });

  async function openChat(it) {
    openErr = '';
    open = { id: it.id, account: it.account, title: it.title, messages: null };
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
  function setPeriodTo(p) { period = p; dateOpen = false; }
  onMount(() => () => clearTimeout(debounce));
</script>

{#if open}
  <section class="chat-reader mx-auto max-w-3xl" data-testid="chat-reader">
    <div class="mb-4 flex flex-wrap items-center gap-2">
      <button type="button" class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" onclick={() => (open = null)}>← Back to chats</button>
      <a class="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold" href={`https://grok.com/c/${encodeURIComponent(open.id)}`} target="_blank" rel="noopener noreferrer">Open on grok.com ↗</a>
    </div>
    <h2 class="text-xl font-bold">{open.title || 'Untitled chat'}</h2>
    <p class="mb-4 text-xs text-muted">
      {accountLabel(open.account, $grokAccounts)} · created {fmtDate(open.create_time, true)}{#if open.modify_time} · updated {fmtDate(open.modify_time, true)}{/if}
      {#if open.messages} · {open.messages.length} messages{/if}
    </p>
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
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <h2 class="mr-2 text-lg font-bold">Grok chats</h2>
      <input type="search" class="min-w-[14rem] flex-1 rounded-lg border border-line bg-transparent px-3 py-1.5 text-sm"
        placeholder={'Search titles and messages… ("exact phrase" works)'} aria-label="Search chats"
        value={qLive} oninput={(e) => onSearchInput(e.currentTarget.value)} />
      <select class="rounded-lg border border-line bg-transparent px-2 py-1.5 text-sm" aria-label="Account" bind:value={account}>
        <option value="all">All accounts</option>
        {#each $grokAccounts as a (a.id)}<option value={a.id}>{a.name}</option>{/each}
      </select>
      <div class="relative">
        <button type="button" class="rounded-lg border px-3 py-1.5 text-sm font-semibold {period !== 'all' ? 'border-[var(--accent)]' : 'border-line'}"
          aria-haspopup="true" aria-expanded={dateOpen} onclick={() => (dateOpen = !dateOpen)}>
          Date: {period.startsWith('y:') ? period.slice(2) : periodLabel(period)} ▾
        </button>
        {#if dateOpen}
          <div class="chat-date-pop absolute right-0 z-30 mt-1 w-80 rounded-xl border border-line p-3 shadow-lg" role="dialog" aria-label="Date filter">
            <div class="mb-2 flex flex-wrap gap-1">
              {#each PERIODS.filter((p) => !p.id.startsWith('hour')) as p (p.id)}
                <button type="button" class="rounded border px-2 py-0.5 text-xs {period === p.id ? 'border-[var(--accent)] font-bold' : 'border-line'}" onclick={() => setPeriodTo(p.id)}>{p.label}</button>
              {/each}
            </div>
            {#if years.length}
              <div class="mb-2 flex items-center gap-2 text-sm">
                <select class="rounded border border-line bg-transparent px-1 py-0.5" aria-label="Year" bind:value={pickYear}>
                  {#each years as y}<option value={y}>{y}</option>{/each}
                </select>
                <button type="button" class="rounded border px-2 py-0.5 text-xs {period === 'y:' + pickYear ? 'border-[var(--accent)] font-bold' : 'border-line'}" onclick={() => setPeriodTo('y:' + pickYear)}>Whole year</button>
              </div>
              <div class="grid grid-cols-4 gap-1">
                {#each Array.from({ length: 12 }, (_, i) => `${pickYear}-${String(i + 1).padStart(2, '0')}`) as ym}
                  <button type="button" disabled={!monthCount[ym]}
                    class="rounded border px-1 py-1 text-xs disabled:opacity-30 {period === 'm:' + ym ? 'border-[var(--accent)] font-bold' : 'border-line'}"
                    onclick={() => setPeriodTo('m:' + ym)}>
                    {monthLabel(ym, 'short')}<span class="block text-[10px] text-muted">{monthCount[ym] || ''}</span>
                  </button>
                {/each}
              </div>
            {:else}
              <p class="text-xs text-muted">No dated chats yet.</p>
            {/if}
          </div>
        {/if}
      </div>
      <select class="rounded-lg border border-line bg-transparent px-2 py-1.5 text-sm" aria-label="Sort" bind:value={sort}>
        <option value="new">Newest first</option>
        <option value="old">Oldest first</option>
        <option value="updated">Recently updated</option>
        <option value="title">Title A–Z</option>
      </select>
    </div>
    <p class="mb-3 text-sm text-muted" data-testid="chats-total">
      {total.toLocaleString()} chat{total === 1 ? '' : 's'}{q ? ` matching “${q}”` : ''}{period !== 'all' ? ` · ${period.startsWith('y:') ? period.slice(2) : periodLabel(period)}` : ''}
    </p>
    {#if error}<p class="text-sm text-red-500">{error}</p>{/if}
    {#if !loading && !items.length && !error}
      <p class="text-sm text-muted">{q || period !== 'all' || account !== 'all' ? 'No chats match.' : 'No chats archived yet — run Sync (chat backup is on by default in Config → Automation).'}</p>
    {/if}
    {#each groups as grp (grp.key)}
      {#if grp.key}<h3 class="mb-2 mt-4 text-sm font-bold text-muted">{grp.key === 'Unknown date' ? grp.key : monthLabel(grp.key)}</h3>{/if}
      <ul class="flex flex-col gap-2">
        {#each grp.items as it (it.account + '/' + it.id)}
          <li>
            <button type="button" class="chat-row w-full rounded-xl border border-line p-3 text-left hover:border-[var(--accent)]" onclick={() => openChat(it)}>
              <div class="flex flex-wrap items-baseline gap-2">
                <span class="font-semibold">{it.title || 'Untitled chat'}</span>
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

<style>
  :global(.chat-mark) { background: color-mix(in srgb, var(--accent) 35%, transparent); color: inherit; border-radius: 2px; padding: 0 1px; }
  .chat-user { background: color-mix(in srgb, var(--accent) 7%, transparent); }
  .chat-date-pop { background: var(--bg, var(--surface, #111)); }
</style>
