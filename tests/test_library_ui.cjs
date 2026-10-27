// Run with `node --test tests/test_library_ui.cjs` with Playwright installed.
// All API responses are fixtures: these checks never read or move real photos.
const {test, before, after, beforeEach, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync, existsSync} = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
let browser, page, errors, requests, releaseScan, releaseSignIn;
const assets = (folder) => Array.from({length: 4}, (_, i) => ({
  id: `${folder}-${i}`, stem: `Photo ${i + 1}`, jpeg_files: [{}], raw_files: [],
  video_files: [], tags: [], preview_path: `${folder}/${i}.jpg`,
  taken_at: i < 2 ? '2024-06-10T12:00:00' : '2025-06-10T12:00:00',
}));
const scan = (folder) => ({directory: folder, assets: folder === '/empty' ? [] : assets(folder), culling: {role: 'inbox', folders: []}});
const roots = ['photos', 'camera', 'empty', 'slow'].map((name) => ({name, path: `/${name}`}));

before(async () => {
  const executablePath = process.env.KIRA_TEST_BROWSER || [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  ].find(existsSync);
  browser = await chromium.launch({headless: true, ...(executablePath ? {executablePath} : {})});
});
after(async () => { await browser?.close(); });
beforeEach(async () => {
  page = await browser.newPage({viewport: {width: 1566, height: 1080}});
  errors = [];
  requests = [];
  releaseScan = null;
  releaseSignIn = null;
  let groupCreated = false;
  let signIn = {status: 'idle'};
  let organizerConnected = false;
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('dialog', (dialog) => dialog.accept(dialog.type() === 'prompt' ? 'Best shots' : undefined));
  await page.route('http://kira.test/**', async (route) => {
    const url = new URL(route.request().url());
    const folder = url.searchParams.get('path') || '';
    let body;
    if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: readFileSync(path.join(root, 'web/index.html'), 'utf8')});
    if (['/app.js', '/styles.css'].includes(url.pathname)) {
      return route.fulfill({contentType: url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css', body: readFileSync(path.join(root, 'web', url.pathname.slice(1)), 'utf8')});
    }
    if (url.pathname === '/api/bootstrap') body = {token: 'test-token', ipad_url: 'http://kira.test', pair_code: '123456', data_dir: 'Test library', pair_qr_url: '/qr'};
    else if (url.pathname === '/api/jobs') body = {jobs: []};
    else if (url.pathname === '/api/google/status') body = {configured: true, connected: true, inbox: '/photos', organizer: {available: true, browser_available: true, connected: organizerConnected, sign_in: signIn}};
    else if (url.pathname === '/api/google/web-signin') {
      const method = route.request().method();
      if (method === 'POST') {
        requests.push({method, body: route.request().postDataJSON()});
        signIn = {status: 'waiting'};
        releaseSignIn = () => { signIn = {status: 'completed'}; organizerConnected = true; };
      } else if (method === 'DELETE') {
        requests.push({method});
        signIn = {status: 'cancelled'};
      }
      body = signIn;
    }
    else if (url.pathname === '/api/google/health') body = {ok: true};
    else if (url.pathname === '/api/local/browse') body = {parent: null, breadcrumbs: folder ? [{name: folder.slice(1), path: folder}] : [], directories: folder ? (groupCreated ? [{name: 'compare_groups', path: `${folder}/compare_groups`}] : []) : roots};
    else if (url.pathname === '/api/local/scan') {
      if (folder === '/slow') await new Promise((resolve) => { releaseScan = resolve; });
      body = scan(folder);
    } else if (url.pathname === '/api/local/compare-groups') {
      const request = route.request().postDataJSON();
      requests.push(request);
      body = {...scan(request.source_directory), created_groups: []};
    } else if (url.pathname === '/api/local/cull') {
      const request = route.request().postDataJSON();
      requests.push(request);
      groupCreated = true;
      body = {...scan(request.source_directory), moved_assets: 4, destination_group_name: request.group_name};
    } else if (url.pathname === '/api/local/tags') {
      const request = route.request().postDataJSON();
      requests.push(request);
      body = {assignments: Object.fromEntries(request.asset_ids.map((id) => [id, request.add_tags || []]))};
    } else if (url.pathname === '/api/google/uploads') {
      requests.push(route.request().postDataJSON());
      body = {id: 'test-upload'};
    } else if (url.pathname.startsWith('/api/google/operations/')) body = {status: 'completed', completed: 4, total: 4};
    else return route.fulfill({contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"><rect width="320" height="200" fill="#e1eae4"/><path d="M0 200L110 70 180 145 240 105 320 200Z" fill="#9fb8a8"/></svg>'});
    await route.fulfill({json: body});
  });
  await page.goto('http://kira.test/');
  await page.locator('#left-folder-list .folder-row-name').first().waitFor();
});
afterEach(async () => {
  releaseScan?.();
  await page.close();
  assert.deepEqual(errors, [], 'No browser script errors');
});

async function openFolder(side, name) {
  await page.locator(`#${side}-folder-list .folder-row-name`).filter({hasText: name}).click();
  await page.waitForFunction(({side, name}) => document.getElementById(`${side}-folder-name`).textContent === name, {side, name});
}
async function openPair() {
  await openFolder('left', 'photos');
  await page.locator('#browse-folders-right').click();
  await openFolder('right', 'camera');
}
async function selected(side) { return page.locator(`#selection-count-${side}`).innerText(); }

test('Google Photos connects after browser sign-in without a cookie file', async () => {
  await page.locator('#google-organizer > summary').click();
  assert.equal(await page.locator('#google-cookies-path').isVisible(), false);
  await page.locator('#google-organizer-signin').click();
  await page.locator('#google-signin-cancel').waitFor();
  assert.equal(await page.locator('#google-organizer-signin').isDisabled(), true);
  assert.equal(await page.locator('#google-signin-message').innerText(), 'Complete sign-in in the Google Photos window. Kira will connect automatically.');
  assert.ok(releaseSignIn);
  releaseSignIn();
  await page.locator('#google-organizer-controls').waitFor();
  assert.equal(await page.locator('#google-organizer-status').innerText(), 'Connected');
  assert.equal(await page.locator('#google-cookies-path').inputValue(), '');
  assert.deepEqual(requests, [{method: 'POST', body: {}}]);
});

test('pending Google sign-in survives a page reload and can be cancelled', async () => {
  await page.locator('#google-organizer > summary').click();
  await page.locator('#google-organizer-signin').click();
  await page.locator('#google-signin-cancel').waitFor();
  await page.reload();
  await page.locator('#google-organizer > summary').click();
  await page.locator('#google-signin-cancel').waitFor();
  assert.equal(await page.locator('#google-organizer-signin').isDisabled(), true);
  await page.locator('#google-signin-cancel').click();
  await page.waitForFunction(() => document.getElementById('google-signin-message').textContent === 'Sign-in cancelled.');
  assert.equal(await page.locator('#google-organizer-signin').isDisabled(), false);
  assert.deepEqual(requests, [{method: 'POST', body: {}}, {method: 'DELETE'}]);
});

test('selection and comparison belong to the panel that owns the button', async () => {
  await openPair();
  await page.locator('#left-select-all').click();
  assert.equal(await selected('left'), '4 selected');
  assert.equal(await selected('right'), '0 selected');
  await page.locator('#right-select-all').click();
  await page.locator('#left-clear-picked').click();
  assert.equal(await selected('right'), '4 selected');
  assert.equal(await selected('left'), '0 selected');
  await page.locator('#right-compare-picked').click();
  assert.equal(await page.locator('#compare-grid .compare-item').count(), 4);
  assert.match(await page.locator('#compare-grid img').first().getAttribute('src'), /camera/);
  await page.locator('#close-compare').click();
});

test('both close buttons work and closing the left preserves the other selection', async () => {
  await openPair();
  await page.locator('#right-select-all').click();
  await page.locator('#left-close-folder').click();
  assert.equal(await page.locator('#left-folder-name').innerText(), 'camera');
  assert.equal(await selected('left'), '4 selected');
  assert.equal(await page.locator('#library-pane-right').isVisible(), false);
  await page.locator('#browse-folders-right').click();
  await openFolder('right', 'photos');
  await page.locator('#right-close-folder').click();
  assert.equal(await page.locator('#left-folder-name').innerText(), 'camera');
  assert.equal(await selected('left'), '4 selected');
  await page.locator('#left-close-folder').click();
  await page.locator('#left-folder-list .folder-row-name').first().waitFor();
  assert.equal(await page.locator('#left-folder-name').innerText(), 'Desktop');
  assert.equal(await selected('left'), '0 selected');
  assert.equal(await page.locator('#left-close-folder').isDisabled(), true);
});

test('select all uses visible asset identities, preserving filtered-out selections', async () => {
  await openFolder('left', 'photos');
  const tiles = page.locator('#photo-grid-left .photo-tile');
  await tiles.nth(0).locator('.pick-check').check();
  await tiles.nth(1).locator('.pick-check').check();
  assert.equal(await selected('left'), '2 selected');
  await page.locator('#library-filters > summary').click();
  await page.locator('#date-from').fill('2025-01-01');
  assert.equal(await tiles.count(), 2);
  assert.equal(await page.locator('#left-select-all').isDisabled(), false);
  await page.locator('#left-select-all').click();
  assert.equal(await selected('left'), '4 selected');
  await page.locator('#left-clear-picked').click();
  assert.equal(await selected('left'), '0 selected');
});

test('a delayed scan cannot reopen a closed panel or overwrite its replacement', async () => {
  await openFolder('left', 'photos');
  await page.locator('#browse-folders-right').click();
  await page.locator('#right-folder-list .folder-row-name').filter({hasText: 'slow'}).click();
  for (let i = 0; !releaseScan && i < 100; i++) await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(releaseScan, 'The slow scan started');
  await page.locator('#right-close-folder').click();
  await page.locator('#browse-folders-right').click();
  await openFolder('right', 'camera');
  releaseScan();
  await page.waitForResponse((response) => response.url().includes('scan?path=%2Fslow'));
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#right-folder-name').innerText(), 'camera');
  assert.equal(await page.locator('#left-folder-name').innerText(), 'photos');
});

test('More actions stay scoped to their folder and close with Escape', async () => {
  await openPair();
  await page.locator('#right-select-all').click();
  await page.locator('#right-more-menu > summary').click();
  await page.locator('#right-compare-min-size').fill('4');
  await page.locator('#right-auto-compare').click();
  await page.waitForFunction(() => !document.getElementById('right-auto-compare').disabled);
  assert.equal(requests[0].source_directory, '/camera');
  assert.equal(requests[0].min_group_size, 4);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#right-more-menu').getAttribute('open'), null);
  await page.locator('#right-select-all').click();
  await page.locator('#right-more-menu > summary').click();
  await page.locator('#right-google-upload').click();
  await page.waitForFunction(() => document.getElementById('right-google-upload').textContent === 'Upload to Google Photos');
  assert.equal(requests[1].source_directory, '/camera');
  assert.deepEqual(requests[1].asset_ids, assets('/camera').map((asset) => asset.id));
});

test('organizing a panel names its folder and refreshes new comparison folders', async () => {
  await openPair();
  await page.locator('#left-select-all').click();
  await page.locator('#left-move-menu > summary').click();
  await page.locator('#left-new-group').click();
  await page.locator('#left-folder-list .folder-row-name').filter({hasText: 'compare_groups'}).waitFor();
  assert.equal(requests[0].source_directory, '/photos');
  assert.equal(requests[0].group_name, 'Best shots');
  assert.equal(await page.locator('#right-folder-name').innerText(), 'camera');
});

test('empty folders and expanded tools fit desktop and mobile widths', async () => {
  await openFolder('left', 'empty');
  await page.locator('#browse-folders-right').click();
  await openFolder('right', 'camera');
  assert.equal(await page.locator('#left-select-all').isDisabled(), true);
  assert.equal(await page.locator('.cull-folder-row').count(), 0);
  if (process.env.KIRA_UI_SCREENSHOTS) {
    await page.locator('#create-job-card').screenshot({path: path.join(process.env.KIRA_UI_SCREENSHOTS, 'compare-desktop.png'), style: '.topbar { visibility: hidden; }'});
  }
  for (const width of [1566, 1024, 768, 390]) {
    await page.setViewportSize({width, height: 1080});
    await page.locator('#library-filters').evaluate((node) => { node.open = true; });
    const overflows = await page.locator('#create-job-card').evaluate((node) => node.scrollWidth > node.clientWidth);
    assert.equal(overflows, false, `No horizontal overflow at ${width}px`);
    await page.locator('#right-more-menu > summary').click();
    const menu = await page.locator('#right-more-menu .pane-more-menu').boundingBox();
    assert.ok(menu.x >= 0 && menu.x + menu.width <= width, `More menu stays on screen at ${width}px`);
    const newFolder = await page.locator('#right-new-folder').boundingBox();
    const move = await page.locator('#right-move-menu > summary').boundingBox();
    const tags = await page.locator('#right-tags-menu > summary').boundingBox();
    assert.ok(Math.abs(newFolder.y - move.y) < 1 && Math.abs(move.y - tags.y) < 1, `Organize controls share the New folder row at ${width}px`);
    const arrowButton = await page.locator('#move-selected-left').boundingBox();
    const arrow = await page.locator('#move-selected-left svg').boundingBox();
    assert.ok(Math.abs(arrow.x + arrow.width / 2 - arrowButton.x - arrowButton.width / 2) < 1, 'Move arrow is horizontally centered');
    assert.ok(Math.abs(arrow.y + arrow.height / 2 - arrowButton.y - arrowButton.height / 2) < 1, 'Move arrow is vertically centered');
    await page.keyboard.press('Escape');
  }
  if (process.env.KIRA_UI_SCREENSHOTS) {
    await page.locator('#create-job-card').screenshot({path: path.join(process.env.KIRA_UI_SCREENSHOTS, 'compare-mobile.png'), style: '.topbar { visibility: hidden; }'});
  }
});


test('photo clicks open original-size images without changing checkbox selections', async () => {
  await openFolder('left', 'photos');
  const tiles = page.locator('#photo-grid-left .photo-tile');
  await tiles.nth(0).locator('.pick-check').check();
  await tiles.nth(1).locator('.pick-check').check();
  assert.equal(await selected('left'), '2 selected');
  await tiles.nth(2).locator('.photo-open').click();
  assert.equal(await page.locator('#lightbox-modal').isVisible(), true);
  assert.match(await page.locator('#lightbox-image').getAttribute('src'), /original=1/);
  assert.equal(await selected('left'), '2 selected');
  if (process.env.KIRA_UI_SCREENSHOTS) {
    await page.locator('#lightbox-modal').screenshot({path: path.join(process.env.KIRA_UI_SCREENSHOTS, 'photo-viewer.png')});
  }
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#lightbox-modal').isVisible(), false);
  await tiles.nth(3).locator('.pick-check').click({modifiers: ['Shift']});
  assert.equal(await selected('left'), '4 selected');
  await tiles.nth(1).locator('.pick-check').uncheck();
  assert.equal(await selected('left'), '3 selected');
  await tiles.nth(0).locator('.photo-open').focus();
  await page.keyboard.press('Enter');
  await page.locator('#close-lightbox').click();
  assert.equal(await selected('left'), '3 selected');
});

test('Tags applies and removes a tag using the same control, only in its panel', async () => {
  await openPair();
  await page.locator('#left-select-all').click();
  await page.locator('#right-select-all').click();
  await page.locator('#left-tags-menu > summary').click();
  await page.locator('#left-tag-input').fill('Favorite');
  await page.locator('#left-tag-input').press('Enter');
  await page.waitForFunction(() => document.querySelector('#left-tag-list [aria-checked="true"]'));
  assert.deepEqual(requests[0].asset_ids, assets('/photos').map((asset) => asset.id));
  assert.equal(await page.locator('#photo-grid-right .tile-tag').count(), 0);
  await page.locator('#left-tag-list [role="checkbox"]').click();
  await page.waitForFunction(() => document.querySelector('#left-tag-list').children.length === 0);
  assert.deepEqual(requests[1].remove_tags, ['Favorite']);
});

test('Ctrl, Cmd and Shift photo clicks select instead of opening the viewer', async () => {
  await openFolder('left', 'photos');
  const photos = page.locator('#photo-grid-left .photo-open');
  await photos.nth(0).click({modifiers: ['Control']});
  await photos.nth(1).click({modifiers: ['Meta']});
  assert.equal(await selected('left'), '2 selected');
  await photos.nth(3).click({modifiers: ['Shift']});
  assert.equal(await selected('left'), '4 selected');
  await photos.nth(2).click({modifiers: ['Control']});
  assert.equal(await selected('left'), '3 selected');
  assert.equal(await page.locator('#lightbox-modal').isVisible(), false);
  await page.locator('#left-clear-picked').click();
  await photos.nth(2).click({modifiers: ['Shift']});
  assert.equal(await selected('left'), '1 selected', 'Shift without an anchor selects the clicked photo');
  await photos.nth(0).click();
  assert.equal(await page.locator('#lightbox-modal').isVisible(), true);
  assert.equal(await selected('left'), '1 selected');
});

test('Select mode toggles ordinary photo clicks independently in each panel', async () => {
  await openPair();
  const leftPhotos = page.locator('#photo-grid-left .photo-open');
  await page.locator('#left-select-mode').click();
  assert.equal(await page.locator('#left-select-mode').getAttribute('aria-pressed'), 'true');
  assert.equal(await page.locator('#right-select-mode').getAttribute('aria-pressed'), 'false');
  await leftPhotos.nth(0).click();
  await leftPhotos.nth(1).click();
  await leftPhotos.nth(0).click();
  assert.equal(await selected('left'), '1 selected');
  assert.equal(await page.locator('#lightbox-modal').isVisible(), false);
  await leftPhotos.nth(3).click({modifiers: ['Shift']});
  assert.equal(await selected('left'), '4 selected');
  await page.locator('#photo-grid-right .photo-open').first().click();
  assert.equal(await page.locator('#lightbox-modal').isVisible(), true);
  assert.equal(await selected('right'), '0 selected');
  await page.keyboard.press('Escape');
  await page.locator('#left-select-mode').click();
  assert.equal(await page.locator('#left-select-mode').getAttribute('aria-pressed'), 'false');
  assert.equal(await selected('left'), '4 selected');
  await leftPhotos.nth(0).click();
  assert.equal(await page.locator('#lightbox-modal').isVisible(), true);
  assert.equal(await selected('left'), '4 selected');
});
