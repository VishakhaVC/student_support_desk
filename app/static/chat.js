(function () {
  const $ = (id) => document.getElementById(id);
  const box = $('messages'), input = $('input'), sendBtn = $('send');
  let conversationId = null;
  const greeting = 'Hi! Ask me anything, or I can help with fees, scholarships, policies, support requests and your student account. What would you like to explore?';
  function metric(id, value) { const el = $(id); if (el) el.textContent = value === null || value === undefined ? '—' : value; }
  function showMetrics(a) {
    a = a || {}; const tokens = a.tokens || {};
    metric('m-latency', typeof a.latency_ms === 'number' ? (a.latency_ms / 1000).toFixed(2) + ' s' : null);
    metric('m-calls', a.iterations); metric('m-tools', Array.isArray(a.tools) ? a.tools.length : a.tool_call_count);
    metric('m-input', tokens.input_tokens); metric('m-output', tokens.output_tokens);
    metric('m-total', tokens.total_tokens === undefined && tokens.input_tokens != null && tokens.output_tokens != null ? tokens.input_tokens + tokens.output_tokens : tokens.total_tokens);
    metric('m-model', a.model || 'No response yet');
    metric('m-note', a.model ? 'Recorded for the latest response in this conversation.' : 'Metrics appear after an AI response.');
  }
  async function loadMetrics(cid) {
    if (!cid) { showMetrics(null); return; }
    try { const r = await fetch('/api/conversations/' + cid + '/metrics'); if (r.ok) showMetrics(await r.json()); else showMetrics(null); } catch (_) { showMetrics(null); }
  }

  const now = () => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const scroll = () => { box.scrollTop = box.scrollHeight; };

  function add(role, text, time) {
    const row = document.createElement('div'); row.className = 'row ' + role;
    const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text; row.appendChild(b);
    const st = document.createElement('div'); st.className = 'stamp';
    const t = document.createElement('span'); t.textContent = time || now(); st.appendChild(t);
    if (role === 'assistant') {
      const c = document.createElement('button'); c.type = 'button'; c.textContent = 'Copy'; c.dataset.copy = text; st.appendChild(c);
    }
    row.appendChild(st); box.appendChild(row); scroll();
    return row;
  }

  function addDetails(row, data) {
    const a = data.activity || {};
    if ((data.sources || []).length) {
      const s = document.createElement('div'); s.className = 'src';
      data.sources.forEach((x) => {
        const c = document.createElement('span'); c.className = 'chip';
        c.textContent = 'Handbook ' + x.section.split(' ')[0] + ' · ' + x.section.split(' ').slice(1).join(' ');
        s.appendChild(c);
      });
      row.insertBefore(s, row.querySelector('.stamp'));
    }
    const det = document.createElement('details'); det.className = 'activity';
    const sum = document.createElement('summary');
    const tools = (a.tools || []).map((t) => t.name + (t.status === 'ok' ? '' : ' (' + (t.error_code || t.status) + ')')).join(', ') || 'answered directly';
    sum.textContent = 'Activity: ' + tools + ' · ' + a.latency_ms + ' ms';
    det.appendChild(sum);
    const pre = document.createElement('pre');
    const tok = a.tokens ? a.tokens.input_tokens + ' in / ' + a.tokens.output_tokens + ' out' : 'not reported';
    pre.textContent = [
      'Model: ' + a.model, 'Model calls: ' + a.iterations, 'Tokens: ' + tok,
      'Tools: ' + ((a.tools || []).map((t) => t.name + ' → ' + t.status).join(', ') || 'none'),
      'Memory saved: ' + JSON.stringify(a.memory_saved || []) + (a.memory_blocked ? ' (' + a.memory_blocked + ')' : ''),
      a.redactions && a.redactions.length ? 'Sensitive data removed from your message: ' + a.redactions.join(', ') : '',
      'Context used: ' + ((a.context || []).map((c) => c.source + (c.item ? ':' + c.item : '')).join(', ') || 'question only'),
      'Trace: ' + data.trace_id].filter(Boolean).join('\n');
    det.appendChild(pre); row.insertBefore(det, row.querySelector('.stamp'));
  }

  function reset() {
    conversationId = null; box.textContent = ''; $('chat-title').textContent = 'New conversation';
    add('assistant', greeting); showMetrics(null); input.focus();
  }

  async function send(text) {
    text = (text || input.value).trim();
    if (!text) return;
    input.value = ''; input.style.height = '44px';
    add('user', text); sendBtn.disabled = true;
    const typing = document.createElement('div'); typing.className = 'typing'; typing.innerHTML = '<i></i><i></i><i></i>'; typing.setAttribute('aria-label', 'Assistant is typing');
    box.appendChild(typing); scroll();
    try {
      const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': window.csrfToken() },
        body: JSON.stringify({ message: text, conversation_id: conversationId }) });
      const data = await res.json().catch(() => ({}));
      typing.remove();
      if (!res.ok || data.ok === false) {
        add('error', data.message || 'Something went wrong (' + res.status + '). Please try again.');
      } else {
        conversationId = data.conversation_id;
        const row = add(data.status === 'completed' ? 'assistant' : 'error', data.reply);
        addDetails(row, data); showMetrics(data.activity || null); loadConversations();
        if (/support request|ticket/i.test(data.reply) && (data.activity.tools || []).some((t) => t.name === 'create_support_ticket' && t.status === 'ok')) {
          toast('Support request created. Find it under Support Requests.', 'ok');
        }
      }
    } catch (e) {
      typing.remove(); add('error', 'Could not reach the server. Check your connection and try again.');
    } finally { sendBtn.disabled = false; input.focus(); }
  }

  async function open(c) {
    const r = await fetch('/api/conversations/' + c.id + '/messages');
    if (!r.ok) return;
    conversationId = c.id; box.textContent = ''; $('chat-title').textContent = c.title;
    (await r.json()).forEach((m) => add(m.role, m.content, m.time));
    await loadMetrics(c.id); loadConversations();
  }

  async function loadConversations() {
    const res = await fetch('/api/conversations'); const el = $('convlist');
    if (!res.ok) { el.textContent = ''; return; }
    const list = await res.json(); el.textContent = '';
    if (!list.length) { const p = document.createElement('p'); p.className = 'note'; p.textContent = 'Your conversations will appear here.'; el.appendChild(p); return; }
    list.forEach((c) => {
      const a = document.createElement('a'); a.href = '#'; if (c.id === conversationId) a.className = 'cur';
      const s = document.createElement('span'); s.textContent = c.title; a.appendChild(s);
      a.addEventListener('click', (ev) => { ev.preventDefault(); open(c); document.body.classList.remove('nav-open'); });
      el.appendChild(a);
    });
  }

  async function clear(url, what) {
    const r = await askConfirm({ title: 'Delete ' + what + '?', text: 'This removes the messages permanently. Your saved preferences are kept.', confirmLabel: 'Delete', danger: true });
    if (!r.ok) return;
    const res = await fetch(url, { method: 'DELETE', headers: { 'X-CSRFToken': window.csrfToken() } });
    if (res.ok) { toast('Deleted.', 'ok'); reset(); loadConversations(); } else toast('Nothing to delete.', 'error');
  }

  sendBtn.addEventListener('click', () => send());
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  input.addEventListener('input', () => { input.style.height = '44px'; input.style.height = Math.min(input.scrollHeight, 140) + 'px'; });
  document.querySelectorAll('.suggest').forEach((b) => b.addEventListener('click', () => send(b.dataset.q)));
  $('new-chat').addEventListener('click', () => { reset(); loadConversations(); });
  $('clear-chat').addEventListener('click', () => conversationId ? clear('/api/conversations/' + conversationId, 'this conversation') : toast('This conversation has no messages yet.', 'error'));
  $('clear-all').addEventListener('click', () => clear('/api/conversations', 'all conversations'));
  reset(); loadConversations();
})();
