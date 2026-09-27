<script>
  // Master account switch: scopes the library, search, facets, canvases and collection
  // grids to one Grok account (or the items not attributed yet). Counts come from the
  // page's facets for the current view. With a single account and nothing unattributed
  // it renders nothing — there's nothing to switch.
  import { onMount } from 'svelte';
  import Popover from './Popover.svelte';
  import {
    filters, setAccount, grokAccounts, accountCounts, loadGrokAccounts, accountLabel,
    reattributeAccounts, UNKNOWN_ACCOUNT, LOCAL_ACCOUNT
  } from '$lib/state.js';

  onMount(loadGrokAccounts);

  const countOf = $derived(new Map(($accountCounts || []).map((a) => [a.id, a.count])));
  const unknown = $derived(countOf.get(UNKNOWN_ACCOUNT) || 0);
  const local = $derived(countOf.get(LOCAL_ACCOUNT) || 0);
  const current = $derived($filters.account || 'all');
  const visible = $derived(($grokAccounts || []).length > 1 || unknown > 0 || current !== 'all');
  const label = $derived(current === 'all' ? 'All accounts' : accountLabel(current, $grokAccounts));
  const rows = $derived([
    ...($grokAccounts || []).map((a) => ({ id: a.id, name: a.name, count: countOf.get(a.id) || 0 })),
    ...(unknown ? [{ id: UNKNOWN_ACCOUNT, name: 'Unknown account', count: unknown, muted: true }] : []),
    ...(local ? [{ id: LOCAL_ACCOUNT, name: 'Local (imports & generated)', count: local, muted: true }] : [])
  ]);
</script>

{#if visible}
  <Popover align="right" title="Grok account — scope the library to one account"
    ariaLabel={`Account: ${label}`}
    triggerClass="inline-flex h-9 max-w-[11rem] items-center gap-1.5 rounded-lg border px-2.5 text-sm font-semibold transition hover:border-[var(--accent)] {current === 'all' ? 'border-line bg-[var(--surface-2)]' : 'border-[var(--accent)] bg-[var(--surface-2)] text-[var(--accent)]'}">
    {#snippet trigger()}
      <svg viewBox="0 0 24 24" class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>
      <span class="hidden truncate sm:inline">{label}</span>
    {/snippet}
    {#snippet children(close)}
      <div class="w-[16rem] max-w-[calc(100vw-1rem)] rounded-card border border-line bg-[var(--surface-solid)] p-1.5 shadow-[0_18px_44px_-14px_rgba(0,0,0,0.6)]" role="menu">
        {#each [{ id: 'all', name: 'All accounts', count: null }, ...rows] as r (r.id)}
          <button type="button" role="menuitemradio" aria-checked={current === r.id}
            class="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm font-semibold transition {current === r.id ? 'bg-[var(--accent)] text-[var(--on-accent)]' : 'hover:bg-[var(--surface-2)]'} {r.muted && current !== r.id ? 'text-muted' : ''}"
            onclick={() => { setAccount(r.id); close(); }}>
            <span class="truncate">{r.name}</span>
            {#if r.count !== null}<span class="shrink-0 text-xs opacity-70">{r.count}</span>{/if}
          </button>
        {/each}
        <div class="my-1 border-t border-line"></div>
        <button type="button" class="w-full rounded-lg px-3 py-2 text-left text-xs font-semibold text-[var(--accent)] hover:bg-[var(--surface-2)]"
          title="List every account's favorites, canvases and conversations on Grok (no downloads) and record which account each item belongs to"
          onclick={() => { close(); reattributeAccounts(); }}>Re-attribute accounts…</button>
        {#if unknown}
          <p class="px-3 pb-1.5 text-[11px] leading-snug text-muted">{unknown} item{unknown === 1 ? '' : 's'} not attributed yet — the next Sync or a re-attribute fills this in.</p>
        {/if}
      </div>
    {/snippet}
  </Popover>
{/if}
