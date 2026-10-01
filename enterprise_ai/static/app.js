"use strict";

const $ = (selector) => document.querySelector(selector);
const numberFormat = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 });
let state = { entities: [], totals: [], supplier_spend: [], review_candidates: [], recent_actions: [] };
let noticeTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function safeString(value) {
  if (value === undefined || value === null) return "—";
  if (Array.isArray(value)) return value.map(safeString).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function decimalDisplay(value) {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(value));
  if (!match) return safeString(value);
  const whole = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${match[1]}${whole}.${(match[3] || "").padEnd(2, "0")}`;
}

function amount(value, currency) {
  return `${safeString(currency)} ${decimalDisplay(value)}`;
}

function notify(message, error = false) {
  const notice = $("#notification");
  window.clearTimeout(noticeTimer);
  notice.classList.toggle("error", error);
  notice.textContent = message;
  notice.hidden = false;
  noticeTimer = window.setTimeout(() => { notice.hidden = true; }, error ? 15000 : 7000);
}

async function api(path, body) {
  const options = { headers: { Accept: "application/json" } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let result;
  try { result = await response.json(); }
  catch (_) { throw new Error("The local server returned an unreadable response. Check that it is running."); }
  if (!response.ok) {
    throw new Error(safeString(result.error || result.detail || result.message || `Request failed (${response.status}).`));
  }
  return result;
}

async function busy(button, task) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Working…";
  try { await task(); }
  catch (error) { notify(error.message || "An unexpected error occurred.", true); }
  finally { button.disabled = false; button.textContent = label; }
}

function emptyRow(message) {
  const row = element("tr");
  const cell = element("td", "empty-cell", message);
  cell.colSpan = 4;
  row.append(cell);
  return row;
}

function renderState(next) {
  window.dispatchEvent(new Event("outcome:dataset"));
  state = next || {};
  const data = state.dataset || {};
  const loaded = Boolean(state.dataset);
  $("#supplier-count").textContent = loaded ? numberFormat.format(data.supplier_count || 0) : "—";
  $("#entity-count").textContent = loaded ? numberFormat.format((state.entities || []).length) : "—";
  $("#invoice-count").textContent = loaded ? numberFormat.format(data.invoice_count || 0) : "—";
  $("#review-count").textContent = loaded ? numberFormat.format((state.review_candidates || []).length) : "—";
  const warnings = $("#dataset-warnings");
  warnings.replaceChildren();
  warnings.hidden = !(state.warnings || []).length;
  if ((state.warnings || []).length) {
    warnings.append(element("strong", "", "Import notes"));
    for (const warning of state.warnings) warnings.append(element("p", "", safeString(warning)));
  }
  renderEntities();
  renderSpend();
  renderGraph();
  renderReviews(loaded);
  renderActions();
}

function renderEntities() {
  const body = $("#entities-body");
  body.replaceChildren();
  const entities = state.entities || [];
  if (!entities.length) {
    body.append(emptyRow("Load the sample dataset or import CSV files to begin."));
    return;
  }
  for (const entity of entities) {
    const row = element("tr");
    const name = element("td");
    name.append(element("strong", "", entity.display_name), element("small", "", `${safeString(entity.country)} · ${safeString(entity.entity_id)}`));
    const records = element("td");
    for (const id of entity.source_supplier_ids || []) records.append(element("span", "record-chip", id));
    if (!(entity.source_supplier_ids || []).length) records.textContent = "—";
    const basis = element("td");
    basis.append(element("small", "", safeString(entity.match_basis)));
    const identifiers = [
      (entity.registration_ids || []).length ? `Registration: ${safeString(entity.registration_ids)}` : "",
      (entity.tax_ids || []).length ? `Tax: ${safeString(entity.tax_ids)}` : "",
    ].filter(Boolean).join(" · ");
    if (identifiers) basis.title = identifiers;
    const action = element("td");
    const button = element("button", "stage-button", "Stage sync ↗");
    button.type = "button";
    button.setAttribute("aria-label", `Stage local sync for ${entity.display_name}`);
    button.addEventListener("click", () => busy(button, async () => {
      await api("/api/actions", { entity_id: entity.entity_id, operation: "sync_supplier" });
      await refresh();
      notify("Supplier sync staged. Review its payload in the action queue before approving.");
      $("#actions").scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    action.append(button);
    row.append(name, records, basis, action);
    body.append(row);
  }
}

function renderSpend() {
  const totals = $("#currency-totals");
  const bars = $("#spend-bars");
  totals.replaceChildren();
  bars.replaceChildren();
  if (!(state.totals || []).length) {
    totals.append(element("p", "empty-state", "Spend totals will appear after import."));
    return;
  }
  for (const total of state.totals || []) {
    const card = element("div", "currency-total");
    card.append(element("span", "", safeString(total.currency)), element("strong", "", decimalDisplay(total.amount)), element("small", "", `${total.invoice_count || 0} invoice records`));
    totals.append(card);
    const group = (state.supplier_spend || []).filter((item) => item.currency === total.currency).sort((a, b) => Number(b.amount) - Number(a.amount));
    if (!group.length) continue;
    bars.append(element("p", "bar-group-label", `TOP SUPPLIERS · ${safeString(total.currency)}`));
    const max = Math.max(1, ...group.map((item) => Math.abs(Number(item.amount))));
    for (const item of group.slice(0, 5)) {
      const row = element("div", "bar-row");
      const label = element("span", "bar-label", item.name || item.entity_id);
      label.title = safeString(item.name || item.entity_id);
      const bar = element("progress", "bar-track");
      bar.max = max;
      bar.value = Math.abs(Number(item.amount));
      bar.setAttribute("aria-label", `${safeString(item.name)} spend: ${amount(item.amount, item.currency)}`);
      row.append(label, bar, element("span", "bar-value", amount(item.amount, item.currency)));
      bars.append(row);
    }
  }
}

function svgElement(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function graphCategory(node) {
  if (/entity|canonical/i.test(node.type || "")) return "entity";
  if (/supplier|source|record/i.test(node.type || "")) return "source";
  return "other";
}

function renderGraph() {
  const host = $("#graph-view");
  host.replaceChildren();
  const graph = state.graph || { nodes: [], edges: [] };
  const allNodes = graph.nodes || [];
  const nodes = allNodes.slice(0, 24);
  $("#graph-count").textContent = `${allNodes.length} nodes`;
  $("#graph-note").textContent = allNodes.length > nodes.length
    ? `Showing ${nodes.length} of ${allNodes.length} nodes. The full dataset remains available to queries.`
    : "Connections make the evidence inspectable; they do not guarantee correctness.";
  if (!nodes.length) {
    host.append(element("p", "empty-state", "A source-linked graph will appear here."));
    return;
  }
  const svg = svgElement("svg", { viewBox: "0 0 520 260", class: "graph-svg", role: "img", "aria-labelledby": "graph-title graph-description" });
  const title = svgElement("title", { id: "graph-title" });
  title.textContent = "Supplier evidence relationship graph";
  const description = svgElement("desc", { id: "graph-description" });
  description.textContent = `Graph contains ${allNodes.length} nodes and ${(graph.edges || []).length} relationships. Displayed labels: ${nodes.map((node) => node.label).join(", ")}.`;
  svg.append(title, description);
  const columns = ["source", "entity", "other"];
  const positions = new Map();
  for (const [columnIndex, category] of columns.entries()) {
    const group = nodes.filter((node) => graphCategory(node) === category);
    for (const [index, node] of group.entries()) {
      positions.set(node.id, { x: 80 + columnIndex * 180, y: 25 + ((index + 0.5) / Math.max(1, group.length)) * 205 });
    }
  }
  for (const edge of graph.edges || []) {
    const from = positions.get(typeof edge.source === "object" ? edge.source.id : edge.source);
    const to = positions.get(typeof edge.target === "object" ? edge.target.id : edge.target);
    if (!from || !to) continue;
    const line = svgElement("line", { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: "graph-edge" });
    const hint = svgElement("title");
    hint.textContent = safeString(edge.relation);
    line.append(hint);
    svg.append(line);
  }
  for (const node of nodes) {
    const point = positions.get(node.id);
    const category = graphCategory(node);
    const circle = svgElement("circle", { cx: point.x, cy: point.y, r: category === "entity" ? 8 : 5, class: `graph-node graph-node-${category}` });
    const hint = svgElement("title");
    hint.textContent = `${safeString(node.label)} (${safeString(node.type)})`;
    circle.append(hint);
    const text = svgElement("text", { x: point.x, y: point.y + (category === "entity" ? 20 : 16), "text-anchor": "middle", class: "graph-text" });
    const label = safeString(node.label || node.id);
    text.textContent = label.length > 23 ? `${label.slice(0, 21)}…` : label;
    svg.append(circle, text);
  }
  host.append(svg);
}

function renderReviews(loaded) {
  const host = $("#review-list");
  host.replaceChildren();
  const candidates = state.review_candidates || [];
  if (!candidates.length) {
    host.append(element("p", "empty-state", loaded ? "No ambiguous identity candidates were flagged by the current rules. This does not prove that every identity is correct." : "Load data to check for ambiguous identities."));
    return;
  }
  for (const candidate of candidates) {
    const card = element("article", "review-item");
    const names = element("p", "review-names");
    names.append(document.createTextNode(safeString(candidate.left_name)), element("span", "", "↔"), document.createTextNode(safeString(candidate.right_name)));
    card.append(names, element("p", "review-reason", candidate.reason), element("small", "review-id", `${safeString(candidate.left_id)} / ${safeString(candidate.right_id)} · Kept separate`));
    host.append(card);
  }
}

function evidenceList(items) {
  const list = element("ul", "evidence-list");
  for (const evidence of items || []) {
    const row = element("li");
    const parts = [evidence.source, evidence.line ? `line ${evidence.line}` : "", evidence.record_id].filter(Boolean);
    row.append(element("span", "evidence-source", parts.join(" · ")), element("span", "evidence-excerpt", evidence.excerpt));
    list.append(row);
  }
  return list;
}

function renderAnswer(result) {
  const host = $("#query-result");
  host.replaceChildren();
  host.hidden = false;
  const label = element("div", "answer-label");
  label.append(element("span", "", (result.evidence || []).length ? "SOURCE-BACKED RESPONSE" : "REVIEW REQUIRED"), element("span", "pill pill-teal", safeString(result.route?.intent || "Deterministic")));
  host.append(label, element("p", "answer-text", result.answer));
  if (result.route) host.append(element("p", "routing-note", `Route: ${safeString(result.route.provider)}. ${safeString(result.route.reason)}`));
  const context = result.context || {};
  if (Number.isFinite(Number(context.selected_characters)) && Number.isFinite(Number(context.full_characters))) {
    const selected = Number(context.selected_characters);
    const full = Number(context.full_characters);
    host.append(element("p", "context-note", `${numberFormat.format(selected)} serialized evidence characters selected · ${numberFormat.format(full)} raw source characters imported. ${context.token_estimate_note || "This is a context-size estimate, not a measurement of billed tokens or model quality."}`));
  }
  if ((result.unknowns || []).length) {
    host.append(element("p", "unknowns", `Unknowns / limits: ${result.unknowns.map(safeString).join(" ")}`));
  }
  const evidence = result.evidence || [];
  const details = element("details", "evidence-details");
  details.open = true;
  details.append(element("summary", "", `Inspect source evidence (${evidence.length})`));
  if (evidence.length) details.append(evidenceList(evidence));
  else details.append(element("p", "routing-note", "No source evidence was returned for this question."));
  host.append(details);
}

function renderActions() {
  const host = $("#actions-list");
  host.replaceChildren();
  const actions = state.recent_actions || [];
  const pending = actions.filter((action) => ["pending", "approved"].includes(action.status)).length;
  $("#action-count").textContent = String(pending);
  if (!actions.length) {
    host.append(element("p", "empty-state", "Your action queue is empty. Start with “Stage sync” on a supplier."));
    return;
  }
  for (const action of actions) {
    const card = element("article", "action-item");
    const header = element("div", "action-item-header");
    const description = element("div");
    const supplier = (state.entities || []).find((entity) => entity.entity_id === action.entity_id);
    const name = action.payload?.display_name || supplier?.display_name || action.entity_id;
    description.append(element("p", "action-name", `Sync ${safeString(name)}`), element("p", "action-meta", `Local mock portal · ${safeString(action.action_id)}`));
    const buttons = element("div", "action-buttons");
    const status = safeString(action.status);
    buttons.append(element("span", `pill ${status === "executed" ? "pill-teal" : "pill-amber"}`, status));
    if (status === "pending" || status === "approved") {
      const verb = status === "pending" ? "approve" : "execute";
      const button = element("button", "button button-secondary", status === "pending" ? "Approve payload" : "Execute locally");
      button.type = "button";
      button.addEventListener("click", () => busy(button, async () => {
        await api(`/api/actions/${encodeURIComponent(action.action_id)}/${verb}`, {});
        await refresh();
        notify(verb === "approve" ? "Payload approved. Execute locally when ready." : "Supplier synced to the local mock portal.");
      }));
      buttons.append(button);
    }
    header.append(description, buttons);
    const details = element("details", "payload-details");
    details.append(element("summary", "", "Inspect payload and evidence"), element("pre", "", JSON.stringify(action.payload || {}, null, 2)));
    if ((action.evidence || []).length) details.append(evidenceList(action.evidence));
    card.append(header, details);
    host.append(card);
  }
}

async function refreshPortal() {
  const result = await api("/api/portal");
  const suppliers = result.suppliers || [];
  $("#portal-count").textContent = `${suppliers.length} supplier${suppliers.length === 1 ? "" : "s"}`;
  const host = $("#portal-content");
  host.replaceChildren();
  if (!suppliers.length) host.append(element("p", "empty-state", "No suppliers synced yet."));
  for (const supplier of suppliers) {
    const row = element("div", "portal-record");
    row.append(element("strong", "", supplier.display_name || supplier.name || supplier.entity_id || "Supplier"));
    if (supplier.stale) row.append(element("span", "pill pill-amber", "Earlier dataset — review before reuse"));
    const details = element("details", "payload-details");
    details.append(element("summary", "", "Inspect saved record"), element("pre", "", JSON.stringify(supplier, null, 2)));
    row.append(details);
    host.append(row);
  }
}

async function refresh() {
  renderState(await api("/api/state"));
  await refreshPortal();
}

$("#demo-button").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  renderState(await api("/api/demo", {}));
  $("#query-result").hidden = true;
  await refreshPortal();
  notify("Synthetic sample data loaded. Try a question or stage a supplier sync.");
}));

$("#import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const supplierFile = $("#suppliers-file").files[0];
  const spendFile = $("#spend-file").files[0];
  if (!supplierFile || !spendFile) { notify("Select both supplier and spend CSV files.", true); return; }
  if (supplierFile.size > 5000000 || spendFile.size > 5000000) { notify("Please keep each CSV file below 5 MB for this local prototype.", true); return; }
  busy(event.currentTarget.querySelector("button[type=submit]"), async () => {
    const [suppliers_csv, spend_csv] = await Promise.all([supplierFile.text(), spendFile.text()]);
    renderState(await api("/api/analyze", { suppliers_csv, spend_csv }));
    $("#query-result").hidden = true;
    await refreshPortal();
    $("#import-panel").open = false;
    notify("Data imported. Exact identity matches and spend totals are ready to inspect.");
  });
});

$("#query-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;
  busy(event.currentTarget.querySelector("button[type=submit]"), async () => {
    renderAnswer(await api("/api/query", { question, budget_tokens: Number($("#budget-tokens").value) }));
  });
});

document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => {
  $("#question").value = button.dataset.question;
  $("#question").focus();
}));

refresh().catch((error) => notify(`Unable to load workspace: ${error.message}`, true));
