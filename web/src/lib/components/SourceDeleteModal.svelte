<script>
  // Confirm + run a "Delete on grok.com" job. Preview lists accounts and counts; for >10
  // items the user must type DELETE. Progress polls /api/source-delete/<job>. Local copies
  // stay; only the source account loses the item.
  import Modal from './Modal.svelte';
  import Button from './Button.svelte';
  import { previewSourceDelete, startSourceDelete, sourceDeleteStatus, cancelSourceDelete } from '$lib/api.js';
  import { toast } from '$lib/toast.js';

  let { kind = 'media', items = [], onclose = () => {}, ondone = () => {} } = $props();
  let preview = $state(null);
  let err = $state('');
  let typed = $state('');
  let jobId = $state('');
  let job = $state(null);
  let busy = $state(false);

  $effect(() => {
    previewSourceDelete(kind, items).then((p) => { preview = p; }).catch((e) => { err = String(e.message || e); });
  });

  const canStart = $derived(!!preview?.enabled && !!preview?.count &&
    (preview.type_confirm ? typed.trim() === 'DELETE' : true) && !busy && !jobId);

  async function start() {
    if (!canStart) return;
    busy = true; err = '';
    try {
      const r = await startSourceDelete(kind, items, preview.type_confirm ? 'DELETE' : true);
      jobId = r.job;
      poll();
    } catch (e) {
      err = String(e.message || e); busy = false;
    }
  }
  async function poll() {
    try {
      job = await sourceDeleteStatus(jobId);
      if (job.running) { setTimeout(poll, 600); return; }
      busy = false;
      const ok = job.succeeded || 0, fail = job.failed || 0;
      toast(fail ? `Deleted ${ok} on grok.com, ${fail} failed` : `Deleted ${ok} on grok.com`, { type: fail ? 'error' : 'success' });
      // Keep the modal open so the per-item log is readable; ondone runs when they Close.
    } catch (e) {
      err = String(e.message || e); busy = false;
    }
  }
</script>

<Modal {onclose} ariaLabel="Delete on grok.com" panelClass="panel flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-card">
  <div class="border-b border-line p-4">
    <h2 class="text-lg font-bold">Delete on grok.com</h2>
    <p class="mt-1 text-sm text-muted">Removes {kind === 'chat' ? 'these chats' : 'these Imagine posts'} from the Grok account. Your local backup stays. <span class="font-bold text-[var(--danger-ink,#f66)]">This cannot be undone on grok.com.</span></p>
  </div>
  <div class="flex-1 overflow-y-auto p-4 text-sm" data-testid="srcdel-body">
    {#if err}<p class="mb-2 text-[var(--danger-ink,#f66)]">{err}</p>{/if}
    {#if !preview}
      <p class="text-muted">Checking…</p>
    {:else if !preview.enabled}
      <p class="rounded-lg border border-line p-3">This is disabled. Turn on <b>Config → Automation → Allow deleting on grok.com</b> first.</p>
    {:else}
      <p class="mb-2 font-semibold">{preview.count} {kind === 'chat' ? 'chat' : 'item'}{preview.count === 1 ? '' : 's'} across {preview.accounts.length} account{preview.accounts.length === 1 ? '' : 's'}:</p>
      <ul class="mb-3 list-inside list-disc text-muted">
        {#each preview.accounts as a}<li>{a.name} ({a.count})</li>{/each}
        {#if preview.unresolved}<li class="text-[var(--danger-ink,#f66)]">{preview.unresolved} with no known Grok account — will be skipped</li>{/if}
      </ul>
      {#if preview.type_confirm && !jobId}
        <label class="block text-sm">Type <b>DELETE</b> to confirm
          <input class="mt-1 w-full rounded-lg border border-line bg-transparent px-3 py-1.5" aria-label="Type DELETE to confirm" autocomplete="off" bind:value={typed} />
        </label>
      {/if}
      {#if job}
        <div class="mt-3 rounded-lg border border-line p-3" data-testid="srcdel-progress">
          <p class="font-semibold">{job.running ? 'Deleting…' : 'Done'} — {job.done}/{job.total} (ok {job.succeeded}, failed {job.failed})</p>
          <ul class="mt-2 max-h-48 overflow-y-auto text-xs text-muted">
            {#each job.results || [] as r}
              <li class={r.ok ? '' : 'text-[var(--danger-ink,#f66)]'}>{r.id.slice(0, 8)}… {r.ok ? (r.already ? 'already gone' : 'deleted') : (r.error || 'failed')}{#if r.account} · {r.account}{/if}</li>
            {/each}
          </ul>
        </div>
      {/if}
    {/if}
  </div>
  <div class="flex justify-end gap-2 border-t border-line p-3">
    {#if job?.running}
      <Button variant="secondary" onclick={() => cancelSourceDelete(jobId)}>Cancel remaining</Button>
    {:else if job && !job.running}
      <Button variant="secondary" onclick={() => { ondone(job); onclose(); }}>Close</Button>
    {:else}
      <Button variant="secondary" onclick={onclose}>Cancel</Button>
      <Button variant="danger" disabled={!canStart} onclick={start} data-testid="srcdel-go">Delete on grok.com</Button>
    {/if}
  </div>
</Modal>
