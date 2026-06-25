/* ── auth guard ─────────────────────────────────────────────────────────────
   Every page load: if no token, redirect immediately.
   All API calls attach Authorization: Bearer <token>.
   Any 401 response → back to /auth.                                        */

var TOKEN = localStorage.getItem('token');
if (!TOKEN) window.location.href = '/auth';

function logout() {
  localStorage.removeItem('token');
  localStorage.removeItem('username');
  window.location.href = '/auth';
}

function authHeaders() {
  return { 'Authorization': 'Bearer ' + TOKEN };
}

// show username in nav
(function() {
  var el = document.getElementById('nav-username');
  if (el) el.textContent = localStorage.getItem('username') || '';
})();

/* ── matrix rain ────────────────────────────────────────────────────────── */
(function initRain() {
  var canvas = document.getElementById('rain');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var chars = '01アルゴリズムRAGORACLEarxiv{}<>/=01';
  var fontSize = 14;
  var width, height, columns, drops;
  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
    columns = Math.floor(width / fontSize);
    drops = Array.from({ length: columns }, function() { return Math.random() * (-height / fontSize); });
  }
  window.addEventListener('resize', resize);
  resize();
  var frame = 0;
  function draw() {
    frame++;
    if (frame % 3 === 0) {
      ctx.fillStyle = 'rgba(6, 9, 7, 0.16)';
      ctx.fillRect(0, 0, width, height);
      ctx.font = fontSize + 'px monospace';
      for (var i = 0; i < columns; i++) {
        var char = chars[Math.floor(Math.random() * chars.length)];
        var x = i * fontSize;
        var y = drops[i] * fontSize;
        ctx.fillStyle = Math.random() > 0.94 ? '#cfe9da' : '#1f6b46';
        ctx.fillText(char, x, y);
        if (y > height && Math.random() > 0.985) drops[i] = 0;
        drops[i]++;
      }
    }
    requestAnimationFrame(draw);
  }
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) draw();
})();

/* ── tabs ───────────────────────────────────────────────────────────────── */
var tabs = document.querySelectorAll('.tab');
var panes = { research: document.getElementById('pane-research'), admin: document.getElementById('pane-admin') };

tabs.forEach(function(tab) {
  tab.addEventListener('click', function() {
    tabs.forEach(function(t) { t.classList.remove('is-active'); t.setAttribute('aria-selected', 'false'); });
    tab.classList.add('is-active');
    tab.setAttribute('aria-selected', 'true');
    Object.values(panes).forEach(function(p) { p.hidden = true; });
    var target = panes[tab.dataset.pane];
    target.hidden = false;
    if (tab.dataset.pane === 'admin') refreshKbStats();
  });
});

/* ── fetch-based SSE helper ─────────────────────────────────────────────────
   Uses fetch so we can send Authorization headers (EventSource can't),
   and also so HTTP error bodies are readable (EventSource swallows them).  */
async function streamSSE(url, onEvent) {
  var response;
  try {
    response = await fetch(url, { headers: authHeaders() });
  } catch (err) {
    onEvent({ type: 'error', message: 'could not reach the server. is it still running?' });
    return;
  }
  if (response.status === 401) {
    logout(); return;
  }
  if (!response.ok) {
    var message = 'request failed (HTTP ' + response.status + ')';
    try {
      var body = await response.json();
      if (typeof body.detail === 'string') message = body.detail;
      else if (Array.isArray(body.detail) && body.detail[0]) message = body.detail[0].msg || message;
    } catch (_) {}
    onEvent({ type: 'error', message: message });
    return;
  }
  var reader = response.body.getReader();
  var decoder = new TextDecoder();
  var buffer = '';
  while (true) {
    var chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    var sepIndex;
    while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
      var rawEvent = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      var dataLine = rawEvent.split('\n').find(function(line) { return line.startsWith('data:'); });
      if (dataLine) {
        try { onEvent(JSON.parse(dataLine.slice(5).trim())); } catch (_) {}
      }
    }
  }
}

/* ── helpers ────────────────────────────────────────────────────────────── */
function appendLine(container, text, cls) {
  var div = document.createElement('div');
  div.className = 'log-line' + (cls ? ' log-line--' + cls : '');
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

function appendDetail(container, summaryText, fullText) {
  var details = document.createElement('details');
  details.className = 'log-detail';
  var summary = document.createElement('summary');
  summary.textContent = summaryText;
  var pre = document.createElement('pre');
  pre.textContent = fullText || '(empty)';
  details.appendChild(summary);
  details.appendChild(pre);
  container.appendChild(details);
  container.scrollTop = container.scrollHeight;
}

function truncate(text, n) {
  if (!text) return '';
  return text.length > n ? text.slice(0, n) + '…' : text;
}

/* ── kb stats ───────────────────────────────────────────────────────────── */
async function refreshKbStats() {
  var el = document.querySelector('#kbStats .kb-stats__value');
  if (!el) return;
  try {
    var res = await fetch('/api/kb/stats', { headers: authHeaders() });
    if (res.status === 401) { logout(); return; }
    var data = await res.json();
    el.textContent = data.error ? 'unavailable (' + truncate(data.error, 60) + ')' : data.total_vector_count + ' chunk(s) indexed';
  } catch (err) { el.textContent = 'unavailable'; }
}

/* ── research stream ────────────────────────────────────────────────────── */
var researchForm   = document.getElementById('researchForm');
var researchInput  = document.getElementById('researchInput');
var researchRunBtn = document.getElementById('researchRunBtn');
var researchLog    = document.getElementById('researchLog');
var researchBusy   = false;

researchForm.addEventListener('submit', function(e) { e.preventDefault(); runResearch(); });

async function runResearch() {
  if (researchBusy) return;
  var query = researchInput.value.trim();
  if (!query) { appendLine(researchLog, '[error] type a question first', 'error'); return; }
  researchBusy = true;
  researchLog.innerHTML = '';
  researchInput.disabled = true;
  researchRunBtn.disabled = true;
  appendLine(researchLog, 'research@oracle:~$ ' + query);
  appendLine(researchLog, '[oracle] analyzing query...', 'decision');
  await streamSSE('/api/research/stream?query=' + encodeURIComponent(query), handleResearchEvent);
  researchBusy = false;
  researchInput.disabled = false;
  researchRunBtn.disabled = false;
}

function handleResearchEvent(data) {
  if (data.type === 'decision') {
    appendLine(researchLog, '[oracle] → routing to ' + data.tool + '  ' + truncate(JSON.stringify(data.input), 90), 'decision');
  } else if (data.type === 'tool_result') {
    appendLine(researchLog, '[' + data.tool + '] done', 'tool');
    appendDetail(researchLog, data.tool + ' output ▸', data.output);
  } else if (data.type === 'final') {
    renderReport(researchLog, data.report);
    researchInput.value = '';
  } else if (data.type === 'error') {
    appendLine(researchLog, '[error] ' + data.message, 'error');
  }
}

function renderReport(container, report) {
  var wrap = document.createElement('div');
  wrap.className = 'report';
  function section(heading, bodyEl) {
    var sec = document.createElement('div'); sec.className = 'report__section';
    var h = document.createElement('h3');   h.className = 'report__heading'; h.textContent = heading;
    sec.appendChild(h); sec.appendChild(bodyEl); wrap.appendChild(sec);
  }
  function paragraph(text) {
    var p = document.createElement('p'); p.className = 'report__body'; p.textContent = text || '—'; return p;
  }
  function list(items) {
    var ul = document.createElement('ul'); ul.className = 'report__list';
    (items || []).forEach(function(item) { var li = document.createElement('li'); li.textContent = item; ul.appendChild(li); });
    return ul;
  }
  section('INTRODUCTION',   paragraph(report.introduction));
  section('RESEARCH STEPS', list(report.research_steps));
  section('REPORT',         paragraph(report.main_body));
  section('CONCLUSION',     paragraph(report.conclusion));
  section('SOURCES',        list(report.sources));
  container.appendChild(wrap);
  container.scrollTop = container.scrollHeight;
}

/* ── ingest stream ──────────────────────────────────────────────────────── */
var ingestForm   = document.getElementById('ingestForm');
var ingestQuery  = document.getElementById('ingestQuery');
var ingestMax    = document.getElementById('ingestMax');
var ingestRunBtn = document.getElementById('ingestRunBtn');
var ingestLog    = document.getElementById('ingestLog');
var ingestBusy   = false;

ingestForm.addEventListener('submit', function(e) { e.preventDefault(); runIngest(); });

async function runIngest() {
  if (ingestBusy) return;
  var query      = ingestQuery.value.trim();
  var maxResults = parseInt(ingestMax.value, 10);
  ingestLog.innerHTML = '';
  if (!query) { appendLine(ingestLog, '[error] type an arxiv query first, e.g. cat:cs.AI', 'error'); return; }
  if (!Number.isFinite(maxResults) || maxResults < 1 || maxResults > 50) {
    appendLine(ingestLog, '[error] --max must be between 1 and 50 (got "' + ingestMax.value + '")', 'error'); return;
  }
  ingestBusy = true;
  ingestQuery.disabled = true;
  ingestMax.disabled = true;
  ingestRunBtn.disabled = true;
  appendLine(ingestLog, 'admin@ingest:~$ ' + query + ' --max ' + maxResults);
  await streamSSE('/api/ingest/stream?query=' + encodeURIComponent(query) + '&max_results=' + maxResults, handleIngestEvent);
  ingestBusy = false;
  ingestQuery.disabled = false;
  ingestMax.disabled = false;
  ingestRunBtn.disabled = false;
}

function handleIngestEvent(data) {
  if (data.type === 'log') {
    appendLine(ingestLog, '[ingest] ' + data.message);
  } else if (data.type === 'progress') {
    appendLine(ingestLog, '[ingest] ' + truncate(data.paper, 60) + ' — ' + data.chunks_indexed + ' chunk(s) indexed so far', 'tool');
  } else if (data.type === 'done') {
    appendLine(ingestLog, '[ingest] complete — ' + data.total_papers + ' paper(s), ' + data.total_chunks + ' chunk(s) added', 'decision');
    refreshKbStats();
  } else if (data.type === 'error') {
    appendLine(ingestLog, '[error] ' + data.message, 'error');
  }
}

/* ── boot ───────────────────────────────────────────────────────────────── */
function boot() {
  var lines = [
    '[boot] initializing oracle...',
    '[boot] tools online: rag_search · rag_search_filter · fetch_arxiv · web_search · final_answer',
    '[boot] ready. press enter or click run ▸',
  ];
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  lines.forEach(function(line, i) {
    setTimeout(function() { appendLine(researchLog, line, i === lines.length - 1 ? 'tool' : null); }, reduced ? 0 : i * 260);
  });
}

boot();
refreshKbStats();
