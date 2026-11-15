(() => {
  'use strict';

  const state = {
    token: localStorage.getItem('kira-token') || '',
    desktop: false,
    bootstrap: null,
    library: null,
    shoot: null,
    filter: 'kept',
    selected: new Set(),
    photo: null,
    version: null,
    cleanup: [],
    cleanupChosen: new Set(),
    watched: new Map(),
    browseTarget: null,
    browsePath: '',
    drive: null,
    drivePath: localStorage.getItem('kira-drive') || '',
    driveFilter: 'all',
    driveDeep: localStorage.getItem('kira-drive-deep') === '1',
    driveLimit: 0,
    gp: null,
    gpMonth: '',
    gpFilter: '',
    gpLimit: 0,
  };

  const el = (id) => document.getElementById(id);
  const views = ['pair', 'setup', 'library', 'shoot', 'import', 'drives', 'gphotos', 'review', 'ipad', 'cloud'];
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const plural = (count, word, many = `${word}s`) => `${count} ${count === 1 ? word : many}`;
  const bytes = (value) => {
    if (!value) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(units.length - 1, Math.floor(Math.log(value) / Math.log(1024)));
    return `${(value / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
  };
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : 'never');
  // Capture times come in camera format ("2026:07:04 10:00:00", local time); timestamps come as ISO 8601.
  const captureDate = (value) => {
    const match = /^(\d{4})[:-](\d\d)[:-](\d\d)[ T](\d\d):(\d\d):(\d\d)/.exec(value || '');
    if (!match) return null;
    const [, y, mo, d, h, mi, sec] = match.map(Number);
    return new Date(y, mo - 1, d, h, mi, sec);
  };
  const day = (value) => captureDate(value)?.toLocaleDateString() ?? '';

  function toast(message, isError = false) {
    const node = el('toast');
    node.textContent = message;
    node.classList.toggle('error', isError);
    node.classList.remove('hidden');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.add('hidden'), isError ? 7000 : 3500);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('X-Kira-Token', state.token);
    if (options.body && typeof options.body === 'string' && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, {...options, headers});
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = {error: text}; }
    if (!response.ok) {
      const error = new Error(payload.error || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  const post = (path, body = {}) => api(path, {method: 'POST', body: JSON.stringify(body)});
  const withToken = (path) => `${path}${path.includes('?') ? '&' : '?'}token=${encodeURIComponent(state.token)}`;
  const fileUrl = (shootId, photoId, name, suffix = '') => withToken(`/api/shoots/${shootId}/photos/${photoId}/${encodeURIComponent(name)}${suffix}`);
  const previewUrl = (shootId, photoId, name, size = 480) => fileUrl(shootId, photoId, name, `/preview?size=${size}`);

  async function guarded(button, work) {
    if (button) button.disabled = true;
    try { return await work(); } catch (error) { toast(error.message, true); return null; } finally { if (button) button.disabled = false; }
  }

  // -- Navigation -------------------------------------------------------------------

  function show(view) {
    views.forEach((name) => el(`view-${name}`).classList.toggle('hidden', name !== view));
    document.querySelectorAll('#nav a').forEach((link) => link.classList.toggle('active', link.dataset.view === view || (view === 'shoot' && link.dataset.view === 'library')));
  }

  async function route() {
    if (!state.token) return show('pair');
    if (!state.library) await loadLibrary();
    if (!state.library.configured) return show('setup');
    const [view, arg] = (location.hash.slice(1) || 'library').split('/');
    if (view === 'shoot' && arg) { show('shoot'); return openShoot(arg); }
    if (!views.includes(view) || view === 'pair' || view === 'setup') return show('library');
    show(view);
    if (view === 'library') renderLibrary();
    if (view === 'import') renderImport();
    if (view === 'drives') loadDrive();
    if (view === 'gphotos') loadGPhotos();
    if (view === 'review') loadReview();
    if (view === 'ipad') loadIpad();
    if (view === 'cloud') loadCloud();
  }

  // -- Start-up and pairing --------------------------------------------------------------

  async function start() {
    const pairingSecret = new URLSearchParams(location.search).get('pair');
    try {
      const response = await fetch('/api/bootstrap');
      if (response.ok) {
        state.bootstrap = await response.json();
        state.desktop = true;
        state.token = state.bootstrap.token;
        localStorage.setItem('kira-token', state.token);
      }
    } catch { /* remote devices cannot reach bootstrap */ }
    document.body.classList.toggle('is-desktop', state.desktop);
    document.body.classList.toggle('is-remote', !state.desktop);
    if (pairingSecret && !state.desktop) {
      try {
        const response = await api('/api/pair', {method: 'POST', body: JSON.stringify({secret: pairingSecret})});
        state.token = response.token;
        localStorage.setItem('kira-token', state.token);
        history.replaceState(null, '', '/#ipad');
      } catch {
        el('pair-error').textContent = 'That QR link has expired. Scan the current code shown on the desktop.';
      }
    }
    if (state.token) {
      try { await loadLibrary(); } catch (error) {
        if (error.status === 401) { localStorage.removeItem('kira-token'); state.token = ''; }
      }
    }
    setConnected(Boolean(state.token));
    el('nav').classList.toggle('hidden', !state.token);
    await route();
    setInterval(refreshBadges, 15000);
  }

  function setConnected(connected) {
    const node = el('connection-state');
    node.classList.toggle('connected', connected);
    node.lastChild.textContent = connected ? (state.desktop ? 'This PC' : 'Paired') : 'Not paired';
  }

  el('pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    el('pair-error').textContent = '';
    try {
      const response = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: el('pair-code').value})});
      state.token = response.token;
      localStorage.setItem('kira-token', state.token);
      setConnected(true);
      el('nav').classList.remove('hidden');
      await loadLibrary();
      location.hash = 'ipad';
      route();
    } catch (error) {
      el('pair-error').textContent = error.message;
    }
  });

  window.addEventListener('hashchange', route);

  // -- Library ---------------------------------------------------------------------------

  async function loadLibrary() {
    state.library = await api('/api/library');
    updateBadges();
    return state.library;
  }

  function updateBadges() {
    const lib = state.library || {};
    setBadge('review-badge', lib.review_count);
    setBadge('ipad-badge', lib.outbox_waiting);
  }

  function setBadge(id, count) {
    const node = el(id);
    node.textContent = count || '';
    node.classList.toggle('hidden', !count);
  }

  async function refreshBadges() {
    if (!state.token || document.hidden) return;
    try { await loadLibrary(); } catch { /* offline */ }
  }

  el('setup-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    el('setup-error').textContent = '';
    try {
      await post('/api/library/root', {path: el('setup-path').value});
      await loadLibrary();
      location.hash = 'library';
      route();
    } catch (error) {
      el('setup-error').textContent = error.message;
    }
  });

  function renderLibrary() {
    const lib = state.library;
    el('library-root').textContent = lib.root;
    el('library-offline').classList.toggle('hidden', lib.online);
    el('library-empty').classList.toggle('hidden', lib.shoots.length > 0);
    el('empty-trash').classList.toggle('hidden', !lib.trash_count);
    el('empty-trash').textContent = `Empty trash (${plural(lib.trash_count || 0, 'file')})`;
    el('shoot-grid').innerHTML = lib.shoots.map((shoot) => {
      const cover = shoot.cover ? `<img loading="lazy" src="${previewUrl(shoot.id, shoot.cover.photo_id, shoot.cover.file)}" alt="">` : '<div class="no-cover"></div>';
      const dates = shoot.first_capture ? `${day(shoot.first_capture)}${day(shoot.last_capture) !== day(shoot.first_capture) ? ` – ${day(shoot.last_capture)}` : ''}` : shoot.year;
      return `<a class="shoot-card" href="#shoot/${shoot.id}">${cover}<div class="shoot-card-body"><b>${escapeHtml(shoot.name)}</b><span class="muted">${escapeHtml(dates)}</span><span class="muted">${plural(shoot.photo_count, 'photo')} · ${shoot.edited_count} edited</span></div></a>`;
    }).join('');
  }

  el('empty-trash').addEventListener('click', () => guarded(el('empty-trash'), async () => {
    if (!confirm(`Permanently erase ${plural(state.library.trash_count, 'file')} in the library's _trash folder? This cannot be undone.`)) return;
    const result = await post('/api/trash/empty', {confirm: true});
    toast(`Erased ${plural(result.removed, 'file')}`);
    await loadLibrary();
    renderLibrary();
  }));

  el('new-shoot').addEventListener('click', async () => {
    const name = prompt('Name the shoot, for example "2026-07 NYC"');
    if (!name) return;
    await guarded(el('new-shoot'), async () => {
      const shoot = await post('/api/shoots', {name});
      await loadLibrary();
      location.hash = `shoot/${shoot.id}`;
    });
  });

  // -- Shoot -----------------------------------------------------------------------------

  async function openShoot(shootId, keepSelection = false) {
    try {
      state.shoot = await api(`/api/shoots/${shootId}`);
    } catch (error) {
      toast(error.message, true);
      location.hash = 'library';
      return;
    }
    if (!keepSelection) state.selected.clear();
    renderShoot();
  }

  function visiblePhotos() {
    const photos = state.shoot.photos;
    if (state.filter === 'rejected') return photos.filter((p) => p.rejected);
    const kept = photos.filter((p) => !p.rejected);
    if (state.filter === 'edited') return kept.filter((p) => p.files.some((f) => f.role === 'edit'));
    if (state.filter === 'unedited') return kept.filter((p) => !p.files.some((f) => f.role === 'edit'));
    return kept;
  }

  function renderShoot() {
    const shoot = state.shoot;
    el('shoot-title').textContent = shoot.name;
    el('shoot-meta').textContent = `${plural(shoot.photo_count, 'photo')} · ${shoot.edited_count} edited · ${shoot.rejected_count} rejected · ${shoot.path}`;
    const photos = visiblePhotos();
    el('shoot-empty').classList.toggle('hidden', photos.length > 0);
    el('photo-grid').innerHTML = photos.map((photo) => {
      const edits = photo.files.filter((f) => f.role === 'edit').length;
      const formats = [...new Set(photo.files.filter((f) => f.role === 'original').map((f) => f.name.split('.').pop().toUpperCase()))].join('+');
      const image = photo.cover ? `<img loading="lazy" src="${previewUrl(shoot.id, photo.id, photo.cover)}" alt="">` : `<div class="no-cover">${escapeHtml(formats || photo.kind)}</div>`;
      const badges = [
        edits ? `<span class="pill edited">${edits === 1 ? 'Edited' : `${edits} edits`}</span>` : '',
        photo.kind === 'video' ? '<span class="pill">Video</span>' : '',
      ].join('');
      return `<div class="photo-card ${state.selected.has(photo.id) ? 'selected' : ''}" data-id="${photo.id}">
        <button type="button" class="select-box" aria-label="Select ${escapeHtml(photo.stem)}" data-select="${photo.id}"></button>
        <button type="button" class="photo-open" data-open="${photo.id}">${image}</button>
        <div class="photo-caption"><span>${escapeHtml(photo.stem)}</span><span class="muted">${escapeHtml(formats)}</span></div>
        <div class="photo-badges">${badges}</div>
      </div>`;
    }).join('');
    updateSelection();
  }

  function updateSelection() {
    const count = state.selected.size;
    el('selection-count').textContent = count ? `${count} selected` : '';
    el('send-ipad').disabled = !count;
    el('reject-selected').disabled = !count;
    el('reject-selected').textContent = state.filter === 'rejected' ? 'Restore' : 'Reject';
    document.querySelectorAll('.photo-card').forEach((card) => card.classList.toggle('selected', state.selected.has(card.dataset.id)));
  }

  document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((item) => item.classList.toggle('active', item === button));
    state.selected.clear();
    renderShoot();
  }));

  el('photo-grid').addEventListener('click', (event) => {
    const select = event.target.closest('[data-select]');
    if (select) {
      const id = select.dataset.select;
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
      return updateSelection();
    }
    const open = event.target.closest('[data-open]');
    if (open) {
      if (event.shiftKey || event.ctrlKey || event.metaKey) {
        const id = open.dataset.open;
        state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
        return updateSelection();
      }
      openPhoto(open.dataset.open);
    }
  });

  el('select-all').addEventListener('click', () => {
    const photos = visiblePhotos();
    const all = photos.every((p) => state.selected.has(p.id));
    photos.forEach((p) => (all ? state.selected.delete(p.id) : state.selected.add(p.id)));
    updateSelection();
  });

  el('send-ipad').addEventListener('click', () => guarded(el('send-ipad'), async () => {
    const result = await post('/api/ipad/send', {shoot_id: state.shoot.id, photo_ids: [...state.selected], format: el('send-format').value});
    toast(`${plural(result.queued, 'photo')} waiting for the iPad${result.skipped.length ? ` (${result.skipped.length} skipped)` : ''}`);
    state.selected.clear();
    updateSelection();
    loadLibrary();
  }));

  el('reject-selected').addEventListener('click', () => guarded(el('reject-selected'), async () => {
    await post(`/api/shoots/${state.shoot.id}/reject`, {photo_ids: [...state.selected], rejected: state.filter !== 'rejected'});
    await openShoot(state.shoot.id);
  }));

  el('shoot-rename').addEventListener('click', async () => {
    const name = prompt('New name for this shoot', state.shoot.name);
    if (!name || name === state.shoot.name) return;
    await guarded(el('shoot-rename'), async () => {
      await post(`/api/shoots/${state.shoot.id}/rename`, {name});
      await Promise.all([openShoot(state.shoot.id), loadLibrary()]);
    });
  });

  el('shoot-rescan').addEventListener('click', () => guarded(el('shoot-rescan'), async () => {
    const report = await post(`/api/shoots/${state.shoot.id}/rescan`);
    const parts = [];
    if (report.added.length) parts.push(`${report.added.length} added`);
    if (report.changed.length) parts.push(`${report.changed.length} changed (kept as new versions)`);
    if (report.missing.length) parts.push(`${report.missing.length} missing`);
    if (report.waiting.length) parts.push(`${report.waiting.length} still being written`);
    toast(parts.length ? parts.join(', ') : 'Everything is up to date');
    await openShoot(state.shoot.id, true);
  }));

  el('shoot-reveal').addEventListener('click', () => guarded(el('shoot-reveal'), () => post(`/api/shoots/${state.shoot.id}/reveal`)));

  el('shoot-trash').addEventListener('click', async () => {
    const count = state.shoot.photos.length;
    if (!confirm(`Delete “${state.shoot.name}”${count ? ` and its ${plural(count, 'photo')}` : ''}? The folder moves to the library trash; nothing is erased until you empty the trash.`)) return;
    await guarded(el('shoot-trash'), async () => {
      const result = await post(`/api/shoots/${state.shoot.id}/trash`);
      toast(`Moved ${plural(result.trashed, 'file')} to the trash`);
      await loadLibrary();
      location.hash = '#library';
    });
  });

  // Drag and drop into a shoot
  const dropZone = el('drop-zone');
  ['dragenter', 'dragover'].forEach((type) => document.addEventListener(type, (event) => {
    if (el('view-shoot').classList.contains('hidden')) return;
    event.preventDefault();
    dropZone.classList.add('active');
  }));
  ['dragleave', 'drop'].forEach((type) => document.addEventListener(type, (event) => {
    if (type === 'dragleave' && event.relatedTarget) return;
    dropZone.classList.remove('active');
  }));
  document.addEventListener('drop', async (event) => {
    if (el('view-shoot').classList.contains('hidden')) return;
    event.preventDefault();
    const files = [...event.dataTransfer.files];
    if (!files.length) return;
    const counts = {};
    for (const [index, file] of files.entries()) {
      dropZone.textContent = `Adding ${index + 1} of ${files.length}: ${file.name}`;
      try {
        const result = await uploadFile(`/api/shoots/${state.shoot.id}/upload`, file);
        counts[result.status] = (counts[result.status] || 0) + 1;
      } catch (error) {
        counts.failed = (counts.failed || 0) + 1;
        toast(`${file.name}: ${error.message}`, true);
      }
    }
    dropZone.textContent = 'Drop files here to add them to this shoot. Kira files edits next to their originals.';
    toast(Object.entries(counts).map(([status, n]) => `${n} ${status}`).join(', '));
    await Promise.all([openShoot(state.shoot.id, true), loadLibrary()]);
  });

  async function uploadFile(path, file, onProgress = () => {}) {
    const sha256 = await KiraHash.fileSHA256(file, (done) => onProgress(done / file.size / 2));
    const headers = {'X-Kira-Sha256': sha256, 'Content-Type': 'application/octet-stream'};
    return api(`${path}?name=${encodeURIComponent(file.name)}`, {method: 'POST', body: file, headers});
  }

  // -- Photo detail -----------------------------------------------------------------------

  const roleLabel = (file) => ({original: 'Original', edit: `Edit ${file.edit || ''}`.trim(), copy: 'Copy', live: 'Live Photo video', sidecar: 'Sidecar', project: 'Project file'}[file.role] || file.role);

  const isVideoName = (name) => /\.(3g2|3gp|avi|m2ts|m4v|mkv|mov|mp4|mpe?g|mts|webm|wmv)$/i.test(name);

  // Shows a still, or plays a video with its frame as the poster. Browsers that can't decode the
  // video (HEVC without the Windows extension) keep the frame and say so.
  function setViewer(viewer, image, video = '') {
    const img = viewer.querySelector('img');
    const player = viewer.querySelector('video');
    const none = viewer.querySelector('.viewer-none');
    const showStill = (message) => {
      player.classList.add('hidden');
      player.removeAttribute('src');
      none.textContent = message;
      none.classList.toggle('hidden', !message);
      if (!image) return img.classList.add('hidden');
      img.classList.remove('hidden');
      img.onerror = () => { img.classList.add('hidden'); none.textContent = 'No preview for this file.'; none.classList.remove('hidden'); };
      img.src = image;
    };
    player.pause();
    if (!video) {
      showStill(image ? '' : 'No preview for this file.');
      player.load();
      return;
    }
    img.classList.add('hidden');
    none.classList.add('hidden');
    player.classList.remove('hidden');
    player.onerror = () => showStill("This browser can't play this video. Download it to watch.");
    player.poster = image;
    player.src = video;
  }

  document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('close', () => dialog.querySelectorAll('video').forEach((player) => player.pause())));

  function openPhoto(photoId) {
    state.photo = state.shoot.photos.find((p) => p.id === photoId);
    state.version = state.photo.final || state.photo.cover || state.photo.files[0]?.name;
    renderPhoto();
    el('photo-dialog').showModal();
  }

  function renderPhoto() {
    const photo = state.photo;
    el('photo-title').textContent = photo.stem;
    el('photo-meta').textContent = [captureDate(photo.capture_time)?.toLocaleString() ?? '', photo.model, photo.origin !== 'other' ? photo.origin : ''].filter(Boolean).join(' · ');
    const order = {original: 0, live: 1, edit: 2, project: 3, sidecar: 4, copy: 5};
    const files = [...photo.files].sort((a, b) => order[a.role] - order[b.role] || (a.edit || 0) - (b.edit || 0));
    el('version-list').innerHTML = files.map((file) => `
      <li class="${file.name === state.version ? 'current' : ''} ${file.missing ? 'missing' : ''}" data-version="${escapeHtml(file.name)}">
        <span class="version-role">${escapeHtml(roleLabel(file))}${file.name === photo.final ? ' <span class="pill final">Final</span>' : ''}</span>
        <span class="version-name">${escapeHtml(file.name)}</span>
        <span class="muted">${bytes(file.size)}${file.source ? ` · from ${escapeHtml(file.source)}` : ''}${file.missing ? ' · missing' : ''}</span>
      </li>`).join('');
    const file = photo.files.find((f) => f.name === state.version);
    const still = previewUrl(state.shoot.id, photo.id, state.version, 1600);
    const playable = file && !file.missing && isVideoName(file.name);
    setViewer(el('photo-viewer'), still, playable ? fileUrl(state.shoot.id, photo.id, state.version, '?inline=1') : '');
    const finalable = file && ['original', 'edit'].includes(file.role) && photo.kind !== 'video';
    el('make-final').disabled = !finalable || (photo.final === file.name && photo.final_pinned);
    el('make-final').textContent = photo.final === file?.name ? (photo.final_pinned ? 'Final' : 'Keep as Final') : 'Make Final';
    el('toggle-reject').textContent = photo.rejected ? 'Restore' : 'Reject';
  }

  el('version-list').addEventListener('click', (event) => {
    const item = event.target.closest('[data-version]');
    if (!item) return;
    state.version = item.dataset.version;
    renderPhoto();
  });

  el('make-final').addEventListener('click', () => guarded(el('make-final'), async () => {
    const photo = await post(`/api/shoots/${state.shoot.id}/final`, {photo_id: state.photo.id, name: state.version});
    Object.assign(state.photo, photo);
    renderPhoto();
    openShoot(state.shoot.id, true);
  }));

  el('download-version').addEventListener('click', () => {
    window.location.href = fileUrl(state.shoot.id, state.photo.id, state.version);
  });

  el('toggle-reject').addEventListener('click', () => guarded(el('toggle-reject'), async () => {
    await post(`/api/shoots/${state.shoot.id}/reject`, {photo_ids: [state.photo.id], rejected: !state.photo.rejected});
    el('photo-dialog').close();
    await openShoot(state.shoot.id, true);
  }));

  el('delete-version').addEventListener('click', async () => {
    const whole = state.photo.files.length === 1;
    if (!confirm(whole ? `Move ${state.photo.stem} to the library trash?` : `Move ${state.version} to the library trash? Other versions stay.`)) return;
    await guarded(el('delete-version'), async () => {
      await post(`/api/shoots/${state.shoot.id}/delete`, {photo_id: state.photo.id, name: state.version});
      el('photo-dialog').close();
      await openShoot(state.shoot.id, true);
    });
  });

  document.querySelectorAll('dialog [data-close]').forEach((button) => button.addEventListener('click', () => button.closest('dialog').close()));

  // -- Folder browser -------------------------------------------------------------------------

  document.querySelectorAll('[data-browse]').forEach((button) => button.addEventListener('click', () => {
    state.browseTarget = button.dataset.browse;
    browse(el(state.browseTarget).value || '');
    el('browse-dialog').showModal();
  }));

  async function browse(path) {
    try {
      const result = await api(`/api/browse?path=${encodeURIComponent(path)}`);
      state.browsePath = result.current;
      el('browse-current').textContent = result.current || 'Choose a drive';
      el('browse-choose').disabled = !result.current;
      el('browse-crumbs').innerHTML = result.breadcrumbs.map((crumb) => `<button type="button" data-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.name)}</button>`).join('<span>›</span>');
      const up = result.parent !== null && result.current ? `<li><button type="button" data-path="${escapeHtml(result.parent)}">↑ Up</button></li>` : (result.current ? '<li><button type="button" data-path="">↑ Drives</button></li>' : '');
      el('browse-list').innerHTML = up + result.directories.map((dir) => `<li><button type="button" data-path="${escapeHtml(dir.path)}">${escapeHtml(dir.name || dir.path)}</button></li>`).join('');
    } catch (error) {
      if (path) return browse('');
      toast(error.message, true);
    }
  }

  el('browse-dialog').addEventListener('click', (event) => {
    const target = event.target.closest('[data-path]');
    if (target) browse(target.dataset.path);
  });
  el('browse-choose').addEventListener('click', () => {
    el(state.browseTarget).value = state.browsePath;
    el(state.browseTarget).dispatchEvent(new Event('change'));
    el('browse-dialog').close();
  });

  // -- Import ---------------------------------------------------------------------------------

  function shootOptions(select) {
    const current = select.value;
    select.innerHTML = '<option value="">New shoot…</option>' + state.library.shoots.map((shoot) => `<option value="${shoot.id}">${escapeHtml(shoot.name)}</option>`).join('');
    select.value = state.library.shoots.some((s) => s.id === current) ? current : '';
  }

  function renderImport() {
    shootOptions(el('import-shoot'));
    shootOptions(el('picker-shoot'));
    el('import-shoot-name').classList.toggle('hidden', Boolean(el('import-shoot').value));
    el('picker-shoot-name').classList.toggle('hidden', Boolean(el('picker-shoot').value));
  }
  el('import-shoot').addEventListener('change', renderImport);
  el('picker-shoot').addEventListener('change', renderImport);

  function shootChoice(select, input) {
    return select.value ? {shoot_id: select.value} : {shoot_name: input.value.trim()};
  }

  function describeImport(report) {
    const counts = report.counts || {};
    const labels = {new: 'new photos', original: 'extra originals (RAW/JPEG pairs)', edit: 'edits filed with their originals', copy: 'lower-quality copies set aside', duplicate: 'already in the library', review: 'waiting for review', sidecar: 'sidecars', project: 'project files', loose: 'loose files', ignored: 'ignored'};
    const lines = Object.entries(counts).map(([key, value]) => `<li>${value} ${escapeHtml(labels[key] || key)}</li>`);
    if (report.failed?.length) lines.push(`<li class="error-text">${report.failed.length} failed: ${escapeHtml(report.failed.slice(0, 3).map((f) => `${f.name} (${f.error})`).join(', '))}</li>`);
    return `<ul>${lines.join('') || '<li>Nothing new.</li>'}</ul>`;
  }

  el('import-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const button = event.submitter;
    guarded(button, async () => {
      const task = await post('/api/import', {source: el('import-source').value, ...shootChoice(el('import-shoot'), el('import-shoot-name'))});
      el('import-result').innerHTML = '<p class="muted">Importing… you can keep working.</p>';
      watchTask(task, async (done) => {
        await loadLibrary();
        renderImport();
        if (done.state === 'done') {
          el('import-result').innerHTML = describeImport(done.result) + `<a href="#shoot/${done.result.shoot_id}">Open the shoot →</a>`;
        } else {
          el('import-result').innerHTML = `<p class="error-text">${escapeHtml(done.error || done.state)}</p>`;
        }
      });
    });
  });

  el('import-check').addEventListener('click', () => guarded(el('import-check'), async () => {
    const result = await post('/api/import/check', {source: el('import-source').value});
    const verdict = result.safe_to_wipe
      ? '<p class="notice ok">Safe to wipe: every file is in the library and backed up.</p>'
      : `<p class="notice warn">Not yet safe to wipe.</p>`;
    el('import-result').innerHTML = `${verdict}<ul><li>${result.imported} of ${result.total} files are in the library</li><li>${result.backed_up} backed up to Drive or Google Photos</li>${result.not_imported.length ? `<li>Not imported: ${escapeHtml(result.not_imported.slice(0, 8).join(', '))}${result.not_imported.length > 8 ? '…' : ''}</li>` : ''}</ul>`;
  }));

  function describePickerFiling(google) {
    if (!google) return '';
    const parts = [];
    if (google.archived) parts.push(`<p class="notice ok">${plural(google.archived, 'photo')} added to the “${escapeHtml(google.album)}” album and archived in Google Photos, so the picker won't offer them again.</p>`);
    if (google.not_found.length) parts.push(`<p class="muted">Couldn't find these in Google Photos to archive, so they were left as they are: ${google.not_found.map(escapeHtml).join(', ')}</p>`);
    if (google.error) parts.push(`<p class="error-text">${escapeHtml(google.error)}</p>`);
    return parts.join('');
  }

  el('picker-start').addEventListener('click',() => guarded(el('picker-start'), async () => {
    const choice = shootChoice(el('picker-shoot'), el('picker-shoot-name'));
    if (!choice.shoot_id && !choice.shoot_name) throw new Error('Choose a shoot or name a new one first');
    const session = await post('/api/photos/picker');
    window.open(session.picker_uri, '_blank', 'noopener');
    el('picker-result').innerHTML = '<p class="muted">Choose photos in the Google Photos tab, then press Done there. Waiting…</p>';
    for (let attempt = 0; attempt < 360; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 5000));
      const status = await api(`/api/photos/picker/${session.id}`);
      if (status.ready) {
        const task = await post(`/api/photos/picker/${session.id}/import`, choice);
        el('picker-result').innerHTML = '<p class="muted">Downloading the chosen photos…</p>';
        watchTask(task, async (done) => {
          await loadLibrary();
          renderImport();
          el('picker-result').innerHTML = done.state === 'done' ? describeImport(done.result) + describePickerFiling(done.result.google) + `<a href="#shoot/${done.result.shoot_id}">Open the shoot →</a>` : `<p class="error-text">${escapeHtml(done.error)}</p>`;
        });
        return;
      }
    }
    el('picker-result').innerHTML = '<p class="error-text">The picker timed out. Start again.</p>';
  }));

  // -- Review -----------------------------------------------------------------------------------

  async function loadReview() {
    const {items} = await api('/api/review');
    el('review-empty').classList.toggle('hidden', items.length > 0);
    el('review-list').innerHTML = items.map((item) => `
      <div class="card review-item" data-review="${item.id}">
        <img loading="lazy" src="${withToken(`/api/review/${item.id}/preview?size=480`)}" alt="" onerror="this.classList.add('hidden')">
        <div class="stack">
          <b>${escapeHtml(item.name)}</b>
          <span class="muted">${escapeHtml(item.reason)} · from ${escapeHtml(item.source)}${item.capture_time ? ` · taken ${escapeHtml(day(item.capture_time))}` : ''}</span>
          <div class="candidates">${item.candidates.map((c) => `
            <div class="candidate">
              ${c.cover ? `<img loading="lazy" src="${previewUrl(c.shoot_id, c.photo_id, c.cover)}" alt="">` : '<div class="no-cover"></div>'}
              <span>${escapeHtml(c.stem)}<br><span class="muted">${escapeHtml(c.shoot_name)}</span></span>
              <button class="primary-button small" type="button" data-action="edit" data-shoot="${c.shoot_id}" data-photo="${c.photo_id}">Edit of this</button>
              <button class="quiet-button small" type="button" data-action="copy" data-shoot="${c.shoot_id}" data-photo="${c.photo_id}">Copy of this</button>
            </div>`).join('')}</div>
          <div class="row">
            <button class="quiet-button small" type="button" data-action="new">It's a new photo</button>
            <button class="danger-button small" type="button" data-action="discard">Discard</button>
          </div>
        </div>
      </div>`).join('');
  }

  el('review-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const id = button.closest('[data-review]').dataset.review;
    guarded(button, async () => {
      await post(`/api/review/${id}`, {action: button.dataset.action, shoot_id: button.dataset.shoot, photo_id: button.dataset.photo});
      await Promise.all([loadReview(), loadLibrary()]);
    });
  });

  // -- iPad --------------------------------------------------------------------------------------

  async function loadIpad() {
    const {items} = await api('/api/ipad/outbox');
    const visible = items.filter((item) => item.state === 'waiting' || !item.returned);
    el('outbox-empty').classList.toggle('hidden', visible.length > 0);
    el('outbox-list').innerHTML = visible.map((item) => `
      <div class="outbox-item">
        <img loading="lazy" src="${previewUrl(item.shoot_id, item.photo_id, item.file_name)}" alt="" onerror="this.classList.add('hidden')">
        <div><b>${escapeHtml(item.file_name)}</b><br><span class="muted">${escapeHtml(item.shoot_name)} · ${bytes(item.size)} · ${item.state === 'waiting' ? 'waiting' : item.returned ? 'edit received' : 'on the iPad'}</span></div>
        ${state.desktop ? '' : `<a class="quiet-button small link-button" href="${escapeHtml(item.url)}" download>Download</a>`}
        <button class="quiet-button small" type="button" data-remove="${item.id}" aria-label="Remove">✕</button>
      </div>`).join('');
    const waiting = items.filter((item) => item.state === 'waiting');
    el('ipad-download-all').classList.toggle('hidden', !waiting.length);
    if (state.desktop && state.bootstrap) {
      el('pair-qr').src = `${state.bootstrap.pair_qr_url}?v=${Date.now()}`;
      el('ipad-url').textContent = state.bootstrap.ipad_url;
      el('pair-code-display').textContent = state.bootstrap.pair_code;
      const setup = await api('/api/ipad/setup');
      el('receive-url').textContent = setup.receive_url;
      el('outbox-url').textContent = setup.outbox_urls;
    }
  }

  el('outbox-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove]');
    if (button) guarded(button, async () => { await api(`/api/ipad/outbox/${button.dataset.remove}`, {method: 'DELETE'}); await Promise.all([loadIpad(), loadLibrary()]); });
  });
  el('outbox-clear').addEventListener('click', () => guarded(el('outbox-clear'), async () => { await post('/api/ipad/outbox/clear'); loadIpad(); }));

  el('ipad-download-all').addEventListener('click', async (event) => {
    event.preventDefault();
    const {items} = await api('/api/ipad/outbox?state=waiting');
    for (const item of items) {
      const link = document.createElement('a');
      link.href = item.url;
      link.download = item.file_name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      await new Promise((resolve) => setTimeout(resolve, 900));
    }
    setTimeout(loadIpad, 1500);
  });

  el('ipad-upload').addEventListener('change', async (event) => {
    const files = [...event.target.files];
    const out = el('ipad-upload-result');
    const lines = [];
    for (const [index, file] of files.entries()) {
      out.innerHTML = `<p class="muted">Sending ${index + 1} of ${files.length}: ${escapeHtml(file.name)}</p>`;
      try {
        const result = await uploadFile('/api/ipad/receive', file);
        lines.push(`<li>${escapeHtml(file.name)}: ${escapeHtml(result.detail || result.status)}</li>`);
      } catch (error) {
        lines.push(`<li class="error-text">${escapeHtml(file.name)}: ${escapeHtml(error.message)}</li>`);
      }
    }
    out.innerHTML = `<ul>${lines.join('')}</ul>`;
    event.target.value = '';
    loadIpad();
    loadLibrary();
  });

  document.querySelectorAll('[data-copy]').forEach((button) => button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(el(button.dataset.copy).textContent); toast('Copied'); } catch { toast('Copy failed; select the text instead', true); }
  }));

  // -- Cloud --------------------------------------------------------------------------------------

  async function loadCloud() {
    const status = await api('/api/google/status');
    const account = status.account;
    el('google-account-state').textContent = !account.configured
      ? 'Add Google OAuth credentials (google-oauth-client.json) next to Kira to enable backup and uploads.'
      : account.connected ? (account.needs_consent ? 'Connected, but Kira needs extra permissions. Click Connect again.' : 'Connected for Drive backup, Photos uploads and the Photos picker.') : 'Not connected.';
    el('google-connect').textContent = account.connected ? 'Reconnect' : 'Connect Google';
    el('google-connect').disabled = !account.configured;
    el('google-disconnect').classList.toggle('hidden', !account.connected);
    const drive = status.drive;
    el('drive-facts').innerHTML = `
      <dt>Backed up</dt><dd>${drive.backed_up ?? 0} files</dd>
      <dt>Waiting</dt><dd>${drive.pending ?? 0} files${drive.pending_bytes ? ` (${bytes(drive.pending_bytes)})` : ''}</dd>
      <dt>Last run</dt><dd>${escapeHtml(when(drive.last_run))}</dd>
      ${drive.last_error ? `<dt>Last problem</dt><dd class="error-text">${escapeHtml(drive.last_error)}</dd>` : ''}
      ${status.missing ? `<dt>Missing locally</dt><dd class="error-text">${status.missing} files</dd>` : ''}`;
    el('drive-backup').disabled = !drive.connected;
    el('drive-restore').classList.toggle('hidden', !status.missing);
    const photos = status.photos;
    el('photos-facts').innerHTML = `
      <dt>Finals to upload</dt><dd>${photos.pending_finals}${photos.skipped_finals ? ` (${photos.skipped_finals} can't be uploaded, e.g. RAW)` : ''}</dd>
      <dt>Web session</dt><dd>${photos.web_signed_in ? `signed in${photos.web.account ? ` as ${escapeHtml(photos.web.account)}` : ''}` : 'not signed in'}</dd>
      <dt>Last check</dt><dd>${escapeHtml(when(photos.last_check))}</dd>
      ${photos.phone_not_in_photos ? `<dt>Phone photos not in Google Photos</dt><dd class="error-text">${photos.phone_not_in_photos} (kept on Drive instead)</dd>` : ''}`;
    el('photos-upload').disabled = !photos.upload_connected || !photos.pending_finals;
    el('photos-check').disabled = !photos.web_signed_in;
    el('cleanup-plan').disabled = !photos.web_signed_in;
    el('web-signin').textContent = photos.web_signed_in ? 'Sign out of Photos web' : 'Sign in to Photos web';
    const warn = (drive.pending || 0) + (status.missing || 0);
    setBadge('cloud-badge', warn);
    const plan = await api('/api/photos/cleanup');
    state.cleanup = plan.items || [];
    state.cleanupChosen = new Set(state.cleanup.map((item) => item.media_key));
    renderCleanup();
  }

  function renderCleanup() {
    const items = state.cleanup;
    el('cleanup-actions').classList.toggle('hidden', !items.length);
    el('cleanup-list').innerHTML = items.length ? items.map((item) => `
      <label class="cleanup-item">
        <input type="checkbox" data-key="${escapeHtml(item.media_key)}" ${state.cleanupChosen.has(item.media_key) ? 'checked' : ''}>
        <img loading="lazy" src="${previewUrl(item.shoot_id, item.photo_id, item.name)}" alt="" onerror="this.classList.add('hidden')">
        <span><b>${escapeHtml(item.name)}</b><br><span class="muted">${escapeHtml(item.shoot_name)} · ${escapeHtml(item.reason_text)}</span></span>
      </label>`).join('') : '<p class="muted">Click Find copies to see what could be hidden.</p>';
    el('cleanup-apply').textContent = `Hide ${state.cleanupChosen.size} in Google Photos`;
    el('cleanup-apply').disabled = !state.cleanupChosen.size;
    el('cleanup-toggle').textContent = state.cleanupChosen.size === items.length ? 'Select none' : 'Select all';
  }

  el('cleanup-list').addEventListener('change', (event) => {
    const key = event.target.dataset.key;
    if (!key) return;
    event.target.checked ? state.cleanupChosen.add(key) : state.cleanupChosen.delete(key);
    renderCleanup();
  });
  el('cleanup-toggle').addEventListener('click', () => {
    state.cleanupChosen = state.cleanupChosen.size === state.cleanup.length ? new Set() : new Set(state.cleanup.map((item) => item.media_key));
    renderCleanup();
  });

  el('google-connect').addEventListener('click', () => guarded(el('google-connect'), async () => {
    const {url} = await post('/api/google/connect');
    window.open(url, '_blank');
    const poll = setInterval(async () => {
      const status = await api('/api/google/status');
      if (status.account.connected && !status.account.needs_consent) { clearInterval(poll); loadCloud(); }
    }, 3000);
    setTimeout(() => clearInterval(poll), 300000);
  }));
  el('google-disconnect').addEventListener('click', () => guarded(el('google-disconnect'), async () => { await post('/api/google/disconnect'); loadCloud(); }));

  el('web-signin').addEventListener('click', () => guarded(el('web-signin'), async () => {
    const status = await api('/api/google/status');
    if (status.photos.web_signed_in) {
      if (!confirm('Sign Kira out of the Google Photos web session?')) return;
      await api('/api/google/web-session', {method: 'DELETE'});
      return loadCloud();
    }
    if (await webSignIn('photos-result')) loadCloud();
  }));

  // Opens the Google Photos web sign-in window and reports progress in `output`; true once signed in.
  async function webSignIn(output) {
    await post('/api/google/web-signin');
    el(output).innerHTML = '<p class="muted">Sign in to Google Photos in the window that opened. When your photos appear, close that window.</p>';
    for (let attempt = 0; attempt < 400; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 2500));
      const web = await api('/api/google/web-signin');
      const signin = web.sign_in || {};
      if (signin.status === 'verifying') el(output).innerHTML = '<p class="muted">Checking the sign-in…</p>';
      // Only a finished, verified sign-in counts; an older saved session may still be on file.
      if (signin.status === 'completed' && web.connected) { el(output).innerHTML = `<p class="notice ok">Signed in${web.account ? ` as ${escapeHtml(web.account)}` : ''}.</p>`; return true; }
      if (['failed', 'cancelled'].includes(signin.status)) { el(output).innerHTML = `<p class="error-text">${escapeHtml(signin.error || 'Sign-in was cancelled')}</p>`; return false; }
    }
    return false;
  }

  function cloudTask(button, path, output, describe) {
    button.addEventListener('click', () => guarded(button, async () => {
      const task = await post(path);
      el(output).innerHTML = `<p class="muted">${escapeHtml(task.title)}…</p>`;
      watchTask(task, async (done) => {
        el(output).innerHTML = done.state === 'done' ? describe(done.result) : `<p class="error-text">${escapeHtml(done.error || done.state)}</p>`;
        loadCloud();
      });
    }));
  }

  const failures = (result) => (result.failed?.length ? `<p class="error-text">${result.failed.length} failed, e.g. ${escapeHtml(result.failed[0].file)}: ${escapeHtml(result.failed[0].error)}</p>` : '');
  cloudTask(el('drive-backup'), '/api/backup/run', 'drive-result', (r) => `<p class="notice ok">${r.uploaded} uploaded, ${r.moved} moved on Drive.</p>${failures(r)}`);
  cloudTask(el('drive-restore'), '/api/backup/restore', 'drive-result', (r) => `<p class="notice ok">${r.restored} files restored from Drive.</p>${failures(r)}`);
  cloudTask(el('photos-upload'), '/api/photos/upload-finals', 'photos-result', (r) => `<p class="notice ok">${r.uploaded} Finals uploaded to their shoot albums.</p>${r.skipped.length ? `<p class="muted">${r.skipped.length} skipped: ${escapeHtml(r.skipped.slice(0, 3).map((s) => `${s.file} (${s.reason})`).join(', '))}</p>` : ''}${failures(r)}`);
  cloudTask(el('photos-check'), '/api/photos/check', 'photos-result', (r) => `<p class="notice ok">Checked ${r.checked} files; Google Photos holds ${r.present}.</p>${r.phone_not_in_photos ? `<p class="notice warn">${r.phone_not_in_photos} phone photos aren't in Google Photos as exact copies, so Drive backup keeps them.</p>` : ''}`);

  el('cleanup-plan').addEventListener('click', () => guarded(el('cleanup-plan'), async () => {
    const task = await post('/api/photos/cleanup/plan');
    el('cleanup-list').innerHTML = '<p class="muted">Looking up copies in Google Photos…</p>';
    watchTask(task, (done) => {
      if (done.state !== 'done') { el('cleanup-list').innerHTML = `<p class="error-text">${escapeHtml(done.error)}</p>`; return; }
      state.cleanup = done.result.items;
      state.cleanupChosen = new Set(state.cleanup.map((item) => item.media_key));
      if (!state.cleanup.length) { el('cleanup-list').innerHTML = '<p class="notice ok">Nothing to hide. Google Photos only shows your Finals.</p>'; el('cleanup-actions').classList.add('hidden'); return; }
      renderCleanup();
    });
  }));

  el('cleanup-apply').addEventListener('click', () => guarded(el('cleanup-apply'), async () => {
    if (!confirm(`Hide ${state.cleanupChosen.size} items in Google Photos? They move to Archive and stay in your account.`)) return;
    const task = await post('/api/photos/cleanup/apply', {media_keys: [...state.cleanupChosen]});
    watchTask(task, (done) => {
      toast(done.state === 'done' ? `${done.result.archived} items hidden` : done.error, done.state !== 'done');
      loadCloud();
    });
  }));

  // -- Drives -----------------------------------------------------------------------------------

  const sourcePreview = (path, size = 480) => withToken(`/api/source/preview?size=${size}&path=${encodeURIComponent(path)}`);
  const sourceFile = (path) => withToken(`/api/source/file?path=${encodeURIComponent(path)}`);
  const isMedia = (file) => ['photo', 'video'].includes(file.kind);
  const extension = (name) => (name.includes('.') ? name.split('.').pop().toUpperCase() : '');
  const isRaw = (name) => /\.(3fr|arw|cr2|cr3|crw|dng|erf|iiq|kdc|mef|mos|mrw|nef|nrw|orf|pef|raf|rw2|rwl|sr2|srf|srw|x3f)$/i.test(name);
  // A thumbnail that fails to load becomes the same placeholder a file without one gets.
  const noCover = (label = '') => `this.outerHTML='<div class=&quot;no-cover&quot;>${escapeHtml(label)}</div>'`;

  function folderCard(dir) {
    const isDrive = dir.media === undefined;
    const cover = dir.cover
      ? `<img loading="lazy" src="${sourcePreview(dir.cover)}" alt="" onerror="${noCover()}">`
      : `<div class="no-cover folder">${isDrive ? 'Drive' : ''}</div>`;
    const counts = isDrive
      ? dir.path
      : [dir.media ? plural(dir.media, 'photo or video', 'photos and videos') : '', dir.folders ? plural(dir.folders, 'folder') : ''].filter(Boolean).join(' · ') || 'No photos';
    return `<a class="shoot-card" href="#drives" data-drive-path="${escapeHtml(dir.path)}">${cover}<div class="shoot-card-body"><b>${escapeHtml(dir.name || dir.path)}</b><span class="muted">${escapeHtml(counts)}</span></div></a>`;
  }

  // One card per capture, like a shoot: IMG_1234.HEIC, .MOV and .AAE side by side are one item.
  function driveGroups(files) {
    const groups = new Map();
    for (const file of files) {
      const stem = file.name.replace(/\.[^.]+$/, '');
      const key = `${file.folder || ''}\${stem}`.toLowerCase();
      if (!groups.has(key)) groups.set(key, {stem, folder: file.folder || '', files: []});
      groups.get(key).files.push(file);
    }
    const rank = (file) => (file.kind === 'photo' ? (isRaw(file.name) ? 1 : 0) : file.kind === 'video' ? 2 : 3);
    return [...groups.values()].map((group) => {
      const media = group.files.filter(isMedia);
      const cover = [...group.files].sort((a, b) => rank(a) - rank(b))[0];
      return {
        ...group,
        cover: isMedia(cover) ? cover : null,
        video: media.length > 0 && media.every((file) => file.kind === 'video'),
        size: group.files.reduce((total, file) => total + file.size, 0),
        inLibrary: media.filter((file) => file.library),
        missing: media.filter((file) => file.checked && !file.library),
        duplicates: group.files.filter((file) => file.duplicate_of),
        copies: Math.max(0, ...group.files.map((file) => file.copies || 0)),
        unchecked: group.files.some((file) => !file.checked),
      };
    });
  }

  async function loadDrive(path = state.drivePath) {
    el('drive-path').value = path;
    if (!path) {
      const roots = await api('/api/browse?path=');
      state.drive = null;
      el('drive-crumbs').innerHTML = '';
      el('drive-title').textContent = 'Drives';
      el('drive-summary').textContent = 'Look through a card or any folder without importing it. Kira shows which files the library already has and which repeat on the same drive. Nothing on the drive is moved.';
      el('drive-folders').innerHTML = roots.directories.map((dir) => folderCard({name: dir.name, path: dir.path})).join('');
      el('drive-grid').innerHTML = '';
      el('drive-empty').classList.add('hidden');
      el('drive-toolbar').classList.add('hidden');
      el('drive-more-row').classList.add('hidden');
      ['drive-scan', 'drive-links', 'drive-trash-copies', 'drive-import'].forEach((id) => el(id).classList.add('hidden'));
      return;
    }
    try {
      state.drive = await api(`/api/source?path=${encodeURIComponent(path)}${state.driveDeep ? '&recursive=1' : ''}`);
    } catch (error) {
      toast(error.message, true);
      return loadDrive('');
    }
    state.drivePath = state.drive.current;
    localStorage.setItem('kira-drive', state.drivePath);
    renderDrive();
  }

  function openDrive(path) {
    el('drives-result').innerHTML = '';
    state.driveLimit = DRIVE_PAGE;
    state.drivePath = path;
    if (!path) localStorage.removeItem('kira-drive');
    return loadDrive(path);
  }

  // Every file below a big card runs to thousands; draw them a page at a time.
  const DRIVE_PAGE = 600;

  function renderDrive() {
    const drive = state.drive;
    const sum = drive.summary;
    el('drive-path').value = drive.current;
    const parents = [{name: '← Drives', path: ''}, ...drive.breadcrumbs.slice(0, -1)];
    el('drive-crumbs').innerHTML = parents.map((crumb) => `<button type="button" data-drive-path="${escapeHtml(crumb.path)}">${escapeHtml(crumb.name)}</button>`).join('<span>›</span>');
    el('drive-title').textContent = drive.breadcrumbs.at(-1)?.name || drive.current;
    const unchecked = sum.files - sum.checked;
    const whole = drive.drive.copies ? ` Whole drive: ${plural(drive.drive.copies, 'extra copy', 'extra copies')} of ${plural(drive.drive.groups, 'file')} (${bytes(drive.drive.copy_bytes)}).` : '';
    const counts = sum.files ? `${plural(sum.files, 'file')} · ${sum.in_library} in the library · ${sum.not_in_library} not in the library · ${plural(sum.duplicates, 'duplicate')}${unchecked ? ` · ${unchecked} not checked yet` : ''} · ` : '';
    el('drive-summary').textContent = drive.library === 'inside'
      ? 'This folder is inside the library. Open it from the Library page instead.'
      : `${counts}${drive.current}${whole}`;
    el('drive-scan').classList.toggle('hidden', drive.library === 'inside');
    el('drive-import').classList.toggle('hidden', drive.library !== 'other-drive');
    el('drive-links').classList.toggle('hidden', drive.library !== 'other-drive' || !drive.drive.copies);
    el('drive-trash-copies').classList.toggle('hidden', drive.library !== 'same-drive');
    el('drive-folders').innerHTML = drive.recursive ? '' : drive.directories.map(folderCard).join('');
    const groups = driveGroups(drive.files);
    const shown = groups.filter((group) => {
      if (state.driveFilter === 'missing') return group.missing.length > 0;
      if (state.driveFilter === 'duplicates') return group.duplicates.length > 0 || group.copies > 0;
      return true;
    });
    state.driveShown = shown;
    const limit = state.driveLimit || DRIVE_PAGE;
    el('drive-toolbar').classList.toggle('hidden', !groups.length && !drive.directories.length);
    el('drive-count').textContent = groups.length ? plural(shown.length, 'item') : '';
    el('drive-empty').classList.toggle('hidden', shown.length > 0 || (!groups.length && !drive.recursive && drive.directories.length > 0));
    el('drive-empty').textContent = state.driveFilter === 'all' ? 'No photos or videos here.' : drive.summary.checked < drive.summary.files ? 'Nothing found among the checked files. Click Check files to check the rest.' : 'Nothing found.';
    el('drive-more-row').classList.toggle('hidden', shown.length <= limit);
    el('drive-more').textContent = `Show more (${shown.length - limit} left)`;
    el('drive-grid').innerHTML = shown.slice(0, limit).map((group, index) => {
      const formats = [...new Set(group.files.map((file) => extension(file.name)))].join('+');
      const image = group.cover
        ? `<img loading="lazy" src="${sourcePreview(group.cover.path)}" alt="" onerror="${noCover(formats)}">`
        : `<div class="no-cover">${escapeHtml(formats)}</div>`;
      const shoots = [...new Set(group.inLibrary.map((file) => file.library.shoot_name))].join(', ');
      const badges = [
        group.missing.length ? '<span class="pill warn">Not in library</span>' : group.inLibrary.length ? `<span class="pill ok" title="In ${escapeHtml(shoots)}">In library</span>` : '',
        group.duplicates.length ? '<span class="pill warn">Duplicate</span>' : '',
        group.copies ? `<span class="pill" title="Kept; the same bytes appear ${plural(group.copies, 'more time')} on this drive">+${group.copies}</span>` : '',
        group.unchecked ? '<span class="pill">Not checked</span>' : '',
        group.video ? '<span class="pill">Video</span>' : '',
      ].join('');
      const why = group.duplicates.length ? `<div class="photo-why">Same bytes as ${escapeHtml(group.duplicates[0].duplicate_of)}</div>` : '';
      return `<div class="photo-card">
        <button type="button" class="photo-open" data-drive-group="${index}">${image}</button>
        <div class="photo-caption"><span title="${escapeHtml(group.stem)}">${escapeHtml(group.stem)}</span><span class="muted">${escapeHtml(formats)}</span></div>
        ${group.folder ? `<div class="photo-folder" title="${escapeHtml(group.folder)}">${escapeHtml(group.folder)}</div>` : ''}${why}<div class="photo-badges">${badges}</div>
      </div>`;
    }).join('');
  }

  function openSource(group, path) {
    state.sourceGroup = group;
    const file = group.files.find((item) => item.path === path) || group.cover || group.files[0];
    el('source-title').textContent = group.stem;
    el('source-meta').textContent = [group.folder, plural(group.files.length, 'file'), bytes(group.size)].filter(Boolean).join(' · ');
    el('source-list').innerHTML = group.files.map((item) => {
      const status = [
        bytes(item.size),
        item.library ? `in ${item.library.shoot_name}` : item.checked && isMedia(item) ? 'not in the library' : '',
        item.duplicate_of ? `same bytes as ${item.duplicate_of}` : '',
        item.checked ? '' : 'not checked',
      ].filter(Boolean).join(' · ');
      return `<li class="${item === file ? 'current' : ''}" data-source-file="${escapeHtml(item.path)}"><span class="version-name">${escapeHtml(item.name)}</span><span class="muted">${escapeHtml(status)}</span></li>`;
    }).join('');
    const still = isMedia(file) ? sourcePreview(file.path, 1600) : '';
    setViewer(el('source-viewer'), still, file.kind === 'video' ? sourceFile(file.path) : '');
    if (!el('source-dialog').open) el('source-dialog').showModal();
  }

  el('source-list').addEventListener('click', (event) => {
    const item = event.target.closest('[data-source-file]');
    if (item) openSource(state.sourceGroup, item.dataset.sourceFile);
  });

  el('view-drives').addEventListener('click', (event) => {
    const folder = event.target.closest('[data-drive-path]');
    if (folder) {
      event.preventDefault();
      return openDrive(folder.dataset.drivePath);
    }
    const open = event.target.closest('[data-drive-group]');
    if (open) openSource(state.driveShown[Number(open.dataset.driveGroup)]);
  });
  document.querySelectorAll('[data-drive-scope]').forEach((button) => {
    button.classList.toggle('active', (button.dataset.driveScope === 'deep') === state.driveDeep);
    button.addEventListener('click', () => {
      state.driveDeep = button.dataset.driveScope === 'deep';
      localStorage.setItem('kira-drive-deep', state.driveDeep ? '1' : '0');
      document.querySelectorAll('[data-drive-scope]').forEach((item) => item.classList.toggle('active', item === button));
      if (state.drivePath) openDrive(state.drivePath);
    });
  });
  el('drive-more').addEventListener('click', () => {
    state.driveLimit = (state.driveLimit || DRIVE_PAGE) + DRIVE_PAGE;
    renderDrive();
  });
  el('drive-path').addEventListener('change', () => openDrive(el('drive-path').value.trim()));
  document.querySelectorAll('[data-drive-filter]').forEach((button) => button.addEventListener('click', () => {
    state.driveFilter = button.dataset.driveFilter;
    state.driveLimit = DRIVE_PAGE;
    document.querySelectorAll('[data-drive-filter]').forEach((item) => item.classList.toggle('active', item === button));
    if (state.drive) renderDrive();
  }));

  el('drive-scan').addEventListener('click', () => guarded(el('drive-scan'), async () => {
    const task = await post('/api/source/scan', {path: state.drivePath});
    el('drives-result').innerHTML = '<p class="muted">Checking files… you can keep browsing.</p>';
    watchTask(task, async (done) => {
      if (done.state === 'done') {
        const r = done.result;
        el('drives-result').innerHTML = `<p class="notice ok">Checked ${plural(r.total, 'file')}: ${r.hashed} hashed now, the rest already known.</p>${r.failed.length ? `<p class="error-text">${r.failed.length} could not be read, e.g. ${escapeHtml(r.failed[0].name)}</p>` : ''}`;
      } else {
        el('drives-result').innerHTML = `<p class="error-text">${escapeHtml(done.error || done.state)}</p>`;
      }
      await loadDrive();
    });
  }));

  el('drive-links').addEventListener('click', () => guarded(el('drive-links'), async () => {
    const r = await post('/api/source/links', {path: state.drivePath});
    el('drives-result').innerHTML = `<p class="notice ok">Wrote ${plural(r.links, 'shortcut')} for ${plural(r.groups, 'duplicated file')} to <code>${escapeHtml(r.folder)}</code>. Nothing was moved; DUPLICATES.txt there lists which copy each one repeats.</p>`;
    await loadDrive();
  }));

  el('drive-trash-copies').addEventListener('click', () => guarded(el('drive-trash-copies'), async () => {
    if (!confirm('Move every checked file under this folder whose exact bytes are already in the library into the library trash? A WHY.txt in the trash records where each came from.')) return;
    const r = await post('/api/source/trash-copies', {path: state.drivePath});
    el('drives-result').innerHTML = `<p class="notice ok">Moved ${plural(r.moved, 'file')} to <code>${escapeHtml(r.trash_folder || 'the trash')}</code>.</p>${r.failed.length ? `<p class="error-text">${r.failed.length} could not be moved, e.g. ${escapeHtml(r.failed[0].name)}: ${escapeHtml(r.failed[0].error)}</p>` : ''}`;
    await loadLibrary();
    await loadDrive();
  }));

  el('drive-import').addEventListener('click', () => {
    el('import-source').value = state.drivePath;
    location.hash = 'import';
  });

  // -- Google Photos ----------------------------------------------------------------------------

  // Google refuses its thumbnail links without the signed-in session, so Kira fetches and caches them.
  const gpThumb = (mediaKey, size = 400) => withToken(`/api/gphotos/thumbnail?size=${size}&key=${encodeURIComponent(mediaKey)}`);
  const gpStatus = {
    missing: '<span class="pill warn">Not in library</span>',
    similar: '<span class="pill" title="The library has this shot under the same name, as a different file">Same shot</span>',
    library: '<span class="pill ok">In library</span>',
  };
  const monthName = (month) => {
    const [year, number] = month.split('-').map(Number);
    return number ? new Date(year, number - 1, 1).toLocaleDateString(undefined, {month: 'long', year: 'numeric'}) : 'No date';
  };
  const gpCounts = (counts) => [
    counts.missing ? `${counts.missing} not in library` : '',
    counts.similar ? `${counts.similar} same shot, other file` : '',
    counts.library ? `${counts.library} in library` : '',
  ].filter(Boolean).join(' · ');

  async function loadGPhotos() {
    const params = new URLSearchParams({month: state.gpMonth, status: state.gpFilter});
    try {
      state.gp = await api(`/api/gphotos/index?${params}`);
    } catch (error) {
      return toast(error.message, true);
    }
    renderGPhotos();
  }

  function openGPMonth(month) {
    state.gpMonth = month;
    state.gpLimit = DRIVE_PAGE;
    document.querySelectorAll('[data-gp-scope]').forEach((item) => item.classList.toggle('active', (item.dataset.gpScope === 'all') === (month === 'all')));
    return loadGPhotos();
  }

  function renderGPhotos() {
    const gp = state.gp;
    const scanned = Boolean(gp.scanned_at);
    el('gp-signin').classList.toggle('hidden', gp.web.connected);
    el('gp-scan').disabled = !gp.web.connected;
    el('gp-scan').textContent = scanned ? 'Scan again' : 'Scan Google Photos';
    setBadge('gphotos-badge', gp.totals.missing);
    const inMonth = state.gpMonth && state.gpMonth !== 'all';
    el('gp-crumbs').innerHTML = inMonth ? '<button type="button" data-gp-month="">← Google Photos</button>' : '';
    el('gp-title').textContent = inMonth ? monthName(state.gpMonth) : 'Google Photos';
    const month = gp.months.find((item) => item.month === state.gpMonth);
    const totals = inMonth ? month || {items: 0} : gp.totals;
    el('gp-summary').textContent = !gp.web.connected && !scanned
      ? 'Sign in to Photos web, then scan to see which Google Photos items the library doesn\'t have yet.'
      : !scanned
        ? 'Scan to see which Google Photos items the library doesn\'t have yet.'
        : `${plural(totals.items, 'item')}${gpCounts(totals) ? ` · ${gpCounts(totals)}` : ''} · scanned ${when(gp.scanned_at)}`;
    el('gp-toolbar').classList.toggle('hidden', !gp.totals.items);

    // Month cards when nothing narrower is open; a filter hides months without such items.
    const months = state.gpMonth ? [] : gp.months.filter((item) => !state.gpFilter || item[state.gpFilter]);
    el('gp-months').innerHTML = months.map((item) => {
      const cover = item.cover ? `<img loading="lazy" src="${escapeHtml(gpThumb(item.cover))}" alt="" onerror="${noCover()}">` : '<div class="no-cover"></div>';
      return `<a class="shoot-card" href="#gphotos" data-gp-month="${escapeHtml(item.month)}">${cover}<div class="shoot-card-body"><b>${escapeHtml(monthName(item.month))}</b><span class="muted">${escapeHtml(plural(item.items, 'item'))}</span><span class="muted">${escapeHtml(gpCounts(item))}</span></div></a>`;
    }).join('');

    const items = state.gpMonth ? gp.items : [];
    const limit = state.gpLimit || DRIVE_PAGE;
    state.gpShown = items;
    el('gp-count').textContent = state.gpMonth ? plural(items.length, 'item') : plural(months.length, 'month');
    el('gp-more-row').classList.toggle('hidden', items.length <= limit);
    el('gp-more').textContent = `Show more (${items.length - limit} left)`;
    el('gp-grid').innerHTML = items.slice(0, limit).map((item, index) => {
      const label = item.file_name.includes('.') ? item.file_name.split('.').pop().toUpperCase() : '';
      const image = item.thumbnail ? `<img loading="lazy" src="${escapeHtml(gpThumb(item.media_key))}" alt="" onerror="${noCover(label)}">` : `<div class="no-cover">${escapeHtml(label)}</div>`;
      const badges = [gpStatus[item.status] || '', item.video ? '<span class="pill">Video</span>' : '', item.archived ? '<span class="pill" title="Archived in Google Photos">Archived</span>' : ''].join('');
      return `<div class="photo-card">
        <button type="button" class="photo-open" data-gp-item="${index}">${image}</button>
        <div class="photo-caption"><span title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name || 'Unnamed')}</span><span class="muted">${escapeHtml(day(item.local_time))}</span></div>
        <div class="photo-badges">${badges}</div>
      </div>`;
    }).join('');
    const nothing = state.gpMonth ? !items.length : !months.length;
    el('gp-empty').classList.toggle('hidden', !scanned || !nothing);
    el('gp-empty').textContent = state.gpFilter === 'missing' ? 'Everything here is in the library.' : 'Nothing here.';
  }

  function openGPItem(item) {
    el('gp-item-title').textContent = item.file_name || 'Unnamed';
    const where = item.match;
    const status = {
      library: `In the library: ${where?.shoot_name} / ${where?.file}`,
      similar: `Same shot as ${where?.shoot_name} / ${where?.file}, but a different file (for example a converted or edited copy).`,
      missing: 'Not in the library.',
    }[item.status] || 'Not compared yet.';
    el('gp-item-facts').innerHTML = `
      <dt>Status</dt><dd>${escapeHtml(status)}</dd>
      <dt>Taken</dt><dd>${escapeHtml(captureDate(item.local_time)?.toLocaleString() ?? 'unknown')}</dd>
      <dt>File</dt><dd>${item.size ? bytes(item.size) : 'size unknown'} · ${item.width}×${item.height}${item.video ? ' · video' : ''}</dd>
      <dt>Stored as</dt><dd>${item.original_quality ? 'Original quality' : 'Storage saver (compressed by Google)'}${item.archived ? ' · archived' : ''}${item.owned ? '' : ' · shared with you'}</dd>`;
    el('gp-image').src = item.thumbnail ? gpThumb(item.media_key, 1600) : '';
    el('gp-open').href = `https://photos.google.com/photo/${encodeURIComponent(item.media_key)}`;
    el('gp-shoot').classList.toggle('hidden', !where);
    if (where) el('gp-shoot').href = `#shoot/${where.shoot_id}`;
    el('gp-dialog').showModal();
  }

  el('gp-shoot').addEventListener('click', () => el('gp-dialog').close());

  el('view-gphotos').addEventListener('click', (event) => {
    const month = event.target.closest('[data-gp-month]');
    if (month) {
      event.preventDefault();
      return openGPMonth(month.dataset.gpMonth);
    }
    const item = event.target.closest('[data-gp-item]');
    if (item) openGPItem(state.gpShown[Number(item.dataset.gpItem)]);
  });
  document.querySelectorAll('[data-gp-scope]').forEach((button) => button.addEventListener('click', () => openGPMonth(button.dataset.gpScope === 'all' ? 'all' : '')));
  document.querySelectorAll('[data-gp-filter]').forEach((button) => button.addEventListener('click', () => {
    state.gpFilter = button.dataset.gpFilter;
    state.gpLimit = DRIVE_PAGE;
    document.querySelectorAll('[data-gp-filter]').forEach((item) => item.classList.toggle('active', item === button));
    loadGPhotos();
  }));
  el('gp-more').addEventListener('click', () => {
    state.gpLimit = (state.gpLimit || DRIVE_PAGE) + DRIVE_PAGE;
    renderGPhotos();
  });
  el('gp-signin').addEventListener('click', () => guarded(el('gp-signin'), async () => {
    if (await webSignIn('gp-result')) loadGPhotos();
  }));
  el('gp-scan').addEventListener('click', () => guarded(el('gp-scan'), async () => {
    const task = await post('/api/gphotos/scan');
    el('gp-result').innerHTML = '<p class="muted">Reading Google Photos… you can keep using Kira.</p>';
    watchTask(task, async (done) => {
      if (done.state === 'done' && !done.result.cancelled) {
        const r = done.result;
        el('gp-result').innerHTML = `<p class="notice ok">Compared ${plural(r.items, 'item')}: ${r.missing} not in the library, ${r.similar} the same shot as a different file, ${r.library} in the library.</p>`;
      } else {
        el('gp-result').innerHTML = `<p class="error-text">${escapeHtml(done.error || done.state)}</p>`;
      }
      await loadGPhotos();
      // The saved session can expire while still on file; offer a fresh sign-in after any failure.
      if (done.state !== 'done') el('gp-signin').classList.remove('hidden');
    });
  }));

  // -- Background tasks -----------------------------------------------------------------------

  function watchTask(task, onDone) {
    state.watched.set(task.id, {task, onDone});
    renderTasks();
    if (!watchTask.timer) watchTask.timer = setInterval(pollTasks, 1200);
  }

  async function pollTasks() {
    if (!state.watched.size) { clearInterval(watchTask.timer); watchTask.timer = null; return; }
    for (const [id, entry] of [...state.watched]) {
      try {
        const task = await api(`/api/tasks/${id}`);
        entry.task = task;
        if (task.state !== 'running') {
          state.watched.delete(id);
          if (task.state === 'failed') toast(`${task.title}: ${task.error}`, true);
          await entry.onDone(task);
        }
      } catch {
        state.watched.delete(id);
      }
    }
    renderTasks();
  }

  function renderTasks() {
    el('tasks').innerHTML = [...state.watched.values()].map(({task}) => {
      const percent = task.total ? Math.round((task.done / task.total) * 100) : 0;
      return `<div class="task"><div class="row spread"><b>${escapeHtml(task.title)}</b><span class="muted">${task.total ? `${task.done}/${task.total}` : ''}</span></div>
        <div class="bar"><span style="width:${percent}%"></span></div><span class="muted task-current">${escapeHtml(task.current || '')}</span></div>`;
    }).join('');
  }

  start();
})();
