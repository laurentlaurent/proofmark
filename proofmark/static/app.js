"use strict";

/* Proofmark front end. No framework and no build step: the whole working
   state lives in `state`, and the server is stateless. */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const FIELDS = [
  ["availability", "Availability", "Platforms, versions, regions, dates"],
  ["how_it_works", "How it works", "Steps from the player's point of view"],
  ["limits", "Limits and rules", "Minimums, maximums, fees"],
  ["support_notes", "Support notes", "Edge cases and troubleshooting"],
  ["ui_text", "Words on screen", "Exact labels and messages"],
  ["settings", "Settings", "Options players can change"],
  ["value_to_user", "Why it matters", "Benefits for players"],
  ["audience", "Who it's for", "Players or groups who get it"],
  ["known_issues", "Known issues", "Limitations players may hit"],
];
const GLYPHS = { pass: "\u2713", warn: "!", fail: "\u00d7", info: "i" };
const MAX_SCREENSHOTS = 4;
const MAX_IMAGE_SIDE = 1280;
const STORAGE_KEY = "proofmark.model";
const REVIEWER_KEY = "proofmark.reviewer";

const state = {
  config: null,
  llm: { provider: "demo", model: "", api_key: "", base_url: "", vision: false },
  screenshots: [],
  facts: null,
  kbMatches: [],
  versionDiff: "",
  numberIssues: [],
  warnings: [],
  extractMeta: {},
  drafts: {},
  pending: {},
  order: [],
  active: null,
  view: "preview",
  reviewer: "",
};

/* ---------- small helpers ---------- */

const esc = (value) => String(value ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const lines = (text) => text.split("\n").map((l) => l.replace(/^\s*(?:[-*\u2022]|\d+[.)])\s+/, "").trim()).filter(Boolean);
const docLabel = (type) => state.config?.doc_types?.[type]?.label ?? type;
const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const scrollToEl = (el) => el.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("Can't reach the Proofmark server. Is it still running?");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const data = await response.json();
      if (data.error) message = data.error;
      else if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = "Invalid request: " + data.detail.map((d) => d.msg).join("; ");
    } catch { /* keep the generic message */ }
    throw new Error(message);
  }
  return response;
}
const apiJson = async (path, body) => (await api(path, body)).json();

function showError(message) {
  $("#error-text").textContent = message;
  $("#error").hidden = false;
  scrollToEl($("#error"));
}
function clearError() { $("#error").hidden = true; }

let toastTimer;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (label) button.textContent = label;
}

function setStep(step) {
  $$("#steps li").forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle("current", n === step);
    li.classList.toggle("done", n < step);
    li.querySelector(".step-num").textContent = n < step ? "\u2713" : String(n);
    if (n === step) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function sha256(text) {
  if (!window.crypto || !window.crypto.subtle) return "";
  const digest = await window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---------- request payloads ---------- */

function sources({ withImages = true } = {}) {
  return {
    spec: $("#spec").value,
    previous_spec: $("#previous_spec").value,
    code_diff: $("#code_diff").value,
    screenshots: withImages ? state.screenshots.map((s) => s.url) : [],
  };
}
function docSettings() {
  return { product: $("#product").value.trim() || "Blitz", tone: $("#tone").value, language: $("#language").value };
}
function llmChoice() {
  const l = state.llm;
  return { provider: l.provider, model: l.model || null, api_key: l.api_key || null, base_url: l.base_url || null, vision: l.vision };
}
const isDemo = () => state.llm.provider === "demo";

/* ---------- model settings ---------- */

function providerPreset(id) { return state.config.providers.find((p) => p.id === id); }

function updateModelButton() {
  const preset = providerPreset(state.llm.provider);
  $("#model-label").textContent = isDemo() ? "Demo mode, no AI" : `${state.llm.model || preset.model} (${preset.label.split(" (")[0]})`;
  $("#model-dot").classList.toggle("ai", !isDemo());
  $("#demo-notice").hidden = !isDemo();
  const factCheck = $("#fact-check");
  if (factCheck) { factCheck.disabled = isDemo(); factCheck.checked = !isDemo(); }
}

function fillModelForm(providerId, keepValues = false) {
  const preset = providerPreset(providerId);
  $("#model-error").hidden = true;
  $("#provider").value = providerId;
  const keyNote = preset.needs_key
    ? (preset.has_env_key ? ` A key was found in .env (${preset.key_env}).` : ` Needs ${preset.key_env} in .env, or paste a key below.`)
    : "";
  $("#provider-help").innerHTML = esc(preset.help + keyNote) +
    (preset.key_url ? ` <a href="${esc(preset.key_url)}" target="_blank" rel="noopener">Get a free key</a>` : "");
  const same = keepValues && state.llm.provider === providerId;
  $("#model").value = same ? (state.llm.model || preset.model) : preset.model;
  $("#base_url").value = same ? (state.llm.base_url || preset.base_url) : preset.base_url;
  $("#api_key").value = same ? state.llm.api_key : "";
  $("#vision").checked = same ? state.llm.vision : preset.vision;
  $("#api_key").placeholder = preset.has_env_key ? "Leave empty to use the key from .env" : "Paste your API key";
  const demo = providerId === "demo";
  $("#model-row").hidden = demo;
  $("#base-url-row").hidden = demo || providerId === "gemini" || providerId === "groq";
  $("#key-row").hidden = demo || providerId === "ollama";
  $("#vision-row").hidden = demo;
}

function openModelDialog() {
  fillModelForm(state.llm.provider, true);
  $("#model-dialog").showModal();
}

function saveModel(event) {
  event.preventDefault();
  const provider = $("#provider").value;
  const preset = providerPreset(provider);
  const model = $("#model").value.trim();
  const apiKey = $("#api_key").value.trim();
  if (preset.needs_key && !preset.has_env_key && !apiKey) {
    $("#model-error").textContent = `${preset.label} needs an API key. Paste one, or add ${preset.key_env} to .env and restart.`;
    $("#model-error").hidden = false;
    $("#api_key").focus();
    return;
  }
  state.llm = { provider, model, api_key: apiKey, base_url: $("#base_url").value.trim(), vision: $("#vision").checked };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ provider, model, base_url: state.llm.base_url, vision: state.llm.vision }));
  } catch { /* private mode: fine */ }
  $("#model-dialog").close();
  updateModelButton();
  toast(provider === "demo" ? "Demo mode: template drafts, no AI." : `Using ${model || preset.model}.`);
}

function restoreModel() {
  const fallback = state.config.default_provider;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); } catch { saved = null; }
  const preset = saved && providerPreset(saved.provider);
  if (preset && (!preset.needs_key || preset.has_env_key)) {
    state.llm = { provider: saved.provider, model: saved.model || "", api_key: "", base_url: saved.base_url || "", vision: !!saved.vision };
  } else {
    const p = providerPreset(fallback);
    state.llm = { provider: fallback, model: p.model, api_key: "", base_url: p.base_url, vision: p.vision };
  }
  $("#provider").innerHTML = state.config.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
  updateModelButton();
}

/* ---------- sources ---------- */

async function loadSample(id) {
  clearError();
  try {
    const sample = await apiJson(`/api/samples/${encodeURIComponent(id)}`);
    $("#spec").value = sample.sources.spec;
    $("#previous_spec").value = sample.sources.previous_spec;
    $("#code_diff").value = sample.sources.code_diff;
    $("#previous-wrap").open = !!sample.sources.previous_spec;
    $("#diff-wrap").open = !!sample.sources.code_diff;
    state.screenshots = [];
    for (const [i, url] of sample.sources.screenshots.entries()) {
      state.screenshots.push({ name: `sample-${i + 1}`, url: await downscale(url) });
    }
    $("#shots-wrap").open = state.screenshots.length > 0;
    renderThumbs();
    const settings = sample.settings || {};
    if (settings.product) $("#product").value = settings.product;
    if (settings.tone) $("#tone").value = settings.tone;
    if (settings.language) $("#language").value = settings.language;
    toast(`Loaded the ${sample.title} sample.`);
  } catch (error) {
    showError(error.message);
  }
}

function readFile(file, asDataUrl = false) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    if (asDataUrl) reader.readAsDataURL(file); else reader.readAsText(file);
  });
}

function downscale(dataUrl) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(img.width, img.height));
      if (scale === 1) { resolve(dataUrl); return; }
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      resolve(canvas.toDataURL("image/jpeg", 0.88));
    };
    img.onerror = () => resolve(dataUrl);
    img.src = dataUrl;
  });
}

async function addScreenshots(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  for (const file of images) {
    if (state.screenshots.length >= MAX_SCREENSHOTS) {
      toast(`Up to ${MAX_SCREENSHOTS} screenshots.`);
      break;
    }
    state.screenshots.push({ name: file.name, url: await downscale(await readFile(file, true)) });
  }
  renderThumbs();
}

function renderThumbs() {
  $("#thumbs").innerHTML = state.screenshots.map((shot, i) => `
    <li><img src="${esc(shot.url)}" alt="Screenshot ${i + 1}: ${esc(shot.name)}">
      <button type="button" data-remove-shot="${i}" aria-label="Remove ${esc(shot.name)}">&times;</button></li>`).join("");
}

/* ---------- step 2: facts ---------- */

async function extractFacts() {
  if (!$("#spec").value.trim()) {
    showError("Add a feature spec first: paste it, upload a file, or load a sample.");
    $("#spec").focus();
    return;
  }
  const button = $("#extract-btn");
  clearError();
  setBusy(button, true, "Extracting facts\u2026");
  $("#extract-status").textContent = isDemo()
    ? "Reading the spec with rules (demo mode)."
    : "Reading the sources and looking for gaps. This takes 5 to 30 seconds.";
  try {
    const data = await apiJson("/api/extract", { sources: sources(), settings: docSettings(), llm: llmChoice() });
    Object.assign(state, {
      facts: data.facts, kbMatches: data.kb_matches, versionDiff: data.version_diff,
      numberIssues: data.number_issues, warnings: data.warnings, extractMeta: data.meta,
      drafts: {}, pending: {}, order: [], active: null,
    });
    $("#empty").hidden = true;
    $("#drafts").hidden = true;
    renderFacts();
    setStep(2);
    $("#extract-status").textContent = "";
    scrollToEl($("#facts"));
  } catch (error) {
    $("#extract-status").textContent = "";
    showError(error.message);
  } finally {
    setBusy(button, false, "Extract facts");
  }
}

function changeLine(change) {
  const kind = change.kind.charAt(0).toUpperCase() + change.kind.slice(1);
  return `${kind}: ${change.description}`;
}
function parseChange(line) {
  const match = line.match(/^(added|changed|removed|fixed)\s*:\s*(.+)$/i);
  return match ? { kind: match[1].toLowerCase(), description: match[2].trim() } : { kind: "changed", description: line };
}

function readFacts() {
  const facts = structuredClone(state.facts);
  facts.feature_name = $("#f-feature_name").value.trim() || "Untitled feature";
  facts.one_liner = $("#f-one_liner").value.trim();
  for (const [key] of FIELDS) facts[key] = lines($(`#f-${key}`).value);
  facts.changes = lines($("#f-changes").value).map(parseChange);
  facts.internal_terms = lines($("#f-internal_terms").value);
  return facts;
}

function textareaFor(key, items, rows) {
  return `<textarea id="f-${key}" rows="${Math.max(rows, Math.min(items.length + 1, 9))}">${esc(items.join("\n"))}</textarea>`;
}

function renderFacts() {
  const f = state.facts;
  const meta = state.extractMeta;
  const hasPrevious = !!$("#previous_spec").value.trim();
  const readBy = meta.provider === "demo" ? "Read with rules (demo mode)" : `Read by ${esc(meta.model)}`;
  const images = meta.images_used ? `, including ${meta.images_used} screenshot${meta.images_used > 1 ? "s" : ""}` : "";

  const fieldBlocks = FIELDS.map(([key, label, hint]) => `
    <div class="fact-field">
      <label for="f-${key}">${esc(label)}</label>
      <p class="hint">${esc(hint)}. One per line.</p>
      ${textareaFor(key, f[key], 3)}
    </div>`).join("");

  const warnings = [
    ...state.warnings.map((w) => `<div class="notice warn"><p>${esc(w)}</p></div>`),
    state.numberIssues.length ? `<div class="notice warn"><div><p>These numbers in the fact sheet don't appear in the sources. Check them before drafting:</p>
      <ul class="numbers-list">${state.numberIssues.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div></div>` : "",
  ].join("");

  const questions = f.open_questions.length ? `
    <ol class="questions">${f.open_questions.map((q, i) => `
      <li class="question" data-index="${i}">
        <p><span class="highlight">${esc(q)}</span></p>
        <textarea rows="2" aria-label="Answer to: ${esc(q)}" placeholder="Answer, if you know it"></textarea>
        <div class="question-actions"><button type="button" class="link-button" data-dismiss="${i}">Not relevant</button></div>
      </li>`).join("")}</ol>
    <button type="button" class="button secondary small" id="apply-answers">Add answers to the fact sheet</button>`
    : `<p class="hint">No open questions. Every draft can rely on the fact sheet alone.</p>`;

  const confirmed = f.clarifications.length ? `
    <div class="fact-field wide"><h3>Confirmed answers</h3>
      <ul class="confirmed">${f.clarifications.map((c, i) => `
        <li><div><strong>${esc(c.question)}</strong>${esc(c.answer)}</div>
          <button type="button" class="link-button" data-unconfirm="${i}">Remove</button></li>`).join("")}</ul></div>` : "";

  const kbList = state.kbMatches.length ? `<ul class="kb-list">${state.kbMatches.map((m) => `
      <li class="kb-item"><strong>${esc(m.title)}</strong>
        ${m.excerpt ? `<blockquote>${esc(m.excerpt)}</blockquote>` : ""}
        <span class="kb-meta">${m.score >= 0.3 ? "Strong match" : "Possible match"} on ${esc(m.terms.slice(0, 4).join(", "))}. ${esc(m.path)}</span></li>`).join("")}</ul>`
    : `<p class="hint">No existing article seems affected. ${state.config.kb_articles} articles checked.</p>`;

  const diff = state.versionDiff ? `
    <details class="spec-diff"><summary>Spec changes since the previous version</summary>
      <pre class="diff">${state.versionDiff.split("\n").map((line) => {
        const cls = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
        return `<span class="${cls}">${esc(line) || " "}</span>`;
      }).join("")}</pre></details>` : "";

  const docOptions = Object.entries(state.config.doc_types).map(([type, meta]) => {
    const needsPrevious = type === "whats_changed" && !(f.changes.length || hasPrevious);
    const checked = !needsPrevious && (type !== "whats_changed" || hasPrevious);
    return `<label class="doc-option"><input type="checkbox" value="${type}" ${checked ? "checked" : ""} ${needsPrevious ? "disabled" : ""}>
      <span>${esc(meta.label)}<small>${esc(needsPrevious ? "Needs a previous version" : meta.blurb)}</small></span></label>`;
  }).join("");

  $("#facts").innerHTML = `
    <header class="section-head">
      <div><h2 id="facts-title">Fact sheet</h2>
        <p class="hint">The single source of truth for every draft. Fix anything wrong here, once.</p></div>
      <p class="meta">${readBy}${images} in ${meta.seconds} seconds.</p>
    </header>
    ${warnings}
    <div class="facts-body">
      <div class="panel">
        <label class="fact-name">Feature name<input id="f-feature_name" value="${esc(f.feature_name)}"></label>
        <label class="fact-summary" for="f-one_liner">In one sentence</label>
        <textarea id="f-one_liner" rows="2">${esc(f.one_liner)}</textarea>
        <div class="fact-grid">
          ${fieldBlocks}
          <div class="fact-field wide">
            <label for="f-changes">Changes from the previous version</label>
            <p class="hint">Start each line with Added, Changed, Removed or Fixed.</p>
            ${textareaFor("changes", f.changes.map(changeLine), 2)}
          </div>
          <div class="fact-field wide">
            <label for="f-internal_terms">Internal terms</label>
            <p class="hint">Code names, tickets and flags. Flagged if they appear in player-facing drafts.</p>
            ${textareaFor("internal_terms", f.internal_terms, 2)}
          </div>
          ${confirmed}
        </div>
      </div>
      <div class="side">
        <section class="panel" aria-labelledby="gaps-title">
          <h3 id="gaps-title">Questions the sources don't answer</h3>
          <p class="hint">Players will ask these. Answer what you know and it becomes a fact; drafts leave the rest out.</p>
          ${questions}
        </section>
        <section class="panel" aria-labelledby="kb-title">
          <h3 id="kb-title">Help Centre articles to review</h3>
          <p class="hint">Existing articles that mention this feature's topics may now be out of date.</p>
          ${kbList}
        </section>
      </div>
    </div>
    ${diff}
    <section class="panel draft-picker" aria-labelledby="picker-title">
      <h3 id="picker-title">Draft these documents</h3>
      <p class="hint">Each draft is written from the fact sheet above, not from the raw spec.</p>
      <div class="doc-options">${docOptions}</div>
      <div class="picker-actions">
        <label class="check"><input type="checkbox" id="fact-check" ${isDemo() ? "disabled" : "checked"}>
          Fact-check each draft with a second AI pass${isDemo() ? " (needs an AI model)" : ""}</label>
        <button type="button" class="button primary" id="draft-btn">Draft documents</button>
      </div>
    </section>`;
  $("#facts").hidden = false;
  $$("#facts textarea").forEach(autosize);
  updateDraftButton();
}

function autosize(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight + 2, 360)}px`;
}

function updateDraftButton() {
  const button = $("#draft-btn");
  if (!button) return;
  const count = $$(".doc-option input:checked").length;
  button.textContent = count === 1 ? "Draft 1 document" : `Draft ${count} documents`;
  button.disabled = count === 0;
}

function applyAnswers() {
  const facts = readFacts();
  const remaining = [];
  let added = 0;
  $$(".question").forEach((li) => {
    const question = facts.open_questions[Number(li.dataset.index)];
    const answer = li.querySelector("textarea").value.trim();
    if (answer) { facts.clarifications.push({ question, answer }); added += 1; } else remaining.push(question);
  });
  if (!added) { toast("Type an answer first, or mark the question as not relevant."); return; }
  facts.open_questions = remaining;
  state.facts = facts;
  renderFacts();
  toast(added === 1 ? "1 answer added to the fact sheet." : `${added} answers added to the fact sheet.`);
}

function dismissQuestion(index) {
  const facts = readFacts();
  const answers = $$(".question").map((li) => li.querySelector("textarea").value);
  answers.splice(index, 1);
  facts.open_questions.splice(index, 1);
  state.facts = facts;
  renderFacts();
  $$(".question textarea").forEach((t, i) => { t.value = answers[i] ?? ""; });
}

function unconfirm(index) {
  const facts = readFacts();
  const [removed] = facts.clarifications.splice(index, 1);
  if (removed) facts.open_questions.push(removed.question);
  state.facts = facts;
  renderFacts();
}

/* ---------- step 3: drafts ---------- */

async function runPool(items, limit, worker) {
  const queue = [...items];
  const runners = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) await worker(queue.shift());
  });
  await Promise.all(runners);
}

async function draftDocuments() {
  const types = $$(".doc-option input:checked").map((input) => input.value);
  if (!types.length) return;
  clearError();
  state.facts = readFacts();
  state.order = types;
  state.drafts = {};
  state.pending = Object.fromEntries(types.map((t) => [t, "loading"]));
  state.active = types[0];
  state.view = "preview";
  const button = $("#draft-btn");
  setBusy(button, true, "Drafting\u2026");
  $("#drafts").hidden = false;
  renderDrafts();
  setStep(3);
  scrollToEl($("#drafts"));
  const factCheck = $("#fact-check").checked;
  await runPool(types, isDemo() ? 5 : 2, (type) => draftOne(type, "", factCheck));
  setBusy(button, false);
  updateDraftButton();
  const failed = types.filter((t) => String(state.pending[t] || "").startsWith("error"));
  toast(failed.length ? `${failed.length} draft(s) failed. Open the tab to retry.` : "All drafts are ready for review.");
}

async function draftOne(type, instruction = "", factCheck = !isDemo()) {
  state.pending[type] = "loading";
  refreshDraftView(type);
  try {
    const draft = await apiJson("/api/generate", {
      facts: state.facts, doc_type: type, settings: docSettings(), llm: llmChoice(),
      sources: sources({ withImages: false }), kb_matches: state.kbMatches, instruction, fact_check: factCheck,
    });
    state.drafts[type] = draft;
    delete state.pending[type];
  } catch (error) {
    state.pending[type] = `error:${error.message}`;
  }
  refreshDraftView(type);
}

function refreshDraftView(type) {
  renderTabs();
  if (state.active === type) renderPanel();
}

function overallStatus(draft) {
  const statuses = draft.checks.map((c) => c.status);
  if (statuses.includes("fail")) return "fail";
  if (statuses.includes("warn")) return "warn";
  return "pass";
}

function tabState(type) {
  const pending = state.pending[type];
  if (pending === "loading") return ["loading", "Drafting"];
  if (pending) return ["error", "Failed"];
  const status = overallStatus(state.drafts[type]);
  return [status, { pass: "All checks passed", warn: "Needs review", fail: "Must fix" }[status]];
}

function renderDrafts() {
  $("#drafts").innerHTML = `
    <header class="section-head">
      <div><h2 id="drafts-title">Drafts</h2>
        <p class="hint">Read each draft next to its proof marks, edit if needed, then export.</p></div>
      <button type="button" class="button secondary" id="export-btn">Download all (.zip)</button>
    </header>
    <div class="tabs" role="tablist" aria-label="Documents" id="tabs"></div>
    <div role="tabpanel" id="draft-panel" tabindex="0"></div>`;
  renderTabs();
  renderPanel();
}

function renderTabs() {
  const tabs = $("#tabs");
  if (!tabs) return;
  tabs.innerHTML = state.order.map((type) => {
    const [status, text] = tabState(type);
    const selected = type === state.active;
    const approved = state.drafts[type] && state.drafts[type].approval
      ? `<span class="approved-tag">Approved</span>` : "";
    return `<button type="button" role="tab" class="tab" id="tab-${type}" data-doc="${type}"
      aria-selected="${selected}" aria-controls="draft-panel" tabindex="${selected ? 0 : -1}">
      <span class="state ${status}" aria-hidden="true"></span>${esc(docLabel(type))}<span class="sr-only">, ${esc(text)}</span>${approved}</button>`;
  }).join("");
  $("#draft-panel")?.setAttribute("aria-labelledby", `tab-${state.active}`);
}

function marksHtml(checks) {
  return `<ul class="marks">${checks.map((c) => `
    <li class="mark-item ${c.status}">
      <span class="glyph" aria-hidden="true">${GLYPHS[c.status]}</span>
      <div><strong>${esc(c.label)}</strong><span class="sr-only">: ${c.status}</span>
        <p>${esc(c.detail)}</p>
        ${c.items.length ? `<ul>${c.items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
      </div>
    </li>`).join("")}</ul>`;
}

function renderPanel() {
  const panel = $("#draft-panel");
  if (!panel) return;
  const type = state.active;
  const pending = state.pending[type];
  if (pending === "loading") {
    panel.innerHTML = `
      <div class="draft-layout">
        <article class="sheet placeholder" aria-busy="true">
          <div class="skeleton title"></div><div class="skeleton"></div><div class="skeleton" style="width:88%"></div>
          <div class="skeleton" style="width:94%"></div><div class="skeleton" style="width:70%"></div>
          <p>Drafting the ${esc(docLabel(type))} from the fact sheet${isDemo() ? "" : ", then fact-checking it"}.</p>
        </article>
      </div>`;
    return;
  }
  if (pending) {
    panel.innerHTML = `
      <div class="notice error"><p>${esc(pending.slice(6))}</p></div>
      <button type="button" class="button primary" data-action="retry">Try again</button>`;
    return;
  }
  const draft = state.drafts[type];
  const meta = draft.meta || {};
  const by = meta.provider === "demo" ? "Built from templates (demo mode)" : `Drafted by ${esc(meta.model)}`;
  const editing = state.view === "edit";
  panel.innerHTML = `
    <div class="toolbar">
      <div class="segmented" role="group" aria-label="View">
        <button type="button" data-view="preview" aria-pressed="${!editing}">Preview</button>
        <button type="button" data-view="edit" aria-pressed="${editing}">Edit Markdown</button>
      </div>
      <span class="spacer"></span>
      <button type="button" class="button ghost small" data-action="copy">Copy Markdown</button>
      <button type="button" class="button ghost small" data-action="download">Download .md</button>
    </div>
    <div class="draft-layout">
      <div>
        ${editing
          ? `<textarea class="md-editor" id="md-editor" spellcheck="true" aria-label="Markdown source">${esc(draft.markdown)}</textarea>`
          : `<article class="sheet" lang="${esc(docSettings().language.slice(0, 2).toLowerCase())}">${draft.html}</article>`}
        <div class="redraft">
          <label for="redraft-note">Redraft with a note</label>
          <div class="row">
            <input id="redraft-note" placeholder="For example: shorter, and lead with the new limits" autocomplete="off">
            <button type="button" class="button secondary" data-action="redraft">Redraft</button>
          </div>
          <p class="meta">${by} in ${meta.seconds} seconds${meta.instruction ? `, with the note \u201c${esc(meta.instruction)}\u201d` : ""}.</p>
        </div>
        <section class="signoff" id="signoff" aria-labelledby="signoff-title"></section>
      </div>
      <aside class="margin" aria-label="Proof marks"><h3>Proof marks</h3><div id="marks">${marksHtml(draft.checks)}</div></aside>
    </div>`;
  renderSignoff();
}

function renderSignoff() {
  const box = $("#signoff");
  const draft = state.drafts[state.active];
  if (!box || !draft) return;
  const approval = draft.approval;
  box.classList.toggle("approved", Boolean(approval));
  if (approval) {
    const when = new Date(approval.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    box.innerHTML = `
      <h3 id="signoff-title">Sign-off</h3>
      <p class="approved-line"><span class="glyph" aria-hidden="true">\u2713</span>
        <span>Approved by <strong>${esc(approval.by)}</strong> on ${esc(when)}.${approval.note ? ` Note: \u201c${esc(approval.note)}\u201d` : ""}</span></p>
      <button type="button" class="link-button" data-action="unapprove">Withdraw approval</button>`;
    return;
  }
  const failing = draft.checks.some((c) => c.status === "fail");
  box.innerHTML = `
    <h3 id="signoff-title">Sign-off</h3>
    <p class="form-error" id="signoff-error" role="alert" hidden></p>
    <div class="signoff-row">
      <label>Reviewer<input id="reviewer" value="${esc(state.reviewer)}" placeholder="Your name" autocomplete="name"></label>
      <label>Note <span class="muted">${failing ? "required: this draft has failing checks" : "optional"}</span>
        <input id="approval-note" autocomplete="off" placeholder="${failing ? "Why it can be published anyway" : "For example: limits confirmed with Payments"}"></label>
      <button type="button" class="button primary" data-action="approve">Approve this draft</button>
    </div>
    <p class="hint">Approvals are saved in the export and the audit log. Editing or redrafting withdraws them.</p>`;
}

function signoffError(message, focusId) {
  const box = $("#signoff-error");
  box.textContent = message;
  box.hidden = false;
  $(`#${focusId}`).focus();
}

async function approveDraft(type) {
  const draft = state.drafts[type];
  const by = $("#reviewer").value.trim();
  const note = $("#approval-note").value.trim();
  if (!by) { signoffError("Add your name so the approval can be recorded.", "reviewer"); return; }
  if (draft.checks.some((c) => c.status === "fail") && !note) {
    signoffError("This draft has failing checks. Say in the note why it can be published anyway.", "approval-note");
    return;
  }
  state.reviewer = by;
  try { localStorage.setItem(REVIEWER_KEY, by); } catch { /* private mode: fine */ }
  draft.approval = { by, note, at: new Date().toISOString(), sha256: await sha256(draft.markdown) };
  renderTabs();
  renderSignoff();
  toast(`${docLabel(type)} approved by ${by}.`);
}

function withdrawApproval(type, message) {
  const draft = state.drafts[type];
  if (!draft || !draft.approval) return;
  draft.approval = null;
  renderTabs();
  if (state.active === type) renderSignoff();
  if (message) toast(message);
}

const recheck = debounce(async (type, markdown) => {
  try {
    const result = await apiJson("/api/check", {
      doc_type: type, markdown, facts: state.facts, sources: sources({ withImages: false }), settings: docSettings(),
    });
    const draft = state.drafts[type];
    if (!draft || draft.markdown !== markdown) return;
    Object.assign(draft, { title: result.title, html: result.html, checks: result.checks });
    if (state.active === type && $("#marks")) $("#marks").innerHTML = marksHtml(result.checks);
    renderTabs();
    if (state.active === type) renderSignoff();
  } catch (error) {
    toast(error.message);
  }
}, 600);

async function exportAll() {
  const drafts = state.order.map((t) => state.drafts[t]).filter(Boolean);
  if (!drafts.length) { toast("No finished drafts to export yet."); return; }
  const button = $("#export-btn");
  setBusy(button, true, "Preparing\u2026");
  try {
    const response = await api("/api/export", {
      facts: state.facts, drafts, settings: docSettings(), sources: sources(), extract_meta: state.extractMeta,
    });
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    download(await response.blob(), match ? match[1] : "proofmark-docs.zip");
    const [approved, total] = (response.headers.get("X-Proofmark-Approved") || "0/0").split("/");
    const logged = response.headers.get("X-Proofmark-Audit-Log") === "saved";
    toast(`Exported ${total} draft${total === "1" ? "" : "s"}, ${approved} approved.` +
      (logged ? " Recorded in the audit log." : ""));
  } catch (error) {
    showError(error.message);
  } finally {
    setBusy(button, false, "Download all (.zip)");
  }
}

/* ---------- events ---------- */

function bindEvents() {
  $("#extract-btn").addEventListener("click", extractFacts);
  $("#model-button").addEventListener("click", openModelDialog);
  $("#demo-setup").addEventListener("click", openModelDialog);
  $("#model-cancel").addEventListener("click", () => $("#model-dialog").close());
  $("#model-form").addEventListener("submit", saveModel);
  $("#provider").addEventListener("change", (e) => fillModelForm(e.target.value));
  $("#error-close").addEventListener("click", clearError);

  $("#samples").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-sample]");
    if (chip) loadSample(chip.dataset.sample);
  });

  $$("input[type=file][data-target]").forEach((input) => input.addEventListener("change", async () => {
    const file = input.files[0];
    if (!file) return;
    $(`#${input.dataset.target}`).value = await readFile(file);
    input.value = "";
    toast(`Loaded ${file.name}.`);
  }));

  $("#shot-input").addEventListener("change", async (e) => { await addScreenshots(e.target.files); e.target.value = ""; });
  const zone = $("#dropzone");
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("over")));
  zone.addEventListener("drop", (e) => { e.preventDefault(); addScreenshots(e.dataTransfer.files); });
  $("#thumbs").addEventListener("click", (e) => {
    const button = e.target.closest("[data-remove-shot]");
    if (!button) return;
    state.screenshots.splice(Number(button.dataset.removeShot), 1);
    renderThumbs();
  });

  $("#facts").addEventListener("click", (e) => {
    if (e.target.closest("#apply-answers")) applyAnswers();
    else if (e.target.closest("#draft-btn")) draftDocuments();
    else if (e.target.closest("[data-dismiss]")) dismissQuestion(Number(e.target.closest("[data-dismiss]").dataset.dismiss));
    else if (e.target.closest("[data-unconfirm]")) unconfirm(Number(e.target.closest("[data-unconfirm]").dataset.unconfirm));
  });
  $("#facts").addEventListener("change", (e) => { if (e.target.closest(".doc-option")) updateDraftButton(); });
  $("#facts").addEventListener("input", (e) => { if (e.target.tagName === "TEXTAREA") autosize(e.target); });

  $("#drafts").addEventListener("click", async (e) => {
    const tab = e.target.closest("[role=tab]");
    if (tab) { state.active = tab.dataset.doc; renderTabs(); renderPanel(); return; }
    if (e.target.closest("#export-btn")) { exportAll(); return; }
    const viewButton = e.target.closest("[data-view]");
    if (viewButton) { state.view = viewButton.dataset.view; renderPanel(); if (state.view === "edit") $("#md-editor").focus(); return; }
    const action = e.target.closest("[data-action]")?.dataset.action;
    const type = state.active;
    const draft = state.drafts[type];
    if (action === "retry") draftOne(type, "", $("#fact-check")?.checked ?? !isDemo());
    if (!draft) return;
    if (action === "copy") {
      try { await navigator.clipboard.writeText(draft.markdown); toast("Markdown copied."); }
      catch { toast("Copy failed: select the text in Edit Markdown instead."); }
    } else if (action === "download") {
      download(new Blob([draft.markdown], { type: "text/markdown" }), `${type}.md`);
    } else if (action === "approve") {
      approveDraft(type);
    } else if (action === "unapprove") {
      withdrawApproval(type, "Approval withdrawn.");
    } else if (action === "redraft") {
      const note = $("#redraft-note").value.trim();
      draftOne(type, note, $("#fact-check")?.checked ?? !isDemo());
    }
  });
  $("#drafts").addEventListener("input", (e) => {
    if (e.target.id !== "md-editor") return;
    const draft = state.drafts[state.active];
    draft.markdown = e.target.value;
    withdrawApproval(state.active, "Approval withdrawn: the draft changed.");
    recheck(state.active, draft.markdown);
  });
  $("#drafts").addEventListener("keydown", (e) => {
    const tab = e.target.closest("[role=tab]");
    if (tab && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      const i = state.order.indexOf(tab.dataset.doc);
      const next = state.order[(i + (e.key === "ArrowRight" ? 1 : -1) + state.order.length) % state.order.length];
      state.active = next;
      renderTabs();
      renderPanel();
      $(`#tab-${next}`).focus();
    }
    if (e.target.id === "redraft-note" && e.key === "Enter") $("[data-action=redraft]").click();
    if ((e.target.id === "approval-note" || e.target.id === "reviewer") && e.key === "Enter") {
      $("[data-action=approve]").click();
    }
  });
}

async function init() {
  bindEvents();
  setStep(1);
  try {
    state.config = await apiJson("/api/config");
  } catch (error) {
    showError(error.message);
    return;
  }
  restoreModel();
  try { state.reviewer = localStorage.getItem(REVIEWER_KEY) || ""; } catch { state.reviewer = ""; }
  $("#samples").innerHTML = `<span>Try a sample:</span>` + state.config.samples.map((s) =>
    `<button type="button" class="sample-chip" data-sample="${esc(s.id)}" title="${esc(s.description)}">${esc(s.title)}</button>`).join("");
}

init();
