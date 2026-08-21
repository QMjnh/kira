(() => {
  const CHUNK_SIZE = 16 * 1024 * 1024;
  const state = {
    token: localStorage.getItem('kira-token') || '',
    desktop: false,
    bootstrap: null,
    jobs: [],
    currentJob: null,
    browserPath: '',
    browserParent: null,
    libraryDirectory: '',
    libraryAssets: [],
    culling: null,
    selectedAssets: new Set(),
    libraryPanes: {
      left: {directory: '', assets: [], culling: null, selectedAssets: new Set()},
      right: {directory: '', assets: [], culling: null, selectedAssets: new Set()},
    },
    activePaneId: 'left',
    browserTargetPane: 'left',
    compareZoom: 1,
    comparePan: {x: 0, y: 0},
    googleDownloadSettingsOpen: false,
    google: {configured: false, connected: false},
  };

  const el = (id) => document.getElementById(id);
  const pairView = el('pair-view');
  const dashboardView = el('dashboard-view');
  const jobView = el('job-view');
  const connectionState = el('connection-state');

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
      setDisconnected('Pairing with Dell…');
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
        el('pair-error').textContent = 'That QR link has expired. Scan the current code shown on the Dell.';
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
    setConnected(state.desktop ? 'Dell server running' : 'Connected to Dell');
    showOnly(dashboardView);
    el('desktop-services').classList.toggle('hidden', !state.desktop);
    el('desktop-setup').classList.toggle('hidden', !state.desktop);
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
    const organizerStatus = el('google-organizer-status');
    organizerStatus.textContent = connected
      ? 'Connected'
      : organizer.available === false ? 'Client unavailable' : 'Not connected';
    organizerStatus.title = connected && organizer.account
      ? `Google account: ${organizer.account}`
      : '';
    el('google-organizer-connect').classList.toggle('hidden', connected);
    el('google-organizer-controls').classList.toggle('hidden', !connected);
    el('google-organizer-connect-button').disabled = organizer.available === false;
    if (!el('google-album-title').value) {
      el('google-album-title').value = localStorage.getItem('kira-google-album-title') || '';
    }
    const matchSource = el('google-match-source');
    matchSource.querySelector('strong').textContent = state.libraryDirectory
      ? state.libraryDirectory
      : 'Open a local folder below first.';
    const matchButton = el('google-match-folder');
    matchButton.disabled = !connected
      || !state.libraryDirectory
      || !el('google-album-title').value.trim();
    matchButton.textContent = el('google-archive-after-upload').checked
      ? 'Match folder → album + archive'
      : 'Match folder → album';
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
    el('photo-workspace').classList.remove('hidden');
    el('folder-browser').classList.add('hidden');
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

  async function browseDirectory(path = '') {
    const result = await api(`/api/local/browse?path=${encodeURIComponent(path)}`);
    state.browserPath = result.current;
    state.browserParent = result.parent;
    const breadcrumbs = el('folder-path');
    breadcrumbs.replaceChildren();
    const computer = document.createElement('button');
    computer.type = 'button';
    computer.textContent = 'Computer';
    computer.addEventListener('click', () => browseDirectory('').catch(showError));
    breadcrumbs.appendChild(computer);
    (result.breadcrumbs || []).forEach((crumb) => {
      const separator = document.createElement('span');
      separator.textContent = '›';
      separator.setAttribute('aria-hidden', 'true');
      breadcrumbs.appendChild(separator);
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = crumb.name;
      button.title = crumb.path;
      button.addEventListener('click', () => browseDirectory(crumb.path).catch(showError));
      breadcrumbs.appendChild(button);
    });
    el('folder-up').disabled = result.parent === null && result.current === '';
    el('use-folder').disabled = !result.current;
    const list = el('folder-list');
    list.replaceChildren();
    result.directories.forEach((directory) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'folder-row';
      button.textContent = `📁 ${directory.name}`;
      button.addEventListener('click', () => browseDirectory(directory.path).catch(showError));
      list.appendChild(button);
    });
    if (!result.directories.length) list.innerHTML = '<p class="panel-copy">No subfolders.</p>';
  }

  async function scanLibraryFolder() {
    if (!state.browserPath) return;
    const result = await api(`/api/local/scan?path=${encodeURIComponent(state.browserPath)}`);
    applyLibraryScan(result, state.browserTargetPane);
    el('photo-workspace').classList.remove('hidden');
    el('folder-browser').classList.add('hidden');
    if (!result.assets.length) toast('No supported photos were found in that folder.');
  }

  function folderLabel(path) {
    const normalized = path.replace(/[\\/]+$/, '');
    return normalized.split(/[\\/]/).pop() || path;
  }

  function paneFor(paneId = state.activePaneId) {
    return state.libraryPanes[paneId];
  }

  function paneSummary(pane) {
    const photoCount = pane.assets.filter((asset) => asset.jpeg_files.length).length;
    const videoCount = pane.assets.filter((asset) => (asset.video_files || []).length).length;
    if (!pane.directory) return 'No folder open';
    return `${photoCount} photo group${photoCount === 1 ? '' : 's'} · ${videoCount} video group${videoCount === 1 ? '' : 's'}`;
  }

  function renderPaneHeaders() {
    const leftPane = paneFor('left');
    const rightPane = paneFor('right');
    const hasComparison = Boolean(rightPane.directory);
    el('library-split').classList.toggle('single-pane', !hasComparison);
    el('library-pane-right').classList.toggle('hidden', !hasComparison);
    el('library-split').querySelector('.split-move-actions').classList.toggle('hidden', !hasComparison);
    el('browse-folders').classList.toggle('hidden', Boolean(leftPane.directory));
    el('browse-folders-right').classList.toggle('hidden', !leftPane.directory);
    el('browse-folders-right').textContent = hasComparison ? 'Change comparison' : 'Compare a folder';
    ['left', 'right'].forEach((paneId) => {
      const pane = paneFor(paneId);
      const name = pane.directory ? folderLabel(pane.directory) : 'Choose a folder';
      el(`library-pane-${paneId}`).classList.toggle('active', paneId === state.activePaneId);
      el(`${paneId}-folder-name`).textContent = name;
      const pathButton = el(`${paneId}-folder-path`);
      pathButton.textContent = pane.directory || 'No folder open';
      pathButton.title = pane.directory ? `Browse from ${pane.directory}` : '';
      pathButton.disabled = !pane.directory;
      el(`photo-count-${paneId}`).textContent = paneSummary(pane);
      el(`selection-count-${paneId}`).textContent = `${pane.selectedAssets.size} selected`;
    });
    const active = paneFor();
    el('create-job-card').querySelectorAll('.folder-dependent').forEach((node) => {
      node.classList.toggle('hidden', !active.directory);
    });
    el('photo-count').textContent = active.directory
      ? `${paneSummary(active)}${hasComparison ? ' · click either folder to make it active' : ''}`
      : 'Open a folder to begin.';
  }

  async function openFolderBrowserForPane(paneId) {
    state.browserTargetPane = paneId;
    el('folder-browser').classList.remove('hidden');
    await browseDirectory(paneFor(paneId).directory || '');
    el('folder-browser').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  function setActivePane(paneId) {
    if (!state.libraryPanes[paneId]) return;
    state.activePaneId = paneId;
    const pane = paneFor(paneId);
    state.libraryDirectory = pane.directory;
    state.libraryAssets = pane.assets;
    state.culling = pane.culling;
    state.selectedAssets = pane.selectedAssets;
    state.browserPath = pane.directory;
    renderPaneHeaders();
    renderCullFolders();
    renderGroupPicker();
    renderGoogleDestination();
    renderGoogleOrganizer();
    updateSelectionUi();
  }

  function applyLibraryScan(result, paneId = state.activePaneId) {
    const pane = paneFor(paneId);
    // Culling is JPEG-driven: RAW-only files stay out of the visual workspace,
    // while a same-stem RAW remains available as the selected transfer source.
    pane.directory = result.directory;
    pane.assets = result.assets.filter((asset) => asset.jpeg_files.length > 0 || (asset.video_files || []).length > 0);
    pane.culling = result.culling || null;
    pane.selectedAssets.clear();
    state.browserTargetPane = paneId;
    setActivePane(paneId);
    renderPhotoGrid();
  }

  async function openCullFolder(path) {
    state.browserPath = path;
    const result = await api(`/api/local/scan?path=${encodeURIComponent(path)}`);
    applyLibraryScan(result, state.activePaneId);
  }

  function renderCullFolders() {
    const nav = el('cull-folders');
    nav.replaceChildren();
    (state.culling?.folders || []).forEach((folder) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `cull-folder${folder.path === state.libraryDirectory ? ' active' : ''}`;
      button.textContent = folder.name;
      button.title = folder.path;
      button.disabled = folder.path === state.libraryDirectory;
      button.addEventListener('click', () => openCullFolder(folder.path).catch(showError));
      nav.appendChild(button);
    });
    const canRestore = Boolean(state.culling && state.culling.role !== 'inbox');
    el('restore-inbox').classList.toggle('hidden', !canRestore);
  }

  function comparisonGroups() {
    return (state.culling?.folders || [])
      .filter((folder) => folder.role === 'group')
      .map((folder) => folder.group_name || folder.name.replace(/^Compare:\s*/, ''));
  }

  function selectedComparisonGroupName() {
    const selected = el('compare-group-select').value;
    return selected === '__new__' ? el('compare-group-name').value.trim() : selected;
  }

  function renderGroupPicker(preferredGroup = '') {
    const select = el('compare-group-select');
    const previous = preferredGroup || select.value;
    select.replaceChildren();

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Choose comparison group…';
    select.appendChild(placeholder);

    const groups = comparisonGroups();
    groups.forEach((name) => {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name;
      select.appendChild(option);
    });

    const create = document.createElement('option');
    create.value = '__new__';
    create.textContent = '+ New comparison group…';
    select.appendChild(create);

    if (groups.includes(previous)) select.value = previous;
    else if (!groups.length) select.value = '__new__';
    else select.value = '';
    el('compare-group-name').classList.toggle('hidden', select.value !== '__new__');
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
    const fragment = document.createDocumentFragment();
    pane.assets.forEach((asset) => {
      const selected = pane.selectedAssets.has(asset.id);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `photo-tile${selected ? ' selected' : ''}`;
      button.setAttribute('aria-pressed', String(selected));
      button.dataset.assetId = asset.id;
      const media = asset.preview_path
        ? `<img src="${escapeHtml(previewUrl(asset.preview_path, 480, asset.preview_version))}" alt="${escapeHtml(asset.stem)}" loading="lazy" decoding="async">`
        : '<span class="photo-placeholder video-placeholder"><span>▶</span>Video</span>';
      const formatTags = [];
      if (asset.jpeg_files.length) formatTags.push(`<span class="format-tag">JPEG${asset.jpeg_files.length > 1 ? ` ×${asset.jpeg_files.length}` : ''}</span>`);
      if ((asset.video_files || []).length) formatTags.push(`<span class="format-tag">VIDEO${asset.video_files.length > 1 ? ` ×${asset.video_files.length}` : ''}</span>`);
      const tags = formatTags.join('');
      button.innerHTML = `${media}<span class="pick-check">${selected ? '✓' : '+'}</span><span class="photo-tile-info"><strong>${escapeHtml(asset.stem)}</strong><span class="format-tags">${tags}</span></span>`;
      button.addEventListener('click', () => {
        setActivePane(paneId);
        toggleAsset(asset.id, button, paneId);
      });
      fragment.appendChild(button);
    });
    grid.appendChild(fragment);
  }

  function renderPhotoGrid() {
    renderPanePhotoGrid('left');
    renderPanePhotoGrid('right');
    renderPaneHeaders();
    updateSelectionUi();
  }

  function toggleAsset(assetId, tile, paneId = state.activePaneId) {
    const pane = paneFor(paneId);
    if (pane.selectedAssets.has(assetId)) pane.selectedAssets.delete(assetId);
    else pane.selectedAssets.add(assetId);
    state.selectedAssets = pane.selectedAssets;
    const selected = pane.selectedAssets.has(assetId);
    tile.classList.toggle('selected', selected);
    tile.setAttribute('aria-pressed', String(selected));
    tile.querySelector('.pick-check').textContent = selected ? '✓' : '+';
    renderPaneHeaders();
    updateSelectionUi();
  }

  function syncSelectionTiles() {
    ['left', 'right'].forEach((paneId) => {
      const pane = paneFor(paneId);
      document.querySelectorAll(`#photo-grid-${paneId} .photo-tile`).forEach((tile) => {
        const selected = pane.selectedAssets.has(tile.dataset.assetId);
        tile.classList.toggle('selected', selected);
        tile.setAttribute('aria-pressed', String(selected));
        tile.querySelector('.pick-check').textContent = selected ? '✓' : '+';
      });
    });
    renderPaneHeaders();
  }

  function updateSelectionUi() {
    const count = state.selectedAssets.size;
    el('selection-summary').textContent = `${count} media group${count === 1 ? '' : 's'} selected for iPad editing`;
    el('selection-job-form').querySelector('button[type="submit"]').disabled = count === 0;
    el('compare-picked').disabled = count < 2;
    el('compare-picked').textContent = `Compare selected${count ? ` (${count})` : ''}`;
    el('select-all').disabled = state.libraryAssets.length === 0 || count === state.libraryAssets.length;
    el('clear-picked').disabled = count === 0;
    el('mark-unselect').disabled = count === 0;
    el('mark-select').disabled = count === 0;
    el('restore-inbox').disabled = count === 0;
    el('group-selected').disabled = count === 0 || !selectedComparisonGroupName();
    el('google-upload-selected').disabled = count === 0 || !state.google.connected;
    const left = paneFor('left');
    const right = paneFor('right');
    el('move-selected-left').disabled = !left.directory || !right.directory || left.selectedAssets.size === 0;
    el('move-selected-right').disabled = !left.directory || !right.directory || right.selectedAssets.size === 0;
  }

  async function moveSelectedAssets(action) {
    if (!state.selectedAssets.size) return;
    const labels = {
      select: 'Move the selected photo groups into the select folder?',
      unselect: 'Move the selected photo groups into the unselect folder? Nothing will be deleted.',
      restore: 'Move the selected photo groups back to the inbox folder?',
      group: 'Move the selected photo groups into this comparison group?',
    };
    if (!window.confirm(labels[action])) return;
    const controls = ['mark-unselect', 'mark-select', 'restore-inbox', 'group-selected'].map(el);
    controls.forEach((button) => { button.disabled = true; });
    const groupButton = el('group-selected');
    if (action === 'group') groupButton.textContent = 'Checking photos…';
    const groupName = selectedComparisonGroupName();
    try {
      const result = await api('/api/local/cull', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: state.libraryDirectory,
          asset_ids: Array.from(state.selectedAssets),
          action,
          group_name: groupName,
        }),
      });
      applyLibraryScan(result);
      if (action === 'group') renderGroupPicker(result.destination_group_name || groupName);
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
      groupButton.textContent = 'Group to compare';
      updateSelectionUi();
    }
  }

  async function moveSelectedAssetsBetweenDirectories(sourcePaneId, destinationPaneId) {
    const sourcePane = paneFor(sourcePaneId);
    const destinationPane = paneFor(destinationPaneId);
    if (!sourcePane.directory || !destinationPane.directory || !sourcePane.selectedAssets.size) return;
    if (!window.confirm(`Move the selected photo groups from the ${sourcePaneId} folder to the ${destinationPaneId} folder?`)) return;
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
      toast(`${result.moved_assets} photo group${result.moved_assets === 1 ? '' : 's'} moved from ${sourcePaneId} to ${destinationPaneId}.`);
    } catch (error) {
      toast(error.message);
      updateSelectionUi();
    }
  }

  function selectedPreviewAssets() {
    return state.libraryAssets.filter((asset) => state.selectedAssets.has(asset.id) && asset.preview_path);
  }

  function openCompare() {
    const assets = selectedPreviewAssets();
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
    assets.forEach((asset) => {
      const item = document.createElement('div');
      item.className = 'compare-item';
      item.dataset.assetId = asset.id;
      item.innerHTML = `<img src="${escapeHtml(previewUrl(asset.preview_path, 1600, asset.preview_version))}" alt="${escapeHtml(asset.stem)}" decoding="async"><div class="compare-label"><strong>${escapeHtml(asset.stem)}</strong><button type="button" class="is-picked">Selected</button></div>`;
      const pickButton = item.querySelector('button');
      pickButton.addEventListener('click', (event) => {
        event.stopPropagation();
        if (state.selectedAssets.has(asset.id)) state.selectedAssets.delete(asset.id);
        else state.selectedAssets.add(asset.id);
        const picked = state.selectedAssets.has(asset.id);
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
      const finished = (postprocess.status === 'complete' || postprocess.status === 'complete_with_warnings')
        && postprocess.layout_version === 2;
      el('organize-source').disabled = finished;
      el('organize-source').textContent = finished ? 'Source folder organized' : 'Finish & organize source folder';
      result.classList.toggle('hidden', !finished);
      if (finished) {
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
      row.innerHTML = `<div class="file-name"><strong>${escapeHtml(file.filename)}</strong><small>${formatBytes(file.size)}${file.matched_filename ? ` · matched to ${escapeHtml(file.matched_filename)}` : ''}</small></div><span class="match-badge ${escapeHtml(file.match_status)}">${escapeHtml(file.match_status)}</span>`;
      returns.appendChild(row);
    });
    if (!job.returns.length) returns.innerHTML = '<p class="panel-copy">No edits returned yet.</p>';
  }

  async function uploadFile(jobId, file, kind, onProgress) {
    const started = await api(`/api/jobs/${jobId}/uploads/start`, {
      method: 'POST',
      body: JSON.stringify({
        kind,
        filename: file.name,
        size: file.size,
        last_modified: file.lastModified || 0,
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
      await uploadFile(jobId, file, kind, (currentBytes) => {
        const transferred = completedBeforeCurrent + currentBytes;
        const percent = total ? Math.round((transferred / total) * 100) : 100;
        progressElements.bar.value = percent;
        progressElements.value.textContent = `${percent}%`;
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
    if (!window.confirm('Disconnect Google Photos from Kira? Imported local files will stay on this computer.')) return;
    try {
      await api('/api/google/disconnect', {method: 'POST', body: '{}'});
      await refreshGoogleStatus();
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
      toast('Google Photos album and Archive automation connected.');
    } catch (error) {
      showError(error);
    }
  });

  el('google-organizer-disconnect').addEventListener('click', async () => {
    if (!window.confirm('Remove the encrypted Google Photos web session from Kira?')) return;
    try {
      await api('/api/google/web-session', {method: 'DELETE'});
      await refreshGoogleStatus();
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
    if (!state.libraryDirectory || !albumTitle || !state.google.organizer?.connected) return;
    const confirmed = window.confirm(
      `Match every supported file directly inside:\n${state.libraryDirectory}\n\n` +
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
          source_directory: state.libraryDirectory,
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

  el('google-upload-selected').addEventListener('click', async () => {
    if (!state.selectedAssets.size || !state.google.connected) return;
    const button = el('google-upload-selected');
    button.disabled = true;
    try {
      const operation = await api('/api/google/uploads', {
        method: 'POST',
        body: JSON.stringify({
          source_directory: state.libraryDirectory,
          asset_ids: Array.from(state.selectedAssets),
        }),
      });
      const finished = await pollGoogleOperation(operation.id);
      if (finished.status === 'failed') throw new Error(finished.error || 'Google Photos upload failed.');
      toast(`Uploaded ${finished.completed} media item${finished.completed === 1 ? '' : 's'}${finished.failed ? `; ${finished.failed} failed` : ''}.`);
    } catch (error) {
      showError(error);
    } finally {
      el('google-progress').classList.add('hidden');
      updateSelectionUi();
    }
  });

  el('browse-folders').addEventListener('click', () => openFolderBrowserForPane('left').catch(showError));
  el('browse-folders-right').addEventListener('click', () => openFolderBrowserForPane('right').catch(showError));
  el('left-folder-path').addEventListener('click', () => openFolderBrowserForPane('left').catch(showError));
  el('right-folder-path').addEventListener('click', () => openFolderBrowserForPane('right').catch(showError));
  ['left', 'right'].forEach((paneId) => {
    el(`library-pane-${paneId}`).addEventListener('click', (event) => {
      if (!event.target.closest('button')) setActivePane(paneId);
    });
  });

  el('folder-up').addEventListener('click', () => browseDirectory(state.browserParent || '').catch(showError));
  el('close-folder-browser').addEventListener('click', () => el('folder-browser').classList.add('hidden'));
  el('use-folder').addEventListener('click', () => scanLibraryFolder().catch(showError));
  el('move-selected-left').addEventListener('click', () => moveSelectedAssetsBetweenDirectories('left', 'right'));
  el('move-selected-right').addEventListener('click', () => moveSelectedAssetsBetweenDirectories('right', 'left'));
  el('mark-unselect').addEventListener('click', () => moveSelectedAssets('unselect'));
  el('mark-select').addEventListener('click', () => moveSelectedAssets('select'));
  el('restore-inbox').addEventListener('click', () => moveSelectedAssets('restore'));
  el('group-selected').addEventListener('click', () => moveSelectedAssets('group'));
  el('compare-group-name').addEventListener('input', updateSelectionUi);
  el('compare-group-select').addEventListener('change', () => {
    const creating = el('compare-group-select').value === '__new__';
    el('compare-group-name').classList.toggle('hidden', !creating);
    if (creating) el('compare-group-name').focus();
    updateSelectionUi();
  });
  el('select-all').addEventListener('click', () => {
    state.selectedAssets.clear();
    state.libraryAssets.forEach((asset) => state.selectedAssets.add(asset.id));
    syncSelectionTiles();
    updateSelectionUi();
  });
  el('clear-picked').addEventListener('click', () => {
    state.selectedAssets.clear();
    syncSelectionTiles();
    updateSelectionUi();
  });
  el('compare-picked').addEventListener('click', openCompare);
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
          source_directory: state.libraryDirectory,
          selected_ids: Array.from(state.selectedAssets),
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
      button.disabled = state.selectedAssets.size === 0;
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
      toast('Returned edits are verified on the Dell.');
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
      toast(`Organized ${result.source_directory}`);
      await openJob(state.currentJob.id);
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Finish & organize source folder';
      toast(error.message);
    }
  });
  el('delete-job').addEventListener('click', async () => {
    if (!state.currentJob) return;
    const finished = ['complete', 'complete_with_warnings'].includes(state.currentJob.postprocess?.status);
    const message = finished
      ? `Delete the Kira job “${state.currentJob.name}”?\n\nThe organized source folders will be kept.`
      : `Delete the Kira job “${state.currentJob.name}”?\n\nThis job has not been organized. Its Kira transfer copies and returned edits will be deleted.`;
    if (!window.confirm(message)) return;
    const button = el('delete-job');
    button.disabled = true;
    try {
      await api(`/api/jobs/${state.currentJob.id}`, {method: 'DELETE'});
      state.currentJob = null;
      toast('Job deleted. Source folders were kept.');
      await loadDashboard();
    } catch (error) {
      button.disabled = false;
      toast(error.message);
    }
  });

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
