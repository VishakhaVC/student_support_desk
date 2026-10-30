/* Shared behaviour: theme, mobile navigation, toasts, confirmation dialogs, table search, AJAX approvals. */
(function () {
  const csrf = () => (document.querySelector('meta[name=csrf-token]') || {}).content || '';
  window.csrfToken = csrf;

  // ---- theme
  const root = document.documentElement;
  const tt = document.getElementById('theme-toggle');
  if (tt) tt.addEventListener('click', () => {
    const next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
  });

  // ---- persistent desktop sidebar
  const collapse = document.getElementById('collapse-sidebar');
  try { if (localStorage.getItem('sidebar-collapsed') === '1') document.body.classList.add('sidebar-collapsed'); } catch (_) {}
  if (collapse) collapse.addEventListener('click', () => {
    const closed = document.body.classList.toggle('sidebar-collapsed');
    collapse.textContent = closed ? '›' : '‹';
    collapse.setAttribute('aria-label', closed ? 'Expand sidebar' : 'Collapse sidebar');
    collapse.setAttribute('aria-expanded', String(!closed));
    try { localStorage.setItem('sidebar-collapsed', closed ? '1' : '0'); } catch (_) {}
  });
  if (collapse && document.body.classList.contains('sidebar-collapsed')) { collapse.textContent = '›'; collapse.setAttribute('aria-expanded', 'false'); }

  // ---- mobile navigation
  const mb = document.getElementById('menu-btn'), scrim = document.getElementById('scrim');
  if (mb) mb.addEventListener('click', () => document.body.classList.toggle('nav-open'));
  if (scrim) scrim.addEventListener('click', () => document.body.classList.remove('nav-open'));

  // ---- toasts
  const box = document.getElementById('toasts');
  function dismiss(t) { t.style.opacity = '0'; setTimeout(() => t.remove(), 180); }
  function arm(t) {
    const x = document.createElement('button'); x.className = 'x'; x.type = 'button'; x.setAttribute('aria-label', 'Dismiss'); x.textContent = '×';
    x.addEventListener('click', () => dismiss(t)); t.appendChild(x);
    setTimeout(() => dismiss(t), t.classList.contains('error') ? 9000 : 5500);
  }
  window.toast = function (msg, kind) {
    const t = document.createElement('div'); t.className = 'toast ' + (kind || 'ok'); t.textContent = msg; box.appendChild(t); arm(t);
  };
  box.querySelectorAll('.toast').forEach(arm);

  // ---- confirmation dialog (returns a Promise<{ok, note}>)
  const dlg = document.getElementById('confirm-dialog');
  window.askConfirm = function (o) {
    return new Promise((resolve) => {
      const $ = (id) => document.getElementById(id);
      $('cd-title').textContent = o.title || 'Confirm'; $('cd-text').textContent = o.text || '';
      const wrap = $('cd-note-wrap'), note = $('cd-note'), err = $('cd-err'), ok = $('cd-ok');
      wrap.hidden = !o.noteLabel; note.value = ''; err.hidden = true; note.classList.remove('invalid');
      $('cd-note-label').textContent = o.noteLabel || '';
      ok.textContent = o.confirmLabel || 'Confirm'; ok.className = 'btn' + (o.danger ? ' solid-danger' : '');
      const done = (v) => { ok.onclick = null; $('cd-cancel').onclick = null; dlg.onclose = null; if (dlg.open) dlg.close(); resolve(v); };
      ok.onclick = () => {
        const v = note.value.trim();
        if (o.noteLabel && o.noteRequired && v.length < 3) { err.textContent = 'Please enter at least 3 characters.'; err.hidden = false; note.classList.add('invalid'); note.focus(); return; }
        done({ ok: true, note: v });
      };
      $('cd-cancel').onclick = () => done({ ok: false });
      dlg.onclose = () => resolve({ ok: false });
      dlg.showModal(); (o.noteLabel ? note : ok).focus();
    });
  };

  // Buttons that need confirmation (and optionally a note) before their form is submitted.
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-confirm]');
    if (!b || b.dataset.confirmed === '1' || b.hasAttribute('data-ajax-decision')) return;
    e.preventDefault();
    const r = await askConfirm({ title: b.dataset.confirmTitle, text: b.dataset.confirm, confirmLabel: b.dataset.confirmLabel, danger: b.dataset.danger === '1',
      noteLabel: b.dataset.noteLabel, noteRequired: b.dataset.noteRequired === '1' });
    if (!r.ok) return;
    const form = b.form;
    if (b.dataset.noteName) {
      let h = form.querySelector('input[name="' + b.dataset.noteName + '"][data-injected]');
      if (!h) { h = document.createElement('input'); h.type = 'hidden'; h.name = b.dataset.noteName; h.dataset.injected = '1'; form.appendChild(h); }
      h.value = r.note;
    }
    b.dataset.confirmed = '1'; form.requestSubmit(b);
  });

  // ---- instant table search
  document.querySelectorAll('input[data-table-filter]').forEach((inp) => {
    const rows = () => document.querySelectorAll(inp.dataset.tableFilter + ' tbody tr[data-search]');
    inp.addEventListener('input', () => {
      const q = inp.value.trim().toLowerCase(); let shown = 0;
      rows().forEach((r) => { const m = !q || r.dataset.search.includes(q); r.hidden = !m; if (m) shown++; });
      const none = document.querySelector(inp.dataset.tableFilter + '-none'); if (none) none.hidden = shown !== 0;
    });
  });

  // ---- clickable table rows
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('tr[data-href]');
    if (tr && !e.target.closest('a,button,form,input,select,textarea')) location.href = tr.dataset.href;
  });

  // ---- AJAX approval decisions (admin approvals page)
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-ajax-decision]');
    if (!b) return;
    e.preventDefault();
    const approve = b.dataset.ajaxDecision === 'approve', ref = b.dataset.ref;
    const r = await askConfirm({ title: approve ? 'Approve ' + ref + '?' : 'Reject ' + ref + '?',
      text: approve ? 'The requested change will be applied to the student record once.' : 'The student record will stay unchanged.',
      confirmLabel: approve ? 'Approve and apply' : 'Reject', danger: !approve, noteLabel: approve ? 'Remarks (optional)' : 'Reason for rejection', noteRequired: !approve });
    if (!r.ok) return;
    b.disabled = true;
    try {
      const res = await fetch('/api/approvals/' + b.dataset.id + '/' + (approve ? 'approve' : 'reject'), { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify({ note: r.note }) });
      const data = await res.json().catch(() => ({}));
      const row = b.closest('tr');
      if (res.ok && data.ok) {
        row.querySelector('.status-cell').innerHTML = '<span class="badge ' + (approve ? 'b-ok' : 'b-bad') + '">' + (approve ? 'approved' : 'rejected') + '</span>';
        row.querySelector('.action-cell').innerHTML = '<span class="note">' + (approve ? 'Applied just now' : 'Not applied') + '</span>';
        const pill = document.getElementById('nav-approvals');
        if (pill) { const n = parseInt(pill.textContent, 10) - 1; n > 0 ? pill.textContent = n : pill.remove(); }
        toast(ref + (approve ? ' approved and applied.' : ' rejected. No change was made.'), 'ok');
        setTimeout(() => window.location.reload(), 650);
      } else {
        toast(data.message || (res.status === 409 ? 'That request is no longer pending.' : 'Could not complete the action.'), 'error'); b.disabled = false;
      }
    } catch (err) { toast('Could not reach the server.', 'error'); b.disabled = false; }
  });

  // ---- copy to clipboard
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    try { await navigator.clipboard.writeText(b.dataset.copy); toast('Copied.', 'ok'); } catch (err) { toast('Copy is not available in this browser.', 'error'); }
  });
})();
