const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const path = require('node:path');

function appWithFetch(fetch, overrides = {}) {
    let app;
    const context = {
        Vue: {
            ref: value => ({ value }),
            computed: fn => ({ get value() { return typeof fn === 'function' ? fn() : fn.get(); } }),
            onMounted() {}, onUnmounted() {},
            createApp(options) { app = options.setup(); return { mount() {} }; },
        },
        fetch, console, FormData, setTimeout: () => 0, clearTimeout() {},
        setInterval: () => 0, clearInterval() {},
        window: { location: { protocol: 'http:', host: 'localhost', search: '' } },
        document: {}, localStorage: { removeItem() {} },
        ...overrides,
    };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8'), context);
    return app;
}

test('changing URL while parsing cannot reuse old formats for the new source', async () => {
    let complete;
    const requests = [];
    const app = appWithFetch((url, options) => {
        requests.push({ url, body: JSON.parse(options.body) });
        return new Promise(resolve => { complete = resolve; });
    });
    app.url.value = 'https://example.com/first.m3u8';
    const pending = app.parseUrl();
    app.url.value = 'https://example.com/second.m3u8';
    complete({ ok: true, json: async () => ({ title: 'First stream', is_playlist: false, has_video: true, video_formats: [] }) });
    await pending;
    assert.equal(app.sourceChanged.value, true);
    await app.startDownload();
    assert.equal(requests.length, 1);
    assert.equal(app.toast.value.type, 'error');
    app.url.value = 'https://example.com/first.m3u8';
    assert.equal(app.sourceChanged.value, false);
});

test('invalid settings keep dialog open and display validation failure', async () => {
    const app = appWithFetch(async () => ({ ok: false, json: async () => ({ detail: [{ msg: 'out of range' }] }) }));
    app.showSettings.value = true;
    app.tempConfig.value.max_concurrent_downloads = 0;
    await app.saveSettings();
    assert.equal(app.showSettings.value, true);
    assert.equal(app.toast.value.type, 'error');
    assert.match(app.toast.value.message, /範圍/);
});

test('website authentication errors retain cookie guidance without logging out of the tool', async () => {
    const requests = [];
    const app = appWithFetch(async url => {
        requests.push(url);
        return { ok: false, status: 400, json: async () => ({ detail: '請先登入 YouTube 再試', login_required: true }) };
    });
    app.isAuthenticated.value = true;
    app.url.value = 'https://www.youtube.com/watch?v=example';
    await app.parseUrl();
    assert.equal(app.loginRequiredUrl.value, app.url.value);
    assert.equal(app.isAuthenticated.value, true);
    assert.deepEqual(requests, ['/api/parse']);
});

test('cookie help opens imports without starting a remote browser', async () => {
    const requests = [];
    const app = appWithFetch(async url => {
        requests.push(url);
        return { ok: true, json: async () => url === '/api/sites' ? { sites: [] } : { exists: false } };
    });
    app.showSettings.value = true;
    await app.openSites();
    assert.equal(app.showSites.value, true);
    assert.equal(app.showSettings.value, false);
    assert.deepEqual(requests, ['/api/sites', '/api/cookie-status']);
});

for (const outcome of ['valid', 'expired', 'server-error', 'network-error']) {
    test(`websocket policy close verifies HTTP login before treating it as ${outcome}`, async () => {
        const sockets = [];
        const timers = [];
        const user = { id: 'alice', username: 'alice', role: 'user' };
        const quota = { limit: 5, remaining: 5 };
        const requests = [];
        const app = appWithFetch(async path => {
            requests.push(path);
            if (path === '/api/auth/me') {
                if (outcome === 'network-error') throw new Error('offline');
                const status = outcome === 'expired' ? 401 : outcome === 'server-error' ? 502 : 200;
                return { ok: status === 200, status, json: async () => ({ user, quota }) };
            }
            return { ok: true, json: async () => path === '/api/sites' ? { sites: [] } : { user, quota } };
        }, {
            WebSocket: class { constructor() { sockets.push(this); } close() {} },
            setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
        });
        app.tokenInput.value = 'test-token';
        await app.login();
        app.tasks.value = [{ task_id: 'keep-task' }];
        await sockets[0].onclose({ code: 1008 });
        assert.ok(requests.includes('/api/auth/me'));
        assert.equal(app.isAuthenticated.value, outcome !== 'expired');
        if (outcome === 'expired') {
            assert.equal(app.tasks.value.length, 0);
            assert.match(app.authError.value, /登入已失效/);
            assert.equal(timers.length, 0);
        } else {
            assert.equal(app.tasks.value.length, 1);
            assert.equal(app.authError.value, '');
            assert.equal(timers[0].delay, 10000);
        }
    });
}

test('late session check cannot log out a newer login', async () => {
    const sockets = [];
    let completeCheck;
    const app = appWithFetch(async path => {
        if (path === '/api/auth/me') return new Promise(resolve => { completeCheck = resolve; });
        return { ok: true, json: async () => path === '/api/sites' ? { sites: [] } : { user: { id: 'alice' }, quota: {} } };
    }, { WebSocket: class { constructor() { sockets.push(this); } close() {} } });
    app.tokenInput.value = 'test-token';
    await app.login();
    const pending = sockets[0].onclose({ code: 1008 });
    app.tokenInput.value = 'test-token';
    await app.login();
    completeCheck({ status: 401 });
    await pending;
    assert.equal(app.isAuthenticated.value, true);
    assert.equal(app.authError.value, '');
});
