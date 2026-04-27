const { createApp, ref, computed, onMounted, onUnmounted } = Vue;

createApp({
    setup() {
        const isAuthenticated = ref(false);
        const tokenInput = ref('');
        const authToken = ref('');
        const authError = ref('');

        const url = ref('');
        const isParsing = ref(false);
        const parseResult = ref(null);
        const selectedFormat = ref('audio_only');
        const batchDownloadFormat = ref('video');

        const tasks = ref([]);
        const wsConnected = ref(false);
        let ws = null;

        const showSettings = ref(false);
        const config = ref({ max_concurrent_downloads: 2 });
        const tempConfig = ref({ max_concurrent_downloads: 2 });
        const isUpdating = ref(false);
        const updateMsg = ref('');
        const updateSuccess = ref(false);
        const ytdlpVersion = ref('讀取中...');

        const cookieStatus = ref(false);
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
                        'Authorization': `Bearer ${authToken.value}`
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

        // Check local storage for token
        onMounted(() => {
            const savedToken = localStorage.getItem('auth_token');
            if (savedToken) {
                tokenInput.value = savedToken;
                login();
            }
        });

        onUnmounted(() => {
            if (ws) ws.close();
        });

        const reversedTasks = computed(() => {
            return [...tasks.value].reverse();
        });

        const getHeaders = () => {
            return {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${authToken.value}`
            };
        };

        const login = () => {
            if (!tokenInput.value) {
                authError.value = '請輸入 Token';
                return;
            }
            authToken.value = tokenInput.value;
            connectWebSocket();
        };

        const logout = () => {
            localStorage.removeItem('auth_token');
            isAuthenticated.value = false;
            authToken.value = '';
            tokenInput.value = '';
            if (ws) ws.close();
        };

        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const wsUrl = `${protocol}//${window.location.host}/ws`;
            
            ws = new WebSocket(wsUrl);

            ws.onopen = () => {
                ws.send(JSON.stringify({ token: authToken.value }));
                wsConnected.value = true;
                isAuthenticated.value = true;
                authError.value = '';
                localStorage.setItem('auth_token', authToken.value);
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'state_update') {
                        tasks.value = data.data.tasks;
                        config.value = data.data.config;
                        if(!showSettings.value) {
                            tempConfig.value = { ...config.value };
                        }
                    }
                } catch (e) {
                    console.error("WS parse error", e);
                }
            };

            ws.onclose = (e) => {
                wsConnected.value = false;
                if (e.code === 1008) {
                    authError.value = 'Token 無效或認證失敗';
                    isAuthenticated.value = false;
                    localStorage.removeItem('auth_token');
                } else {
                    // Try reconnect if it's an unexpected close and we think we are authenticated
                    if (isAuthenticated.value) {
                        setTimeout(connectWebSocket, 3000);
                    }
                }
            };

            ws.onerror = (e) => {
                console.error("WebSocket error:", e);
                ws.close();
            };
        };

        const parseUrl = async () => {
            if (!url.value) return;
            isParsing.value = true;
            parseResult.value = null;
            selectedFormat.value = 'audio_only';

            try {
                const res = await fetch('/api/parse', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({ url: url.value })
                });

                if (!res.ok) {
                    if (res.status === 401) throw new Error('登入逾時，請重新登入');
                    const err = await res.json();
                    throw new Error(err.detail || '解析失敗');
                }

                parseResult.value = await res.json();

                if (parseResult.value.is_playlist && parseResult.value.entries) {
                    parseResult.value.entries.forEach(entry => {
                        entry.selected = true;
                    });
                }

                if (!parseResult.value.is_playlist && parseResult.value.video_formats && parseResult.value.video_formats.length > 0) {
                    selectedFormat.value = parseResult.value.video_formats[0].id; // Default to best video
                }
            } catch (e) {
                alert(e.message);
                if (e.message.includes('登入')) logout();
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
            
            const selectedItems = parseResult.value.entries.filter(e => e.selected);
            if (selectedItems.length === 0) return;

            try {
                const res = await fetch('/api/download/batch', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({
                        items: selectedItems.map(e => ({ url: e.url, title: e.title })),
                        is_audio_only: batchDownloadFormat.value === 'audio'
                    })
                });

                if (!res.ok) {
                    const err = await res.json();
                    throw new Error(err.detail || '批次加入下載失敗');
                }
                
                const data = await res.json();
                url.value = '';
                parseResult.value = null;
                
                batchDownloadMsg.value = `成功加入 ${data.added_count} 筆下載任務`;
                batchDownloadSuccess.value = true;
                setTimeout(() => {
                    batchDownloadMsg.value = '';
                }, 3000);
            } catch (e) {
                batchDownloadMsg.value = e.message;
                batchDownloadSuccess.value = false;
                setTimeout(() => {
                    batchDownloadMsg.value = '';
                }, 3000);
            }
        };

        const startDownload = async () => {
            if (!parseResult.value) return;
            
            const isAudio = selectedFormat.value === 'audio_only';
            const fmtId = isAudio ? '' : selectedFormat.value;

            try {
                const res = await fetch('/api/download', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify({
                        url: url.value,
                        format_id: fmtId,
                        is_audio_only: isAudio,
                        title: parseResult.value.title
                    })
                });

                if (!res.ok) throw new Error('加入下載失敗');
                
                url.value = '';
                parseResult.value = null;

            } catch (e) {
                alert(e.message);
            }
        };

        const downloadFile = (task) => {
            const downloadUrl = `/api/download/${task.task_id}?token=${encodeURIComponent(authToken.value)}`;
            const a = document.createElement('a');
            a.href = downloadUrl;
            // The actual filename is set by the Content-Disposition header in the backend,
            // but setting download attribute helps the browser know it's a download action.
            a.download = ''; 
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        };

        const deleteTask = async (taskId) => {
            if (!confirm('確定要刪除這個任務與檔案嗎？')) return;
            try {
                await fetch(`/api/task/${taskId}`, {
                    method: 'DELETE',
                    headers: getHeaders()
                });
            } catch (e) {
                console.error(e);
            }
        };

        const saveSettings = async () => {
            try {
                await fetch('/api/config', {
                    method: 'POST',
                    headers: getHeaders(),
                    body: JSON.stringify(tempConfig.value)
                });
                showSettings.value = false;
            } catch (e) {
                alert('儲存設定失敗');
            }
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
                if (data.new_version) {
                    ytdlpVersion.value = data.new_version;
                    updateMsg.value = `更新成功！已升級至版本: ${data.new_version}`;
                } else {
                    updateMsg.value = '更新成功！';
                }
            } catch (e) {
                updateSuccess.value = false;
                updateMsg.value = '更新失敗: ' + e.message;
            } finally {
                isUpdating.value = false;
                setTimeout(() => {
                    updateMsg.value = '';
                }, 3000);
            }
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

        return {
            isAuthenticated, tokenInput, authError, login, logout,
            url, isParsing, parseUrl, parseResult, selectedFormat, startDownload,
            tasks, reversedTasks, wsConnected, deleteTask, downloadFile,
            showSettings, tempConfig, saveSettings,
            isUpdating, updateMsg, updateSuccess, updateYtDlp,
            getStatusText, getProgressBarClass,
            ytdlpVersion, openSettings,
            cookieStatus, cookieFile, cookieMsg, cookieMsgSuccess, isUploadingCookie, cookieFileInput,
            handleCookieFileChange, uploadCookie,
            batchDownloadMsg, batchDownloadSuccess, startBatchDownload,
            batchDownloadFormat, selectedCount, selectAll
        };
    }
}).mount('#app');