/* Role-gated read-only dossiers. Never interpolate source values as HTML. */
(() => {
  let offset = 0, timer, revision = 0;
  const node = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); return n; };
  async function refresh() {
    clearTimeout(timer);
    const host = document.querySelector('#risks');
    host.hidden = !window.outcomeCan('investigate');
    if (host.hidden) return;
    const current = ++revision;
    try {
      const response = await fetch(`/api/risk-dossiers?limit=25&offset=${offset}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Risk dossiers unavailable');
      if (current !== revision) return;
      const status = document.querySelector('#risk-status');
      status.textContent = result.monitor_error || `${result.scan.count || 0} active signals · ${result.total} retained dossiers · scanned ${result.scan.scanned_at || 'not yet'}. ${result.scan.insufficient_or_partial_month_baselines || 0} monthly comparisons lacked a complete observed baseline. ${result.scan.bank_observed_current_records || 0} current supplier records have bank-link evidence. ${result.scan.coverage}`;
      document.querySelector('#risk-prev').disabled = offset === 0;
      document.querySelector('#risk-next').disabled = offset + result.limit >= result.total;
      host.querySelector('a').href = `/api/risk-export?limit=25&offset=${offset}`;
      const list = document.querySelector('#risk-list'); list.replaceChildren();
      if (!result.dossiers.length) list.append(node('p', 'No risk dossiers on this page. Sparse history is not evidence of safety.'));
      for (const item of result.dossiers) {
        const card = node('details'); card.className = 'evaluation-card';
        card.append(node('summary', `${item.title} · ${item.active_signal ? 'Active signal' : 'Historical observation'}`));
        card.append(node('p', item.recommendation), node('p', `Dossier ${item.dossier_id} · snapshot ${item.snapshot}`));
        card.append(node('pre', JSON.stringify(item.evidence, null, 2)), node('p', item.limitations));
        list.append(card);
      }
    } catch (error) { document.querySelector('#risk-status').textContent = error.message; }
    timer = setTimeout(refresh, 30000);
  }
  document.querySelector('#risk-prev').addEventListener('click', () => { offset = Math.max(0, offset - 25); refresh(); });
  document.querySelector('#risk-next').addEventListener('click', () => { offset += 25; refresh(); });
  window.addEventListener('outcome:dataset', () => { offset = 0; refresh(); });
  window.outcomeAccessReady.then(refresh);
})();
