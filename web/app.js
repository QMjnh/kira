(() => {
  const CHUNK_SIZE = 16 * 1024 * 1024;
  const state = {
    token: localStorage.getItem('kira-token') || '',
    desktop: false,
    bootstrap: null,
    jobs: [],
    currentJob: null,
    tagFilter: new Set(),
    dateFrom: '',
    dateTo: '',
    rightPaneOpen: false,
    tileAnchor: {left: null, right: null},
    libraryPanes: {
      left: emptyPane(),
      right: emptyPane(),
    },
    activePaneId: 'left',
    compareZoom: 1,
    comparePan: {x: 0, y: 0},
    googleDownloadSettingsOpen: false,
    showRawOnly: localStorage.getItem('kira-show-raw-only') === 'true',
    google: {configured: false, connected: false, organizer: {}},
  };

  const el = (id) => document.getElementById(id);
  const pairView = el('pair-view');
  const dashboardView = el('dashboard-view');
  const jobView = el('job-view');
  const connectionState = el('connection-state');
  let googleSignInPolling = false;

  function showOnly(view) {
    [pairView, dashboardView, jobView].forEach((item) => item.classList.toggle('hidden', item !== view));
  }

  function setConnected(label = 'Connected directly') {
    connectionState.classList.add('connected');
    connectionState.innerHTML = '<span></span>' + label;
  }

  function setDisconnected(label = 'Not connected') {
    connectionState.classList.remove('connected');
    connectionState.innerHTML = '<span></span>' + label;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('X-Kira-Token', state.token);
    if (options.body && typeof options.body === 'string') headers.set('Content-Type', 'application/json');
    const response = await fetch(path, {...options, headers});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error || `Request failed (${response.status})`);
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }

  async function initialize() {
    const pairingSecret = new URLSearchParams(window.location.search).get('pair');
    try {
      const bootstrapResponse = await fetch('/api/bootstrap');
      if (bootstrapResponse.ok) {
        state.bootstrap = await bootstrapResponse.json();
        state.desktop = true;
        state.token = state.bootstrap.token;
        localStorage.setItem('kira-token', state.token);
      }
    } catch (_) {
      // A remote device cannot access the local bootstrap endpoint.
    }

    if (pairingSecret && !state.desktop) {
      setDisconnected('Pairing with desktop…');
      try {
        const response = await api('/api/pair', {
          method: 'POST',
          body: JSON.stringify({secret: pairingSecret}),
        });
        state.token = response.token;
        localStorage.setItem('kira-token', state.token);
        history.replaceState({}, document.title, window.location.pathname);
      } catch (error) {
        localStorage.removeItem('kira-token');
        state.token = '';
        history.replaceState({}, document.title, window.location.pathname);
        el('pair-error').textContent = 'That QR link has expired. Scan the current code shown on the desktop.';
      }
    }

    if (!state.token) {
      setDisconnected('Pairing required');
      showOnly(pairView);
      return;
    }

    try {
      await loadDashboard();
    } catch (error) {
      if (error.status === 401) {
        localStorage.removeItem('kira-token');
        state.token = '';
        setDisconnected('Pairing required');
        showOnly(pairView);
        return;
      }
      setDisconnected('Connection error');
      toast(error.message);
    }
  }

  async function loadDashboard() {
    const response = await api('/api/jobs');
    state.jobs = response.jobs;
    setConnected(state.desktop ? 'Running on this desktop' : 'Connected to desktop');
    showOnly(dashboardView);
    el('desktop-services').classList.toggle('hidden', !state.desktop);
    el('desktop-setup').classList.toggle('hidden', !state.desktop);
    el('open-history').classList.toggle('hidden', !state.desktop);
    el('create-job-card').classList.toggle('hidden', !state.desktop);
    el('google-photos-card').classList.toggle('hidden', !state.desktop);
    el('photo-workspace').classList.toggle('hidden', !state.desktop);
    if (state.desktop && state.bootstrap) {
      el('ipad-url').textContent = state.bootstrap.ipad_url;
      el('pair-code-display').textContent = state.bootstrap.pair_code;
      el('data-dir').textContent = state.bootstrap.data_dir;
      el('pair-qr').src = `${state.bootstrap.pair_qr_url}?v=${Date.now()}`;
    }
    if (state.desktop) {
      renderPhotoGrid();
      await refreshGoogleStatus();
      await checkGoogleHealth();
      await navigatePane('left', '').catch(showError);
    }
    renderJobs();
  }

  async function refreshGoogleStatus() {
    try {
      state.google = await api('/api/google/status');
      renderGoogleStatus();
      return state.google;
    } catch (error) {
      el('google-status').textContent = 'Unavailable';
      el('google-copy').textContent = error.message;
      return state.google;
    }
  }

  async function checkGoogleHealth() {
    const notice = el('google-health-notice');
    try {
      const health = await api('/api/google/health');
      if (health.ok) {
        notice.classList.add('hidden');
        notice.textContent = '';
        return health;
      }
      notice.textContent = health.message || 'Google Photos needs to reconnect.';
      notice.classList.remove('hidden');
      return health;
    } catch (error) {
      notice.textContent = error.message || 'Could not check the Google Photos connection.';
      notice.classList.remove('hidden');
      return null;
    }
  }

  function renderGoogleStatus() {
    const google = state.google;
    el('google-status').textContent = google.connected ? 'Connected' : google.configured ? 'Not connected' : 'Setup required';
    el('google-connect').classList.toggle('hidden', google.connected);
    el('google-pick').classList.toggle('hidden', !google.connected);
    el('google-disconnect').classList.toggle('hidden', !google.connected);
    el('google-connect').disabled = !google.configured;
    el('google-copy').textContent = google.configured
      ? google.connected
        ? 'Choose media in Google Photos, then save it into the folder below. Exact duplicates are skipped.'
        : 'Connect once, then choose Google photos and videos to save into a Kira folder.'
      : `Download a Desktop OAuth credential from Google Cloud and save it as ${google.credentials_path}`;
    renderGoogleImportPreferences();
    renderGoogleOrganizer();
    renderGoogleDestination();
    updateSelectionUi();
  }

  function renderGoogleOrganizer() {
    const organizer = state.google.organizer || {};
    const connected = Boolean(organizer.connected);
    const directory = paneFor().directory;
    const organizerStatus = el('google-organizer-status');
    organizerStatus.textContent = connected
      ? 'Connected'
      : organizer.available === false ? 'Client unavailable' : 'Not connected';
    organizerStatus.title = connected && organizer.account
      ? `Google account: ${organizer.account}`
      : '';
    el('google-organizer-connect').classList.toggle('hidden', connected);
    el('google-organizer-controls').classList.toggle('hidden', !connected);
    renderGoogleSignIn(organizer.sign_in);
    if (['starting', 'waiting'].includes(organizer.sign_in?.status)) pollGoogleSignIn().catch(showError);
    if (!el('google-album-title').value) {
      el('google-album-title').value = localStorage.getItem('kira-google-album-title') || '';
    }
    const matchSource = el('google-match-source');
    matchSource.querySelector('strong').textContent = directory || 'Open a local folder below first.';
    const matchButton = el('google-match-folder');
    matchButton.disabled = !connected
      || !directory
      || !el('google-album-title').value.trim();
    matchButton.textContent = el('google-archive-after-upload').checked
      ? 'Match folder → album + archive'
      : 'Match folder → album';
  }

  function renderGoogleSignIn(signIn = {status: 'idle'}) {
    const pending = ['starting', 'waiting'].includes(signIn.status);
    const unavailable = state.google.organizer?.available === false || state.google.organizer?.browser_available === false;
    el('google-organizer-signin').disabled = pending || unavailable;
    el('google-organizer-reconnect').disabled = pending || unavailable;
    el('google-organizer-connect-button').disabled = pending || state.google.organizer?.available === false;
    el('google-signin-cancel').classList.toggle('hidden', !pending);
    el('google-signin-message').textContent = signIn.status === 'starting'
      ? 'Opening Google Photos on this desktop…'
      : signIn.status === 'waiting' ? 'Complete sign-in in the Google Photos window. Kira will connect automatically.'
      : signIn.status === 'failed' ? signIn.error || 'Google Photos sign-in failed. Try again.'
      : signIn.status === 'cancelled' ? 'Sign-in cancelled.'
      : unavailable && state.google.organizer?.available !== false ? 'Install Chrome or Edge to sign in, or import a session manually.' : '';
  }

  async function pollGoogleSignIn() {
    if (googleSignInPolling) return;
    googleSignInPolling = true;
    try {
      while (true) {
        const signIn = await api('/api/google/web-signin');
        state.google.organizer.sign_in = signIn;
        renderGoogleSignIn(signIn);
        if (signIn.status === 'completed') {
          await refreshGoogleStatus();
          await checkGoogleHealth();
          toast('Google Photos organizer connected.');
          return;
        }
        if (signIn.status === 'failed') throw new Error(signIn.error || 'Google Photos sign-in failed. Try again.');
        if (!['starting', 'waiting'].includes(signIn.status)) return;
        await wait(1000);
      }
    } finally {
      googleSignInPolling = false;
    }
  }

  function googleImportPreferences() {
    const threshold = Number.parseInt(el('google-zip-threshold').value, 10);
    return {
      download_mode: el('google-download-mode').value,
      zip_threshold: Number.isFinite(threshold) ? threshold : 50,
    };
  }

  function renderGoogleImportPreferences() {
    el('google-download-mode').value = localStorage.getItem('kira-google-download-mode') || 'automatic';
    el('google-zip-threshold').value = localStorage.getItem('kira-google-zip-threshold') || '50';
    const storedDestination = localStorage.getItem('kira-google-destination-folder');
    el('google-destination-folder').value = storedDestination && storedDestination !== '/inbox'
      ? storedDestination
      : (state.google.inbox || '');
    updateGooglePreferenceUi();
  }

  function updateGooglePreferenceUi() {
    const mode = el('google-download-mode').value;
    el('google-zip-threshold-field').classList.toggle('hidden', mode !== 'automatic');
    el('google-import-settings').classList.toggle('hidden', !state.googleDownloadSettingsOpen);
    const toggle = el('google-download-settings-toggle');
    toggle.setAttribute('aria-expanded', String(state.googleDownloadSettingsOpen));
    toggle.classList.toggle('is-open', state.googleDownloadSettingsOpen);
  }

  function saveGoogleImportPreferences() {
    const preferences = googleImportPreferences();
    if (preferences.zip_threshold < 2 || preferences.zip_threshold > 2000) {
      throw new Error('Automatic ZIP threshold must be between 2 and 2000 items.');
    }
    localStorage.setItem('kira-google-download-mode', preferences.download_mode);
    localStorage.setItem('kira-google-zip-threshold', String(preferences.zip_threshold));
  }

  function renderGoogleDestination() {
    const destinationFolder = el('google-destination-folder').value.trim() || state.google.inbox || '—';
    el('google-destination').textContent = destinationFolder;
    el('google-pick').textContent = 'Add Google media';
    localStorage.setItem('kira-google-destination-folder', destinationFolder);
  }

  function googleImportAlbumTitle(destinationFolder) {
    const value = destinationFolder.trim();
    const normalized = value.replace(/[\\/]+$/, '').toLowerCase();
    if (!value || normalized === '/inbox' || normalized === 'inbox' || value === state.google.inbox) {
      return 'inbox';
    }
    return value.split(/[\\/]/).filter(Boolean).pop() || 'inbox';
  }

  function wait(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  async function waitForGoogleConnection() {
    for (let attempt = 0; attempt < 120; attempt += 1) {
      await wait(1000);
      const status = await refreshGoogleStatus();
      if (status.connected) {
        toast('Google Photos connected.');
        await checkGoogleHealth();
        return;
      }
    }
    toast('Google sign-in is still waiting. You can try Connect again.');
  }

  async function pollPickerSession(sessionId) {
    el('google-progress').classList.remove('hidden');
    el('google-progress-label').textContent = 'Waiting for your Google Photos selection…';
    el('google-progress-value').textContent = '';
    el('google-progress-bar').removeAttribute('value');
    for (let attempt = 0; attempt < 400; attempt += 1) {
      const session = await api(`/api/google/picker/sessions/${encodeURIComponent(sessionId)}`);
      if (session.mediaItemsSet) return session;
      const duration = Number.parseFloat(session.pollingConfig?.pollInterval || '2');
      await wait(Math.max(1000, duration * 1000));
    }
    throw new Error('Google Photos selection timed out. Try choosing photos again.');
  }

  async function pollGoogleOperation(operationId) {
    const progress = el('google-progress');
    progress.classList.remove('hidden');
    while (true) {
      const operation = await api(`/api/google/operations/${encodeURIComponent(operationId)}`);
      const processed = operation.kind === 'match_folder'
        ? Number(operation.scanned || 0)
        : operation.completed + operation.duplicates + operation.failed;
      const percent = operation.total ? Math.round((processed / operation.total) * 100) : 0;
      el('google-progress-label').textContent = operation.kind === 'match_folder' && operation.phase === 'matching_google_photos'
        ? 'Scanning Google Photos for matching image content…'
        : operation.kind === 'match_folder' && operation.phase === 'organizing_google_photos'
          ? `${operation.archive ? 'Adding matched items to the album and archiving' : 'Adding matched items to the album'}…`
        : operation.kind === 'import' && operation.phase === 'organizing_google_photos'
          ? `${operation.archive ? 'Creating the album and archiving' : 'Creating the album'}…`
        : operation.kind === 'import'
        ? `Checking and importing ${processed} of ${operation.total}…`
        : operation.kind === 'match_folder'
          ? `Matching folder content ${processed} of ${operation.total}…`
          : operation.kind === 'organize'
          ? `${operation.archive ? 'Adding to album and archiving' : 'Adding to album'} ${processed} of ${operation.total}…`
          : `Uploading ${processed} of ${operation.total}…`;
      el('google-progress-value').textContent = `${percent}%`;
      el('google-progress-bar').value = percent;
      if (['complete', 'complete_with_errors', 'failed'].includes(operation.status)) return operation;
      await wait(750);
    }
  }

  async function importGoogleSelection(sessionId) {
    const preferences = googleImportPreferences();
    const destinationFolder = el('google-destination-folder').value.trim() || state.google.inbox || '';
    const albumTitle = googleImportAlbumTitle(destinationFolder);
    const archive = el('google-import-archive').checked;
    const operation = await api('/api/google/imports', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sessionId,
        destination_folder: destinationFolder,
        download_mode: preferences.download_mode,
        zip_threshold: preferences.zip_threshold,
        album_title: archive ? albumTitle : '',
        archive,
      }),
    });
    const finished = await pollGoogleOperation(operation.id);
    if (finished.status === 'failed') throw new Error(finished.error || 'Google Photos import failed.');
    const result = await api(`/api/local/scan?path=${encodeURIComponent(finished.directory)}`);
    applyLibraryScan(result, state.activePaneId);
    await renderPaneBrowserForDirectory(state.activePaneId, finished.directory);
    const details = [
      `${finished.completed} added`,
      `${finished.duplicates} exact duplicate${finished.duplicates === 1 ? '' : 's'} skipped`,
      `${finished.possible_edits} possible edit${finished.possible_edits === 1 ? '' : 's'}`,
      `${finished.related_variants} related variant${finished.related_variants === 1 ? '' : 's'}`,
    ];
    if (finished.failed) details.push(`${finished.failed} failed`);
    details.push(finished.download_mode === 'zip' ? 'temporary ZIP extracted and removed' : 'saved as individual files');
    if (finished.organize_status === 'complete') {
      details.push(`${finished.organized} added to ${finished.album?.title || 'album'}${finished.archived ? ' and archived' : ''}`);
    } else if (finished.organize_status === 'partial') {
      details.push(
        `${finished.organized} added to ${finished.album?.title || 'album'}${finished.archived ? ' and archived' : ''}; ` +
        `${finished.organize_unmatched} downloaded item${finished.organize_unmatched === 1 ? '' : 's'} could not be matched in Google Photos`,
      );
    } else if (finished.organize_error) {
      details.push(`album and Archive failed: ${finished.organize_error}`);
    }
    el('google-result').textContent = `Import complete: ${details.join(' · ')}`;
    el('google-result').classList.remove('hidden');
    toast(`Google import complete: ${details.join(', ')}.`);
  }

  function renderJobs() {
    const list = el('jobs-list');
    list.replaceChildren();
    el('empty-jobs').classList.toggle('hidden', state.jobs.length !== 0);
    state.jobs.forEach((job) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'job-card';
      button.innerHTML = `
        <div class="job-card-top">
          <div><div class="eyebrow">${escapeHtml(formatDate(job.created_at))}</div><h3>${escapeHtml(job.name)}</h3></div>
          <span class="job-count">${job.file_count} media files</span>
        </div>
        <p>${job.return_count ? `${job.return_count} edits returned` : 'Waiting for Lightroom exports'}</p>
        <div class="job-stats"><span><strong>${job.file_count}</strong> sent</span><span><strong>${job.matched_count}</strong> matched</span></div>`;
      button.addEventListener('click', () => openJob(job.id));
      list.appendChild(button);
    });
  }

  async function renderPaneBrowserForDirectory(paneId, path, pane = paneFor(paneId), request = pane.navigationRequest) {
    const browse = await api(`/api/local/browse?path=${encodeURIComponent(path || '')}`);
    if (paneFor(paneId) !== pane || pane.navigationRequest !== request) return false;
    pane.parent = browse.parent;
    pane.breadcrumbs = browse.breadcrumbs || [];
    pane.directories = browse.directories || [];
    renderPaneBrowser(paneId);
    return true;
  }
  async function navigatePane(paneId, path = '') {
    const pane = paneFor(paneId);
    const request = pane.navigationRequest = (pane.navigationRequest || 0) + 1;
    if (!await renderPaneBrowserForDirectory(paneId, path, pane, request)) return;
    if (path) {
      const scan = await api(`/api/local/scan?path=${encodeURIComponent(path)}`);
      if (paneFor(paneId) !== pane || pane.navigationRequest !== request) return;
      applyLibraryScan(scan, paneId);
    } else {
      pane.directory = '';
      pane.assets = [];
      pane.culling = null;
      pane.selectedAssets.clear();
      setActivePane(paneId);
      renderPhotoGrid();
    }
  }

  function renderPaneBrowser(paneId) {
    const pane = paneFor(paneId);
    const crumbs = el(`${paneId}-folder-path`);
    crumbs.replaceChildren();
    const desktop = document.createElement('button');
    desktop.type = 'button';
    desktop.textContent = 'Desktop';
    if (!pane.breadcrumbs.length) desktop.disabled = true;
    else desktop.addEventListener('click', () => navigatePane(paneId, '').catch(showError));
    crumbs.appendChild(desktop);
    pane.breadcrumbs.forEach((crumb, index) => {
      const separator = document.createElement('span');
      separator.textContent = '›';
      separator.setAttribute('aria-hidden', 'true');
      crumbs.appendChild(separator);
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = crumb.name;
      button.title = crumb.path;
      if (index === pane.breadcrumbs.length - 1) button.disabled = true;
      else button.addEventListener('click', () => navigatePane(paneId, crumb.path).catch(showError));
      crumbs.appendChild(button);
    });
    el(`${paneId}-folder-up`).disabled = !pane.breadcrumbs.length;

    const list = el(`${paneId}-folder-list`);
    list.replaceChildren();
    const atRoots = !pane.breadcrumbs.length;
    pane.directories.forEach((directory) => {
      const row = document.createElement('div');
      row.className = 'folder-row';
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'folder-row-name';
      open.textContent = `📁 ${directory.name}`;
      open.title = directory.path;
      open.addEventListener('click', () => navigatePane(paneId, directory.path).catch(showError));
      row.appendChild(open);
      if (!atRoots) {
        const renameButton = document.createElement('button');
        renameButton.type = 'button';
        renameButton.className = 'folder-row-action';
        renameButton.textContent = 'Rename';
        renameButton.setAttribute('aria-label', `Rename ${directory.name}`);
        renameButton.addEventListener('click', () => renameFolderInPane(paneId, directory).catch(showError));
        const deleteButton = document.createElement('button');
        deleteButton.type = 'button';
        deleteButton.className = 'folder-row-action danger';
        deleteButton.textContent = 'Delete';
        deleteButton.setAttribute('aria-label', `Delete ${directory.name}`);
        deleteButton.addEventListener('click', () => deleteFolderInPane(paneId, directory).catch(showError));
        row.append(renameButton, deleteButton);
      }
      list.appendChild(row);
    });
    if (!pane.directories.length) {
      const empty = document.createElement('p');
      empty.className = 'empty-folders';
      empty.textContent = atRoots ? 'No drives found.' : 'No subfolders here.';
      list.appendChild(empty);
    }
  }

  async function createFolderInPane(paneId) {
    const pane = paneFor(paneId);
    if (!pane.directory) return;
    const name = window.prompt('Name for the new folder:', '');
    if (name === null) return;
    await api('/api/local/mkdir', {
      method: 'POST',
      body: JSON.stringify({parent_path: pane.directory, name}),
    });
    toast(`Created folder “${name.trim()}”.`);
    await navigatePane(paneId, pane.directory);
  }

  async function renameFolderInPane(paneId, directory) {
    const name = window.prompt(`Rename “${directory.name}” to:`, directory.name);
    if (name === null || name.trim() === directory.name) return;
    const result = await api('/api/local/rename', {
      method: 'POST',
      body: JSON.stringify({path: directory.path, name}),
    });
    toast(`Renamed to “${result.name}”.`);
    await navigatePane(paneId, paneFor(paneId).directory);
  }

  async function deleteFolderInPane(paneId, directory) {
    if (!window.confirm(
      `Move the folder “${directory.name}” to the Recycle Bin?\n\n` +
      'The folder and everything inside it leaves this view. Nothing is permanently deleted; restore it from the Recycle Bin if needed.',
    )) return;
    await api('/api/local/delete', {
      method: 'POST',
      body: JSON.stringify({path: directory.path}),
    });
    toast(`“${directory.name}” moved to the Recycle Bin.`);
    await navigatePane(paneId, paneFor(paneId).directory);
  }

  function folderLabel(path) {
    const normalized = path.replace(/[\\/]+$/, '');
    return normalized.split(/[\\/]/).pop() || path;
  }

  function paneFor(paneId = state.activePaneId) {
    return state.libraryPanes[paneId];
  }

  function emptyPane() {
    return {directory: '', assets: [], culling: null, selectedAssets: new Set(), selectMode: false, parent: null, breadcrumbs: [], directories: []};
  }

  function closePane(paneId) {
    document.querySelectorAll('.pane-more[open]').forEach((menu) => { menu.open = false; });
    if (paneId === 'left' && state.rightPaneOpen) {
      // Keep the remaining folder and its selection when closing the left side.
      state.libraryPanes.left = {...paneFor('right'), comparing: false, uploading: false, organizing: false, tagging: false};
      state.tileAnchor.left = state.tileAnchor.right;
    } else if (paneId === 'left') {
      state.libraryPanes.left = emptyPane();
      state.tileAnchor.left = null;
    }
    state.libraryPanes.right = emptyPane();
    state.tileAnchor.right = null;
    state.rightPaneOpen = false;
    setActivePane('left');
    renderPaneBrowser('left');
    renderPaneBrowser('right');
    renderPhotoGrid();
    if (!paneFor('left').directory) navigatePane('left', '').catch(showError);
    el('browse-folders-right').focus();
  }

  function isAssetDisplayable(asset) {
    if (state.showRawOnly) return true;
    return asset.jpeg_files.length > 0 || (asset.video_files || []).length > 0;
  }

  function displayableAssets(pane) {
    return pane.assets.filter(isAssetDisplayable);
  }

  function paneSummary(pane) {
    if (!pane.directory) return 'No folder open';
    const displayable = displayableAssets(pane);
    const photoCount = displayable.filter((asset) => asset.jpeg_files.length).length;
    const videoCount = displayable.filter((asset) => (asset.video_files || []).length).length;
    const hiddenCount = pane.assets.length - displayable.length;
    let summary = `${photoCount} photo group${photoCount === 1 ? '' : 's'} · ${videoCount} video group${videoCount === 1 ? '' : 's'}`;
    if (hiddenCount > 0 && !state.showRawOnly) {
      summary += ` · ${hiddenCount} RAW-only hidden`;
    } else if (state.showRawOnly) {
      const rawOnly = displayable.filter((asset) => asset.raw_files.length > 0 && asset.jpeg_files.length === 0 && !(asset.video_files || []).length).length;
      if (rawOnly) summary += ` · ${rawOnly} RAW-only`;
    }
    return summary;
  }

  function renderDisplaySettings() {
    const toggle = el('show-raw-toggle');
    if (!toggle) return;
    toggle.checked = state.showRawOnly;
    const hiddenNote = el('raw-hidden-note');
    if (!hiddenNote) return;
    const active = paneFor();
    if (!active.directory) {
      hiddenNote.classList.add('hidden');
      hiddenNote.textContent = '';
      hiddenNote.removeAttribute('title');
      return;
    }
    const hidden = active.assets.length - displayableAssets(active).length;
    if (!state.showRawOnly && hidden > 0) {
      hiddenNote.textContent = `${hidden} hidden`;
      hiddenNote.title = `${hidden} RAW-only group${hidden === 1 ? '' : 's'} hidden — enable “Show RAW” to display`;
      hiddenNote.classList.remove('hidden');
    } else {
      hiddenNote.classList.add('hidden');
      hiddenNote.textContent = '';
      hiddenNote.removeAttribute('title');
    }
  }

  function renderPaneHeaders() {
    el('library-split').classList.toggle('single-pane', !state.rightPaneOpen);
    el('library-pane-right').classList.toggle('hidden', !state.rightPaneOpen);
    el('library-split').querySelector('.split-move-actions').classList.toggle('hidden', !state.rightPaneOpen);
    el('browse-folders-right').classList.toggle('hidden', state.rightPaneOpen);
    ['left', 'right'].forEach((paneId) => {
      const pane = paneFor(paneId);
      const name = pane.breadcrumbs.length ? folderLabel(pane.directory || pane.breadcrumbs.at(-1).path) : 'Desktop';
      el(`library-pane-${paneId}`).classList.toggle('active', paneId === state.activePaneId);
      el(`${paneId}-folder-name`).textContent = name;
      const canManage = Boolean(pane.directory);
      el(`${paneId}-new-folder`).disabled = !canManage;
      el(`${paneId}-close-folder`).disabled = paneId === 'left' && !canManage && !state.rightPaneOpen;
      el(`photo-count-${paneId}`).textContent = paneSummary(pane);
      el(`selection-count-${paneId}`).textContent = `${pane.selectedAssets.size} selected`;
    });
    const active = paneFor();
    el('create-job-card').querySelectorAll('.folder-dependent').forEach((node) => {
      node.classList.toggle('hidden', !active.directory);
    });
  }

  function setActivePane(paneId) {
    if (!state.libraryPanes[paneId]) return;
    state.activePaneId = paneId;
    renderPaneHeaders();
    renderGoogleDestination();
    renderGoogleOrganizer();
    renderDisplaySettings();
    updateSelectionUi();
  }

  function applyLibraryScan(result, paneId = state.activePaneId) {
    const pane = paneFor(paneId);
    pane.directory = result.directory;
    pane.assets = result.assets;
    pane.culling = result.culling || null;
    pane.selectedAssets.clear();
    setActivePane(paneId);
    renderPhotoGrid();
  }

  function hasPhotoFilter() {
    return state.tagFilter.size > 0 || Boolean(state.dateFrom || state.dateTo);
  }

  function matchesDateFilter(asset) {
    if (!state.dateFrom && !state.dateTo) return true;
    const taken = String(asset.taken_at || '').slice(0, 10);
    if (!taken) return false;
    if (state.dateFrom && taken < state.dateFrom) return false;
    if (state.dateTo && taken > state.dateTo) return false;
    return true;
  }

  function visiblePaneAssets(pane) {
    let visible = displayableAssets(pane);
    if (state.tagFilter.size) {
      visible = visible.filter((asset) => (asset.tags || []).some((tag) => state.tagFilter.has(tag.toLowerCase())));
    }
    if (state.dateFrom || state.dateTo) {
      visible = visible.filter(matchesDateFilter);
    }
    return visible;
  }

  function renderDateFilterBar() {
    const bar = el('date-filter-bar');
    if (!bar) return;
    el('date-from').value = state.dateFrom;
    el('date-to').value = state.dateTo;
    el('date-clear').classList.toggle('hidden', !state.dateFrom && !state.dateTo);
    const filters = [];
    if (state.dateFrom || state.dateTo) filters.push('Date range');
    if (state.tagFilter.size) filters.push(`${state.tagFilter.size} tag${state.tagFilter.size === 1 ? '' : 's'}`);
    if (state.showRawOnly) filters.push('RAW visible');
    el('filter-summary').textContent = filters.join(' · ');
  }

  function knownTags() {
    const counts = new Map();
    ['left', 'right'].forEach((paneId) => {
      displayableAssets(paneFor(paneId)).forEach((asset) => {
        (asset.tags || []).forEach((tag) => counts.set(tag, (counts.get(tag) || 0) + 1));
      });
    });
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }

  function renderTagFilterBar() {
    const tags = knownTags();
    const chips = el('tag-filter-chips');
    chips.replaceChildren();
    tags.forEach(([tag]) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = `tag-chip${state.tagFilter.has(tag.toLowerCase()) ? ' active' : ''}`;
      chip.textContent = tag;
      chip.setAttribute('aria-pressed', String(state.tagFilter.has(tag.toLowerCase())));
      chip.addEventListener('click', () => {
        toggleTagFilter(tag);
      });
      chips.appendChild(chip);
    });
    const showBar = tags.length > 0 || state.tagFilter.size > 0;
    el('tag-filter-bar').classList.toggle('hidden', !showBar);
    el('tag-clear').classList.toggle('hidden', state.tagFilter.size === 0);
    const datalist = el('tag-options');
    datalist.replaceChildren();
    tags.forEach(([tag]) => {
      const option = document.createElement('option');
      option.value = tag;
      datalist.appendChild(option);
    });
  }

  function toggleTagFilter(tag) {
    const key = String(tag).toLowerCase();
    if (state.tagFilter.has(key)) state.tagFilter.delete(key);
    else state.tagFilter.add(key);
    renderTagFilterBar();
    renderPhotoGrid();
  }

  function renderPaneOrganizers(paneId) {
    const pane = paneFor(paneId);
    const disabled = !pane.selectedAssets.size || Boolean(pane.organizing);
    el(`${paneId}-move-menu`).querySelectorAll('[data-cull-action]').forEach((button) => {
      button.disabled = disabled;
      if (button.dataset.cullAction === 'restore') button.classList.toggle('hidden', !pane.culling || pane.culling.role === 'inbox');
    });
    el(`${paneId}-new-group`).disabled = disabled;
    const groups = el(`${paneId}-move-groups`);
    groups.replaceChildren();
    (pane.culling?.folders || []).filter((folder) => folder.role === 'group').forEach((folder) => {
      const name = folder.group_name || folder.name.replace(/^Compare:\s*/, '');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'quiet-button compact-button';
      button.textContent = name;
      button.disabled = disabled || folder.path === pane.directory;
      button.addEventListener('click', () => moveSelectedAssets('group', paneId, name));
      groups.appendChild(button);
    });
    const taggingDisabled = !pane.selectedAssets.size || Boolean(pane.tagging);
    el(`${paneId}-tag-input`).disabled = taggingDisabled;
    el(`${paneId}-tag-form`).querySelector('button').disabled = taggingDisabled || !el(`${paneId}-tag-input`).value.trim();
    const tags = el(`${paneId}-tag-list`);
    const focusedTag = tags.contains(document.activeElement) ? document.activeElement.dataset.tag : null;
    tags.replaceChildren();
    const selected = pane.assets.filter((asset) => pane.selectedAssets.has(asset.id));
    knownTags().forEach(([tag]) => {
      const count = selected.filter((asset) => (asset.tags || []).includes(tag)).length;
      const all = selected.length > 0 && count === selected.length;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'quiet-button compact-button tag-choice';
      button.dataset.tag = tag;
      button.setAttribute('role', 'checkbox');
      button.setAttribute('aria-checked', all ? 'true' : count ? 'mixed' : 'false');
      button.title = `${all ? 'Remove' : 'Apply'} tag on selection`;
      button.innerHTML = `<span aria-hidden="true">${all ? '✓' : count ? '−' : '+'}</span>${escapeHtml(tag)}`;
      button.disabled = taggingDisabled;
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        updateSelectedTags(all ? 'remove' : 'add', paneId, tag);
      });
      tags.appendChild(button);
    });
    if (focusedTag) {
      const replacement = [...tags.children].find((button) => button.dataset.tag === focusedTag);
      (replacement || el(`${paneId}-tag-input`)).focus();
    }
  }

  function previewUrl(path, size, version) {
    return `/api/local/preview?path=${encodeURIComponent(path)}&size=${size}&v=${encodeURIComponent(version || '')}&token=${encodeURIComponent(state.token)}`;
  }

  function renderPanePhotoGrid(paneId) {
    const pane = paneFor(paneId);
    const grid = el(`photo-grid-${paneId}`);
    grid.replaceChildren();
    if (!pane.directory) {
      grid.innerHTML = '<p class="pane-empty panel-copy">Open a folder to compare it here.</p>';
      return;
    }
    if (!pane.assets.length) {
      grid.innerHTML = '<p class="pane-empty panel-copy">No supported media in this folder.</p>';
      return;
    }
    const displayable = displayableAssets(pane);
    if (!displayable.length) {
      const hidden = pane.assets.length;
      grid.innerHTML = `<p class="pane-empty panel-copy">No JPEG or video previews in this folder — ${hidden} RAW-only group${hidden === 1 ? '' : 's'} hidden. Enable “Show RAW-only photos” to display them.</p>`;
      return;
    }
    const visible = visiblePaneAssets(pane);
    if (!visible.length) {
      const hiddenByType = pane.assets.length - displayable.length;
      if (hiddenByType > 0 && !hasPhotoFilter()) {
        grid.innerHTML = '<p class="pane-empty panel-copy">No photos match the current display settings.</p>';
      } else if (state.dateFrom || state.dateTo) {
        grid.innerHTML = '<p class="pane-empty panel-copy">No photos match the selected date range.</p>';
      } else {
        grid.innerHTML = '<p class="pane-empty panel-copy">No photos match the selected tag filter.</p>';
      }
      return;
    }
    const fragment = document.createDocumentFragment();
    visible.forEach((asset) => {
      const selected = pane.selectedAssets.has(asset.id);
      const tile = document.createElement('div');
      tile.className = `photo-tile${selected ? ' selected' : ''}`;
      tile.dataset.assetId = asset.id;
      let media;
      if (asset.preview_path) {
        media = `<img src="${escapeHtml(previewUrl(asset.preview_path, 480, asset.preview_version))}" alt="${escapeHtml(asset.stem)}" loading="lazy" decoding="async">`;
      } else if ((asset.video_files || []).length) {
        media = '<span class="photo-placeholder video-placeholder"><span>▶</span>Video</span>';
      } else if (asset.raw_files.length) {
        media = `<span class="photo-placeholder raw-placeholder"><span>RAW</span>${escapeHtml(asset.raw_files[0].filename)}</span>`;
      } else if ((asset.other_files || []).length) {
        media = `<span class="photo-placeholder raw-placeholder"><span>${escapeHtml(asset.other_files[0].filename.split('.').pop().toUpperCase())}</span>${escapeHtml(asset.stem)}</span>`;
      } else {
        media = '<span class="photo-placeholder video-placeholder"><span>•</span>Media</span>';
      }
      const formatTags = [];
      (asset.tags || []).forEach((tag) => formatTags.push(`<span class="format-tag tile-tag">${escapeHtml(tag)}</span>`));
      if (asset.raw_files.length) formatTags.push(`<span class="format-tag">RAW${asset.raw_files.length > 1 ? ` ×${asset.raw_files.length}` : ''}</span>`);
      if (asset.jpeg_files.length) formatTags.push(`<span class="format-tag">JPEG${asset.jpeg_files.length > 1 ? ` ×${asset.jpeg_files.length}` : ''}</span>`);
      if ((asset.video_files || []).length) formatTags.push(`<span class="format-tag">VIDEO${asset.video_files.length > 1 ? ` ×${asset.video_files.length}` : ''}</span>`);
      if ((asset.other_files || []).length && !asset.jpeg_files.length && !asset.raw_files.length && !(asset.video_files || []).length) {
        formatTags.push(`<span class="format-tag">${escapeHtml(asset.other_files[0].filename.split('.').pop().toUpperCase())}</span>`);
      }
      const tags = formatTags.join('');
      tile.innerHTML = `<button class="photo-open" type="button" data-photo-name="${escapeHtml(asset.stem)}" data-has-preview="${Boolean(asset.preview_path)}" aria-label="Open ${escapeHtml(asset.stem)}">${media}<span class="photo-tile-info"><strong>${escapeHtml(asset.stem)}</strong><span class="format-tags">${tags}</span></span></button><input type="checkbox" class="pick-check" aria-label="Select ${escapeHtml(asset.stem)}" ${selected ? 'checked' : ''}>`;
      tile.querySelector('.photo-open').addEventListener('click', (event) => {
        setActivePane(paneId);
        if (event.ctrlKey || event.metaKey || event.shiftKey || pane.selectMode || !asset.preview_path) {
          selectPaneAsset(paneId, asset.id, event.shiftKey || !pane.selectedAssets.has(asset.id), event.shiftKey);
        } else {
          openLightbox(asset);
        }
      });
      tile.querySelector('.pick-check').addEventListener('click', (event) => {
        event.stopPropagation();
        selectPaneAsset(paneId, asset.id, event.target.checked, event.shiftKey);
      });
      fragment.appendChild(tile);
    });
    grid.appendChild(fragment);
  }

  function renderPhotoGrid() {
    renderPanePhotoGrid('left');
    renderPanePhotoGrid('right');
    renderPaneHeaders();
    renderDateFilterBar();
    renderTagFilterBar();
    renderDisplaySettings();
    updateSelectionUi();
  }

  function openLightbox(asset) {
    if (!asset.preview_path) return;
    el('lightbox-title').textContent = asset.stem;
    el('lightbox-image').alt = asset.stem;
    el('lightbox-image').src = `${previewUrl(asset.preview_path, 1600, asset.preview_version)}&original=1`;
    el('lightbox-modal').showModal();
  }

  function selectPaneAsset(paneId, assetId, selected, range = false) {
    setActivePane(paneId);
    const pane = paneFor(paneId);
    const visible = visiblePaneAssets(pane);
    const anchorIndex = visible.findIndex((asset) => asset.id === state.tileAnchor[paneId]);
    const targetIndex = visible.findIndex((asset) => asset.id === assetId);
    if (targetIndex === -1) return;
    if (range && anchorIndex !== -1) {
      const from = Math.min(anchorIndex, targetIndex);
      const to = Math.max(anchorIndex, targetIndex);
      for (let i = from; i <= to; i++) {
        if (selected) pane.selectedAssets.add(visible[i].id);
        else pane.selectedAssets.delete(visible[i].id);
      }
    } else {
      if (selected) pane.selectedAssets.add(assetId);
      else pane.selectedAssets.delete(assetId);
      state.tileAnchor[paneId] = assetId;
    }
    syncSelectionTiles();
    updateSelectionUi();
  }

  function syncSelectionTiles() {
    ['left', 'right'].forEach((paneId) => {
      const pane = paneFor(paneId);
      document.querySelectorAll(`#photo-grid-${paneId} .photo-tile`).forEach((tile) => {
        const selected = pane.selectedAssets.has(tile.dataset.assetId);
        tile.classList.toggle('selected', selected);
        tile.querySelector('.pick-check').checked = selected;
      });
    });
    renderPaneHeaders();
  }

  function updateSelectionUi() {
    const active = paneFor();
    const count = active.selectedAssets.size;
    el('selection-summary').textContent = `${count} selected in ${folderLabel(active.directory)}`;
    el('selection-job-form').querySelector('button[type="submit"]').disabled = count === 0;
    ['left', 'right'].forEach((paneId) => {
      const pane = paneFor(paneId);
      const visible = visiblePaneAssets(pane);
      const selectMode = Boolean(pane.selectMode);
      el(`${paneId}-select-mode`).setAttribute('aria-pressed', String(selectMode));
      el(`${paneId}-select-mode`).title = selectMode ? 'Photo clicks select. Turn off to open photos.' : 'Make photo clicks select instead of opening photos.';
      el(`photo-grid-${paneId}`).classList.toggle('select-mode', selectMode);
      el(`photo-grid-${paneId}`).querySelectorAll('.photo-open').forEach((button) => {
        const selecting = selectMode || button.dataset.hasPreview !== 'true';
        button.setAttribute('aria-label', `${selecting ? 'Select' : 'Open'} ${button.dataset.photoName}`);
      });
      el(`${paneId}-select-all`).disabled = !visible.length || visible.every((asset) => pane.selectedAssets.has(asset.id));
      el(`${paneId}-clear-picked`).disabled = !pane.selectedAssets.size;
      el(`${paneId}-compare-picked`).disabled = selectedPreviewAssets(pane).length < 2;
      el(`${paneId}-auto-compare`).disabled = !pane.directory || pane.comparing || !pane.assets.length;
      el(`${paneId}-auto-compare`).textContent = pane.comparing ? 'Comparing…' : 'Auto-compare folder';
      el(`${paneId}-google-upload`).disabled = !pane.selectedAssets.size || !state.google.connected || pane.uploading;
      el(`${paneId}-google-upload`).textContent = pane.uploading ? 'Uploading…' : 'Upload to Google Photos';
      renderPaneOrganizers(paneId);
    });
    const left = paneFor('left');
    const right = paneFor('right');
    [['left', left, right], ['right', right, left]].forEach(([paneId, source, destination]) => {
      const label = `Move selection from ${folderLabel(source.directory) || 'this folder'} to ${folderLabel(destination.directory) || 'the other folder'}`;
      el(`move-selected-${paneId}`).title = label;
      el(`move-selected-${paneId}`).setAttribute('aria-label', label);
    });
    el('move-selected-left').disabled = !left.directory || !right.directory || left.selectedAssets.size === 0;
    el('move-selected-right').disabled = !left.directory || !right.directory || right.selectedAssets.size === 0;
  }

  async function updateSelectedTags(action, paneId, value) {
    const pane = paneFor(paneId);
    if (!value || !pane.selectedAssets.size || pane.tagging) return;
    const assetIds = Array.from(pane.selectedAssets);
    pane.tagging = true;
    updateSelectionUi();
    const body = action === 'add'
      ? {asset_ids: assetIds, add_tags: [value]}
      : {asset_ids: assetIds, remove_tags: [value]};
    try {
      const result = await api('/api/local/tags', {method: 'POST', body: JSON.stringify(body)});
      ['left', 'right'].forEach((paneId) => {
        paneFor(paneId).assets.forEach((asset) => {
          if (Object.prototype.hasOwnProperty.call(result.assignments, asset.id)) {
            asset.tags = result.assignments[asset.id];
          }
        });
      });
      el(`${paneId}-tag-input`).value = '';
      renderTagFilterBar();
      renderPhotoGrid();
      toast(`${action === 'add' ? 'Added' : 'Removed'} tag “${value}” ${action === 'add' ? 'on' : 'from'} ${assetIds.length} photo group${assetIds.length === 1 ? '' : 's'}.`);
    } catch (error) {
      toast(error.message);
    } finally {
      pane.tagging = false;
      updateSelectionUi();
    }
  }

  async function moveSelectedAssets(action, paneId, groupName = '') {
    const pane = paneFor(paneId);
    if (!pane.selectedAssets.size || pane.organizing) return;
    const directory = pane.directory;
    const labels = {
      select: 'Move the selected photo groups into the select folder?',
      unselect: 'Move the selected photo groups into the unselect folder? Nothing will be deleted.',
      restore: 'Move the selected photo groups back to the inbox folder?',
      group: 'Move the selected photo groups into this comparison group?',
    };
    if (!window.confirm(labels[action])) return;
    pane.organizing = true;
    el(`${paneId}-move-menu`).open = false;
    updateSelectionUi();
    try {
      const result = await api('/api/local/cull', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: directory,
          asset_ids: Array.from(pane.selectedAssets),
          action,
          group_name: groupName,
        }),
      });
      if (paneFor(paneId) === pane && pane.directory === directory) {
        applyLibraryScan(result, paneId);
        await renderPaneBrowserForDirectory(paneId, result.directory);
      }
      const closedGroup = result.group_deleted ? ' The empty comparison group was removed.' : '';
      const duplicates = result.deleted_duplicate_files || 0;
      const renamed = (result.renamed_files || []).length;
      const details = [];
      if (duplicates) details.push(`${duplicates} visually identical duplicate${duplicates === 1 ? '' : 's'} removed from the source folder`);
      if (renamed) details.push(`${renamed} same-name file${renamed === 1 ? '' : 's'} kept with a variant name`);
      const destinationLabel = action === 'group' ? (result.destination_group_name || groupName) : result.destination;
      const moved = `${result.moved_assets} photo group${result.moved_assets === 1 ? '' : 's'} moved to ${destinationLabel}`;
      toast(`${moved}${details.length ? `; ${details.join('; ')}` : ''}.${closedGroup}`);
    } catch (error) {
      toast(error.message);
    } finally {
      pane.organizing = false;
      updateSelectionUi();
    }
  }

  async function autoCompareGroups(paneId) {
    const pane = paneFor(paneId);
    if (!pane.directory || pane.comparing) return;
    const minSize = Math.min(50, Math.max(2, Math.round(Number(el(`${paneId}-compare-min-size`).value) || 3)));
    if (!window.confirm(`Find similar photos in this folder and create comparison groups of at least ${minSize} photos? Similar photos will be moved into new compare folders.`)) return;
    const button = el(`${paneId}-auto-compare`);
    const directory = pane.directory;
    pane.comparing = true;
    button.disabled = true;
    const originalLabel = button.textContent;
    button.textContent = 'Comparing…';
    try {
      const result = await api('/api/local/compare-groups', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: directory,
          min_group_size: minSize,
        }),
      });
      if (paneFor(paneId) === pane && pane.directory === directory) {
        applyLibraryScan(result, paneId);
        await renderPaneBrowserForDirectory(paneId, directory);
      }
      const groups = result.created_groups || [];
      if (!groups.length) {
        toast(`No clusters of at least ${result.min_group_size || minSize} similar photos were found.`);
      } else {
        const moved = groups.reduce((total, group) => total + (group.moved_assets || 0), 0);
        const duplicates = groups.reduce((total, group) => total + (group.deleted_duplicate_files || 0), 0);
        const details = [];
        if (duplicates) details.push(`${duplicates} visually identical duplicate${duplicates === 1 ? '' : 's'} removed`);
        const movedLabel = `${moved} photo group${moved === 1 ? '' : 's'} moved`;
        toast(`${groups.length} comparison group${groups.length === 1 ? '' : 's'} created; ${movedLabel}${details.length ? `; ${details.join('; ')}` : ''}.`);
      }
    } catch (error) {
      toast(error.message);
    } finally {
      pane.comparing = false;
      button.textContent = originalLabel;
      updateSelectionUi();
    }
  }

  async function moveSelectedAssetsBetweenDirectories(sourcePaneId, destinationPaneId) {
    const sourcePane = paneFor(sourcePaneId);
    const destinationPane = paneFor(destinationPaneId);
    if (!sourcePane.directory || !destinationPane.directory || !sourcePane.selectedAssets.size) return;
    const paneNames = {left: 'origin folder', right: 'comparison folder'};
    if (!window.confirm(`Move the selected photo groups from the ${paneNames[sourcePaneId]} to the ${paneNames[destinationPaneId]}?`)) return;
    const button = sourcePaneId === 'left' ? el('move-selected-left') : el('move-selected-right');
    button.disabled = true;
    try {
      const result = await api('/api/local/move', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: sourcePane.directory,
          destination_directory: destinationPane.directory,
          asset_ids: Array.from(sourcePane.selectedAssets),
        }),
      });
      applyLibraryScan(result.source, sourcePaneId);
      applyLibraryScan(result.destination, destinationPaneId);
      setActivePane(sourcePaneId);
      updateSelectionUi();
      toast(`${result.moved_assets} photo group${result.moved_assets === 1 ? '' : 's'} moved to the ${paneNames[destinationPaneId]}.`);
    } catch (error) {
      toast(error.message);
      updateSelectionUi();
    }
  }

  function selectedPreviewAssets(pane) {
    return pane.assets.filter((asset) => pane.selectedAssets.has(asset.id) && asset.preview_path);
  }

  function openCompare() {
    const pane = paneFor();
    const assets = selectedPreviewAssets(pane);
    if (assets.length < 2) {
      toast('Select at least two photos that have JPEG previews.');
      return;
    }
    state.compareZoom = 1;
    state.comparePan = {x: 0, y: 0};
    el('compare-zoom').value = '100';
    el('compare-zoom-value').textContent = '100%';
    const grid = el('compare-grid');
    grid.replaceChildren();
    const count = assets.length;
    const cols = count <= 2 ? count : count <= 4 ? 2 : 3;
    const rows = Math.ceil(count / cols);
    grid.style.setProperty('--compare-cols', String(cols));
    grid.style.setProperty('--compare-rows', String(rows));
    assets.forEach((asset) => {
      const item = document.createElement('div');
      item.className = 'compare-item';
      item.dataset.assetId = asset.id;
      item.innerHTML = `<img src="${escapeHtml(previewUrl(asset.preview_path, 1600, asset.preview_version))}" alt="${escapeHtml(asset.stem)}" decoding="async"><div class="compare-label"><strong>${escapeHtml(asset.stem)}</strong><button type="button" class="is-picked">Selected</button></div>`;
      const pickButton = item.querySelector('button');
      pickButton.addEventListener('click', (event) => {
        event.stopPropagation();
        if (pane.selectedAssets.has(asset.id)) pane.selectedAssets.delete(asset.id);
        else pane.selectedAssets.add(asset.id);
        const picked = pane.selectedAssets.has(asset.id);
        pickButton.classList.toggle('is-picked', picked);
        pickButton.textContent = picked ? 'Selected' : 'Removed';
        updateSelectionUi();
      });
      attachSynchronizedPan(item);
      grid.appendChild(item);
    });
    applyCompareTransform();
    el('compare-modal').classList.remove('hidden');
  }

  function attachSynchronizedPan(item) {
    let start = null;
    item.addEventListener('pointerdown', (event) => {
      if (event.target.closest('button')) return;
      start = {pointerX: event.clientX, pointerY: event.clientY, panX: state.comparePan.x, panY: state.comparePan.y};
      item.setPointerCapture(event.pointerId);
    });
    item.addEventListener('pointermove', (event) => {
      if (!start || state.compareZoom <= 1) return;
      const maxX = item.clientWidth * (state.compareZoom - 1) / 2;
      const maxY = item.clientHeight * (state.compareZoom - 1) / 2;
      state.comparePan.x = Math.max(-maxX, Math.min(maxX, start.panX + event.clientX - start.pointerX));
      state.comparePan.y = Math.max(-maxY, Math.min(maxY, start.panY + event.clientY - start.pointerY));
      applyCompareTransform();
    });
    const stop = () => { start = null; };
    item.addEventListener('pointerup', stop);
    item.addEventListener('pointercancel', stop);
  }

  function applyCompareTransform() {
    document.querySelectorAll('#compare-grid img').forEach((image) => {
      image.style.transform = `translate(${state.comparePan.x}px, ${state.comparePan.y}px) scale(${state.compareZoom})`;
    });
  }

  async function openJob(jobId) {
    state.currentJob = await api(`/api/jobs/${jobId}`);
    renderJob(state.currentJob);
    showOnly(jobView);
    window.scrollTo({top: 0, behavior: 'smooth'});
  }

  function downloadUrl(path) {
    const separator = path.includes('?') ? '&' : '?';
    return `${path}${separator}token=${encodeURIComponent(state.token)}`;
  }

  function renderJob(job) {
    el('job-title').textContent = job.name;
    const sourceLabel = job.edit_source_format ? ` · ${job.edit_source_format.toUpperCase()} edit sources` : '';
    el('job-summary').textContent = `${job.files.length} files ready${sourceLabel} · ${job.returns.length} edits returned`;
    const bundle = el('download-bundle');
    bundle.href = downloadUrl(`/api/jobs/${job.id}/bundle.zip`);
    bundle.classList.toggle('hidden', job.files.length === 0);
    const showPairing = state.desktop && state.bootstrap;
    el('job-pairing').classList.toggle('hidden', !showPairing);
    if (showPairing) {
      el('job-ipad-url').textContent = state.bootstrap.ipad_url;
      el('job-pair-code').textContent = state.bootstrap.pair_code;
      el('job-pair-qr').src = `${state.bootstrap.pair_qr_url}?v=${Date.now()}`;
    }
    el('export-panel').classList.toggle('hidden', !state.desktop);
    el('delete-job').classList.toggle('hidden', !state.desktop);
    if (state.desktop) {
      const postprocess = job.postprocess || {status: 'pending'};
      const result = el('organize-result');
      const finished = postprocess.status === 'complete' && postprocess.layout_version >= 2;
      const hasResult = ['complete', 'complete_with_warnings'].includes(postprocess.status);
      el('organize-source').disabled = finished;
      el('organize-source').textContent = finished ? 'Source folder organized' : hasResult ? 'Retry organization' : 'Finish & organize source folder';
      result.classList.toggle('hidden', !hasResult);
      const warnings = el('organize-errors');
      warnings.replaceChildren();
      warnings.classList.toggle('hidden', !postprocess.errors?.length);
      for (const message of postprocess.errors || []) {
        const item = document.createElement('li');
        item.textContent = message;
        warnings.append(item);
      }
      if (hasResult) {
        const warningText = postprocess.errors?.length ? ` · ${postprocess.errors.length} warning(s)` : '';
        result.textContent = `${postprocess.source_directory} · ${postprocess.moved_count} moved · ${postprocess.copied_count} edits copied${warningText}`;
      }
    }

    const files = el('job-files');
    files.replaceChildren();
    job.files.forEach((file) => {
      const row = document.createElement('div');
      row.className = 'file-row';
      const verification = file.sha256 ? `SHA-256 ${file.sha256.slice(0, 10)}…` : 'Verified while packaging';
      row.innerHTML = `<div class="file-name"><strong>${escapeHtml(file.filename)}</strong><small>${formatBytes(file.size)} · ${verification}</small></div>`;
      const link = document.createElement('a');
      link.className = 'download-link';
      link.textContent = 'Download';
      link.href = downloadUrl(`/api/jobs/${job.id}/files/${file.id}/download`);
      row.appendChild(link);
      files.appendChild(row);
    });
    if (!job.files.length) files.innerHTML = '<p class="panel-copy">No source photos have been uploaded yet.</p>';

    const returns = el('job-returns');
    returns.replaceChildren();
    job.returns.forEach((file) => {
      const row = document.createElement('div');
      row.className = 'return-row';
      const label = file.association_status === 'suggested' ? 'Possible original' : file.match_status;
      row.innerHTML = `<div class="file-name"><strong>${escapeHtml(file.filename)}</strong><small>${formatBytes(file.size)}${file.matched_filename ? ` · possible original: ${escapeHtml(file.matched_filename)}` : ''}</small></div><span class="match-badge ${escapeHtml(file.match_status)}">${escapeHtml(label)}</span>`;
      returns.appendChild(row);
    });
    if (!job.returns.length) returns.innerHTML = '<p class="panel-copy">No edits returned yet.</p>';
  }

  async function uploadFile(jobId, file, kind, onProgress) {
    const sha256 = await KiraHash.fileSHA256(file, bytes => onProgress(bytes, 'checking'));
    onProgress(0, 'sending');
    const started = await api(`/api/jobs/${jobId}/uploads/start`, {
      method: 'POST',
      body: JSON.stringify({
        kind,
        filename: file.name,
        size: file.size,
        last_modified: file.lastModified || 0,
        sha256,
      }),
    });
    if (started.complete) {
      onProgress(file.size);
      return started.record;
    }

    let offset = started.offset;
    onProgress(offset);
    while (offset < file.size) {
      const end = Math.min(offset + CHUNK_SIZE, file.size);
      const chunk = file.slice(offset, end);
      let result;
      try {
        result = await api(`/api/jobs/${jobId}/uploads/${started.upload_id}/chunk?offset=${offset}`, {
          method: 'PUT',
          headers: {'Content-Type': 'application/octet-stream'},
          body: chunk,
        });
      } catch (error) {
        if (error.status === 409 && Number.isFinite(error.body.expected_offset)) {
          offset = error.body.expected_offset;
          onProgress(offset);
          continue;
        }
        throw error;
      }
      offset = result.offset;
      onProgress(offset);
    }
    return api(`/api/jobs/${jobId}/uploads/${started.upload_id}/complete`, {method: 'POST', body: '{}'});
  }

  async function uploadFiles(jobId, files, kind, progressElements) {
    const total = files.reduce((sum, file) => sum + file.size, 0);
    let completedBeforeCurrent = 0;
    progressElements.container.classList.remove('hidden');
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      progressElements.label.textContent = `${kind === 'originals' ? 'Sending' : 'Returning'} ${index + 1} of ${files.length}: ${file.name}`;
      await uploadFile(jobId, file, kind, (currentBytes, phase) => {
        progressElements.label.textContent = `${phase === 'checking' ? 'Checking' : 'Returning'} ${index + 1} of ${files.length}: ${file.name}`;
        const transferred = completedBeforeCurrent + currentBytes;
        const percent = total ? Math.round((transferred / total) * 100) : 100;
        progressElements.bar.value = percent;
        progressElements.value.textContent = phase === 'checking' ? 'Checking…' : `${percent}%`;
      });
      completedBeforeCurrent += file.size;
    }
    progressElements.label.textContent = `Verified ${files.length} ${files.length === 1 ? 'file' : 'files'}`;
    progressElements.bar.value = 100;
    progressElements.value.textContent = '100%';
  }

  function progressSet(prefix) {
    return {
      container: el(`${prefix}-progress`),
      label: el(`${prefix}-progress-label`),
      value: el(`${prefix}-progress-value`),
      bar: el(`${prefix}-progress-bar`),
    };
  }

  el('pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    el('pair-error').textContent = '';
    try {
      const response = await api('/api/pair', {method: 'POST', body: JSON.stringify({code: el('pair-code').value})});
      state.token = response.token;
      localStorage.setItem('kira-token', state.token);
      await loadDashboard();
    } catch (error) {
      el('pair-error').textContent = error.message;
    }
  });

  el('google-connect').addEventListener('click', async () => {
    const popup = window.open('about:blank', 'kira-google-oauth', 'width=620,height=760');
    try {
      if (!popup) throw new Error('Allow pop-ups for Kira, then try connecting again.');
      const result = await api('/api/google/oauth/start', {method: 'POST', body: '{}'});
      popup.location.href = result.authorization_url;
      await waitForGoogleConnection();
    } catch (error) {
      if (popup) popup.close();
      showError(error);
    }
  });

  el('google-pick').addEventListener('click', async () => {
    const popup = window.open('about:blank', 'kira-google-picker', 'width=900,height=760');
    try {
      if (!popup) throw new Error('Allow pop-ups for Kira, then choose photos again.');
      saveGoogleImportPreferences();
      const session = await api('/api/google/picker/sessions', {
        method: 'POST',
        body: JSON.stringify({max_items: 2000}),
      });
      const pickerUrl = `${String(session.pickerUri).replace(/\/$/, '')}/autoclose`;
      popup.location.href = pickerUrl;
      await pollPickerSession(session.id);
      await importGoogleSelection(session.id);
    } catch (error) {
      if (popup) popup.close();
      showError(error);
    } finally {
      el('google-progress').classList.add('hidden');
    }
  });

  el('google-download-settings-toggle').addEventListener('click', () => {
    state.googleDownloadSettingsOpen = !state.googleDownloadSettingsOpen;
    updateGooglePreferenceUi();
  });

  el('google-download-mode').addEventListener('change', () => {
    updateGooglePreferenceUi();
    saveGoogleImportPreferences();
  });
  el('google-zip-threshold').addEventListener('change', () => {
    saveGoogleImportPreferences();
  });
  el('google-destination-folder').addEventListener('input', renderGoogleDestination);

  el('google-disconnect').addEventListener('click', async () => {
    if (!window.confirm('Disconnect Google Photos from Kira? Imported local files will stay on this desktop.')) return;
    try {
      await api('/api/google/disconnect', {method: 'POST', body: '{}'});
      await refreshGoogleStatus();
      await checkGoogleHealth();
      toast('Google Photos disconnected. Imported files were kept.');
    } catch (error) {
      showError(error);
    }
  });

  el('google-organizer-connect-button').addEventListener('click', async () => {
    const cookiesPath = el('google-cookies-path').value.trim();
    const accountIndex = Number.parseInt(el('google-account-index').value, 10);
    if (!cookiesPath) {
      showError(new Error('Choose or enter the exported cookie JSON or cookies.txt path.'));
      return;
    }
    try {
      await api('/api/google/web-session', {
        method: 'POST',
        body: JSON.stringify({cookies_path: cookiesPath, account_index: Number.isFinite(accountIndex) ? accountIndex : 0}),
      });
      await refreshGoogleStatus();
      await checkGoogleHealth();
      toast('Google Photos album and Archive automation connected.');
    } catch (error) {
      showError(error);
    }
  });

  async function signInToGooglePhotos() {
    try {
      const signIn = await api('/api/google/web-signin', {method: 'POST', body: '{}'});
      state.google.organizer.sign_in = signIn;
      renderGoogleSignIn(signIn);
      await pollGoogleSignIn();
    } catch (error) {
      showError(error);
    }
  }
  el('google-organizer-signin').addEventListener('click', signInToGooglePhotos);
  el('google-organizer-reconnect').addEventListener('click', signInToGooglePhotos);
  el('google-signin-cancel').addEventListener('click', async () => {
    try {
      const signIn = await api('/api/google/web-signin', {method: 'DELETE'});
      state.google.organizer.sign_in = signIn;
      renderGoogleSignIn(signIn);
    } catch (error) {
      showError(error);
    }
  });

  el('google-organizer-disconnect').addEventListener('click', async () => {
    if (!window.confirm('Remove the encrypted Google Photos web session from Kira?')) return;
    try {
      await api('/api/google/web-session', {method: 'DELETE'});
      await refreshGoogleStatus();
      await checkGoogleHealth();
      toast('Google Photos web session removed.');
    } catch (error) {
      showError(error);
    }
  });

  el('google-album-title').addEventListener('input', () => {
    localStorage.setItem('kira-google-album-title', el('google-album-title').value.trim());
    renderGoogleOrganizer();
  });
  el('google-archive-after-upload').addEventListener('change', renderGoogleOrganizer);

  el('google-match-folder').addEventListener('click', async () => {
    const albumTitle = el('google-album-title').value.trim();
    const archive = el('google-archive-after-upload').checked;
    const directory = paneFor().directory;
    if (!directory || !albumTitle || !state.google.organizer?.connected) return;
    const confirmed = window.confirm(
      `Match every supported file directly inside:\n${directory}\n\n` +
      `Confirmed Google Photos matches will be added to “${albumTitle}”` +
      `${archive ? ' and archived' : ''}. Continue?`
    );
    if (!confirmed) return;
    const button = el('google-match-folder');
    button.disabled = true;
    button.textContent = 'Matching folder…';
    try {
      const operation = await api('/api/google/match-folder', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: directory,
          album_title: albumTitle,
          archive,
        }),
      });
      const finished = await pollGoogleOperation(operation.id);
      if (finished.status === 'failed') {
        throw new Error(finished.error || 'Google Photos folder matching failed.');
      }
      if (!finished.matched) {
        toast(
          `Checked ${finished.scanned} local files; no content matches were found in Google Photos` +
          `${finished.failed ? `; ${finished.failed} file${finished.failed === 1 ? '' : 's'} failed and were logged` : ''}.`
        );
      } else {
        toast(
          `Matched ${finished.matched} Google item${finished.matched === 1 ? '' : 's'} ` +
          `from ${finished.matched_local_files} local file${finished.matched_local_files === 1 ? '' : 's'}; ` +
          `${finished.organized} added to ${albumTitle}${finished.archived_count ? ` and ${finished.archived_count} archived` : ''}` +
          `${finished.failed ? `; ${finished.failed} failed and were logged` : ''}.`
        );
      }
    } catch (error) {
      showError(error);
    } finally {
      el('google-progress').classList.add('hidden');
      renderGoogleOrganizer();
    }
  });

  async function uploadPaneSelection(paneId) {
    const pane = paneFor(paneId);
    if (!pane.selectedAssets.size || !state.google.connected || pane.uploading) return;
    const button = el(`${paneId}-google-upload`);
    pane.uploading = true;
    button.disabled = true;
    try {
      const operation = await api('/api/google/uploads', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: pane.directory,
          asset_ids: Array.from(pane.selectedAssets),
        }),
      });
      const finished = await pollGoogleOperation(operation.id);
      if (finished.status === 'failed') throw new Error(finished.error || 'Google Photos upload failed.');
      toast(`Uploaded ${finished.completed} media item${finished.completed === 1 ? '' : 's'}${finished.failed ? `; ${finished.failed} failed` : ''}.`);
    } catch (error) {
      showError(error);
    } finally {
      pane.uploading = false;
      el('google-progress').classList.add('hidden');
      updateSelectionUi();
    }
  }

  ['left', 'right'].forEach((paneId) => {
    el(`${paneId}-select-mode`).addEventListener('click', () => {
      const pane = paneFor(paneId);
      pane.selectMode = !pane.selectMode;
      setActivePane(paneId);
    });
    el(`${paneId}-close-folder`).addEventListener('click', () => closePane(paneId));
    el(`${paneId}-select-all`).addEventListener('click', () => {
      setActivePane(paneId);
      const pane = paneFor(paneId);
      visiblePaneAssets(pane).forEach((asset) => pane.selectedAssets.add(asset.id));
      syncSelectionTiles();
      updateSelectionUi();
    });
    el(`${paneId}-clear-picked`).addEventListener('click', () => {
      setActivePane(paneId);
      paneFor(paneId).selectedAssets.clear();
      state.tileAnchor[paneId] = null;
      syncSelectionTiles();
      updateSelectionUi();
    });
    el(`${paneId}-compare-picked`).addEventListener('click', () => {
      setActivePane(paneId);
      openCompare();
    });
    el(`${paneId}-auto-compare`).addEventListener('click', () => autoCompareGroups(paneId));
    el(`${paneId}-google-upload`).addEventListener('click', () => uploadPaneSelection(paneId));
    el(`${paneId}-move-menu`).querySelectorAll('[data-cull-action]').forEach((button) => {
      button.addEventListener('click', () => moveSelectedAssets(button.dataset.cullAction, paneId));
    });
    el(`${paneId}-new-group`).addEventListener('click', () => {
      const name = window.prompt('New comparison group name:');
      if (name?.trim()) moveSelectedAssets('group', paneId, name.trim());
    });
    el(`${paneId}-tag-input`).addEventListener('input', () => {
      el(`${paneId}-tag-form`).querySelector('button').disabled = !el(`${paneId}-tag-input`).value.trim();
    });
    el(`${paneId}-tag-form`).addEventListener('submit', (event) => {
      event.preventDefault();
      updateSelectedTags('add', paneId, el(`${paneId}-tag-input`).value.trim());
    });
    el(`library-pane-${paneId}`).addEventListener('click', (event) => {
      if (!event.target.closest('button')) setActivePane(paneId);
    });
    el(`${paneId}-folder-up`).addEventListener('click', () => navigatePane(paneId, paneFor(paneId).parent || '').catch(showError));
    el(`${paneId}-new-folder`).addEventListener('click', () => createFolderInPane(paneId).catch(showError));
  });

  document.addEventListener('click', (event) => {
    document.querySelectorAll('.pane-more[open]').forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    document.querySelectorAll('.pane-more[open]').forEach((menu) => {
      menu.open = false;
      menu.querySelector('summary').focus();
    });
  });

  el('browse-folders-right').addEventListener('click', () => {
    const pane = paneFor('right');
    state.rightPaneOpen = true;
    setActivePane('right');
    // Render the navigator shell immediately so the pane is never a dead end,
    // then fill in the real drive/folder listing.
    renderPaneBrowser('right');
    navigatePane('right', pane.directory || '').catch(showError);
  });
  el('move-selected-left').addEventListener('click', () => moveSelectedAssetsBetweenDirectories('left', 'right'));
  el('move-selected-right').addEventListener('click', () => moveSelectedAssetsBetweenDirectories('right', 'left'));
  el('tag-clear').addEventListener('click', () => {
    state.tagFilter.clear();
    renderTagFilterBar();
    renderPhotoGrid();
  });
  el('date-from').addEventListener('input', (event) => {
    state.dateFrom = event.target.value;
    renderDateFilterBar();
    renderPhotoGrid();
  });
  el('date-to').addEventListener('input', (event) => {
    state.dateTo = event.target.value;
    renderDateFilterBar();
    renderPhotoGrid();
  });
  el('date-clear').addEventListener('click', () => {
    state.dateFrom = '';
    state.dateTo = '';
    renderDateFilterBar();
    renderPhotoGrid();
  });
  el('show-raw-toggle').addEventListener('change', (event) => {
    state.showRawOnly = event.target.checked;
    localStorage.setItem('kira-show-raw-only', String(state.showRawOnly));
    // Deselect hidden assets so selection counts match the RAW display setting.
    if (!state.showRawOnly) {
      ['left', 'right'].forEach((paneId) => {
        const pane = paneFor(paneId);
        const visibleIds = new Set(displayableAssets(pane).map((a) => a.id));
        for (const id of Array.from(pane.selectedAssets)) {
          if (!visibleIds.has(id)) pane.selectedAssets.delete(id);
        }
      });
    }
    renderPhotoGrid();
    toast(state.showRawOnly ? 'Showing all files including RAW-only' : 'Hiding RAW-only photos');
  });
  el('close-lightbox').addEventListener('click', () => el('lightbox-modal').close());
  el('lightbox-modal').addEventListener('click', (event) => {
    if (event.target === el('lightbox-modal') || event.target.classList.contains('lightbox-stage')) el('lightbox-modal').close();
  });
  el('lightbox-modal').addEventListener('close', () => el('lightbox-image').removeAttribute('src'));
  el('close-compare').addEventListener('click', () => {
    el('compare-modal').classList.add('hidden');
    syncSelectionTiles();
  });
  el('compare-zoom').addEventListener('input', (event) => {
    state.compareZoom = Number(event.target.value) / 100;
    if (state.compareZoom === 1) state.comparePan = {x: 0, y: 0};
    el('compare-zoom-value').textContent = `${event.target.value}%`;
    applyCompareTransform();
  });
  el('compare-grid').addEventListener('wheel', (event) => {
    event.preventDefault();
    const step = event.deltaY < 0 ? 10 : -10;
    const percent = Math.min(300, Math.max(100, Math.round(state.compareZoom * 100) + step));
    state.compareZoom = percent / 100;
    if (state.compareZoom === 1) state.comparePan = {x: 0, y: 0};
    el('compare-zoom').value = String(percent);
    el('compare-zoom-value').textContent = `${percent}%`;
    applyCompareTransform();
  }, {passive: false});
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !el('compare-modal').classList.contains('hidden')) {
      el('compare-modal').classList.add('hidden');
      syncSelectionTiles();
    }
  });

  el('selection-job-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    const originalLabel = button.textContent;
    button.textContent = 'Creating job…';
    try {
      const job = await api('/api/jobs/from-selection', {
        method: 'POST',
        body: JSON.stringify({
          name: el('job-name').value,
          source_directory: paneFor().directory,
          selected_ids: Array.from(paneFor().selectedAssets),
          source_format: el('edit-source-format').value,
        }),
      });
      el('selection-job-form').reset();
      toast('Edit job ready on the iPad. Source photos were not copied.');
      await loadDashboard();
      await openJob(job.id);
    } catch (error) {
      toast(error.message);
    } finally {
      button.textContent = originalLabel;
      button.disabled = paneFor().selectedAssets.size === 0;
    }
  });

  el('return-files').addEventListener('change', () => {
    const count = el('return-files').files.length;
    el('upload-returns').disabled = !count;
    const strong = document.querySelector('.return-picker-content strong');
    strong.textContent = count ? `${count} ${count === 1 ? 'file' : 'files'} selected` : 'Choose edited photos or ZIP';
  });

  el('upload-returns').addEventListener('click', async () => {
    const files = Array.from(el('return-files').files);
    if (!state.currentJob || !files.length) return;
    const button = el('upload-returns');
    button.disabled = true;
    try {
      await uploadFiles(state.currentJob.id, files, 'returns', progressSet('return'));
      el('return-files').value = '';
      document.querySelector('.return-picker-content strong').textContent = 'Choose edited photos or ZIP';
      toast('Returned edits are verified on the desktop.');
      await openJob(state.currentJob.id);
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  });

  el('back-to-jobs').addEventListener('click', loadDashboard);
  el('refresh-jobs').addEventListener('click', loadDashboard);
  el('organize-source').addEventListener('click', async () => {
    if (!state.currentJob) return;
    const confirmed = window.confirm(
      `Organize the original folder for “${state.currentJob.name}”?\n\n` +
      'Kira will move the existing RAW and JPEG files into subfolders and copy returned Lightroom edits into selected_jpeg/edited.'
    );
    if (!confirmed) return;
    const button = el('organize-source');
    button.disabled = true;
    button.textContent = 'Organizing…';
    try {
      const result = await api(`/api/jobs/${state.currentJob.id}/organize`, {method: 'POST', body: '{}'});
      toast(result.errors?.length ? 'Some files need attention. Review the details and retry.' : `Organized ${result.source_directory}`);
      await openJob(state.currentJob.id);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Finish & organize source folder';
      toast(error.message);
    }
  });
  el('delete-job').addEventListener('click', async () => {
    if (!state.currentJob) return;
    const message = `Remove the transfer job “${state.currentJob.name}”?\n\nCompleted files and their local checkpoints will be kept. Unfinished uploads must be recovered first.`;
    if (!window.confirm(message)) return;
    const button = el('delete-job');
    button.disabled = true;
    try {
      await api(`/api/jobs/${state.currentJob.id}`, {method: 'DELETE'});
      state.currentJob = null;
      toast('Transfer job removed. Checkpoints remain in Saved files.');
      await loadDashboard();
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  });

  el('open-history').addEventListener('click', async () => {
    const list = el('history-list');
    list.textContent = 'Loading saved files…';
    el('history-modal').showModal();
    async function loadSavedFiles(offset = 0) {
      const response = await api(`/api/local/history?offset=${offset}`);
      if (!offset) list.replaceChildren();
      if (!offset && !response.revisions.length) list.textContent = 'Files appear here after they are sent, received, or organized.';
      for (const saved of response.revisions) {
        const row = document.createElement('div');
        row.className = 'saved-file-row';
        const details = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = saved.record.original_filename || saved.record.filename;
        const subtitle = document.createElement('small');
        subtitle.textContent = `${formatBytes(saved.record.size)} · ${new Date(saved.created_at).toLocaleString()} · Local checkpoint`;
        details.append(title, subtitle);
        const restore = document.createElement('button');
        restore.className = 'quiet-button';
        restore.textContent = 'Restore a copy';
        restore.addEventListener('click', async () => {
          const directory = window.prompt('Restore into this folder:', state.libraryPanes[state.activePaneId].directory || '');
          if (!directory) return;
          restore.disabled = true;
          try {
            const result = await api('/api/local/restore', {method:'POST',body:JSON.stringify({revision_id:saved.id,directory})});
            toast(`Restored ${result.path}`);
          } catch (error) { toast(error.message); }
          finally { restore.disabled = false; }
        });
        row.append(details, restore);
        list.append(row);
      }
      if (response.next_offset != null) {
        const more = document.createElement('button');
        more.className = 'quiet-button';
        more.textContent = 'Load older files';
        more.addEventListener('click', async () => {
          more.disabled = true;
          try { await loadSavedFiles(response.next_offset); more.remove(); }
          catch (error) { toast(error.message); more.disabled = false; }
        });
        list.append(more);
      }
    }
    try { await loadSavedFiles(); }
    catch (error) { list.textContent = error.message; }
  });
  el('close-history').addEventListener('click', () => el('history-modal').close());

  let toastTimer;
  function toast(message) {
    const node = el('toast');
    node.textContent = message;
    node.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add('hidden'), 4200);
  }

  function showError(error) {
    toast(error.message || String(error));
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
  }

  function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '' : date.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character]));
  }

  initialize();
})();
