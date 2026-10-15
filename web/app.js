// Lore workspace: streams the agent's steps into "Under the hood", renders the answer, then draws the proof graph.
const $ = (id) => document.getElementById(id);
const state = { project: null, model: null, key: null, running: false, lastEvidence: null, mode: 'ask' };

const SUGGESTED = {
  FalkorDB: [
    'Who knows the query planner code?',
    'Who should review a change to the vector index?',
    'Why does FalkorDB bound the number of queued queries?',
    'Which parts of the code does only one active person know?',
    'I know Python and want to help with the Python client. Where do I start?',
  ],
  _: ['Who knows this project best?', 'Which parts of the code does only one active person know?', 'What was decided recently, and why?'],
};
const TOOL_NAMES = {
  find_areas: 'Find the areas of the code', who_knows: 'Rank who knows them', get_item: 'Read the issue or PR',
  search_history: 'Search PR and issue history', related: 'Find related issues and PRs', suggest_reviewers: 'Pick reviewers',
  knowledge_risk: 'Check knowledge risk', good_first_issues: 'Find good first issues', recall_decisions: 'Check what Lore remembers',
  save_decision: 'Remember this decision',
};

// ---------- tiny, safe markdown: escape first, then bold, code, links, lists ----------
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([\w.-]+\/[\w.-]+)#(\d+)\]/g, (m, repo, n) => `<a href="https://github.com/${repo}/issues/${n}" target="_blank" rel="noopener">${repo.split('/')[1]}#${n}</a>`)
    .replace(/(^|[\s(])@([A-Za-z0-9-]{1,39})\b/g, (m, pre, login) => `${pre}<a href="https://github.com/${login}" target="_blank" rel="noopener">@${login}</a>`);
}
function md(text) {
  const out = []; let list = null;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) { if (!list) { list = []; } list.push(`<li>${inline(item[1])}</li>`); continue; }
    if (list) { out.push(`<ul>${list.join('')}</ul>`); list = null; }
    const h = line.match(/^#{1,4}\s+(.*)$/);
    if (h) out.push(`<p><strong>${inline(h[1])}</strong></p>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push(`<ul>${list.join('')}</ul>`);
  return out.join('');
}

// ---------- setup ----------
async function init() {
  const [projects, models, me] = await Promise.all([fetch('/api/projects').then((r) => r.json()), fetch('/api/models').then((r) => r.json()),
    fetch('/api/me').then((r) => r.json())]);
  state.me = me;
  $('userbox').innerHTML = me.login
    ? `<a class="btn small" href="https://github.com/${esc(me.login)}" target="_blank" rel="noopener">@${esc(me.login)}</a> <a class="btn small" href="/auth/logout">Sign out</a>`
    : (me.signin ? '<a class="btn small primary" href="/auth/login">Sign in with GitHub</a>' : '');
  const want = new URLSearchParams(location.search).get('project');
  $('project').innerHTML = projects.map((p) => `<option value="${esc(p.name)}">${esc(p.target)} (${p.stats.Person || 0} people, ${p.stats.PR || 0} PRs)</option>`).join('')
    + '<option value="__add">+ Add a project…</option>';
  if (want && projects.some((p) => p.name === want)) $('project').value = want;
  state.projects = projects;
  $('model').innerHTML = (models.models || []).map((m) => `<option>${esc(m)}</option>`).join('') || '<option value="">no models</option>';
  state.model = models.default;
  pickProject();
  $('project').onchange = pickProject;
  $('model').onchange = () => (state.model = $('model').value);
  let toured = false; try { toured = !!localStorage.getItem('lore.toured'); } catch (e) {}
  if (!toured) tour(0);
}
function showPane(name) {
  document.querySelectorAll('[data-pane]').forEach((p) => (p.hidden = p.dataset.pane !== name));
  document.querySelectorAll('[data-tab]').forEach((x) => x.classList.toggle('on', x.dataset.tab === name));
}
function pickProject() {
  if ($('project').value === '__add') {
    $('project').value = state.project || $('project').options[0].value;
    showPane('add'); state.mode = 'add';
    if (!state.me?.login) {
      $('addform').innerHTML = state.me?.signin ? '<a class="btn primary" href="/auth/login">Sign in with GitHub to add a project</a>'
        : '<p class="muted">Sign-in isn\'t set up on this server, so projects can\'t be added here.</p>';
    }
    return;
  }
  state.project = $('project').value;
  const p = state.projects.find((x) => x.name === state.project);
  $('fresh').textContent = p ? `data from ${new Date(p.fetched_at * 1000).toLocaleString()}` : '';
  $('chips').innerHTML = (SUGGESTED[state.project] || SUGGESTED._).map((q) => `<button class="chip">${esc(q)}</button>`).join('');
  $('chips').querySelectorAll('.chip').forEach((c) => (c.onclick = () => { $('question').value = c.textContent; ask(c.textContent, { suggested: true }); }));
  if (state.mode === 'triage') loadIssues();
  if (state.mode === 'risk') loadRisk();
  if (state.mode === 'teams') loadTeams();
  if (state.mode === 'memory') loadMemory();
}

// ---------- tabs ----------
document.querySelectorAll('[data-tab]').forEach((b) => (b.onclick = () => {
  document.querySelectorAll('[data-tab]').forEach((x) => x.classList.toggle('on', x === b));
  document.querySelectorAll('[data-pane]').forEach((p) => (p.hidden = p.dataset.pane !== b.dataset.tab));
  state.mode = b.dataset.tab;
  if (state.mode === 'triage') loadIssues();
  if (state.mode === 'risk') loadRisk();
  if (state.mode === 'teams') loadTeams();
  if (state.mode === 'memory') loadMemory();
}));
document.querySelectorAll('[data-side]').forEach((b) => (b.onclick = () => side(b.dataset.side)));
function side(name) {
  document.querySelectorAll('[data-side]').forEach((x) => x.classList.toggle('on', x.dataset.side === name));
  document.querySelectorAll('[data-sidepane]').forEach((p) => (p.hidden = p.dataset.sidepane !== name));
  if (name === 'proof' && state.cy) state.cy.resize();
}

// ---------- asking ----------
$('askbtn').onclick = () => $('question').value.trim() && ask($('question').value.trim());
$('question').onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('askbtn').click(); } };
$('triagebtn').onclick = () => $('issueref').value.trim() && triage($('issueref').value.trim().replace(/.*\/(?:issues|pull)\/(\d+).*/, '#$1'));
$('firstbtn').onclick = () => {
  const skills = $('skills').value.trim() || 'some programming', interest = $('interest').value.trim() || 'anything useful';
  ask(`I know ${skills} and I'm interested in ${interest}. Plan my first contribution to this project: which docs or code to read first, a good first issue near that interest, and who reviews that area.`, { head: 'Your first contribution' });
};
$('copybtn').onclick = () => navigator.clipboard.writeText($('answerbody').innerText).then(() => ($('copybtn').textContent = 'Copied'));
$('ifilter').onchange = loadIssues;
$('keybtn').onclick = () => {
  const k = prompt('Paste your own free Gemini API key (from aistudio.google.com). It is kept in this tab only and sent with your questions, never stored on the server. Leave empty to use Lore\'s key.', '');
  if (k !== null) { state.key = k.trim() || null; $('keybtn').textContent = state.key ? 'Own key: on' : 'Own key'; }
};

function triage(ref) {
  ask(`Triage issue ${ref}. Read it, find the past issues and PRs it relates to and the areas of the code it touches, suggest who should look at it, and draft a short, friendly reply to the reporter that links the related items.`, { head: `Triage of ${ref}`, dry: true });
}

async function loadIssues() {
  $('issues').innerHTML = '<li class="muted">Loading…</li>';
  const rows = await fetch(`/api/issues?project=${encodeURIComponent(state.project)}&filter=${$('ifilter').value}`).then((r) => r.json());
  $('issues').innerHTML = rows.length ? rows.map((i) => `<li data-id="${esc(i.id)}"><div>${esc(i.title)}</div>
    <div class="meta">${esc(i.id)} · opened ${new Date(i.created_at * 1000).toLocaleDateString()} · ${i.comments || 0} comments ${(i.labels || []).map((l) => `<span class="pill">${esc(l)}</span>`).join(' ')}</div></li>`).join('')
    : '<li class="muted">No open issues match.</li>';
  $('issues').querySelectorAll('li[data-id]').forEach((li) => (li.onclick = () => triage(li.dataset.id)));
}

async function loadRisk() {
  $('riskbody').textContent = 'Loading…';
  const r = await fetch(`/api/risk?project=${encodeURIComponent(state.project)}`).then((x) => x.json());
  resetSteps();
  r.trace.forEach((q) => addQuery(q, $('steps')));
  $('riskbody').innerHTML = r.areas.length ? `<table class="grid"><tr><th>Area</th><th>Merged PRs, 180 days</th><th>Active people who know it well</th></tr>
    ${r.areas.map((a) => `<tr><td><code>${esc(a.path)}</code><div class="muted" style="font-size:12px">${esc(a.repo)}</div></td><td>${a.recent_prs}</td>
    <td>${a.knowers.length ? a.knowers.map((k) => `<a href="https://github.com/${esc(k)}" target="_blank">@${esc(k)}</a>`).join(', ') : '<span class="pill amber">nobody yet</span>'}</td></tr>`).join('')}</table>`
    : '<p class="muted">No area is down to one person. Knowledge is spread well.</p>';
}

function resetSteps() { $('steps').innerHTML = ''; }
function addStep(kind, title, detail) {
  $('steps').querySelectorAll('.step.live').forEach((s) => s.classList.remove('live'));
  const div = document.createElement('div');
  div.className = `step ${kind} live`;
  div.innerHTML = `<div class="title">${title}</div>${detail ? `<div class="args">${detail}</div>` : ''}`;
  $('steps').appendChild(div);
  $('steps').scrollTop = $('steps').scrollHeight;
  return div;
}
function addQuery(q, into) {
  const d = document.createElement('details');
  d.innerHTML = `<summary>Cypher · ${esc(q.step)} · ${q.rows} rows · ${q.ms} ms</summary><pre>${esc(q.cypher)}</pre>`;
  (into.lastElementChild && into.lastElementChild.classList.contains('step') ? into.lastElementChild : into).appendChild(d);
}

async function ask(question, opts = {}) {
  if (state.running) return;
  state.running = true;
  $('answer').hidden = false; $('dry').hidden = !opts.dry; $('copybtn').hidden = true; $('copybtn').textContent = 'Copy reply';
  $('answerhead').textContent = opts.head || 'Answer';
  $('answerbody').innerHTML = '<p class="muted">Working through the graph…</p>'; $('answermeta').textContent = '';
  resetSteps(); side('hood');
  addStep('model', 'Question', esc(question));
  const t0 = performance.now();
  let queries = 0;
  try {
    const res = await fetch('/api/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project: state.project, question, model: state.model, key: state.key, suggested: !!opts.suggested }) });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!chunk.startsWith('data: ')) continue;
        const e = JSON.parse(chunk.slice(6));
        if (e.type === 'memory') addStep('model', `Lore remembers @${esc(e.login)}`, e.asked
          ? `${e.asked} earlier question(s)${e.interests.length ? '; interested in ' + esc(e.interests.join(', ')) : ''}${e.changed.length ? `; ${e.changed.length} PR(s) merged in those areas since the last visit` : ''}`
          : 'First visit to this project: starting a memory.');
        else if (e.type === 'model') addStep('model', `Thinking with ${esc(e.model)}`);
        else if (e.type === 'fallback') addStep('fallback', `${esc(e.from)} couldn't answer`, esc(e.reason) + ' · switching to the next free model');
        else if (e.type === 'replay') addStep('fallback', 'Saved answer', esc(e.message));
        else if (e.type === 'call') addStep('call', esc(TOOL_NAMES[e.tool] || e.tool), `${esc(e.tool)}(${esc(JSON.stringify(e.args))})`);
        else if (e.type === 'query') { queries++; addQuery(e, $('steps')); }
        else if (e.type === 'result') { const s = document.createElement('div'); s.className = 'args'; s.textContent = '→ ' + e.summary; $('steps').lastElementChild?.appendChild(s); }
        else if (e.type === 'error') { addStep('error', 'Stopped', esc(e.message)); $('answerbody').innerHTML = `<p>${esc(e.message)}</p>`; }
        else if (e.type === 'answer') {
          $('answerbody').innerHTML = md(e.text);
          $('answermeta').textContent = `${esc(e.model)} · ${queries} graph queries · ${((performance.now() - t0) / 1000).toFixed(1)} s`;
          $('copybtn').hidden = !opts.dry;
          addStep('model', 'Answer ready', `${e.evidence.items.length} sources, ${e.evidence.people.length} people`);
          state.lastEvidence = e.evidence;
          proof(e.evidence);
        }
      }
    }
  } catch (err) {
    addStep('error', 'Connection lost', esc(err.message));
  } finally {
    state.running = false;
    $('steps').querySelectorAll('.step.live').forEach((s) => s.classList.remove('live'));
  }
}

// ---------- teams, memory, adding a project ----------
async function loadTeams() {
  $('teamsbody').textContent = 'Running community detection…';
  const r = await fetch(`/api/teams?project=${encodeURIComponent(state.project)}`).then((x) => x.json());
  resetSteps(); r.trace.forEach((q) => addQuery(q, $('steps')));
  $('teamsbody').innerHTML = r.teams.length ? r.teams.map((t, i) => `<div class="card" style="margin-bottom:12px">
    <strong>Team ${i + 1}</strong> <span class="muted">· ${t.size} people</span>
    <div class="chips">${t.members.map((m) => `<a class="chip" href="https://github.com/${esc(m.login)}" target="_blank" rel="noopener" title="PageRank ${m.centrality}">@${esc(m.login)}${m.active ? '' : ' (inactive)'}</a>`).join('')}</div>
    <div class="muted" style="font-size:13px">Knows best: ${t.areas.map((a) => `<code>${esc(a.path)}</code> <span style="font-size:12px">(${esc(a.repo.split('/')[1] || a.repo)})</span>`).join(', ')}</div></div>`).join('')
    : '<p class="muted">No strong review ties yet: nobody has reviewed the same person 5 times.</p>';
}

async function loadMemory() {
  if (!state.me?.login) {
    $('memorybody').innerHTML = `<p style="margin-top:0">Signed in, Lore remembers what you asked and which areas you care about, in a graph of its own for you. Next time it starts from there and tells you what changed since.</p>
      ${state.me?.signin ? '<a class="btn primary" href="/auth/login">Sign in with GitHub</a>' : '<p class="muted">Sign-in isn\'t set up on this server.</p>'}`;
    return;
  }
  const m = await fetch(`/api/memory?project=${encodeURIComponent(state.project)}`).then((x) => x.json());
  $('memorybody').innerHTML = `<p style="margin-top:0">What Lore remembers about <strong>@${esc(state.me.login)}</strong> in this project. It lives in a separate graph just for you.${state.me.persistent ? '' : ' <span class="pill amber">This server forgets when it restarts.</span>'}</p>
    <h4>Areas you care about</h4>${m.interests.length ? '<div class="chips">' + m.interests.map((i) => `<span class="chip">${esc(i.path)} · ${i.weight}×</span>`).join('') + '</div>' : '<p class="muted">None yet. Ask something.</p>'}
    <h4>Since your last visit</h4>${m.changed.length ? '<ul>' + m.changed.map((c) => `<li><a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.id)}</a> ${esc(c.title)} <span class="muted">(${esc(c.area)})</span></li>`).join('') + '</ul>' : '<p class="muted">Nothing merged in your areas since then.</p>'}
    <h4>Your recent questions</h4>${m.questions.length ? '<ul>' + m.questions.map((q) => `<li>${esc(q.text)} <span class="muted">${new Date(q.at * 1000).toLocaleString()}</span></li>`).join('') + '</ul>' : '<p class="muted">None yet.</p>'}
    <button class="btn small" id="forgetbtn">Forget everything about me</button>`;
  $('forgetbtn').onclick = async () => { if (confirm('Delete your whole memory graph?')) { await fetch('/api/memory', { method: 'DELETE' }); loadMemory(); } };
}

$('addbtn').onclick = async () => {
  const target = $('addtarget').value.trim(); if (!target) return;
  $('addlog').hidden = false; $('addlog').textContent = 'Starting…';
  const r = await fetch('/api/projects/add', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target }) });
  const job = await r.json();
  if (!r.ok) { $('addlog').textContent = job.detail || 'Failed.'; return; }
  const poll = async () => {
    const j = await fetch(`/api/projects/progress?name=${encodeURIComponent(job.name)}`).then((x) => x.json());
    $('addlog').textContent = j.log.join('\n');
    if (j.status === 'running') return setTimeout(poll, 1500);
    if (j.status === 'done') { history.replaceState(null, '', `/app?project=${encodeURIComponent(job.name)}`); setTimeout(() => location.reload(), 1200); }
  };
  if (job.status === 'done') { location.href = `/app?project=${encodeURIComponent(job.name)}`; } else poll();
};

// ---------- proof graph ----------
async function proof(ev) {
  $('sources').innerHTML = ev.items.map((i) => `<li><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.id)}</a> ${esc(i.title || '')}</li>`).join('') || '<li class="muted">No sources.</li>';
  const g = await fetch('/api/proof', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project: state.project, people: ev.people, items: ev.items.map((i) => i.id), areas: ev.areas.map((a) => a.id) }) }).then((r) => r.json());
  (g.trace || []).forEach((q) => addQuery(q, $('steps')));
  const css = getComputedStyle(document.documentElement);
  const color = { person: '#6d4aff', pr: '#1f8a5b', area: '#b7791f', item: '#8a8798' };
  side('proof');
  if (state.cy) state.cy.destroy();
  state.cy = cytoscape({
    container: $('cy'),
    elements: [...g.nodes.map((n) => ({ data: n })), ...g.edges.map((e, k) => ({ data: { id: 'e' + k, ...e } }))],
    style: [
      { selector: 'node', style: { label: 'data(label)', 'font-size': 10, color: css.getPropertyValue('--ink').trim(), 'text-valign': 'bottom', 'text-margin-y': 4,
        'background-color': (n) => color[n.data('kind')] || '#888', width: (n) => (n.data('kind') === 'area' ? 26 : 16), height: (n) => (n.data('kind') === 'area' ? 26 : 16) } },
      { selector: 'edge', style: { width: 1.4, 'line-color': css.getPropertyValue('--line').trim(), 'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'target-arrow-color': css.getPropertyValue('--line').trim(), label: 'data(label)', 'font-size': 8, color: css.getPropertyValue('--muted').trim() } },
    ],
    layout: { name: 'cose', animate: true, animationDuration: 600, nodeRepulsion: 16000, idealEdgeLength: 90, nodeOverlap: 24, padding: 24 },
  });
  state.cy.on('tap', 'node', (e) => { const u = e.target.data('url'); if (u) window.open(u, '_blank', 'noopener'); });
}

// ---------- tour ----------
const TOUR = [
  ['t-project', 'Pick a project', 'FalkorDB is ready to go. Its graph is rebuilt from GitHub every six hours.'],
  ['t-ask', 'Ask in plain words', 'Or tap a suggested question. Triage and First contribution are in the tabs above.'],
  ['t-side', 'Watch it work', 'Each tool Lore picks and each Cypher query it runs appears here. Then Proof shows the graph behind the answer.'],
  ['t-model', 'Choose the model', 'If a model runs out of free quota, Lore switches to the next one by itself and tells you.'],
];
function tour(i) {
  document.querySelectorAll('.hl').forEach((x) => x.classList.remove('hl'));
  document.querySelector('.tour')?.remove();
  if (i >= TOUR.length) { try { localStorage.setItem('lore.toured', '1'); } catch (e) {} return; }
  const [id, h, p] = TOUR[i];
  $(id)?.classList.add('hl');
  const box = document.createElement('div');
  box.className = 'tour';
  box.innerHTML = `<h4>${h}</h4><div class="muted">${p}</div><div class="row"><button class="btn small" id="tskip">Skip</button><span class="muted">${i + 1} / ${TOUR.length}</span><button class="btn small primary" id="tnext">${i + 1 < TOUR.length ? 'Next' : 'Done'}</button></div>`;
  document.body.appendChild(box);
  $('tnext').onclick = () => tour(i + 1);
  $('tskip').onclick = () => tour(TOUR.length);
}
$('helpbtn').onclick = () => tour(0);

init();
