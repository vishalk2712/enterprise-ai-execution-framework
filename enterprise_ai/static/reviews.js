/* Human labels are independent evidence, never execution approval. */
(() => {
  let offset = 0, runId = '', revision = 0;
  const node = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); return n; };
  const get = async (url, data) => {
    const r = await fetch(url, data ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)} : {});
    const result = await r.json(); if (!r.ok) throw new Error(result.error || 'Request failed'); return result;
  };
  const message = text => { document.querySelector('#learning-status').textContent = text; };
  async function loadRows() {
    const current = ++revision;
    const result = await get(`/api/evaluations?limit=25&offset=${offset}${runId ? '&run_id='+encodeURIComponent(runId) : ''}`);
    const feedback = await get('/api/feedback-summary');
    if (current !== revision) return;
    document.querySelector('#feedback-status').textContent = `${feedback.eligible_pairs} eligible reviewed pairs · ${feedback.review_events} saved decisions · ${feedback.unsure_pairs} unsure · ${feedback.conflicting_pairs.length} inconsistent pairs excluded. Models remain inactive until explicitly loaded.`;
    const host = document.querySelector('#evaluation-list'); host.replaceChildren();
    document.querySelector('#review-prev').disabled = offset === 0;
    document.querySelector('#review-next').disabled = offset + result.limit >= result.total;
    message(`${result.total} evaluations · showing ${result.total ? offset+1 : 0}–${Math.min(offset+result.limit,result.total)}. Similarity scores and optional model estimates are shown in pair details.`);
    for (const item of result.evaluations) {
      const card = node('details'); card.className = 'evaluation-card';
      card.append(node('summary', `${item.left_name} ↔ ${item.right_name} · ${item.similarity_score.toFixed(3)} · ${item.algorithmic_outcome} · Human: ${item.human_label || 'Unlabeled'}`));
      card.append(node('p', `Pair: ${item.left_id} / ${item.right_id}. ${item.near_miss ? 'Near-miss retained. ' : ''}Methods: ${item.candidate_methods.join(', ')}. Sampling probability: ${item.sampling_probability.toFixed(4)}.`));
      card.append(node('p', item.match_probability == null ? 'Uncalibrated similarity; no match probability is available.' : item.probability_status === 'model_estimate' ? `Uncalibrated classifier estimate: ${(item.match_probability*100).toFixed(1)}%. Review threshold: ${(item.model_review_threshold*100).toFixed(1)}%. Model: ${item.matching_model_id}.` : `Calibrated candidate-pair estimate: ${(item.match_probability*100).toFixed(1)}%. Model: ${item.matching_model_id || item.calibration_model_id}. Observed calibration does not guarantee precision.`));
      card.append(node('p', `Operational tier: ${item.operational_decision?.tier || 'Historical evaluation'}. ${item.operational_decision?.reasons.join('; ') || 'This run predates the corporate policy.'}`));
      const rationale = node('p','Open this pair to load its evidence rationale.'); card.append(rationale);
      card.addEventListener('toggle', async () => {
        if (card.open && !rationale.dataset.loaded) {
          rationale.dataset.loaded='1';
          try { const explanation=await get('/api/explanation?evaluation_id='+encodeURIComponent(item.evaluation_id)); rationale.textContent=explanation.text; }
          catch(error) { rationale.textContent=error.message; delete rationale.dataset.loaded; }
        }
      });
      card.append(node('pre', JSON.stringify({features:item.features, model_features:item.model_features, feature_schema:item.feature_schema, heuristic_outcome:item.heuristic_outcome, normalized_left:item.normalized_left, normalized_right:item.normalized_right, left:item.left_record, right:item.right_record, latest_decision:item.latest_decision},null,2)));
      const form = node('form'); form.className = 'review-form';
      const label = node('select'); label.setAttribute('aria-label','Human label');
      for (const value of ['Unsure','Match','NonMatch']) { const option = node('option',value); option.value=value; label.append(option); }
      if (item.human_label) label.value=item.human_label;
      const reviewer = node('input'); reviewer.placeholder='Reviewer name'; reviewer.setAttribute('aria-label','Reviewer name'); reviewer.required=true; reviewer.maxLength=100;
      if (window.outcomePrincipal) { reviewer.value=window.outcomePrincipal.id; reviewer.readOnly=true; }
      const reason = node('input'); reason.placeholder='Evidence and decision reason'; reason.setAttribute('aria-label','Decision reason'); reason.required=true; reason.maxLength=2000;
      const submit = node('button',item.latest_decision ? 'Append corrected label' : 'Save human label'); submit.type='submit'; submit.className='button button-secondary';
      form.append(label,reviewer,reason,submit);
      form.addEventListener('submit',async event => {
        event.preventDefault(); submit.disabled=true;
        try { await get('/api/reviews',{evaluation_id:item.evaluation_id,human_label:label.value,reviewer:reviewer.value,reason:reason.value,supersedes:item.latest_decision?.decision_id || null}); await loadRows(); message('Label saved to persistent history. Supplier groups and portal records were not changed.'); }
        catch(error) { message(error.message); submit.disabled=false; }
      });
      if (window.outcomeCan('review')) card.append(form);
      host.append(card);
    }
  }
  async function refreshRuns() {
    const result = await get('/api/resolution-runs');
    const select = document.querySelector('#review-run'); select.replaceChildren();
    for (const run of result.runs) {
      const option = node('option',`${run.created_at.slice(0,19)} · ${run.snapshot_id.slice(0,10)} · ${run.run_id.slice(-8)}`); option.value=run.run_id; select.append(option);
    }
    runId = result.runs[0]?.run_id || ''; offset=0; await loadRows();
  }
  document.querySelector('#review-run').addEventListener('change',event => { runId=event.target.value; offset=0; loadRows().catch(e=>message(e.message)); });
  document.querySelector('#review-prev').addEventListener('click',()=>{offset=Math.max(0,offset-25);loadRows().catch(e=>message(e.message));});
  document.querySelector('#review-next').addEventListener('click',()=>{offset+=25;loadRows().catch(e=>message(e.message));});
  document.querySelector('#review-refresh').addEventListener('click',()=>refreshRuns().catch(e=>message(e.message)));
  document.querySelector('#knowledge-form').addEventListener('submit', async event => {
    event.preventDefault(); const host=document.querySelector('#knowledge-result'); host.replaceChildren();
    try {
      const result=await get('/api/graph?node_id='+encodeURIComponent('supplier:'+document.querySelector('#knowledge-supplier').value.trim())+'&hops=2&limit=60');
      host.append(node('p',`${result.nodes.length} nodes · ${result.edges.length} relationships${result.truncated ? ' · bounded results truncated' : ''}. ${result.note}`));
      const names=new Map(result.nodes.map(n=>[n.id,n.label])); const list=node('ul');
      for (const edge of result.edges) list.append(node('li',`${names.get(edge.source)} → ${edge.relation.replaceAll('_',' ').toLowerCase()} → ${names.get(edge.target)}`));
      host.append(list);
    } catch(error) { host.append(node('p',error.message)); }
  });
  window.addEventListener('outcome:dataset',()=>refreshRuns().catch(e=>message(e.message)));
  window.outcomeAccessReady.then(()=>{if(window.outcomeCan('read')) return refreshRuns();}).catch(e=>message(e.message));
})();
