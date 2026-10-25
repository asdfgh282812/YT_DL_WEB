const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

createApp({
    setup() {
        const isAuthenticated = ref(false);
        const tokenInput = ref('');
        const usernameInput = ref('');
        const passwordInput = ref('');
        const quota = ref({ limit: null, used: 0, reserved: 0, remaining: null });
        const isAdmin = computed(() => currentUser.value?.role === 'admin');
        const completedCount = computed(() => tasks.value.filter(task => task.state === 'COMPLETED').length);
        const quotaText = computed(() => quota.value.limit === null ? '下載部數不限' : `今日可再下載 ${quota.value.remaining} / ${quota.value.limit} 部`);
        const showUserManagement = ref(false);
        const managedUsers = ref([]);
        const userPolicy = ref({ default_daily_limit: 5, timezone: 'Asia/Taipei' });
        const userBusy = ref(false);
        const newUser = ref({ username: '', name: '', password: '', role: 'user', inherit: true, daily_limit: 5 });
        const showSites = ref(false);
        const siteAccounts = ref([]);
        const authError = ref('');
        const authConfig = ref({ sso_enabled: false, token_enabled: true, sso_name: 'SSO' });
        const currentUser = ref(null);
        const loginSso = () => { window.location.href = '/auth/sso/login'; };
        const activeDownloads = ref(0);
        const outputFormat = ref('mp4');
        const audioFormat = ref('mp3');
        const referer = ref('');
        const userAgent = ref('');
        const parsedSource = ref(null);
        const sourceOptions = () => ({ referer: referer.value, user_agent: userAgent.value });
        const sourceChanged = computed(() => !!parsedSource.value && (
            url.value.trim() !== parsedSource.value.url || referer.value !== parsedSource.value.referer || userAgent.value !== parsedSource.value.user_agent
        ));
        const selectedOutput = (audio) => audio ? audioFormat.value : outputFormat.value;

        const url = ref('');
        const isParsing = ref(false);
        const loginRequiredUrl = ref('');
        const parseResult = ref(null);
        const selectedFormat = ref('audio_only');
        const batchDownloadFormat = ref('video');
        
        // Toast System
        const toast = ref({ show: false, message: '', type: 'success' });
        let toastTimeout = null;
        const showToast = (message, type = 'success') => {
            if (toastTimeout) clearTimeout(toastTimeout);
            toast.value = { show: true, message, type };
            toastTimeout = setTimeout(() => {
                toast.value.show = false;
            }, 3000);
        };
        // Confirm Modal System
        const confirmModal = ref({ show: false, message: '', onConfirm: null });
        const showConfirm = (message, callback) => {
            confirmModal.value = { show: true, message, onConfirm: callback };
        };
        const handleConfirm = async () => {
            if (confirmModal.value.onConfirm) {
                await confirmModal.value.onConfirm();
            }
            confirmModal.value.show = false;
        };
        const tasks = ref([]);
        const wsConnected = ref(false);
        let ws = null;
        let reconnectTimer = null;

        const showSettings = ref(false);
        const config = ref({ max_concurrent_downloads: 2, concurrent_fragments: 4, cookie_source: 'file', cookie_browser: 'firefox', cookie_profile: '' });
        const tempConfig = ref({ max_concurrent_downloads: 2, concurrent_fragments: 4, cookie_source: 'file', cookie_browser: 'firefox', cookie_profile: '' });
        const isUpdating = ref(false);
        const updateMsg = ref('');
        const updateSuccess = ref(false);
        const ytdlpVersion = ref('讀取中...');
        // ===== 新增：重啟倒數計時系統 =====
        const isRestarting = ref(false);
        const restartCountdown = ref(60);
        let countdownTimer = null;

        const forceReload = () => {
            window.location.reload();
        };

        const startRestartCountdown = () => {
            isRestarting.value = true;
            showSettings.value = false; // 關閉設定視窗
            restartCountdown.value = 60;
            countdownTimer = setInterval(() => {
                restartCountdown.value--;
                if (restartCountdown.value <= 0) {
                    clearInterval(countdownTimer);
                    forceReload();
                }
            }, 1000);
        };
        // ==================================
        const cookieStatus = ref(false);
        const systemStatus = ref({});
        const cookieFile = ref(null);
        const cookieMsg = ref('');
        const cookieMsgSuccess = ref(false);
        const isUploadingCookie = ref(false);
        const cookieFileInput = ref(null);

        const fetchCookieStatus = async () => {
            try {
                const res = await fetch('/api/cookie-status', { headers: getHeaders() });
                if (res.ok) {
                    const data = await res.json();
                    cookieStatus.value = data.exists;
                    systemStatus.value = data;
                }
            } catch (e) {
                console.error("無法取得 Cookie 狀態", e);
            }
        };

        const handleCookieFileChange = (e) => {
            if (e.target.files && e.target.files.length > 0) {
                cookieFile.value = e.target.files[0];
            } else {
                cookieFile.value = null;
            }
        };

        const uploadCookie = async () => {
            if (!cookieFile.value) return;

            isUploadingCookie.value = true;
            cookieMsg.value = '';

            const formData = new FormData();
            formData.append('file', cookieFile.value);

            try {
                const res = await fetch('/api/upload-cookie', {
                    method: 'POST',
                    headers: {
                        // Same-origin HttpOnly session authenticates the upload.
                    },
                    body: formData
                });

                const data = await res.json();

                if (!res.ok) {
                    throw new Error(data.detail || '上傳失敗');
                }

                cookieMsgSuccess.value = true;
                cookieMsg.value = data.message || 'Cookie 更新成功！';

                // Clear the file input
                if (cookieFileInput.value) {
                    cookieFileInput.value.value = '';
                }
                cookieFile.value = null;

                // Refresh status
                await fetchCookieStatus();
                await fetchSites();

            } catch (e) {
                cookieMsgSuccess.value = false;
                cookieMsg.value = e.message;
            } finally {
                isUploadingCookie.value = false;

                // Hide message after 3 seconds
                setTimeout(() => {
                    cookieMsg.value = '';
                }, 3000);
            }
        };

        const fetchVersion = async () => {
            try {
                const res = await fetch('/api/version', { headers: getHeaders() });
                if (res.ok) {
                    const data = await res.json();
                    ytdlpVersion.value = data.version;
                }
            } catch (e) {
                ytdlpVersion.value = '無法取得';
            }
        };

        const openSettings = () => {
            showSettings.value = true;
            tempConfig.value = { ...config.value };
            fetchVersion();
            fetchCookieStatus();
        };

        onMounted(async () => {
            document.addEventListener('click', closeDropdowns);
            const params = new URLSearchParams(window.location.search);
            if (params.has('auth_error')) {
                const errors = { access_denied: '此 SSO 帳號沒有使用權限', sso_failed: 'SSO 驗證失敗，請重新登入', sso_unavailable: '無法連線到 SSO，請確認服務與設定' };
                authError.value = errors[params.get('auth_error')] || 'SSO 登入失敗';
                history.replaceState({}, '', window.location.pathname);
            }
            try {
                const settingsResponse = await fetch('/api/auth/config');
                if (!settingsResponse.ok) throw new Error('無法取得登入設定');
                authConfig.value = await settingsResponse.json();
                const response = await fetch('/api/auth/me');
                if (response.ok) {
                    const identity = await response.json();
                    currentUser.value = identity.user;
                    quota.value = identity.quota;
                    fetchSites();
                    isAuthenticated.value = true;
                    connectWebSocket();
                } else {
                    // One-time migration from the old localStorage token.
                    const savedToken = localStorage.getItem('auth_token');
                    if (savedToken && authConfig.value.token_enabled) {
                        tokenInput.value = savedToken;
                        await login();
                    }
                }
                localStorage.removeItem('auth_token');
            } catch (error) { authError.value = error.message; }
        });

        onUnmounted(() => {
            document.removeEventListener('click', closeDropdowns);
            clearTimeout(reconnectTimer);
            clearInterval(countdownTimer);
            if (ws) ws.close();
        });

        const reversedTasks = computed(() => {
            return [...tasks.value].reverse();
        });

        const getHeaders = () => {
            return {
                'Content-Type': 'application/json'
            };
        };

        const login = async (method = 'token') => {
            const passwordLogin = method === 'password';
            if (passwordLogin ? !usernameInput.value || !passwordInput.value : !tokenInput.value) { authError.value = passwordLogin ? '請輸入帳號與密碼' : '請輸入 Token'; return; }
            try {
                const response = await fetch('/api/auth/login', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(passwordLogin ? { username: usernameInput.value, password: passwordInput.value } : { token: tokenInput.value })
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || '登入失敗');
                tokenInput.value = passwordInput.value = '';
                currentUser.value = data.user;
                quota.value = data.quota;
                fetchSites();
                localStorage.removeItem('auth_token');
                authError.value = '';
                isAuthenticated.value = true;
                connectWebSocket();
            } catch (error) { authError.value = error.message; }
        };

        const logout = async () => {
            try {
                const response = await fetch('/api/auth/logout', { method: 'POST' });
                if (!response.ok) throw new Error('登出失敗，請重試');
            } catch (error) { showToast(error.message, 'error'); return; }
            localStorage.removeItem('auth_token');
            isAuthenticated.value = false;
            tasks.value = [];
            showSites.value = showSettings.value = showUserManagement.value = false;
            siteAccounts.value = [];
            currentUser.value = null;
            parseResult.value = null;
            loginRequiredUrl.value = '';
            tokenInput.value = '';
            clearTimeout(reconnectTimer);
            if (ws) { const old = ws; ws = null; old.close(); }
        };

        const connectWebSocket = () => {
            clearTimeout(reconnectTimer);
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            if (ws) ws.close();
            const socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
            ws = socket;
            socket.onopen = () => {
                if (ws !== socket) return;
                socket.send(JSON.stringify({}));
            };
            socket.onmessage = (event) => {
                if (ws !== socket) return;
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'state_update') {
                        wsConnected.value = true;
                        tasks.value = data.data.tasks;
                        currentUser.value = data.data.user;
                        quota.value = data.data.quota;
                        if (!isAdmin.value) showSettings.value = showUserManagement.value = false;
                        config.value = data.data.config;
                        activeDownloads.value = data.data.active_downloads || 0;
                        if (!showSettings.value) tempConfig.value = { ...config.value };
                    }
                } catch (error) { console.error('無法讀取任務狀態', error); }
            };
            socket.onclose = async (event) => {
                if (ws !== socket) return;
                wsConnected.value = false;
                if (!isAuthenticated.value) return;
                if (event.code === 1008) {
                    try {
                        const response = await fetch('/api/auth/me', { cache: 'no-store' });
                        if (ws !== socket || !isAuthenticated.value) return;
                        if (response.status === 401) {
                            authError.value = '登入已失效，請重新登入';
                            isAuthenticated.value = false;
                            tasks.value = [];
                            siteAccounts.value = [];
                            currentUser.value = null;
                            parseResult.value = null;
                            showSites.value = showSettings.value = showUserManagement.value = false;
                            ws = null;
                            return;
                        }
                        if (response.ok) {
                            const identity = await response.json();
                            if (ws !== socket || !isAuthenticated.value) return;
                            currentUser.value = identity.user;
                            quota.value = identity.quota;
                        }
                    } catch {
                        // Network or proxy failures do not establish that a session expired.
                    }
                }
                if (ws === socket && isAuthenticated.value) {
                    reconnectTimer = setTimeout(connectWebSocket, event.code === 1008 ? 10000 : 3000);
                }
            };
            socket.onerror = () => socket.close();
        };

        const parseUrl = async () => {
            if (!url.value || isParsing.value) return;
            const requestedSource = { url: url.value.trim(), ...sourceOptions() };
            isParsing.value = true;
            parseResult.value = null;
            loginRequiredUrl.value = '';
            selectedFormat.value = 'best';
            parsedSource.value = null;

            try {
                const res = await fetch('/api/parse', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(requestedSource)
                });

                if (!res.ok) {
                    if (res.status === 401) {
                        const expired = new Error('登入逾時，請重新登入');
                        expired.authExpired = true;
                        throw expired;
                    }
                    const err = await res.json();
                    if (err.login_required) loginRequiredUrl.value = requestedSource.url;
                    throw new Error(apiError(err, '解析失敗'));
                }

                parseResult.value = await res.json();
                parsedSource.value = requestedSource;

                if (parseResult.value.is_playlist && parseResult.value.entries) {
                    parseResult.value.entries.forEach(entry => {
                        entry.selected = true;
                    });
                }

                if (!parseResult.value.is_playlist) {
                    selectedFormat.value = parseResult.value.has_video ? 'best' : 'audio_only';
                }
            } catch (e) {
                showToast(e.message, 'error');
                if (e.authExpired) { await logout(); authError.value = e.message; }
            } finally {
                isParsing.value = false;
            }
        };

        const batchDownloadMsg = ref('');
        const batchDownloadSuccess = ref(false);

        const selectedCount = computed(() => {
            if (!parseResult.value || !parseResult.value.is_playlist || !parseResult.value.entries) return 0;
            return parseResult.value.entries.filter(e => e.selected).length;
        });

        const selectAll = computed({
            get: () => {
                if (!parseResult.value || !parseResult.value.is_playlist || !parseResult.value.entries) return false;
                return parseResult.value.entries.length > 0 && parseResult.value.entries.every(e => e.selected);
            },
            set: (value) => {
                if (parseResult.value && parseResult.value.is_playlist && parseResult.value.entries) {
                    parseResult.value.entries.forEach(e => {
                        e.selected = value;
                    });
                }
            }
        });

        const startBatchDownload = async () => {
            if (!parseResult.value || !parseResult.value.is_playlist) return;
            if (sourceChanged.value) { showToast('來源已變更，請重新解析', 'error'); return; }
            
            const selectedItems = parseResult.value.entries.filter(e => e.selected);
            if (selectedItems.length === 0) return;

            try {
                const res = await fetch('/api/download/batch', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({
                        items: selectedItems.map(e => ({ url: e.url, title: e.title })),
                        is_audio_only: batchDownloadFormat.value === 'audio',
                        output_format: selectedOutput(batchDownloadFormat.value === 'audio'),
                        ...sourceOptions()
                    })
                });

                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(apiError(err, '批次加入下載失敗'));
                }
                
                const data = await res.json();
                url.value = '';
                parseResult.value = null;
                
                // 統一使用新的精美 Toast 提示
                showToast(`成功加入 ${data.added_count} 筆下載任務`, 'success');
            } catch (e) {
                // 統一使用新的精美 Toast 提示
                showToast(e.message || '批次加入下載失敗', 'error');
            }
        };

        const startDownload = async () => {
            if (!parseResult.value) return;
            if (sourceChanged.value) { showToast('來源已變更，請重新解析', 'error'); return; }
            const isAudio = selectedFormat.value === 'audio_only';
            const fmtId = isAudio ? '' : selectedFormat.value;

            try {
                const res = await fetch('/api/download', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({
                        url: parsedSource.value.url,
                        format_id: fmtId,
                        is_audio_only: isAudio,
                        title: parseResult.value.title,
                        output_format: selectedOutput(isAudio),
                        ...sourceOptions()
                    })
                });

                if (!res.ok) {
                    const data = await res.json();
                    throw new Error(typeof data.detail === 'string' ? data.detail : '加入下載失敗，請檢查來源資訊');
                }
                
                url.value = '';
                parseResult.value = null;
                showToast('已加入下載佇列', 'success');

            } catch (e) {
                showToast(e.message, 'error');
            }
        };

        const downloadFile = (task) => {
            const downloadUrl = `/api/download/${task.task_id}`;
            const a = document.createElement('a');
            a.href = downloadUrl;
            // The actual filename is set by the Content-Disposition header in the backend,
            // but setting download attribute helps the browser know it's a download action.
            a.download = ''; 
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        };

        const deleteTask = (taskId) => {
            showConfirm('確定要刪除這個任務與檔案嗎？此動作無法復原。', async () => {
                try {
                    const res = await fetch(`/api/task/${taskId}`, {
                        method: 'DELETE',
                        headers: getHeaders()
                    });
                    if (!res.ok) { const data = await res.json(); throw new Error(data.detail || '刪除失敗'); }
                    showToast('任務已成功刪除', 'success');
                } catch (e) {
                    showToast('刪除失敗：' + e.message, 'error');
                }
            });
        };

        const saveSettings = async () => {
            try {
                const res = await fetch('/api/config', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(tempConfig.value)
                });
                const data = await res.json();
                if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '請檢查下載數與連線數範圍');
                config.value = data.config;
                await fetchCookieStatus();
                showSettings.value = false;
                showToast('設定已成功儲存', 'success'); // 統一通知
            } catch (e) {
                showToast(e.message || '儲存失敗', 'error');
            }
        };

        const testCookies = async () => {
            cookieMsg.value = '讀取中，請稍候…';
            try {
                const res = await fetch('/api/cookies/test', { method: 'POST', headers: getHeaders() });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || '無法讀取 cookies');
                cookieMsgSuccess.value = true;
                cookieMsg.value = data.message;
            } catch (error) { cookieMsgSuccess.value = false; cookieMsg.value = error.message; }
        };
        const deleteCookies = () => showConfirm('確定刪除已上傳的 cookies？', async () => {
            const res = await fetch('/api/cookies', { method: 'DELETE', headers: getHeaders() });
            if (!res.ok) { showToast('刪除 cookies 失敗', 'error'); return; }
            await fetchCookieStatus();
            showToast('已刪除上傳的 cookies');
        });
        const retryTask = async (task) => {
            try {
                const res = await fetch(`/api/task/${task.task_id}/retry`, { method: 'POST', headers: getHeaders() });
                if (!res.ok) { const data = await res.json(); throw new Error(data.detail || '重試失敗'); }
                showToast('已重新加入下載佇列');
            } catch (error) { showToast(error.message, 'error'); }
        };

        const updateYtDlp = async () => {
            isUpdating.value = true;
            updateMsg.value = '';
            try {
                const res = await fetch('/api/update-ytdlp', {
                    method: 'POST',
                    headers: getHeaders()
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || '更新失敗');
                
                updateSuccess.value = true;
                // ===== 修改這裡：判斷是否需要倒數重啟 =====
                if (data.require_restart) {
                    showToast(data.new_version, 'success');
                    startRestartCountdown(); // 呼叫倒數計時函式
                } else {
                    ytdlpVersion.value = data.new_version;
                    showToast(data.new_version, 'success');
                }
                // ===========================================
            } catch (e) {
                updateSuccess.value = false;
                showToast('更新失敗: ' + e.message, 'error');
            } finally {
                isUpdating.value = false;
                setTimeout(() => {
                    updateMsg.value = '';
                }, 3000);
            }
        };

        const apiError = (data, fallback = '操作失敗') => {
            if (typeof data.detail === 'string') return data.detail;
            if (Array.isArray(data.detail)) return data.detail.map(item => (item.msg || '').replace(/^Value error, /, '')).join('；');
            return fallback;
        };
        const api = async (path, options = {}) => {
            const response = await fetch(path, { headers: getHeaders(), ...options });
            const data = await response.json();
            if (!response.ok) throw new Error(apiError(data));
            return data;
        };
        const fetchSites = async () => {
            try { siteAccounts.value = (await api('/api/sites')).sites; }
            catch (error) { console.error('無法讀取 cookies 狀態'); }
        };
        const openSites = async () => {
            showSettings.value = false;
            showSites.value = true;
            cookieMsg.value = '';
            cookieFile.value = null;
            await fetchSites();
            await fetchCookieStatus();
        };
        const forgetSite = (site) => showConfirm(`移除 ${site.name} 的 cookies？`, async () => {
            try {
                await api(`/api/sites/${encodeURIComponent(site.site_key)}`, { method: 'DELETE' });
                await fetchSites();
                await fetchCookieStatus();
                showToast('已移除 cookies');
            } catch (error) { showToast(error.message, 'error'); }
        });
        const openUsers = async () => {
            showUserManagement.value = true;
            userBusy.value = true;
            try {
                const data = await api('/api/admin/users');
                userPolicy.value = data.policy;
                managedUsers.value = data.users.map(user => ({ ...user, inherit: user.daily_limit === null, limitInput: user.daily_limit ?? data.policy.default_daily_limit, newPassword: '' }));
            } catch (error) { showToast(error.message, 'error'); }
            finally { userBusy.value = false; }
        };
        const savePolicy = async () => {
            userBusy.value = true;
            try {
                await api('/api/admin/policy', { method: 'PUT', body: JSON.stringify({ default_daily_limit: userPolicy.value.default_daily_limit }) });
                await openUsers();
                showToast('已更新一般使用者的預設每日上限');
            } catch (error) { showToast(error.message, 'error'); }
            finally { userBusy.value = false; }
        };
        const createUser = async () => {
            userBusy.value = true;
            try {
                const draft = newUser.value;
                await api('/api/admin/users', { method: 'POST', body: JSON.stringify({ username: draft.username, name: draft.name, password: draft.password, role: draft.role, daily_limit: draft.inherit ? null : draft.daily_limit }) });
                newUser.value = { username: '', name: '', password: '', role: 'user', inherit: true, daily_limit: userPolicy.value.default_daily_limit };
                await openUsers();
                showToast('已新增使用者');
            } catch (error) { showToast(error.message, 'error'); }
            finally { userBusy.value = false; }
        };
        const saveUser = async (user) => {
            userBusy.value = true;
            try {
                const changes = { name: user.name, role: user.role, disabled: user.disabled, daily_limit: user.inherit ? null : user.limitInput };
                if (user.newPassword) changes.password = user.newPassword;
                const result = await api(`/api/admin/users/${user.id}`, { method: 'PATCH', body: JSON.stringify(changes) });
                user.newPassword = '';
                if (result.reauth_required) {
                    showUserManagement.value = isAuthenticated.value = false;
                    authError.value = '帳號設定已更新，請重新登入';
                    return;
                }
                if (user.id === currentUser.value?.id && result.user.role !== 'admin') {
                    currentUser.value = result.user;
                    showUserManagement.value = false;
                    showToast('已儲存使用者設定');
                    return;
                }
                await openUsers();
                showToast('已儲存使用者設定');
            } catch (error) { showToast(error.message, 'error'); }
            finally { userBusy.value = false; }
        };
        const downloadAll = () => {
            if (!completedCount.value) return;
            const link = document.createElement('a');
            link.href = '/api/downloads/archive';
            link.download = 'downloads.zip';
            document.body.appendChild(link);
            link.click();
            link.remove();
            showToast(`正在打包 ${completedCount.value} 個完成項目，下載進度請查看瀏覽器`);
        };
        const getStatusText = (state) => {
            const map = {
                'QUEUED': '排隊中',
                'DOWNLOADING': '下載中',
                'PROCESSING': '合併/處理中',
                'COMPLETED': '已完成',
                'ERROR': '發生錯誤'
            };
            return map[state] || state;
        };

        const getProgressBarClass = (state) => {
            switch(state) {
                case 'QUEUED': return 'bg-slate-500';
                case 'DOWNLOADING': return 'bg-gradient-to-r from-blue-500 to-indigo-500 progress-bar-striped animate-stripes';
                case 'PROCESSING': return 'bg-gradient-to-r from-amber-500 to-orange-500 progress-bar-striped animate-stripes';
                case 'COMPLETED': return 'bg-gradient-to-r from-emerald-400 to-teal-500 shadow-[0_0_10px_rgba(52,211,153,0.5)]';
                case 'ERROR': return 'bg-gradient-to-r from-rose-500 to-red-600';
                default: return 'bg-slate-500';
            }
        };
        // ==========================================
        // 補上 AI 遺漏的 Dropdown 控制邏輯
        // ==========================================
        const isBatchDropdownOpen = ref(false);
        const isFormatDropdownOpen = ref(false);

        const closeDropdowns = () => {
            isBatchDropdownOpen.value = false;
            isFormatDropdownOpen.value = false;
        };

        const toggleBatchDropdown = (e) => {
            if (e) e.stopPropagation();
            isBatchDropdownOpen.value = !isBatchDropdownOpen.value;
            isFormatDropdownOpen.value = false; // 開啟這個就關閉另一個
        };

        const toggleFormatDropdown = (e) => {
            if (e) e.stopPropagation();
            isFormatDropdownOpen.value = !isFormatDropdownOpen.value;
            isBatchDropdownOpen.value = false;
        };

        const selectBatchFormat = (format) => {
            batchDownloadFormat.value = format;
            isBatchDropdownOpen.value = false;
        };

        const selectFormat = (formatId) => {
            selectedFormat.value = formatId;
            isFormatDropdownOpen.value = false;
        };

        const getFormatText = computed(() => {
            if (selectedFormat.value === 'audio_only') return `🎵 純音訊 (${audioFormat.value.toUpperCase()})`;
            if (selectedFormat.value === 'best') return '🎬 自動最佳畫質';
            if (!parseResult.value || !parseResult.value.video_formats) return '選擇格式';
            const fmt = parseResult.value.video_formats.find(f => f.id === selectedFormat.value);
            return fmt ? `🎬 ${fmt.resolution} (${fmt.ext})` : '選擇格式';
        });
        // ==========================================
        return {
            isAuthenticated, tokenInput, usernameInput, passwordInput, authError, login, logout, authConfig, currentUser, loginSso,
            quota, quotaText, isAdmin, completedCount, downloadAll,
            showUserManagement, managedUsers, userPolicy, newUser, userBusy, openUsers, savePolicy, createUser, saveUser,
            showSites, siteAccounts, openSites, forgetSite,
            activeDownloads, config, outputFormat, audioFormat, referer, userAgent, sourceChanged, systemStatus, testCookies, deleteCookies, retryTask,
            url, isParsing, loginRequiredUrl, parseUrl, parseResult, selectedFormat, startDownload,
            tasks, reversedTasks, wsConnected, deleteTask, downloadFile,
            showSettings, tempConfig, saveSettings,
            isUpdating, updateMsg, updateSuccess, updateYtDlp,
            getStatusText, getProgressBarClass,
            ytdlpVersion, openSettings,
            cookieStatus, cookieFile, cookieMsg, cookieMsgSuccess, isUploadingCookie, cookieFileInput,
            handleCookieFileChange, uploadCookie,
            batchDownloadMsg, batchDownloadSuccess, startBatchDownload,
            batchDownloadFormat, selectedCount, selectAll,
            toast, showToast,
            isBatchDropdownOpen, isFormatDropdownOpen, toggleBatchDropdown, toggleFormatDropdown,
            selectBatchFormat, selectFormat, getFormatText, closeDropdowns,
            confirmModal, handleConfirm,isRestarting, restartCountdown, forceReload
        };
    }
}).mount('#app');
