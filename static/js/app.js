// Global state to store overclock configurations
let activeOverclocks = {};
let csrfToken = '';
let activeHardwareTab = 'gpus';
let activeDashTab = 'gpus';
let lastStatsData = null;
let lastHugepagesEnabled = false;

// ---- Cluster / remote rig state ----
let currentRigId = 'self';
let currentClusterId = null;     // selected cluster in the header dropdown (null = all rigs)
let clusterData = null;          // last /api/cluster/rigs payload
let activeView = 'cluster';      // cluster | accesses | dashboard
let editingRigId = null;         // rig being edited in rigModal
let editingAccess = null;        // {rigId, accessId} being edited in accessModal
let editingJumpId = null;        // jump server being edited in jumpModal
let clusterPollTimer = null;

// Build the API path for the currently managed rig:
// local rig -> direct /api/... ; remote rig -> /api/remote/<rigId>/api/...
function apiPath(path) {
    if (currentRigId === 'self' || !currentRigId) {
        return path;
    }
    return '/api/remote/' + encodeURIComponent(currentRigId) + '/' + path.replace(/^\//, '');
}

// Route API calls through the SSH proxy when a remote rig is selected
function apiFetch(path, options = {}) {
    return fetch(apiPath(path), options);
}

// Self-healing CSRF: if any POST fails with 403 (stale token after
// server restart or page reload), refresh the token and retry once
const _originalFetch = window.fetch;
window.fetch = async function(url, options = {}) {
    let response = await _originalFetch(url, options);
    const isApiPost = typeof url === 'string' && url.startsWith('/api/') && (options.method || 'GET').toUpperCase() === 'POST';
    if (response.status === 403 && isApiPost) {
        try {
            const statsRes = await _originalFetch('/api/stats');
            if (statsRes.ok) {
                const statsData = await statsRes.json();
                if (statsData.csrf_token) {
                    csrfToken = statsData.csrf_token;
                    options.headers = Object.assign({}, options.headers, { 'X-CSRF-Token': csrfToken });
                    response = await _originalFetch(url, options);
                }
            }
        } catch (e) {
            console.error('CSRF token refresh failed:', e);
        }
    }
    return response;
};

// Toggle between discrete GPU cards and CPU integrated graphics tab
function switchHardwareTab(showGpus) {
    const gpuContainer = document.getElementById('gpuContainer');
    const igpuContainer = document.getElementById('igpuContainer');
    const gpusBtn = document.getElementById('showGpusBtn');
    const igpusBtn = document.getElementById('showIgpusBtn');
    
    activeHardwareTab = showGpus ? 'gpus' : 'igpus';
    
    gpuContainer.classList.toggle('d-none', !showGpus);
    igpuContainer.classList.toggle('d-none', showGpus);
    // CPU Mining card belongs to the iGPU sub-view, OC guide to the GPU Cards sub-view
    const cpuCard = document.getElementById('cpuCardContainer');
    const ocGuide = document.getElementById('ocGuideContainer');
    if (cpuCard) cpuCard.classList.toggle('d-none', !(!showGpus && activeDashTab === 'gpus'));
    if (ocGuide) ocGuide.classList.toggle('d-none', !(showGpus && activeDashTab === 'gpus'));
    
    gpusBtn.classList.toggle('btn-primary', showGpus);
    gpusBtn.classList.toggle('btn-outline-primary', !showGpus);
    gpusBtn.classList.toggle('active', showGpus);
    igpusBtn.classList.toggle('btn-primary', !showGpus);
    igpusBtn.classList.toggle('btn-outline-primary', showGpus);
    igpusBtn.classList.toggle('active', !showGpus);
    
    if (lastStatsData) {
        updateHardwareStatBoxes(lastStatsData);
    }
}

// Stat boxes reflect the active hardware tab (GPUs vs CPU iGPU)
function updateHardwareStatBoxes(data) {
    const gpuCountEl = document.getElementById('statGpuCount');
    const speedEl = document.getElementById('statTotalHashrate');
    if (!gpuCountEl || !speedEl || !data) return;

    if (activeHardwareTab === 'igpus') {
        const igpuCount = (data.igpus || []).length;
        gpuCountEl.textContent = igpuCount + ' iGPU' + (igpuCount === 1 ? '' : 's');

        const cpuHash = (data.system.cpu && data.system.cpu.hashrate) || 0;
        speedEl.textContent = fmtSpeed(cpuHash / 1000.0);
    } else {
        const totalHashrate = data.gpus.reduce((sum, g) => sum + (g.hashrate || 0), 0);
        gpuCountEl.textContent = data.gpus.length + ' GPU';
        speedEl.innerHTML = fmtSpeedHtml(totalHashrate);
    }
}

// Dynamic hashrate formatting: input in MH/s, output in sensible units
function fmtSpeed(mh) {
    const v = Number(mh) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(2) + ' PH/s';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + ' TH/s';
    if (v >= 1e3) return (v / 1e3).toFixed(2) + ' GH/s';
    if (v > 0) return v.toFixed(2) + ' MH/s';
    return '0 MH/s';
}

function fmtSpeedHtml(mh) {
    return '<span class="text-primary-gradient fw-bold">' + fmtSpeed(mh) + '</span>';
}

document.addEventListener('DOMContentLoaded', function() {
    // 1. Theme Toggle Logic
    const htmlElement = document.documentElement;
    const themeToggleBtn = document.getElementById('themeToggleBtn');
    const themeToggleIcon = document.getElementById('themeToggleIcon');

    // Retrieve saved theme or default to dark
    const savedTheme = localStorage.getItem('theme') || 'dark';
    setTheme(savedTheme);

    themeToggleBtn.addEventListener('click', () => {
        const currentTheme = htmlElement.getAttribute('data-bs-theme');
        const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
        setTheme(newTheme);
    });

    function setTheme(theme) {
        htmlElement.setAttribute('data-bs-theme', theme);
        localStorage.setItem('theme', theme);
        
        if (theme === 'dark') {
            themeToggleIcon.className = 'bi bi-moon-stars-fill';
            themeToggleBtn.classList.remove('btn-light');
            themeToggleBtn.classList.add('btn-dark');
        } else {
            themeToggleIcon.className = 'bi bi-sun-fill';
            themeToggleBtn.classList.remove('btn-dark');
            themeToggleBtn.classList.add('btn-light');
        }
    }

    // 2. Authentication Login Handler
    const loginForm = document.getElementById('loginForm');
    const loginOverlay = document.getElementById('loginOverlay');
    const loginError = document.getElementById('loginError');
    const revertBtn = document.getElementById('revertSettingsBtn');

    // Start clean: browsers (incl. Firefox session restore) may re-fill the
    // login field on reload — clear it now and whenever the page is re-shown
    const loginPasswordInput = document.getElementById('loginPassword');
    const wipeLoginField = () => { loginPasswordInput.value = ''; };
    wipeLoginField();
    window.addEventListener('pageshow', wipeLoginField);

    loginForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        const password = document.getElementById('loginPassword').value.trim();
        
        const submitBtn = loginForm.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Verifying...`;
        
        try {
            const response = await fetch('/api/login', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ password: password })
            });
            const data = await response.json();
            
            if (response.ok && data.success) {
                csrfToken = data.csrf_token;
                loginOverlay.classList.add('d-none');
                loginError.classList.add('d-none');
                // Wipe the field: a refresh must never restore a stale password
                document.getElementById('loginPassword').value = '';
                showToast("Authorized successfully!", true);
                fetchStats();
                loadClusterData();
                loadAccessList();
            } else {
                loginError.classList.remove('d-none');
                document.getElementById('loginPassword').value = '';
            }
        } catch (error) {
            console.error("Authentication failed:", error);
            showToast("Network error during authorization check.", false);
        } finally {
            submitBtn.disabled = false;
            submitBtn.innerHTML = "Authorize Session";
        }
    });

    // 3. Rollback Backup Configurations Handler
    revertBtn.addEventListener('click', async function() {
        if (!confirm("Are you sure you want to revert all GPU settings to the last saved stable backup? This will restore files and apply them to the system.")) {
            return;
        }
        
        revertBtn.disabled = true;
        const icon = revertBtn.querySelector('i');
        icon.className = 'bi bi-arrow-counterclockwise spin-animation';
        
        try {
            const response = await apiFetch('/api/revert', {
                method: 'POST',
                headers: {
                    'X-CSRF-Token': csrfToken
                }
            });
            const data = await response.json();
            
            if (response.ok && data.success) {
                showToast(data.message, true);
                fetchStats();
            } else {
                showToast(data.message || "Failed to restore backups.", false);
            }
        } catch (error) {
            console.error("Rollback failed:", error);
            showToast("Network error trying to restore backup.", false);
        } finally {
            revertBtn.disabled = false;
            icon.className = 'bi bi-arrow-counterclockwise';
        }
    });

    // Toggle Huge Pages Handler
    const toggleHpBtn = document.getElementById('toggleHugepagesBtn');
    toggleHpBtn.addEventListener('click', async function() {
        const currentStatus = document.getElementById('hugePagesStatus').textContent.trim();
        const enable = currentStatus !== "Enabled";
        
        toggleHpBtn.disabled = true;
        const icon = toggleHpBtn.querySelector('i');
        icon.className = 'bi bi-gear-fill spin-animation';
        
        try {
            const response = await apiFetch('/api/hugepages', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ enable: enable })
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                fetchStats();
            } else {
                showToast(data.message || "Failed to configure Huge Pages.", false);
            }
        } catch (error) {
            console.error("Huge Pages toggle failed:", error);
            showToast("Network error trying to toggle Huge Pages.", false);
        } finally {
            toggleHpBtn.disabled = false;
            icon.className = 'bi bi-gear-fill';
        }
    });

    // Miner Control Helper
    async function sendMinerControl(action, buttonId) {
        const btn = document.getElementById(buttonId);
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Sending...`;
        
        try {
            const response = await apiFetch('/api/miner/control', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ action: action })
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                fetchStats();
            } else {
                showToast(data.message || `Failed to ${action} miner.`, false);
            }
        } catch (error) {
            console.error(`Miner control error (${action}):`, error);
            showToast(`Network error attempting to ${action} miner.`, false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    }

    document.getElementById('minerStartBtn').addEventListener('click', () => sendMinerControl('start', 'minerStartBtn'));
    document.getElementById('minerStopBtn').addEventListener('click', () => {
        if (confirm("Are you sure you want to STOP mining operations on this rig?")) {
            sendMinerControl('stop', 'minerStopBtn');
        }
    });
    document.getElementById('minerRestartBtn').addEventListener('click', () => sendMinerControl('restart', 'minerRestartBtn'));

    // 4. Poll for GPU Stats and Rig Config
    fetchStats(); // Initial load
    loadFsheets(); // Flight sheets + wallets
    loadFans(); // Extra fan detection
    loadClusterData(); // Cluster rigs + jump library
    checkUpdate(); // Initial check for dashboard updates on GitHub
    const updateInterval = setInterval(checkUpdate, 600000); // Check updates every 10m
    setInterval(updateLastSyncDisplay, 1000); // Live Last Sync clock (independent of auto-refresh)
    setupAutoRefreshMenus(); // Per-section auto-refresh intervals (dropdowns)

    // Manual Refresh button
    const refreshBtn = document.getElementById('refreshStatsBtn');
    refreshBtn.addEventListener('click', () => {
        refreshBtn.disabled = true;
        const icon = refreshBtn.querySelector('i');
        icon.className = 'bi bi-arrow-clockwise spin-animation';
        
        fetchStats().finally(() => {
            setTimeout(() => {
                refreshBtn.disabled = false;
                icon.className = 'bi bi-arrow-clockwise';
            }, 800);
        });
    });

    // Hardware view switch: discrete GPUs vs CPU integrated graphics
    document.getElementById('showGpusBtn').addEventListener('click', () => switchHardwareTab(true));
    document.getElementById('showIgpusBtn').addEventListener('click', () => switchHardwareTab(false));

    // Dashboard section tabs (GPUs / Wallets / Flight Sheets)
    document.getElementById('dashTabGpusBtn').addEventListener('click', () => showDashTab('gpus'));
    document.getElementById('dashTabFansBtn').addEventListener('click', () => showDashTab('fans'));
    document.getElementById('dashTabWalletsBtn').addEventListener('click', () => showDashTab('wallets'));
    document.getElementById('dashTabFsheetsBtn').addEventListener('click', () => showDashTab('fsheets'));
    document.getElementById('dashTabPresetsBtn').addEventListener('click', () => showDashTab('presets'));
    document.getElementById('dashTabServicesBtn').addEventListener('click', () => showDashTab('services'));
    document.getElementById('dashTabStatsBtn').addEventListener('click', () => showDashTab('stats'));
    document.getElementById('gotoWalletsBtn').addEventListener('click', () => showDashTab('wallets'));

    // GPU Fans tab (1:1 copy of the HiveOS worker Autofan page)
    document.getElementById('afTableModeBtn').addEventListener('click', () => {
        document.getElementById('afTableModeView').classList.remove('d-none');
        document.getElementById('afAdvancedModeView').classList.add('d-none');
        document.getElementById('afTableModeBtn').classList.replace('btn-outline-primary', 'btn-primary');
        document.getElementById('afAdvancedModeBtn').classList.replace('btn-primary', 'btn-outline-primary');
        // both views share one state - re-render so unsaved edits stay visible
        renderAfTable();
    });
    document.getElementById('afAdvancedModeBtn').addEventListener('click', () => {
        document.getElementById('afAdvancedModeView').classList.remove('d-none');
        document.getElementById('afTableModeView').classList.add('d-none');
        document.getElementById('afAdvancedModeBtn').classList.replace('btn-outline-primary', 'btn-primary');
        document.getElementById('afTableModeBtn').classList.replace('btn-primary', 'btn-outline-primary');
        renderAfAdvanced();
    });
    document.getElementById('afEnabledSwitch').addEventListener('change', afSetEnabledVisibility);
    document.getElementById('afCriticalAction').addEventListener('change', function() {
        document.getElementById('afCriticalActionAdv').value = this.value;
    });
    document.getElementById('afCriticalActionAdv').addEventListener('change', function() {
        document.getElementById('afCriticalAction').value = this.value;
    });
    document.getElementById('afSaveBtn').addEventListener('click', async function() {
        const payload = afCollectPayload();
        if (!payload) return;
        const btn = this;
        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
        try {
            const response = await apiFetch('/api/autofan/save-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            showToast(data.message || (data.success ? 'AutoFan settings saved.' : 'Failed to save AutoFan settings.'), !!data.success);
            if (data.success) {
                await loadAutofan();
                fetchStats();
            }
        } catch (e) {
            showToast('Network error saving AutoFan settings.', false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });
    document.getElementById('afResetBtn').addEventListener('click', () => {
        // Reset the table to the default values (Auto mode, Static 80,
        // Min 30 / Max 100 / Target Core 60 / Target MEM 90 / Critical 70);
        // press Apply to apply them to the rig
        if (!window._af) return;
        window._af.items = (window._af.items || []).map(it => Object.assign({ index: it.index }, AF_DEFAULTS));
        afApplyState(window._af);
    });

    // CPU mining settings modal save handler
    document.getElementById('cpuSettingsSaveBtn').addEventListener('click', async function() {
        const enable = document.getElementById('cpuHugepagesSelect').value === 'enable';
        const btn = this;
        btn.disabled = true;
        const origHTML = btn.innerHTML;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Applying...`;
        
        try {
            const response = await apiFetch('/api/hugepages', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ enable: enable })
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                fetchStats();
                bootstrap.Modal.getInstance(document.getElementById('cpuSettingsModal')).hide();
            } else {
                showToast(data.message || "Failed to configure Huge Pages.", false);
            }
        } catch (error) {
            console.error("Huge Pages toggle failed:", error);
            showToast("Network error trying to toggle Huge Pages.", false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = origHTML;
        }
    });

    // 5. Form Submit Handlers for Overclocking
    document.getElementById('nvOcForm').addEventListener('submit', function(e) {
        e.preventDefault();
        submitOverclock(this, 'nvOcModal');
    });

    document.getElementById('amdOcForm').addEventListener('submit', function(e) {
        e.preventDefault();
        submitOverclock(this, 'amdOcModal');
    });

    document.getElementById('ocAllApplyBtn').addEventListener('click', function() {
        submitAllOverclock();
    });

    // Any manual edit marks the card dirty — auto-refresh stops overwriting fields
    document.querySelectorAll('#ocAllContainer input').forEach(el => {
        el.addEventListener('input', () => { window._ocAllDirty = true; });
        el.addEventListener('change', () => { window._ocAllDirty = true; });
    });

    // Apply update trigger
    const applyUpdateBtn = document.getElementById('applyUpdateBtn');
    applyUpdateBtn.addEventListener('click', async function() {
        const passwordInput = document.getElementById('updateVerificationPin');
        const password = passwordInput.value.trim();
        if (!password) {
            showToast("Please enter your access password to confirm the update.", false);
            return;
        }
        
        if (!confirm("Are you sure you want to pull the latest updates from GitHub and restart the local dashboard? Rigs configurations will reset to main branch head.")) {
            return;
        }
        
        const overlay = document.getElementById('updateOverlay');
        overlay.classList.remove('d-none');
        overlay.classList.add('d-flex');
        
        try {
            const response = await apiFetch('/api/update/pull', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ password: password })
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                setTimeout(() => {
                    window.location.reload();
                }, 6000);
            } else {
                overlay.classList.remove('d-flex');
                overlay.classList.add('d-none');
                showToast(data.message || "Failed to pull update.", false);
            }
        } catch (error) {
            console.error("Update pull failed:", error);
            overlay.classList.remove('d-flex');
            overlay.classList.add('d-none');
            showToast("Network error trying to pull update.", false);
        }
    });

    // Manual check for updates trigger
    const manualCheckBtn = document.getElementById('manualCheckUpdateBtn');
    manualCheckBtn.addEventListener('click', async () => {
        manualCheckBtn.disabled = true;
        const icon = manualCheckBtn.querySelector('i');
        icon.className = 'bi bi-arrow-repeat spin-animation';
        
        await checkUpdate(true);
        
        setTimeout(() => {
            manualCheckBtn.disabled = false;
            icon.className = 'bi bi-arrow-repeat';
        }, 1000);
    });

    // Reboot / Shutdown bindings (act on the rig selected in the header dropdown)
    document.getElementById('rigRebootBtn').addEventListener('click', async () => {
        if (confirm("Are you sure you want to REBOOT " + getRigName(currentRigId) + "? Mining operations will be suspended during reboot.")) {
            sendSystemPowerAction('/api/system/reboot', 'rigRebootBtn');
        }
    });
    
    document.getElementById('rigShutdownBtn').addEventListener('click', async () => {
        if (confirm("Are you sure you want to SHUTDOWN " + getRigName(currentRigId) + "? Power will be cut from the hardware.")) {
            sendSystemPowerAction('/api/system/shutdown', 'rigShutdownBtn');
        }
    });
    
    // Emergency Overclock Reset trigger
    document.getElementById('emergencyResetClocksBtn').addEventListener('click', async () => {
        if (!confirm("WARNING: This will instantly clear all GPU overclock values (clocks, fans, voltages, power limits) to factory stock configurations. Stabilize rig now?")) {
            return;
        }
        
        const btn = document.getElementById('emergencyResetClocksBtn');
        const origHTML = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Resetting...`;
        
        try {
            const res = await apiFetch('/api/overclock/reset', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                }
            });
            const data = await res.json();
            if (res.ok && data.success) {
                showToast(data.message, true);
                fetchStats();
            } else {
                showToast(data.message || "Failed to reset clocks.", false);
            }
        } catch (e) {
            console.error(e);
            showToast("Network error trying to reset clocks.", false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = origHTML;
        }
    });

    // Background Services control triggers
    document.querySelectorAll('.service-ctrl-btn').forEach(btn => {
        btn.addEventListener('click', async function() {
            const service = this.dataset.service;
            const action = this.dataset.action;
            
            if (!confirm(`Are you sure you want to ${action} the '${service}' service daemon?`)) {
                return;
            }
            
            this.disabled = true;
            const origHTML = this.innerHTML;
            this.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span>`;
            
            try {
                const res = await apiFetch('/api/services/control', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrfToken
                    },
                    body: JSON.stringify({ service, action })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    showToast(data.message, true);
                    if (service === 'hiveos-local') {
                        setTimeout(() => { window.location.reload(); }, 3500);
                    }
                } else {
                    showToast(data.message || "Failed to control service.", false);
                }
            } catch (e) {
                console.error(e);
                showToast("Network error communicating with service manager.", false);
            } finally {
                this.disabled = false;
                this.innerHTML = origHTML;
            }
        });
    });
    
    // Flight Sheets builder + Wallets tab (cloud-style)
    window._fsBuilderItems = [{}];
    loadCatalog();
    rebuildFsItemsContainer();
    document.getElementById('saveFsheetForm').addEventListener('submit', saveFsheetFromBuilder);
    document.getElementById('fsAddMinerBtn').addEventListener('click', () => builderAddRow({}));
    document.getElementById('fsAddSheetBtn').addEventListener('click', openFsBuilder);
    document.getElementById('fsResetBuilderBtn').addEventListener('click', closeFsBuilder);
    document.getElementById('fsMinerCfgApplyBtn').addEventListener('click', applyFsMinerCfgFromModal);
    document.getElementById('fsMinerCfgClearBtn').addEventListener('click', clearFsMinerCfgModal);
    setupFsChips();
    setupFsheetsContainer();
    setupWalletsContainer();
    const fsItems = document.getElementById('fsItemsContainer');
    fsItems.addEventListener('change', onFsRowChange);
    fsItems.addEventListener('input', onFsRowInput);
    fsItems.addEventListener('focusin', function(e) {
        if (e.target.classList.contains('fs-pool')) updatePoolDatalist();
    });
    fsItems.addEventListener('click', function(e) {
        const btn = e.target.closest('[data-action]');
        if (!btn) return;
        if (btn.dataset.action === 'remove-item') {
            const row = btn.closest('.fs-item-row');
            const idx = parseInt(row.dataset.idx, 10);
            (window._fsBuilderItems || []).splice(idx, 1);
            if (!window._fsBuilderItems.length) window._fsBuilderItems = [{}];
            rebuildFsItemsContainer();
        } else if (btn.dataset.action === 'wallet-add') {
            const row = btn.closest('.fs-item-row');
            const coin = row ? row.querySelector('.fs-coin').value.trim().toUpperCase() : '';
            showWalletModal({ coin: coin });
        } else if (btn.dataset.action === 'miner-setup') {
            openFsMinerCfgModal(btn.closest('.fs-item-row'));
        }
    });
    document.getElementById('walletAddBtn').addEventListener('click', () => showWalletModal(null));
    document.getElementById('walletEditSaveBtn').addEventListener('click', saveWalletFromModal);
    document.getElementById('walletsRefreshBtn').addEventListener('click', () => renderWallets());
    document.getElementById('fsheetsRefreshBtn').addEventListener('click', () => loadFsheets());
    document.getElementById('importFsheetsBtn').addEventListener('click', () => {
        document.getElementById('fsheetImportText').value = '';
        bootstrap.Modal.getOrCreateInstance(document.getElementById('fsheetImportModal')).show();
    });
    document.getElementById('fsheetImportFile').addEventListener('change', function() {
        const file = this.files && this.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => { document.getElementById('fsheetImportText').value = reader.result; };
        reader.readAsText(file);
    });
    document.getElementById('fsheetImportClipboardBtn').addEventListener('click', importFromClipboard);
    document.getElementById('fsheetImportSaveBtn').addEventListener('click', importFsheets);
    document.getElementById('fansRefreshBtn').addEventListener('click', function() {
        const btn = this;
        const icon = btn.querySelector('i');
        btn.disabled = true;
        icon.className = 'bi bi-arrow-clockwise spin-animation';
        loadFans().finally(() => {
            setTimeout(() => {
                btn.disabled = false;
                icon.className = 'bi bi-arrow-clockwise';
            }, 600);
        });
    });

    // Diagnostics modal bindings
    const diagModalEl = document.getElementById('diagModal');
    const diagModal = new bootstrap.Modal(diagModalEl);
    
    document.getElementById('openDiagBtn').addEventListener('click', () => {
        diagModal.show();
    });
    
    document.getElementById('runDiagBtn').addEventListener('click', runDiagnostics);
    diagModalEl.addEventListener('shown.bs.modal', runDiagnostics);
    
    async function sendSystemPowerAction(endpoint, btnId) {
        const btn = document.getElementById(btnId);
        const originalHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Sending...`;
        
        try {
            // apiFetch routes to the selected rig (local or via SSH proxy)
            const response = await apiFetch(endpoint, {
                method: 'POST',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(getRigName(currentRigId) + ': ' + data.message, true);
            } else {
                showToast(data.message || "Power command failed.", false);
            }
        } catch (error) {
            console.error("Power action failed:", error);
            showToast("Network error executing power command.", false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = originalHtml;
        }
    }

    // Miner logs viewer
    const logModal = document.getElementById('minerLogModal');
    let logPollInterval = null;
    
    document.getElementById('viewMinerLogBtn').addEventListener('click', () => {
        const modal = new bootstrap.Modal(logModal);
        modal.show();
    });
    
    logModal.addEventListener('shown.bs.modal', () => {
        fetchMinerLog();
        logPollInterval = setInterval(fetchMinerLog, 2000);
    });
    
    logModal.addEventListener('hidden.bs.modal', () => {
        if (logPollInterval) {
            clearInterval(logPollInterval);
            logPollInterval = null;
        }
    });
    
    async function fetchMinerLog() {
        try {
            const response = await apiFetch('/api/miner/log');
            if (response.status === 401) return;
            const data = await response.json();
            
            const consoleEl = document.getElementById('minerLogConsole');
            if (response.ok && data.success) {
                document.getElementById('activeMinerLogName').textContent = data.miner.toUpperCase();
                consoleEl.textContent = data.log || "Log file is currently empty.";
                consoleEl.scrollTop = consoleEl.scrollHeight;
            } else {
                consoleEl.textContent = "Error: " + (data.message || "Failed to read logs.");
            }
        } catch (error) {
            document.getElementById('minerLogConsole').textContent = "Failed to reach backend API for logs.";
        }
    }

    // Watchdog form submit
    document.getElementById('watchdogForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        const btn = this.querySelector('button[type="submit"]');
        btn.disabled = true;
        
        const payload = {
            wd_enabled: document.getElementById('wdEnabled').value,
            wd_min_hashrate: document.getElementById('wdMinHashrate').value
        };
        
        try {
            const response = await apiFetch('/api/watchdog', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
            } else {
                showToast(data.message || "Failed to save watchdog.", false);
            }
        } catch (error) {
            showToast("Network error saving watchdog.", false);
        } finally {
            btn.disabled = false;
        }
    });
    
    // AutoFan is saved through the "Apply" button (afSaveBtn);
    // no separate global form submit here.

    // Save OC Preset form: the same all-GPU overclock fields as the rig's
    // "Set settings for all GPUs" card, stored as a named preset (optionally
    // the default) for algorithm-bound automatic switching
    document.getElementById('saveOcPresetForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        const payload = collectOcFormValues();
        if (!payload) return;
        const btn = this.querySelector('button[type="submit"]');
        btn.disabled = true;
        try {
            const response = await apiFetch('/api/oc-presets/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                document.getElementById('ocPresetName').value = '';
                window._ocFormDirty = false;
                loadOcPresetsList();
            } else {
                showToast(data.message || "Failed to save OC preset.", false);
            }
        } catch (error) {
            showToast("Network error saving OC preset.", false);
        } finally {
            btn.disabled = false;
        }
    });

    // The preset form counts as dirty once the user edits any field, so the
    // live-OC prefill never wipes their input
    ['ocPresetName', 'ocPCore', 'ocPLcore', 'ocPMem', 'ocPLmem', 'ocPPl', 'ocPFan', 'ocPDelay']
        .forEach(id => document.getElementById(id).addEventListener('input', () => { window._ocFormDirty = true; }));
    ['ocPLed', 'ocPPill', 'ocPP0', 'ocPIdle']
        .forEach(id => document.getElementById(id).addEventListener('change', () => { window._ocFormDirty = true; }));

    // 6. Main view routing (Cluster / SSH Accesses / Rig Dashboard)
    document.querySelectorAll('#mainNavTabs .nav-link').forEach(link => {
        link.addEventListener('click', function(e) {
            e.preventDefault();
            showView(this.dataset.view);
        });
    });

    // Change password modal
    document.getElementById('changePasswordBtn').addEventListener('click', () => {
        document.getElementById('currentPasswordInput').value = '';
        document.getElementById('newPasswordInput').value = '';
        document.getElementById('confirmPasswordInput').value = '';
        document.getElementById('applyToClusterCheck').checked = true;
        new bootstrap.Modal(document.getElementById('passwordModal')).show();
    });
    document.getElementById('passwordSaveBtn').addEventListener('click', changePassword);

    // Cluster page bindings
    document.getElementById('syncNowBtn').addEventListener('click', syncNow);
    document.getElementById('createClusterBtn').addEventListener('click', () => openClusterModal(null));
    document.getElementById('clusterModalSaveBtn').addEventListener('click', saveClusterModal);
    document.getElementById('clusterRigsSaveBtn').addEventListener('click', saveClusterRigs);

    // Access page bindings (each Refresh refreshes only its own card)
    document.getElementById('accessRefreshBtn').addEventListener('click', () => loadAccessList({routes: true}));
    document.getElementById('addAccessBtn').addEventListener('click', () => openAccessModal(null, null));
    document.getElementById('addJumpBtn').addEventListener('click', () => openJumpModal(null));
    document.getElementById('jumpRefreshBtn').addEventListener('click', () => loadAccessList({jumps: true}));
    document.getElementById('jumpTestBtn').addEventListener('click', async function() {
        const btn = this;
        const resultEl = document.getElementById('jumpTestResult');
        btn.disabled = true;
        btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i> Testing...';
        resultEl.textContent = '';
        const payload = {
            jump: {
                id: editingJumpId || '',
                host: document.getElementById('jumpServerHostInput').value.trim(),
                port: parseInt(document.getElementById('jumpServerPortInput').value, 10) || 22,
                user: document.getElementById('jumpServerUserInput').value.trim(),
                auth: document.getElementById('jumpServerAuthSelect').value,
                password: document.getElementById('jumpServerPasswordInput').value,
                key_path: document.getElementById('jumpServerKeyPathInput').value.trim()
            }
        };
        try {
            const response = await fetch('/api/cluster/jump/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            resultEl.innerHTML = '<span class="' + (data.success ? 'text-success' : 'text-danger') + '">' +
                escapeHtml(data.message || (data.success ? 'Connection OK' : 'Connection failed')) + '</span>';
        } catch (e) {
            resultEl.innerHTML = '<span class="text-danger">Network error.</span>';
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="bi bi-plug"></i> Test Connection';
        }
    });
    document.getElementById('jumpSaveBtn').addEventListener('click', saveJumpModal);
    document.getElementById('jumpServerAuthSelect').addEventListener('change', function() {
        document.getElementById('jumpServerPasswordBlock').classList.toggle('d-none', this.value !== 'password');
        document.getElementById('jumpServerKeyBlock').classList.toggle('d-none', this.value !== 'key');
    });

    // 7. Auto-refresh interval dropdowns (Off/5s/10s/30s/1m)

    // Initial view from URL hash
    const initialView = (location.hash || '').replace('#', '');
    showView(['cluster', 'accesses', 'dashboard'].includes(initialView) ? initialView : 'cluster');
});

// ---------------- Auto-refresh manager ----------------

const AUTO_REFRESH_ITEMS = [[0, 'Off'], [5, '5s'], [10, '10s'], [30, '30s'], [60, '1m']];
const autoRefreshTimers = {};
const AUTO_REFRESH_LOADERS = {
    stats: () => fetchStats(),
    accesses: () => loadAccessList({routes: true}),
    jumps: () => loadAccessList({jumps: true}),
    cluster: () => { if (activeView === 'cluster') loadClusterData(true); },
    wallets: () => renderWallets(),
    fsheets: () => loadFsheets(),
    fans: () => { if (activeView === 'dashboard' && activeDashTab === 'fans') loadFans(); },
    metrics: () => { if (activeView === 'dashboard' && activeDashTab === 'stats') loadMetricsTab(); }
};

function autoRefreshDefault(target) {
    // Preserve the legacy behavior: stats and cluster data used to poll every 5s
    return (target === 'stats' || target === 'cluster') ? 5 : 0;
}

function getAutoRefreshInterval(target) {
    const v = parseInt(localStorage.getItem('autoRefresh_' + target), 10);
    return (Number.isFinite(v) && AUTO_REFRESH_ITEMS.some(x => x[0] === v)) ? v : autoRefreshDefault(target);
}

// Keep the backend sync worker in step with the Sync dropdown interval
function pushSyncInterval(seconds) {
    fetch('/api/cluster/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
        body: JSON.stringify({ sync_interval: seconds })
    }).catch(() => {});
}

function setAutoRefresh(target, seconds, skipImmediate) {
    localStorage.setItem('autoRefresh_' + target, String(seconds));
    if (autoRefreshTimers[target]) {
        clearInterval(autoRefreshTimers[target]);
        autoRefreshTimers[target] = null;
    }
    const loader = AUTO_REFRESH_LOADERS[target] || (() => {});
    if (seconds > 0) {
        // A newly picked interval takes effect at once: one refresh right now,
        // then every N seconds
        if (!skipImmediate) {
            try { loader(); } catch (e) { /* ignore */ }
        }
        autoRefreshTimers[target] = setInterval(loader, seconds * 1000);
    }
    // The Sync dropdown also defines how often the rig actually syncs with peers
    // (Off only disables UI polling; the background worker keeps its last interval)
    if (target === 'cluster' && seconds >= 5) pushSyncInterval(seconds);
}

function setupAutoRefreshMenus() {
    document.querySelectorAll('.auto-refresh-menu').forEach(menu => {
        const target = menu.dataset.target;
        if (!target || !AUTO_REFRESH_LOADERS[target]) return;
        menu.innerHTML = AUTO_REFRESH_ITEMS.map(([sec, label]) =>
            '<li><button class="dropdown-item" data-interval="' + sec + '">' + label + '</button></li>').join('');
        menu.querySelectorAll('.dropdown-item').forEach(item => {
            item.addEventListener('click', () => {
                setAutoRefresh(target, parseInt(item.dataset.interval, 10));
                paintAutoRefreshMenu(menu, target);
            });
        });
        paintAutoRefreshMenu(menu, target);
        // skipImmediate: restoring the stored interval at startup must not
        // duplicate the initial data load that already happened
        setAutoRefresh(target, getAutoRefreshInterval(target), true);
    });
}

function paintAutoRefreshMenu(menu, target) {
    const cur = getAutoRefreshInterval(target);
    menu.querySelectorAll('.dropdown-item').forEach(item => {
        item.classList.toggle('active', parseInt(item.dataset.interval, 10) === cur);
    });
    // Show the current interval inside the split caret button
    const group = menu.closest('.btn-group');
    const caret = group ? group.querySelector('.dropdown-toggle-split') : null;
    const label = (AUTO_REFRESH_ITEMS.find(x => x[0] === cur) || [0, 'Off'])[1];
    if (caret) caret.innerHTML = '<span class="ar-label">' + label + '</span><span class="visually-hidden">Auto-refresh interval</span>';
}

// Keep open dropdowns above neighboring glass cards (backdrop-filter creates
// stacking contexts, so a menu would otherwise render under the next card)
document.addEventListener('show.bs.dropdown', (e) => {
    const group = e.target.closest('.btn-group');
    if (group) group.classList.add('dd-open');
    const card = e.target.closest('.glass-card');
    if (card) card.classList.add('dd-open');
});
document.addEventListener('hidden.bs.dropdown', (e) => {
    const group = e.target.closest('.btn-group');
    if (group) group.classList.remove('dd-open');
    const card = e.target.closest('.glass-card');
    if (card) card.classList.remove('dd-open');
});

// ---------------- View routing ----------------

function showView(view) {
    activeView = view;
    document.getElementById('view-cluster').classList.toggle('d-none', view !== 'cluster');
    document.getElementById('view-accesses').classList.toggle('d-none', view !== 'accesses');
    document.getElementById('view-dashboard').classList.toggle('d-none', view !== 'dashboard');

    document.querySelectorAll('#mainNavTabs .nav-link').forEach(l => {
        l.classList.toggle('active', l.dataset.view === view);
    });

    location.hash = view;

    if (view === 'cluster') {
        loadClusterData();
    } else if (view === 'accesses') {
        loadAccessList();
    } else if (view === 'dashboard') {
        showDashTab(activeDashTab);
        fetchStats();
    }
}

// Switch the rig being managed in the dashboard view (self = local rig)
function switchRig(rigId) {
    // Managing the local rig is always local (no SSH proxy round-trip)
    if (rigId && clusterData && rigId === clusterData.self_id) rigId = 'self';
    if (rigId === currentRigId) return;
    currentRigId = rigId;
    activeOverclocks = {};
    window._ocAllDirty = false; // new rig -> the all-GPU OC card can re-prefill
    window._mknetUI = null; // new rig -> the 8MK_NET form reloads its config
    lastStatsData = null;
    updateRigScopeUi();
    fetchStats();
    loadTuningSettings();
    loadFsheets();
    renderWallets();
    loadFans();
    showToast('Switched to ' + getRigName(rigId), true);
}

function getRigName(rigId) {
    if (clusterData && clusterData.rigs) {
        const target = (!rigId || rigId === 'self') ? clusterData.self_id : rigId;
        const rig = clusterData.rigs.find(r => r.id === target);
        if (rig) return rig.name;
    }
    return (!rigId || rigId === 'self') ? 'Local rig' : rigId;
}

function updateRigScopeUi() {
    // Top-bar rig selector: shows the managed rig with a REMOTE marker
    const isSelf = currentRigId === 'self' || !currentRigId;
    document.getElementById('rigScopeName').textContent = getRigName(currentRigId);
    document.getElementById('rigScopeBadge').classList.toggle('d-none', isSelf);
    renderRigScopeMenu();
    renderClusterScopeMenu();
}

function renderRigScopeMenu() {
    const menu = document.getElementById('rigScopeMenu');
    if (!menu || !clusterData || !clusterData.rigs) return;
    // When a cluster is selected in the header, show only its rigs
    const filter = (currentClusterId && currentClusterId !== 'all' && clusterData.clusters)
        ? new Set(((clusterData.clusters.find(c => c.id === currentClusterId) || {}).rig_ids) || [])
        : null;
    const items = [];
    const selfRig = clusterData.rigs.find(r => r.is_self);
    if (selfRig && (!filter || filter.has(selfRig.id))) {
        items.push('<li><button class="dropdown-item' + (currentRigId === 'self' ? ' active' : '') + '" onclick="switchRigGlobal(\'self\')">' +
            '<i class="bi bi-hdd-network me-2"></i>' + escapeHtml(selfRig.name) + ' <span class="small text-muted">(local)</span></button></li>');
        items.push('<li><hr class="dropdown-divider"></li>');
    }
    clusterData.rigs.filter(r => !r.is_self).forEach(rig => {
        if (filter && !filter.has(rig.id)) return;
        const active = currentRigId === rig.id;
        const off = !rig.online;
        items.push('<li><button class="dropdown-item' + (active ? ' active' : '') + '" ' + (off ? 'disabled' : '') +
            ' onclick="switchRigGlobal(\'' + rig.id + '\')">' +
            '<i class="bi bi-hdd-rack me-2"></i>' + escapeHtml(rig.name || rig.id) +
            (off ? ' <span class="badge bg-danger-glow text-danger ms-1">OFFLINE</span>' : '') + '</button></li>');
    });
    menu.innerHTML = items.join('') || '<li><span class="dropdown-item text-muted">No rigs</span></li>';
}

window.switchRigGlobal = function(rigId) {
    switchRig(rigId);
};

// Cluster dropdown: lists the created clusters (All rigs + each named cluster)
function renderClusterScopeMenu() {
    const menu = document.getElementById('clusterScopeMenu');
    const btnName = document.getElementById('clusterScopeName');
    if (!menu || !btnName) return;
    if (!clusterData || !clusterData.rigs) {
        menu.innerHTML = '<li><span class="dropdown-item text-muted">Loading...</span></li>';
        return;
    }
    const clusters = clusterData.clusters || [];
    const activeCl = (currentClusterId && currentClusterId !== 'all')
        ? clusters.find(c => c.id === currentClusterId) : null;
    btnName.textContent = activeCl ? activeCl.name : 'All rigs';
    const items = ['<li><h6 class="dropdown-header"><i class="bi bi-diagram-3-fill text-info me-1"></i>Clusters</h6></li>'];
    items.push('<li><button class="dropdown-item' + (!activeCl ? ' active' : '') + '" onclick="selectClusterGlobal(\'all\')">' +
        '<i class="bi bi-collection me-2"></i>All rigs <span class="small text-muted">(' + clusterData.rigs.length + ')</span></button></li>');
    clusters.forEach(cl => {
        const n = (cl.rig_ids || []).filter(id => clusterData.rigs.some(r => r.id === id)).length;
        items.push('<li><button class="dropdown-item' + (activeCl && activeCl.id === cl.id ? ' active' : '') + '" onclick="selectClusterGlobal(\'' + cl.id + '\')">' +
            '<i class="bi bi-diagram-3-fill me-2"></i>' + escapeHtml(cl.name) +
            ' <span class="small text-muted">(' + n + ')</span></button></li>');
    });
    if (!clusters.length) {
        items.push('<li><span class="dropdown-item text-muted small">No clusters yet - create one on the Cluster tab</span></li>');
    }
    menu.innerHTML = items.join('');
}

window.selectClusterGlobal = function(cid) {
    currentClusterId = (!cid || cid === 'all') ? null : cid;
    updateRigScopeUi();
};

// Toast notification helper
function showToast(message, isSuccess = true) {
    const toastEl = document.getElementById('statusToast');
    const toastMessage = document.getElementById('toastMessage');
    const toastIcon = document.getElementById('toastIcon');
    
    toastMessage.textContent = message;
    
    if (isSuccess) {
        toastEl.className = 'toast align-items-center text-bg-success border-0 show';
        toastIcon.className = 'bi bi-check-circle-fill fs-5';
    } else {
        toastEl.className = 'toast align-items-center text-bg-danger border-0 show';
        toastIcon.className = 'bi bi-exclamation-octagon-fill fs-5';
    }
    
    setTimeout(() => {
        toastEl.classList.remove('show');
    }, 4500);
}

// Show the auth overlay in a clean state (never carry a typed password into it)
function showLoginOverlay() {
    const overlay = document.getElementById('loginOverlay');
    if (!overlay) return;
    overlay.classList.remove('d-none');
    // inline display (rendered 'none' for authenticated sessions) must be
    // overridden explicitly — classes alone lose to the inline style
    overlay.style.display = 'flex';
    const pwd = document.getElementById('loginPassword');
    if (pwd) pwd.value = '';
    const err = document.getElementById('loginError');
    if (err) err.classList.add('d-none');
}

// Fetch stats from backend API
async function fetchStats() {
    try {
        const response = await apiFetch('/api/stats');

        // Handle 401 Unauthorized status
        if (response.status === 401) {
            showLoginOverlay();
            return;
        }
        
        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }
        
        const data = await response.json();
        
        // Hide login if active (restored session on page reload)
        if (!document.getElementById('loginOverlay').classList.contains('d-none')) {
            document.getElementById('loginOverlay').classList.add('d-none');
            loadTuningSettings();
            loadClusterData();
            loadAccessList();
        }
        
        // Keep CSRF token fresh (survives page reloads while session is alive)
        if (data.csrf_token) {
            csrfToken = data.csrf_token;
        }
        
        // Save OC data globally to prefill forms
        activeOverclocks = data.overclocks;
        
        // Update header & badges
        document.getElementById('rigScopeName').textContent = getRigName(currentRigId);
        document.getElementById('dashboardRigNameText').textContent = getRigName(currentRigId);
        document.getElementById('dashboardVersion').textContent = data.system.dashboard_version;
        document.getElementById('currentVerText').textContent = data.system.dashboard_version;
        
        // Update System Diagnostics Panel
        document.getElementById('statRigId').textContent = data.system.rig_id;
        document.getElementById('statUptime').textContent = data.system.uptime;
        document.getElementById('statCpu').textContent = data.system.cpu_load[0].toFixed(2);
        document.getElementById('statRam').textContent = data.system.ram_used_pct + '% / ' + data.system.ram_total_gb + ' GB';
        document.getElementById('statMiner').textContent = data.system.active_miner;
        document.getElementById('statVersion').textContent = data.system.hive_version;

        // Calculate and Update GPU Overall Summaries
        let totalPower = 0;
        let sumTemp = 0;
        let sumFan = 0;
        let gpuCount = data.gpus.length;

        data.gpus.forEach(gpu => {
            totalPower += gpu.power;
            sumTemp += gpu.temp;
            sumFan += gpu.fan;
        });

        let avgTemp = gpuCount > 0 ? (sumTemp / gpuCount).toFixed(1) : 0;
        let avgFan = gpuCount > 0 ? (sumFan / gpuCount).toFixed(1) : 0;

        document.getElementById('statAvgTemp').textContent = avgTemp + ' °C';
        document.getElementById('statAvgFan').textContent = avgFan + ' %';
        document.getElementById('statTotalPower').textContent = totalPower.toFixed(1) + ' W';
        const coinAlgo = (data.system.coin || 'Unknown') + (data.miner_algo ? ' (' + data.miner_algo + ')' : '');
        document.getElementById('statCoin').textContent = coinAlgo;
        
        // Total GPUs / Total Speed reflect the active hardware tab
        updateHardwareStatBoxes(data);
        
        // Update CPU Mining Panel
        document.getElementById('cpuModelName').textContent = data.system.cpu.model;
        document.getElementById('cpuTemp').textContent = data.system.cpu.temp + ' °C';
        
        const hpStatus = document.getElementById('hugePagesStatus');
        if (data.system.cpu.hugepages) {
            hpStatus.textContent = "Enabled";
            hpStatus.className = "stat-value text-success";
        } else {
            hpStatus.textContent = "Disabled";
            hpStatus.className = "stat-value text-danger";
        }
        
        const hashrate = data.system.cpu.hashrate;
        const formattedHash = hashrate > 1000 ? (hashrate / 1000).toFixed(2) + ' KH/s' : hashrate.toFixed(0) + ' H/s';
        document.getElementById('cpuHashrateBadge').textContent = formattedHash;
        lastHugepagesEnabled = !!data.system.cpu.hugepages;

        // Render GPU cards + integrated graphics tab
        renderGpus(data.gpus);
        renderIgpus(data.igpus || [], data.system);
        prefillAllOc();
        lastStatsData = data;

    } catch (error) {
        console.error("Error fetching stats:", error);
        document.getElementById('gpuContainer').innerHTML = `
            <div class="col-12">
                <div class="alert alert-danger text-center glass-card py-4" role="alert">
                    <i class="bi bi-wifi-off fs-1 d-block mb-2"></i>
                    <h4 class="alert-heading fw-bold">Failed to Load GPU Stats</h4>
                    <p class="mb-0 small">${error.message}</p>
                </div>
            </div>
        `;
        
        // Also update stat boxes to show error
        document.querySelectorAll('.stat-value').forEach(el => {
            if (el.textContent === 'Loading...') {
                el.textContent = 'Error';
                el.className = 'stat-value text-danger';
            }
        });
    }
}

// Render GPU layout dynamically
function renderGpus(gpus) {
    const container = document.getElementById('gpuContainer');
    container.innerHTML = '';
    
    if (gpus.length === 0) {
        container.innerHTML = `
            <div class="col-12 text-center py-4">
                <p class="text-muted">No mining GPUs detected on this rig.</p>
            </div>
        `;
        return;
    }
    
    gpus.forEach(gpu => {
        const tempClass = gpu.temp > 75 ? 'danger' : (gpu.temp > 65 ? 'warning' : 'success');
        const fanClass = gpu.fan > 80 ? 'danger' : 'primary';
        
        const cardCol = document.createElement('div');
        cardCol.className = 'col-md-6 col-lg-4';
        
        cardCol.innerHTML = `
            <div class="card glass-card h-100">
                <div class="card-body d-flex flex-column justify-content-between">
                    <div>
                        <!-- Card Header -->
                        <div class="gpu-header d-flex justify-content-between align-items-center mb-3">
                            <span class="small fw-semibold text-muted">GPU ${gpu.index}</span>
                            <span class="badge bg-accent-glow text-primary fw-bold font-monospace">${fmtSpeed(gpu.hashrate)}</span>
                        </div>
                        
                        <!-- GPU Specs -->
                        <h3 class="h5 fw-bold mb-1">${gpu.model}</h3>
                        <p class="small text-muted mb-3">
                            <span class="brand-${gpu.brand.toLowerCase()}">${gpu.brand}</span> • PCI Bus ${gpu.id}
                        </p>
                        
                        <!-- Temperature Progress Bar -->
                        <div class="metric-row">
                            <div class="metric-label">
                                <span>Temperature</span>
                                <span class="metric-value">${gpu.temp}°C</span>
                            </div>
                            <div class="progress bg-black bg-opacity-20" style="height: 8px;">
                                <div class="progress-bar progress-bar-glow-${tempClass === 'danger' ? 'red' : (tempClass === 'success' ? 'green' : 'primary')}" 
                                     role="progressbar" style="width: ${gpu.temp}%" aria-valuenow="${gpu.temp}" aria-valuemin="0" aria-valuemax="100"></div>
                            </div>
                        </div>

                        <!-- Fan Speed Progress Bar -->
                        <div class="metric-row">
                            <div class="metric-label">
                                <span>Fan Speed</span>
                                <span class="metric-value">${gpu.fan}%</span>
                            </div>
                            <div class="progress bg-black bg-opacity-20" style="height: 8px;">
                                <div class="progress-bar progress-bar-glow-${fanClass === 'danger' ? 'red' : 'primary'}" 
                                     role="progressbar" style="width: ${gpu.fan}%" aria-valuenow="${gpu.fan}" aria-valuemin="0" aria-valuemax="100"></div>
                            </div>
                        </div>

                        <!-- Clocks and Power Metrics -->
                        <div class="row g-2 mt-2 pt-2 border-top border-secondary-subtle text-center">
                            <div class="col-4">
                                <div class="small text-muted">Core Clock</div>
                                <div class="fw-semibold small">${gpu.core_clock} MHz</div>
                            </div>
                            <div class="col-4">
                                <div class="small text-muted">Mem Clock</div>
                                <div class="fw-semibold small">${gpu.mem_clock} MHz</div>
                            </div>
                            <div class="col-4">
                                <div class="small text-muted">Power</div>
                                <div class="fw-semibold small text-danger-emphasis">${gpu.power}W <span class="text-muted small">/ ${gpu.power_limit}W</span></div>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Action Buttons -->
                    <div class="mt-4">
                        <button class="btn btn-sm btn-outline-primary w-100 py-2 fw-semibold d-flex align-items-center justify-content-center gap-1" 
                                onclick="openOcModal('${gpu.brand}', ${gpu.index})">
                            <i class="bi bi-sliders"></i> Edit Overclocks
                        </button>
                    </div>
                </div>
            </div>
        `;
        
        container.appendChild(cardCol);
    });
}

// Render CPU integrated graphics cards + CPU mining card (separate tab)
function renderIgpus(igpus, system) {
    const container = document.getElementById('igpuContainer');
    container.innerHTML = '';
    
    // CPU mining card with settings access
    const cpu = system && system.cpu ? system.cpu : null;
    if (cpu) {
        const cpuHash = cpu.hashrate || 0;
        const cpuHashStr = cpuHash > 1000 ? (cpuHash / 1000).toFixed(2) + ' KH/s' : cpuHash.toFixed(0) + ' H/s';
        const cpuTempClass = cpu.temp > 85 ? 'red' : (cpu.temp > 70 ? 'primary' : 'green');
        
        const cpuCol = document.createElement('div');
        cpuCol.className = 'col-md-6 col-lg-4';
        cpuCol.innerHTML = `
            <div class="card glass-card h-100">
                <div class="card-body d-flex flex-column justify-content-between">
                    <div>
                        <div class="gpu-header d-flex justify-content-between align-items-center mb-3">
                            <span class="small fw-semibold text-muted">CPU</span>
                            <span class="badge bg-accent-glow text-info fw-bold font-monospace">${cpuHashStr}</span>
                        </div>
                        
                        <h3 class="h6 fw-bold mb-1" style="font-size: 0.95rem;">${cpu.model}</h3>
                        <p class="small text-muted mb-3">
                            <span class="brand-intel">XMRig CPU Mining</span> • Huge Pages: ${cpu.hugepages ? '<span class="text-success fw-semibold">Enabled</span>' : '<span class="text-danger fw-semibold">Disabled</span>'}
                        </p>
                        
                        <div class="metric-row">
                            <div class="metric-label">
                                <span>Temperature</span>
                                <span class="metric-value">${cpu.temp}°C</span>
                            </div>
                            <div class="progress bg-black bg-opacity-20" style="height: 8px;">
                                <div class="progress-bar progress-bar-glow-${cpuTempClass}" 
                                     role="progressbar" style="width: ${cpu.temp}%" aria-valuenow="${cpu.temp}" aria-valuemin="0" aria-valuemax="100"></div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="mt-4">
                        <button class="btn btn-sm btn-outline-info w-100 py-2 fw-semibold d-flex align-items-center justify-content-center gap-1" 
                                onclick="openCpuSettings()">
                            <i class="bi bi-gear"></i> Edit CPU Settings
                        </button>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(cpuCol);
    }
    
    if (!igpus || igpus.length === 0) {
        if (!cpu) {
            container.innerHTML = `
                <div class="col-12 text-center py-4">
                    <p class="text-muted">No integrated graphics detected on this system.</p>
                </div>
            `;
        }
        return;
    }
    
    igpus.forEach(igpu => {
        const tempClass = igpu.temp > 85 ? 'danger' : (igpu.temp > 70 ? 'warning' : 'success');
        
        const cardCol = document.createElement('div');
        cardCol.className = 'col-md-6 col-lg-4';
        
        cardCol.innerHTML = `
            <div class="card glass-card h-100">
                <div class="card-body">
                    <div class="gpu-header d-flex justify-content-between align-items-center mb-3">
                        <span class="small fw-semibold text-muted">iGPU ${igpu.index}</span>
                        <span class="badge bg-accent-glow text-info fw-bold">Integrated</span>
                    </div>
                    
                    <h3 class="h5 fw-bold mb-1">${igpu.model}</h3>
                    <p class="small text-muted mb-3">
                        <span class="brand-${igpu.brand.toLowerCase()}">${igpu.brand}</span> • Built into processor, not used for mining
                    </p>
                    
                    <div class="metric-row">
                        <div class="metric-label">
                            <span>Temperature</span>
                            <span class="metric-value">${igpu.temp}°C</span>
                        </div>
                        <div class="progress bg-black bg-opacity-20" style="height: 8px;">
                            <div class="progress-bar progress-bar-glow-${tempClass === 'danger' ? 'red' : (tempClass === 'success' ? 'green' : 'primary')}" 
                                 role="progressbar" style="width: ${igpu.temp}%" aria-valuenow="${igpu.temp}" aria-valuemin="0" aria-valuemax="100"></div>
                        </div>
                    </div>

                    <div class="row g-2 mt-2 pt-2 border-top border-secondary-subtle text-center">
                        <div class="col-6">
                            <div class="small text-muted">Fan</div>
                            <div class="fw-semibold small">${igpu.fan}%</div>
                        </div>
                        <div class="col-6">
                            <div class="small text-muted">Power</div>
                            <div class="fw-semibold small text-danger-emphasis">${igpu.power}W</div>
                        </div>
                    </div>
                </div>
            </div>
        `;
        
        container.appendChild(cardCol);
    });
}

// Open CPU mining settings modal
window.openCpuSettings = function() {
    document.getElementById('cpuHugepagesSelect').value = lastHugepagesEnabled ? 'enable' : 'disable';
    const modal = new bootstrap.Modal(document.getElementById('cpuSettingsModal'));
    modal.show();
};

// Prefill and open the correct modal for the selected GPU
window.openOcModal = function(brand, index) {    const placeholders = document.querySelectorAll('.gpu-index-placeholder');
    placeholders.forEach(el => el.textContent = index);
    
    const inputs = document.querySelectorAll('.gpu-index-input');
    inputs.forEach(el => el.value = index);

    if (brand === "NVIDIA") {
        const nvCore = activeOverclocks.nvidia?.core?.[index] || "";
        const nvLcore = activeOverclocks.nvidia?.lcore?.[index] || "";
        const nvMem = activeOverclocks.nvidia?.mem?.[index] || "";
        const nvLmem = activeOverclocks.nvidia?.lmem?.[index] || "";
        const nvPl = activeOverclocks.nvidia?.pl?.[index] || "";
        const nvFan = activeOverclocks.nvidia?.fan?.[index] || "";
        const nvDelay = activeOverclocks.nvidia?.delay || "";

        document.getElementById('nvCore').value = nvCore === "0" ? "" : nvCore;
        document.getElementById('nvLcore').value = nvLcore === "0" ? "" : nvLcore;
        document.getElementById('nvMem').value = nvMem === "0" ? "" : nvMem;
        document.getElementById('nvLmem').value = nvLmem === "0" ? "" : nvLmem;
        document.getElementById('nvPl').value = nvPl === "0" ? "" : nvPl;
        document.getElementById('nvFan').value = nvFan === "0" ? "" : nvFan;
        document.getElementById('nvDelay').value = nvDelay === "0" ? "" : nvDelay;
        document.getElementById('nvLed').checked = activeOverclocks.nvidia?.led === "1";
        document.getElementById('nvPill').checked = activeOverclocks.nvidia?.pill === "1";
        document.getElementById('nvP0').checked = activeOverclocks.nvidia?.p0 === "1";
        document.getElementById('nvIdle').checked = activeOverclocks.nvidia?.idle === "1";

        const modal = new bootstrap.Modal(document.getElementById('nvOcModal'));
        modal.show();
    } else if (brand === "AMD") {
        const oc = activeOverclocks.amd || {};
        document.getElementById('amdCore').value = (oc.core?.[index] === "0" ? "" : oc.core?.[index]) || "";
        document.getElementById('amdMem').value = (oc.mem?.[index] === "0" ? "" : oc.mem?.[index]) || "";
        document.getElementById('amdVdd').value = (oc.vdd?.[index] === "0" ? "" : oc.vdd?.[index]) || "";
        document.getElementById('amdVddci').value = (oc.vddci?.[index] === "0" ? "" : oc.vddci?.[index]) || "";
        document.getElementById('amdMvdd').value = (oc.mvdd?.[index] === "0" ? "" : oc.mvdd?.[index]) || "";
        document.getElementById('amdDpm').value = (oc.dpm?.[index] === "0" ? "" : oc.dpm?.[index]) || "";
        document.getElementById('amdRef').value = (oc.ref?.[index] === "0" ? "" : oc.ref?.[index]) || "";
        document.getElementById('amdPl').value = (oc.pl?.[index] === "0" ? "" : oc.pl?.[index]) || "";
        document.getElementById('amdFan').value = (oc.fan?.[index] === "0" ? "" : oc.fan?.[index]) || "";

        const modal = new bootstrap.Modal(document.getElementById('amdOcModal'));
        modal.show();
    }
};

// Submit overclock data via AJAX
async function submitOverclock(formElement, modalId) {
    const formData = new FormData(formElement);
    const payload = {};
    
    formData.forEach((value, key) => {
        if (key === 'index') {
            payload[key] = parseInt(value);
        } else if (key === 'brand') {
            payload[key] = value;
        } else {
            payload[key] = value.trim() === '' ? '0' : value.trim();
        }
    });

    // Unchecked checkboxes are not serialized by FormData — send explicit 0/1
    formElement.querySelectorAll('input[type="checkbox"][name]').forEach(cb => {
        payload[cb.name] = cb.checked ? '1' : '0';
    });

    const submitBtn = formElement.querySelector('button[type="submit"]');
    const originalText = submitBtn.innerHTML;
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Saving...`;

    try {
        const response = await apiFetch('/api/overclock', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify(payload)
        });

        const data = await response.json();
        
        if (response.ok && data.success) {
            showToast(data.message, true);
            const modalInstance = bootstrap.Modal.getInstance(document.getElementById(modalId));
            modalInstance.hide();
            fetchStats();
        } else {
            showToast(data.message || "Failed to apply overclock parameters.", false);
        }
    } catch (error) {
        console.error("Error applying overclock:", error);
        showToast("Network error. Failed to reach the rig API.", false);
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
}

// Prefill the "Set settings for all GPUs" card: a value is shown only when
// every GPU has the same one (mixed/empty -> left blank = unchanged on apply).
// Skipped while the user is editing (dirty) so auto-refresh never wipes input.
function prefillAllOc() {
    if (window._ocAllDirty) return;
    const nv = activeOverclocks?.nvidia;
    if (!nv) return;
    const uniform = (list) => {
        if (!Array.isArray(list) || list.length === 0) return "";
        const vals = [...new Set(list.map(v => String(v).trim() === "0" ? "" : String(v).trim()))];
        return vals.length === 1 ? vals[0] : "";
    };
    document.getElementById('nvAllCore').value = uniform(nv.core);
    document.getElementById('nvAllLcore').value = uniform(nv.lcore);
    document.getElementById('nvAllMem').value = uniform(nv.mem);
    document.getElementById('nvAllLmem').value = uniform(nv.lmem);
    document.getElementById('nvAllPl').value = uniform(nv.pl);
    document.getElementById('nvAllFan').value = uniform(nv.fan);
    const delay = String(nv.delay ?? "").trim();
    document.getElementById('nvAllDelay').value = delay === "0" ? "" : delay;
    document.getElementById('nvAllLed').checked = nv.led === "1";
    document.getElementById('nvAllPill').checked = nv.pill === "1";
    document.getElementById('nvAllP0').checked = nv.p0 === "1";
    document.getElementById('nvAllIdle').checked = nv.idle === "1";
}

// Apply the all-GPU overclock card: only filled clock/power fields are sent,
// flags/delay/LEDs always reflect the checkbox states (rig-wide in HiveOS)
async function submitAllOverclock() {
    const fields = {
        core: 'nvAllCore', lcore: 'nvAllLcore', mem: 'nvAllMem', lmem: 'nvAllLmem',
        pl: 'nvAllPl', fan: 'nvAllFan', delay: 'nvAllDelay'
    };
    const payload = { brand: 'NVIDIA', gpu: 'all' };
    for (const [key, id] of Object.entries(fields)) {
        const v = document.getElementById(id).value.trim();
        if (v !== '') payload[key] = v;
    }
    payload.led = document.getElementById('nvAllLed').checked ? '1' : '0';
    payload.pill = document.getElementById('nvAllPill').checked ? '1' : '0';
    payload.p0 = document.getElementById('nvAllP0').checked ? '1' : '0';
    payload.idle = document.getElementById('nvAllIdle').checked ? '1' : '0';

    const btn = document.getElementById('ocAllApplyBtn');
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Saving...`;
    try {
        const response = await apiFetch('/api/overclock', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            window._ocAllDirty = false; // allow the next stats fetch to re-prefill saved values
            fetchStats();
        } else {
            showToast(data.message || "Failed to apply overclock parameters.", false);
        }
    } catch (error) {
        console.error("Error applying all-GPU overclock:", error);
        showToast("Network error. Failed to reach the rig API.", false);
    } finally {
        btn.disabled = false;
        btn.innerHTML = original;
    }
}

// Check for updates on GitHub
async function checkUpdate(isManual = false) {
    try {
        const response = await apiFetch('/api/update/check');
        if (response.status === 401) {
            if (isManual) showToast("Unauthorized. Please authorize your session first.", false);
            return;
        }
        
        if (response.ok) {
            const data = await response.json();
            if (data.success && data.update_available) {
                document.getElementById('remoteVersionText').textContent = 'v' + data.remote_version;
                document.getElementById('updateBanner').classList.remove('d-none');
                if (isManual) {
                    showToast(`New update found! v${data.remote_version} is available.`, true);
                }
            } else {
                document.getElementById('updateBanner').classList.add('d-none');
                if (isManual) {
                    showToast(`Your dashboard is already up to date (v${data.local_version}).`, true);
                }
            }
        } else {
            if (isManual) showToast("Failed to communicate with update checker API.", false);
        }
    } catch (error) {
        console.error("Failed to check for updates:", error);
        if (isManual) showToast("Network error checking for updates.", false);
    }
}

// Load tuning settings on authorization
async function loadTuningSettings() {
    try {
        const wdRes = await apiFetch('/api/watchdog');
        if (wdRes.ok) {
            const wdData = await wdRes.json();
            if (wdData.success) {
                document.getElementById('wdEnabled').value = wdData.wd_enabled;
                document.getElementById('wdMinHashrate').value = wdData.wd_min_hashrate;
            }
        }
        
        const afRes = await apiFetch('/api/autofan');
        if (afRes.ok) {
            const afData = await afRes.json();
            if (afData.success) {
                window._afData = afData;
                applyAutofanData(afData);
            }
        }
        
        
        loadFsheets();
    } catch (error) {
        console.error("Failed to load tuning configs:", error);
    }
}

// ---------- OC presets (algo-bound GPU overclock, rig all-GPU form values) ----------

function ocSummaryHtml(v) {
    const parts = [];
    if (String(v.lcore || '').trim()) parts.push('Core lock ' + escapeHtml(v.lcore) + ' MHz');
    else if (String(v.core || '').trim()) parts.push('Core +' + escapeHtml(v.core) + ' MHz');
    if (String(v.lmem || '').trim()) parts.push('Mem lock ' + escapeHtml(v.lmem) + ' MHz');
    else if (String(v.mem || '').trim()) parts.push('Mem +' + escapeHtml(v.mem) + ' MHz');
    if (String(v.pl || '').trim()) parts.push('PL ' + escapeHtml(v.pl) + ' W');
    if (String(v.fan || '').trim() && v.fan !== '0') parts.push('Fan ' + escapeHtml(v.fan) + '%');
    const flags = [];
    if (v.led === '1') flags.push('LED off');
    if (v.pill === '1') flags.push('Pill');
    if (v.p0 === '1') flags.push('P0');
    if (v.idle === '1') flags.push('Idle low');
    if (v.delay && v.delay !== '0') flags.push('Delay ' + escapeHtml(v.delay) + 's');
    if (flags.length) parts.push(flags.join(', '));
    return parts.join(' · ');
}

// Algorithms for the binding dropdown: live miner algo, flight sheet algos,
// algorithms already bound to presets
function ocAlgoOptions() {
    const algos = new Set();
    if (lastStatsData && lastStatsData.miner_algo) algos.add(String(lastStatsData.miner_algo).toLowerCase());
    if (Array.isArray(window._ocPresets)) window._ocPresets.forEach(p => { if (p.algo) algos.add(String(p.algo).toLowerCase()); });
    if (window._fsData && Array.isArray(window._fsData.fsheets)) {
        window._fsData.fsheets.forEach(f => (f.items || []).forEach(it => { if (it.algo) algos.add(String(it.algo).toLowerCase()); }));
    }
    return [...algos].sort();
}

function ocAlgoSelectHtml(p) {
    const options = ['<option value="" title="Not bound to an algorithm">—</option>']
        .concat(ocAlgoOptions().map(a =>
            `<option value="${escapeHtml(a)}"${a === String(p.algo || '') ? ' selected' : ''}>${escapeHtml(a)}</option>`));
    if (p.algo && !ocAlgoOptions().includes(String(p.algo))) {
        options.push(`<option value="${escapeHtml(p.algo)}" selected>${escapeHtml(p.algo)}</option>`);
    }
    return `<select class="form-select form-select-sm bg-dark-input text-white border-secondary-subtle oc-algo-select" data-oc-id="${p.id}" title="Auto-applies this preset when the rig mines the chosen algorithm">${options.join('')}</select>`;
}

// Prefill the save form from the rig's current overclock (uniform values only);
// skipped while the user is editing so a refresh never wipes input
function prefillOcPresetForm(live) {
    if (window._ocFormDirty || !live) return;
    const map = { ocPCore: 'core', ocPLcore: 'lcore', ocPMem: 'mem', ocPLmem: 'lmem',
                  ocPPl: 'pl', ocPFan: 'fan', ocPDelay: 'delay' };
    for (const [id, field] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el) el.value = String(live[field] ?? '');
    }
    const flags = { ocPLed: 'led', ocPPill: 'pill', ocPP0: 'p0', ocPIdle: 'idle' };
    for (const [id, flag] of Object.entries(flags)) {
        const el = document.getElementById(id);
        if (el) el.checked = String(live[flag] ?? '0') === '1';
    }
}

function collectOcFormValues() {
    const name = document.getElementById('ocPresetName').value.trim();
    if (!name) {
        showToast("Enter a preset name first.", false);
        return null;
    }
    const val = id => document.getElementById(id).value.trim();
    return {
        name: name,
        algo: '',
        values: {
            core: val('ocPCore'), lcore: val('ocPLcore'),
            mem: val('ocPMem'), lmem: val('ocPLmem'),
            pl: val('ocPPl'), fan: val('ocPFan'), delay: val('ocPDelay'),
            led: document.getElementById('ocPLed').checked ? '1' : '0',
            pill: document.getElementById('ocPPill').checked ? '1' : '0',
            p0: document.getElementById('ocPP0').checked ? '1' : '0',
            idle: document.getElementById('ocPIdle').checked ? '1' : '0'
        }
    };
}

async function loadOcPresetsList() {
    const container = document.getElementById('ocPresetsContainer');
    try {
        const response = await apiFetch('/api/oc-presets');
        const data = await response.json();
        if (response.ok && data.success) {
            window._ocPresets = data.presets || [];
            prefillOcPresetForm(data.live);
            if (!window._ocPresets.length) {
                container.innerHTML = `<div class="text-center text-muted small py-4">No OC presets yet. Fill the form below and save one.</div>`;
                return;
            }

            let html = `
                <table class="table table-sm align-middle mb-0">
                    <colgroup><col style="width:19%"><col style="width:19%"><col><col style="width:76px"><col style="width:116px"></colgroup>
                    <thead>
                        <tr class="small text-muted text-uppercase">
                            <th>Preset</th>
                            <th>Algorithm</th>
                            <th>Overclock</th>
                            <th class="text-center" title="Default preset: applied when no algorithm binding matches">Default</th>
                            <th class="text-end">Actions</th>
                        </tr>
                    </thead>
                    <tbody>`;
            window._ocPresets.forEach(p => {
                const activeBadge = p.active
                    ? ` <span class="badge bg-warning-glow text-warning small" title="The overclock currently applied on the rig">ACTIVE</span>`
                    : '';
                const defaultIcon = p.is_default
                    ? `<button type="button" class="btn btn-xs btn-link p-0 oc-default-btn text-warning" data-oc-id="${p.id}" title="Unset as default"><i class="bi bi-star-fill"></i></button>`
                    : `<button type="button" class="btn btn-xs btn-link p-0 oc-default-btn text-muted" data-oc-id="${p.id}" title="Set as default"><i class="bi bi-star"></i></button>`;
                html += `
                    <tr>
                        <td><span class="small fw-semibold">${escapeHtml(p.name)}</span>${activeBadge}</td>
                        <td>${ocAlgoSelectHtml(p)}</td>
                        <td><span class="small text-muted">${ocSummaryHtml(p.values || {}) || '<span class="fst-italic">empty</span>'}</span></td>
                        <td class="text-center">${defaultIcon}</td>
                        <td class="text-end text-nowrap">
                            <button type="button" class="btn btn-xs btn-outline-success py-0 px-2 oc-apply-btn" data-oc-id="${p.id}" title="Apply these overclock values now">
                                <i class="bi bi-play-circle-fill"></i> Apply
                            </button>
                            <button type="button" class="btn btn-xs btn-outline-danger py-0 px-2 oc-delete-btn" data-oc-id="${p.id}" title="Delete OC preset">
                                <i class="bi bi-trash"></i>
                            </button>
                        </td>
                    </tr>`;
            });
            html += '</tbody></table>';
            container.innerHTML = `<div class="table-responsive oc-preset-table">${html}</div>`;

            container.querySelectorAll('.oc-apply-btn').forEach(btn => {
                btn.addEventListener('click', function() { applyOcPreset(this.getAttribute('data-oc-id')); });
            });
            container.querySelectorAll('.oc-delete-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const p = (window._ocPresets || []).find(x => x.id === this.getAttribute('data-oc-id'));
                    if (p && confirm(`Delete OC preset "${p.name}"?`)) deleteOcPreset(p.id);
                });
            });
            container.querySelectorAll('.oc-default-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const p = (window._ocPresets || []).find(x => x.id === this.getAttribute('data-oc-id'));
                    if (!p) return;
                    ocPresetPost('/api/oc-presets/default', { id: p.is_default ? '' : p.id },
                        "Network error changing the default OC preset.");
                });
            });
            container.querySelectorAll('.oc-algo-select').forEach(sel => {
                sel.addEventListener('change', function() {
                    const p = (window._ocPresets || []).find(x => x.id === this.getAttribute('data-oc-id'));
                    if (!p) return;
                    const previous = String(p.algo || '');
                    const algo = this.value;
                    if (algo === previous) return;
                    ocPresetPost('/api/oc-presets/bind', { id: p.id, algo: algo },
                        "Network error binding the OC preset.");
                });
            });
        } else {
            container.innerHTML = `<div class="text-danger small py-3 text-center">Failed to load OC presets.</div>`;
        }
    } catch (error) {
        container.innerHTML = `<div class="text-danger small py-3 text-center">Connection error.</div>`;
    }
}

async function ocPresetPost(path, body, failMsg) {
    try {
        const response = await apiFetch(path, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify(body)
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            loadOcPresetsList();
            fetchStats();
        } else {
            showToast(data.message || failMsg, false);
            loadOcPresetsList();
        }
    } catch (error) {
        showToast(failMsg, false);
    }
}

function applyOcPreset(id) { ocPresetPost('/api/oc-presets/apply', { id: id }, "Network error applying OC preset."); }
function deleteOcPreset(id) { ocPresetPost('/api/oc-presets/delete', { id: id }, "Network error deleting OC preset."); }

async function runDiagnostics() {
    const runBtn = document.getElementById('runDiagBtn');
    const origHTML = runBtn.innerHTML;
    runBtn.disabled = true;
    runBtn.innerHTML = `<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Running...`;

    document.getElementById('diagGatewayStatus').className = 'badge bg-secondary';
    document.getElementById('diagGatewayStatus').textContent = 'Testing...';
    document.getElementById('diagInternetStatus').className = 'badge bg-secondary';
    document.getElementById('diagInternetStatus').textContent = 'Testing...';
    document.getElementById('diagDnsStatus').className = 'badge bg-secondary';
    document.getElementById('diagDnsStatus').textContent = 'Testing...';
    document.getElementById('diagHiveApiStatus').className = 'badge bg-secondary';
    document.getElementById('diagHiveApiStatus').textContent = 'Testing...';
    document.getElementById('diagGpuLogs').textContent = 'Running system tests...';

    try {
        const res = await apiFetch('/api/diagnostics');
        if (res.ok) {
            const data = await res.json();
            if (data.success) {
                document.getElementById('diagGatewayIp').textContent = data.gateway_ip;
                
                const setBadge = (id, status) => {
                    const el = document.getElementById(id);
                    el.textContent = status;
                    el.className = `badge ${status === 'Online' || status === 'Working' || status === 'Reachable' ? 'bg-success' : 'bg-danger'}`;
                };
                
                setBadge('diagGatewayStatus', data.gateway_ping);
                setBadge('diagInternetStatus', data.internet_wan);
                setBadge('diagDnsStatus', data.dns_resolution);
                setBadge('diagHiveApiStatus', data.hiveos_api);
                document.getElementById('diagGpuLogs').textContent = data.gpu_logs;
            }
        } else {
            document.getElementById('diagGpuLogs').textContent = 'Diagnostics API request failed.';
        }
    } catch (e) {
        console.error(e);
        document.getElementById('diagGpuLogs').textContent = 'Connection timeout checking diagnostics.';
    } finally {
        runBtn.disabled = false;
        runBtn.innerHTML = origHTML;
    }
}

// ---------------- Cluster page ----------------

async function loadClusterData(silent = false) {
    if (!silent && !document.getElementById('clusterRigsContainer').dataset.loaded) {
        document.getElementById('clusterRigsContainer').innerHTML =
            '<div class="col-12 text-center py-5"><div class="spinner-border text-primary" role="status"></div><p class="mt-2 text-muted">Loading clusters...</p></div>';
    }
    try {
        const response = await fetch('/api/cluster/rigs');
        if (response.status === 401) {
            showLoginOverlay();
            return;
        }
        const data = await response.json();
        if (response.ok && data.success) {
            clusterData = data;
            // Heal drift: make the backend sync worker honor the interval chosen in the Sync dropdown
            const uiInterval = getAutoRefreshInterval('cluster');
            if (data.sync_interval && uiInterval >= 5 && data.sync_interval !== uiInterval) {
                pushSyncInterval(uiInterval);
            }
            renderCluster();
            // Keep sshpass availability warning on the accesses page up to date
            document.getElementById('sshpassWarning').classList.toggle('d-none', !!data.sshpass_available);
        }
    } catch (error) {
        if (!silent) {
            document.getElementById('clusterRigsContainer').innerHTML =
                '<div class="col-12"><div class="alert alert-danger text-center glass-card py-4"><i class="bi bi-wifi-off fs-1 d-block mb-2"></i><h4 class="alert-heading fw-bold">Failed to Load Cluster</h4><p class="mb-0 small">' + (error.message || '') + '</p></div></div>';
        }
    }
}

// Live "Last Sync" clock — ticks every second regardless of the cluster auto-refresh interval
function updateLastSyncDisplay() {
    const el = document.getElementById('clusterLastSync');
    if (!el || !clusterData || !(clusterData.last_sync > 0)) return;
    const ago = Math.max(0, Math.round((Date.now() / 1000) - clusterData.last_sync));
    el.textContent = ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago';
    el.className = 'stat-value ' + (clusterData.last_sync_ok ? 'text-success' : 'text-danger');
}

function renderCluster() {
    if (!clusterData || !clusterData.rigs) return;
    const container = document.getElementById('clusterRigsContainer');
    container.dataset.loaded = '1';

    document.getElementById('clusterSyncStatus').textContent = clusterData.last_sync_message || '';
    const online = clusterData.rigs.filter(r => r.online).length;
    document.getElementById('clusterOnline').textContent = online + ' / ' + clusterData.rigs.length;
    // Keep the header rig/cluster dropdowns populated with all cluster rigs
    updateRigScopeUi();

    updateLastSyncDisplay();

    // Farm-wide totals: only online rigs count (offline stats are stale)
    let totalPower = 0, totalGpus = 0, tempSum = 0, tempCount = 0, totalMh = 0;
    clusterData.rigs.forEach(rig => {
        const stats = rig.stats;
        if (!stats || !rig.online) return;
        totalMh += (stats.total_hashrate_mh || 0) + (stats.system && stats.system.cpu ? stats.system.cpu.hashrate / 1000 : 0);
        (stats.gpus || []).forEach(g => {
            totalPower += g.power || 0;
            tempSum += g.temp || 0;
            tempCount += 1;
        });
        totalGpus += (stats.gpus || []).length;
    });
    document.getElementById('clusterTotalHashrate').innerHTML = fmtSpeedHtml(totalMh);
    document.getElementById('clusterTotalPower').textContent = totalPower.toFixed(1) + ' W';
    document.getElementById('clusterTotalGpus').textContent = totalGpus;
    document.getElementById('clusterAvgTemp').textContent = (tempCount ? (tempSum / tempCount).toFixed(1) : 0) + ' °C';

    // Cluster sections (rig groups) + unassigned rigs
    const clusters = clusterData.clusters || [];
    const rigById = {};
    clusterData.rigs.forEach(r => { rigById[r.id] = r; });
    const assigned = new Set();
    container.innerHTML = '';

    clusters.forEach(cl => {
        const members = (cl.rig_ids || []).map(id => rigById[id]).filter(Boolean);
        members.forEach(m => assigned.add(m.id));
        const section = document.createElement('div');
        section.className = 'col-12';
        section.innerHTML = `
            <div class="card glass-card">
                <div class="card-body">
                    <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
                        <h3 class="h5 fw-bold mb-0 d-flex align-items-center gap-2">
                            <i class="bi bi-diagram-3-fill text-primary"></i> ${escapeHtml(cl.name)}
                            <span class="badge bg-secondary-subtle text-secondary-emphasis small">${members.length} rig${members.length === 1 ? '' : 's'}</span>
                        </h3>
                        <div class="d-flex gap-2">
                            <button class="btn btn-sm btn-outline-primary fw-semibold d-flex align-items-center gap-1" title="Add rigs to this cluster" onclick="openClusterRigsModal('${cl.id}')">
                                <i class="bi bi-plus-lg"></i> Add
                            </button>
                            <button class="btn btn-sm btn-outline-secondary" title="Rename cluster" onclick="openClusterModal('${cl.id}')">
                                <i class="bi bi-pencil"></i>
                            </button>
                            <button class="btn btn-sm btn-outline-danger" title="Delete cluster (rigs stay untouched)" onclick="deleteCluster('${cl.id}')">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </div>
                    <div class="row g-4">${members.length ? '' :
                        '<div class="col-12"><p class="text-muted small mb-0">No rigs in this cluster yet. Click Add to select rigs.</p></div>'}</div>
                </div>
            </div>`;
        const grid = section.querySelector('.row.g-4');
        members.forEach(m => grid.appendChild(buildRigCard(m)));
        container.appendChild(section);
    });

    const unassigned = clusterData.rigs.filter(r => !assigned.has(r.id));
    if (unassigned.length) {
        const section = document.createElement('div');
        section.className = 'col-12';
        section.innerHTML = `
            <div class="card glass-card">
                <div class="card-body">
                    <h3 class="h6 fw-bold mb-3 d-flex align-items-center gap-2">
                        <i class="bi bi-collection text-secondary"></i> Unassigned Rigs
                        <span class="badge bg-secondary-subtle text-secondary-emphasis small">${unassigned.length}</span>
                    </h3>
                    <div class="row g-4"></div>
                </div>
            </div>`;
        const grid = section.querySelector('.row.g-4');
        unassigned.forEach(m => grid.appendChild(buildRigCard(m)));
        container.appendChild(section);
    }

    if (!clusters.length) {
        const hint = document.createElement('div');
        hint.className = 'col-12';
        hint.innerHTML = '<div class="alert alert-info small border-info-subtle mb-0"><i class="bi bi-info-circle-fill"></i> ' +
            'Create a cluster with the Add button above, then tick the rigs (added on the SSH Accesses tab) to include.</div>';
        container.appendChild(hint);
    }
}

// Build a rig card column (used inside cluster sections and the unassigned group)
function buildRigCard(rig) {
    const stats = rig.stats;
    const system = stats && stats.system ? stats.system : {};
    const isOnline = !!rig.online;
    const gpuCount = stats && stats.gpus ? stats.gpus.length : null;
    const totalHashMh = stats ? ((stats.total_hashrate_mh || 0) + (system.cpu ? system.cpu.hashrate / 1000 : 0)) : 0;
    const totalHash = !stats ? 'n/a' : (isOnline ? fmtSpeed(totalHashMh) : '—');
    const power = stats ? (isOnline ? (stats.gpus || []).reduce((s, g) => s + (g.power || 0), 0).toFixed(1) + ' W' : '—') : 'n/a';
    const temps = stats && stats.gpus && stats.gpus.length
        ? (isOnline ? (stats.gpus.reduce((s, g) => s + (g.temp || 0), 0) / stats.gpus.length).toFixed(0) + ' °C' : '—') : 'n/a';
    const isSelf = !!rig.is_self;
    // Coin with the mining algorithm in parentheses
    const coinAlgo = isOnline
        ? escapeHtml((system.coin || 'None') + (stats && stats.miner_algo ? ' (' + stats.miner_algo + ')' : ''))
        : '—';

    const statusBadge = isSelf
        ? '<span class="badge bg-success-glow border border-success text-success">THIS RIG</span>'
        : (isOnline
            ? '<span class="badge bg-success-glow border border-success text-success"><span class="pulse-indicator"></span>ONLINE</span>'
            : '<span class="badge bg-danger-glow border border-danger text-danger" title="' + escapeHtml(rig.last_error || '') + '">OFFLINE</span>');

    const col = document.createElement('div');
    col.className = 'col-md-6 col-lg-4' + (isOnline ? '' : ' rig-offline');
    col.innerHTML = `
        <div class="card glass-card h-100 rig-card-clickable" onclick="openRigCard('${rig.id}')" title="Open rig dashboard">
            <div class="card-body d-flex flex-column">
                <div class="d-flex align-items-center gap-2 mb-2">
                    <h3 class="h6 fw-bold mb-0 text-truncate">${escapeHtml(rig.name || rig.id)}</h3>
                    ${statusBadge}
                    <div class="ms-auto d-flex gap-1 flex-shrink-0">
                        <button class="btn btn-xs btn-outline-secondary" title="Edit rig" onclick="event.stopPropagation(); openRigModal('${rig.id}')">
                            <i class="bi bi-pencil"></i>
                        </button>
                        ${isSelf ? '' : '<button class="btn btn-xs btn-outline-danger" title="Remove rig" onclick="event.stopPropagation(); deleteRig(\'' + rig.id + '\')"><i class="bi bi-trash"></i></button>'}
                    </div>
                </div>
                <p class="small text-muted mb-2 text-truncate" title="${escapeHtml(rig.host_label || '')}">
                    <i class="bi bi-hdd-network me-1"></i>${escapeHtml(rig.host_label || '')}
                </p>
                <div class="row g-2 mt-0 pt-2 border-top border-secondary-subtle text-center">
                    <div class="col-4">
                        <div class="small text-muted">GPUs</div>
                        <div class="fw-semibold small">${isOnline ? (gpuCount === null ? 'n/a' : gpuCount) : '—'}</div>
                    </div>
                    <div class="col-4">
                        <div class="small text-muted">Speed</div>
                        <div class="fw-semibold small text-primary-gradient fw-bold">${totalHash}</div>
                    </div>
                    <div class="col-4">
                        <div class="small text-muted">Temp</div>
                        <div class="fw-semibold small">${temps}</div>
                    </div>
                    <div class="col-4">
                        <div class="small text-muted">Miner</div>
                        <div class="fw-semibold small">${isOnline ? escapeHtml((system.active_miner || 'None') + (system.miner_running ? '' : ' (stopped)')) : '—'}</div>
                    </div>
                    <div class="col-4">
                        <div class="small text-muted">Coin</div>
                        <div class="fw-semibold small text-amber-gradient text-truncate" title="${coinAlgo}">${coinAlgo}</div>
                    </div>
                    <div class="col-4">
                        <div class="small text-muted">Power</div>
                        <div class="fw-semibold small text-danger-emphasis">${power}</div>
                    </div>
                </div>
            </div>
        </div>`;
    return col;
}

// Open a rig dashboard from a cluster card click (offline remote rigs are blocked)
window.openRigCard = function(rigId) {
    const rig = clusterData && clusterData.rigs ? clusterData.rigs.find(r => r.id === rigId) : null;
    if (rig && !rig.is_self && !rig.online) {
        showToast('Rig "' + (rig.name || rigId) + '" is offline.', false);
        return;
    }
    openRigDashboard(rigId);
};

// Open the dashboard view scoped to the selected rig
window.openRigDashboard = function(rigId) {
    switchRig(rigId);
    showView('dashboard');
};

window.syncNow = async function() {
    const spinner = document.getElementById('clusterSyncSpinner');
    const status = document.getElementById('clusterSyncStatus');
    spinner.classList.remove('d-none');
    status.textContent = 'Syncing...';
    try {
        await fetch('/api/cluster/sync/now', {
            method: 'POST',
            headers: { 'X-CSRF-Token': csrfToken }
        });
    } catch (e) { /* ignore */ }
    // Give the backend cycle a moment to land, then reload and restore the UI.
    // renderCluster() overwrites the placeholder with the real last-sync message;
    // the fallback only fires when the reload produced nothing (network error).
    setTimeout(async () => {
        await loadClusterData(true);
        spinner.classList.add('d-none');
        if (status.textContent === 'Syncing...') status.textContent = '';
    }, 2500);
};

// ---------------- Cluster create/rename/delete + membership ----------------

let editingClusterId = null;
let membershipClusterId = null;

function openClusterModal(clusterId) {
    editingClusterId = clusterId || null;
    const cl = clusterId && clusterData ? (clusterData.clusters || []).find(c => c.id === clusterId) : null;
    document.getElementById('clusterModalTitle').textContent = cl ? 'Edit Cluster' : 'Create Cluster';
    document.getElementById('clusterNameModalInput').value = cl ? cl.name : '';
    new bootstrap.Modal(document.getElementById('clusterModal')).show();
}
window.openClusterModal = openClusterModal;

async function saveClusterModal() {
    const name = document.getElementById('clusterNameModalInput').value.trim();
    if (!name) { showToast('Enter a cluster name.', false); return; }
    const url = editingClusterId ? '/api/cluster/update' : '/api/cluster/create';
    const payload = editingClusterId ? { id: editingClusterId, name: name } : { name: name };
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Cluster saved.' : 'Failed to save cluster.'), !!data.success);
        if (data.success) {
            bootstrap.Modal.getInstance(document.getElementById('clusterModal')).hide();
            loadClusterData(true);
        }
    } catch (e) {
        showToast('Network error saving cluster.', false);
    }
}

window.openClusterRigsModal = function(clusterId) {
    if (!clusterData || !clusterData.rigs) return;
    membershipClusterId = clusterId;
    const cl = (clusterData.clusters || []).find(c => c.id === clusterId);
    document.getElementById('clusterRigsModalName').textContent = cl ? cl.name : 'cluster';
    const memberSet = new Set((cl && cl.rig_ids) || []);
    const list = document.getElementById('clusterRigsCheckList');
    if (!clusterData.rigs.length) {
        list.innerHTML = '<div class="text-muted small py-2">No rigs yet. Add them on the SSH Accesses tab first.</div>';
    } else {
        list.innerHTML = clusterData.rigs.map(r => {
            const checked = memberSet.has(r.id) ? ' checked' : '';
            return '<div class="form-check d-flex align-items-center gap-2 py-1 mb-0">' +
                '<input class="form-check-input cluster-rig-check" type="checkbox" value="' + escapeHtml(r.id) + '" id="memb_' + escapeHtml(r.id) + '"' + checked + '>' +
                '<label class="form-check-label small flex-grow-1" for="memb_' + escapeHtml(r.id) + '">' +
                    '<span class="fw-semibold">' + escapeHtml(r.name || r.id) + '</span>' +
                    (r.is_self ? ' <span class="text-muted">(local)</span>' : '') +
                    (r.online ? '' : ' <span class="badge bg-danger-glow text-danger">OFFLINE</span>') +
                '</label></div>';
        }).join('');
    }
    new bootstrap.Modal(document.getElementById('clusterRigsModal')).show();
};

async function saveClusterRigs() {
    const ids = Array.from(document.querySelectorAll('.cluster-rig-check:checked')).map(cb => cb.value);
    try {
        const response = await fetch('/api/cluster/members', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: membershipClusterId, rig_ids: ids })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Membership updated.' : 'Failed to update membership.'), !!data.success);
        if (data.success) {
            bootstrap.Modal.getInstance(document.getElementById('clusterRigsModal')).hide();
            loadClusterData(true);
        }
    } catch (e) {
        showToast('Network error saving cluster membership.', false);
    }
}

window.deleteCluster = async function(clusterId) {
    const cl = (clusterData.clusters || []).find(c => c.id === clusterId);
    if (!confirm('Delete cluster "' + (cl ? cl.name : clusterId) + '"? The rigs themselves stay untouched.')) return;
    try {
        const response = await fetch('/api/cluster/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: clusterId })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Cluster deleted.' : 'Failed to delete cluster.'), !!data.success);
        if (data.success) {
            if (currentClusterId === clusterId) currentClusterId = null;
            loadClusterData(true);
        }
    } catch (e) {
        showToast('Network error deleting cluster.', false);
    }
};

// ---------------- Rig modal ----------------

function openRigModal(rigId) {
    editingRigId = rigId;
    const isSelf = rigId === null || (clusterData && clusterData.self_id === rigId);
    const rig = rigId && clusterData ? clusterData.rigs.find(r => r.id === rigId) : null;

    document.getElementById('rigModalTitle').textContent = rig ? ('Edit Rig: ' + rig.name) : 'Add Rig';
    document.getElementById('rigNameInput').value = rig ? rig.name : '';
    document.getElementById('rigHostLabelInput').value = rig ? (rig.host_label || '') : '';

    const pwInput = document.getElementById('rigPasswordInput');
    if (rig && rig.is_self) {
        pwInput.value = '';
        pwInput.disabled = true;
        document.getElementById('rigPasswordHelp').textContent = 'This is the local rig. Its dashboard password is managed with the Password button in the header.';
    } else if (rig) {
        pwInput.value = '********';
        pwInput.disabled = false;
        document.getElementById('rigPasswordHelp').textContent = 'Leave masked (********) to keep the current stored password, or type a new one.';
    } else {
        pwInput.value = '';
        pwInput.disabled = false;
        document.getElementById('rigPasswordHelp').textContent = 'Web panel password of that rig (other rigs use it to call its API).';
    }

    document.getElementById('rigModalDeleteBtn').classList.toggle('d-none', !rig || !!rig.is_self);
    document.getElementById('rigModalTestResults').innerHTML = '';
    renderRigModalAccesses(rig);
    new bootstrap.Modal(document.getElementById('rigModal')).show();
}

function renderRigModalAccesses(rig) {
    const list = document.getElementById('rigModalAccessList');
    const accesses = rig ? rig.accesses : [];
    if (!accesses.length) {
        list.innerHTML = '<div class="text-center text-muted small py-3">No SSH accesses configured for this rig yet.</div>';
        return;
    }
    let html = '<div class="list-group list-group-flush">';
    accesses.forEach(a => {
        const route = a.type === 'jump'
            ? escapeHtml(a.user + '@' + a.host + ':' + a.port) + ' <i class="bi bi-arrow-right"></i> jump ' + escapeHtml(jumpNameById(a.jump_id) || a.jump_host || '?')
            : escapeHtml(a.user + '@' + a.host + ':' + a.port);
        const auth = a.auth === 'key' ? '<i class="bi bi-file-earmark-key"></i> key' : '<i class="bi bi-shield-lock"></i> password';
        html += `
            <div class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-2 px-2 flex-wrap gap-2">
                <div>
                    <span class="fw-semibold small">${escapeHtml(a.name)}</span>
                    <span class="badge bg-secondary-subtle text-secondary-emphasis ms-1">${a.type === 'jump' ? 'JUMP' : 'DIRECT'}</span>
                    <div class="small text-muted font-monospace">${route}</div>
                </div>
                <div class="d-flex gap-2">
                    <span class="small text-muted">${auth}</span>
                    <button class="btn btn-xs btn-outline-primary py-0 px-2" onclick="openAccessModal('${rig ? rig.id : ''}', '${a.id}')" title="Edit access"><i class="bi bi-pencil"></i></button>
                    <button class="btn btn-xs btn-outline-danger py-0 px-2" onclick="deleteAccess('${rig ? rig.id : ''}', '${a.id}')" title="Delete access"><i class="bi bi-trash"></i></button>
                </div>
            </div>`;
    });
    html += '</div>';
    document.getElementById('rigModalAccessList').innerHTML = html;
}

window.openRigModal = openRigModal;

window.saveRigModal = async function() {
    const name = document.getElementById('rigNameInput').value.trim();
    const password = document.getElementById('rigPasswordInput').value.trim();
    const hostLabel = document.getElementById('rigHostLabelInput').value.trim();

    if (!name) { showToast('Please enter a rig name.', false); return; }
    if (editingRigId && !isSelfRig(editingRigId) && password && password !== '********' && password.length < 4) {
        showToast('Dashboard password must be at least 4 characters.', false);
        return;
    }
    if (!editingRigId && !password) {
        showToast('Enter the dashboard password of the remote rig.', false);
        return;
    }

    const payload = { name: name, host_label: hostLabel };
    if (editingRigId) payload.id = editingRigId;
    if (password && password !== '********') payload.password = password;

    try {
        const response = await fetch('/api/cluster/rig', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            bootstrap.Modal.getInstance(document.getElementById('rigModal')).hide();
            loadClusterData(true);
            loadAccessList();
        } else {
            showToast(data.message || 'Failed to save rig.', false);
        }
    } catch (e) {
        showToast('Network error saving rig.', false);
    }
};
document.getElementById('rigModalSaveBtn').addEventListener('click', saveRigModal);
document.getElementById('rigModalAddAccessBtn').addEventListener('click', () => openAccessModal(editingRigId, null));

document.getElementById('rigModalDeleteBtn').addEventListener('click', function() {
    if (!editingRigId) return;
    const name = getRigName(editingRigId);
    if (confirm('Remove rig "' + name + '" from the cluster? The rig itself keeps running untouched.')) {
        deleteRig(editingRigId);
        bootstrap.Modal.getInstance(document.getElementById('rigModal')).hide();
    }
});

window.testRigAccesses = async function(rigId) {
    const resultsEl = document.getElementById('rigModalTestResults');
    resultsEl.innerHTML = '<span class="text-muted"><i class="bi bi-arrow-repeat spin-animation"></i> Testing accesses...</span>';
    try {
        const response = await fetch('/api/cluster/rig/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: rigId })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            let html = '';
            (data.results || []).forEach(r => {
                html += '<div class="' + (r.ok ? 'text-success' : 'text-danger') + '">' +
                        '<i class="bi ' + (r.ok ? 'bi-check-circle-fill' : 'bi-x-circle-fill') + '"></i> ' +
                        escapeHtml(r.name) + ': ' + escapeHtml(r.detail) + '</div>';
            });
            html += '<div class="' + (data.api_ok ? 'text-success' : 'text-warning') + '">' +
                    '<i class="bi ' + (data.api_ok ? 'bi-check-circle-fill' : 'bi-x-circle-fill') + '"></i> ' +
                    escapeHtml(data.api_detail || '') + '</div>';
            resultsEl.innerHTML = html || '<span class="text-warning">No accesses configured for this rig.</span>';
        } else {
            resultsEl.innerHTML = '<span class="text-danger">' + escapeHtml(data.message || 'Test failed.') + '</span>';
        }
    } catch (e) {
        resultsEl.innerHTML = '<span class="text-danger">Network error during test.</span>';
    }
};
document.getElementById('rigModalTestBtn').addEventListener('click', () => {
    if (editingRigId) {
        testRigAccesses(editingRigId);
    } else {
        document.getElementById('rigModalTestResults').innerHTML =
            '<span class="text-warning">Save the rig first, then test its accesses.</span>';
    }
});

window.deleteRig = async function(rigId) {
    if (!confirm('Remove this rig from the cluster?')) return;
    try {
        const response = await fetch('/api/cluster/rig/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: rigId })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Removed.' : 'Failed to remove rig.'), !!data.success);
        if (data.success) {
            if (currentRigId === rigId) switchRig('self');
            loadClusterData(true);
            loadAccessList();
        }
    } catch (e) {
        showToast('Network error removing rig.', false);
    }
};

function isSelfRig(rigId) {
    return !rigId || (clusterData && clusterData.self_id === rigId);
}

// ---------------- SSH accesses page ----------------

async function loadAccessList(parts) {
    // parts: {jumps: bool, routes: bool} — which tables to re-render.
    // Default (no parts) re-renders both: full reload for tab entry/saves.
    const wantJumps = !parts || parts.jumps || (!parts.jumps && !parts.routes);
    const wantRoutes = !parts || parts.routes || (!parts.jumps && !parts.routes);
    try {
        const response = await fetch('/api/cluster/rigs');
        if (response.status === 401) {
            showLoginOverlay();
            return;
        }
        const data = await response.json();
        if (response.ok && data.success) {
            clusterData = data;
            document.getElementById('sshpassWarning').classList.toggle('d-none', !!data.sshpass_available);
            if (wantJumps) renderJumpsTable();
            if (wantRoutes) renderAccessRoutes();
        }
    } catch (e) {
        console.error('Failed to load accesses:', e);
    }
}

// Each SSH card refreshes only its own table: renderJumpsTable() for the
// Jump Servers card, renderAccessRoutes() for the Access Routes card.
// renderAccesses() re-renders both (used by full reloads: tab entry, saves).
function renderJumpsTable() {
    const jumpBody = document.getElementById('jumpTableBody');
    if (!clusterData) return;
    // Fresh data -> dots back to gray "not checked" until re-tested
    jumpTestResults = {};
    const jumps = clusterData.jump_hosts || [];
    if (!jumps.length) {
        jumpBody.innerHTML = '<tr><td colspan="6" class="text-center text-muted small py-3">No jump servers yet. Add one and reuse it for any rig.</td></tr>';
    } else {
        jumpBody.innerHTML = jumps.map(j => {
            const auth = j.auth === 'key' ? '<i class="bi bi-file-earmark-key"></i> key' : '<i class="bi bi-shield-lock"></i> password';
            const dotState = jumpTestResults[j.id];
            const dotCls = dotState === undefined ? '' : (dotState ? 'conn-dot-ok' : 'conn-dot-fail');
            const dotTitle = dotState === undefined ? 'Not checked yet' : (dotState ? 'Connection OK' : 'Connection failed');
            return '<tr>' +
                '<td class="small text-muted"><i class="bi bi-router-fill text-info me-1"></i>All rigs</td>' +
                '<td class="fw-semibold">' + escapeHtml(j.name) + '</td>' +
                '<td><span class="badge bg-warning-glow text-warning">JUMP</span></td>' +
                '<td class="font-monospace small">' + escapeHtml(j.user + '@' + j.host + ':' + j.port) + '</td>' +
                '<td class="small text-muted">' + auth + '</td>' +
                '<td class="text-end"><div class="btn-group btn-group-sm align-items-center">' +
                '<span class="conn-dot ' + dotCls + '" id="jump-dot_' + j.id + '" title="' + dotTitle + '"></span>' +
                '<button class="btn btn-outline-info" title="Test connection" onclick="testJump(\'' + j.id + '\', this)"><i class="bi bi-plug"></i></button>' +
                '<button class="btn btn-outline-primary" title="Edit" onclick="openJumpModal(\'' + j.id + '\')"><i class="bi bi-pencil"></i></button>' +
                '<button class="btn btn-outline-danger" title="Delete" onclick="deleteJump(\'' + j.id + '\')"><i class="bi bi-trash"></i></button>' +
                '</div></td></tr>';
        }).join('');
    }
}

function renderAccessRoutes() {
    const body = document.getElementById('accessesTableBody');
    if (!clusterData || !clusterData.rigs) return;
    // Fresh data -> dots back to gray "not checked" until re-tested
    accessTestResults = {};
    const rows = [];
    clusterData.rigs.forEach(rig => {
        const rigLabel = escapeHtml(rig.name || rig.id) +
            (rig.is_self ? ' <span class="badge bg-secondary-subtle text-secondary-emphasis ms-1 small">this rig</span>' : '');
        if (!rig.accesses.length) {
            rows.push('<tr class="access-group-start"><td class="fw-semibold align-middle">' + rigLabel + '</td>' +
                '<td colspan="5" class="text-muted small">No accesses</td></tr>');
            return;
        }
        rig.accesses.forEach((a, idx) => {
            const jumpName = a.jump_id ? jumpNameById(a.jump_id) : (a.jump_host || '');
            const route = escapeHtml(a.user + '@' + a.host + ':' + a.port) +
                (a.type === 'jump' ? ' <i class="bi bi-arrow-right-short"></i> <span class="text-info">' + escapeHtml(jumpName || '?') + '</span>' : '');
            const auth = a.auth === 'key' ? '<i class="bi bi-file-earmark-key"></i> key' : '<i class="bi bi-shield-lock"></i> password';
            const dotState = accessTestResults[a.id];
            const dotCls = dotState === undefined ? '' : (dotState ? 'conn-dot-ok' : 'conn-dot-fail');
            const dotTitle = dotState === undefined ? 'Not checked yet' : (dotState ? 'Connection OK' : 'Connection failed');
            rows.push('<tr' + (idx === 0 ? ' class="access-group-start"' : '') + '>' +
                (idx === 0 ? '<td rowspan="' + rig.accesses.length + '" class="fw-semibold align-middle">' + rigLabel + '</td>' : '') +
                '<td class="fw-semibold">' + escapeHtml(a.name) + '</td>' +
                '<td><span class="badge ' + (a.type === 'jump' ? 'bg-warning-glow text-warning' : 'bg-accent-glow text-primary') + '">' + (a.type === 'jump' ? 'JUMP' : 'DIRECT') + '</span></td>' +
                '<td class="font-monospace small">' + route + '</td>' +
                '<td class="small text-muted">' + auth + '</td>' +
                '<td class="text-end">' +
                '<div class="btn-group btn-group-sm align-items-center">' +
                '<span class="conn-dot ' + dotCls + '" id="acc-dot_' + a.id + '" title="' + dotTitle + '"></span>' +
                '<button class="btn btn-outline-info" title="Test connection" onclick="testAccess(\'' + rig.id + '\', \'' + a.id + '\', this)"><i class="bi bi-plug"></i></button>' +
                '<button class="btn btn-outline-primary" title="Edit" onclick="openAccessModal(\'' + rig.id + '\', \'' + a.id + '\')"><i class="bi bi-pencil"></i></button>' +
                '<button class="btn btn-outline-danger" title="Delete" onclick="deleteAccess(\'' + rig.id + '\', \'' + a.id + '\')"><i class="bi bi-trash"></i></button>' +
                '</div></td></tr>');
        });
    });

    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-4">No SSH accesses configured yet. Click "Add Access" to create one.</td></tr>';
    } else {
        body.innerHTML = rows.join('');
    }
}

function renderAccesses() {
    if (!clusterData || !clusterData.rigs) return;
    renderJumpsTable();
    renderAccessRoutes();
}

function jumpNameById(jumpId) {
    const j = (clusterData && clusterData.jump_hosts || []).find(x => x.id === jumpId);
    return j ? j.name : '';
}

// Connection status dots (green/red) shown per row on the SSH accesses tab
let accessTestResults = {};
let jumpTestResults = {};

function paintConnDot(dotId, ok) {
    const dot = document.getElementById(dotId);
    if (!dot) return;
    dot.className = 'conn-dot ' + (ok ? 'conn-dot-ok' : 'conn-dot-fail');
    dot.title = ok ? 'Connection OK' : 'Connection failed';
}

window.checkAllAccesses = async function(btn) {
    const items = [];
    (clusterData && clusterData.rigs || []).forEach(r => (r.accesses || []).forEach(a => items.push({ rigId: r.id, access: a })));
    if (!items.length) { showToast('No SSH accesses to check.', false); return; }
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i> Checking...';
    let okCount = 0;
    for (const item of items) {
        const dotId = 'acc-dot_' + item.access.id;
        const dot = document.getElementById(dotId);
        if (dot) dot.className = 'conn-dot conn-dot-warn';
        let ok = false;
        try {
            const response = await fetch('/api/cluster/access/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ rig_id: item.rigId, access: item.access })
            });
            const data = await response.json();
            ok = !!data.success;
        } catch (e) { ok = false; }
        accessTestResults[item.access.id] = ok;
        if (ok) okCount += 1;
        paintConnDot(dotId, ok);
    }
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('Checked ' + items.length + ' access(es): ' + okCount + ' OK, ' + (items.length - okCount) + ' failed.', okCount === items.length);
};

window.checkAllJumps = async function(btn) {
    const jumps = (clusterData && clusterData.jump_hosts) || [];
    if (!jumps.length) { showToast('No jump servers to check.', false); return; }
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i> Checking...';
    let okCount = 0;
    for (const j of jumps) {
        const dotId = 'jump-dot_' + j.id;
        const dot = document.getElementById(dotId);
        if (dot) dot.className = 'conn-dot conn-dot-warn';
        let ok = false;
        try {
            const response = await fetch('/api/cluster/jump/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ jump: { id: j.id } })
            });
            const data = await response.json();
            ok = !!data.success;
        } catch (e) { ok = false; }
        jumpTestResults[j.id] = ok;
        if (ok) okCount += 1;
        paintConnDot(dotId, ok);
    }
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('Checked ' + jumps.length + ' jump server(s): ' + okCount + ' OK, ' + (jumps.length - okCount) + ' failed.', okCount === jumps.length);
};

window.testAccess = async function(rigId, accessId, btn) {
    const rig = clusterData.rigs.find(r => r.id === rigId);
    const access = rig ? rig.accesses.find(a => a.id === accessId) : null;
    if (!access) return;
    if (btn) btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
    const dot = document.getElementById('acc-dot_' + accessId);
    if (dot) { dot.className = 'conn-dot conn-dot-warn'; dot.title = 'Checking...'; }
    try {
        const response = await fetch('/api/cluster/access/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rig_id: rigId, access: access })
        });
        const data = await response.json();
        accessTestResults[accessId] = !!data.success;
        paintConnDot('acc-dot_' + accessId, !!data.success);
        showToast(data.message || (data.success ? 'OK' : 'Test failed'), !!data.success);
    } catch (e) {
        accessTestResults[accessId] = false;
        paintConnDot('acc-dot_' + accessId, false);
        showToast('Network error testing access.', false);
    } finally {
        if (btn) btn.innerHTML = '<i class="bi bi-plug"></i>';
    }
};

window.testJump = async function(jumpId, btn) {
    const jump = ((clusterData && clusterData.jump_hosts) || []).find(j => j.id === jumpId);
    if (!jump) return;
    if (btn) btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
    const dot = document.getElementById('jump-dot_' + jumpId);
    if (dot) { dot.className = 'conn-dot conn-dot-warn'; dot.title = 'Checking...'; }
    try {
        const response = await fetch('/api/cluster/jump/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ jump: { id: jumpId } })
        });
        const data = await response.json();
        jumpTestResults[jumpId] = !!data.success;
        paintConnDot('jump-dot_' + jumpId, !!data.success);
        showToast(data.message || (data.success ? 'OK' : 'Test failed'), !!data.success);
    } catch (e) {
        jumpTestResults[jumpId] = false;
        paintConnDot('jump-dot_' + jumpId, false);
        showToast('Network error testing jump server.', false);
    } finally {
        if (btn) btn.innerHTML = '<i class="bi bi-plug"></i>';
    }
};

// ---------------- Access modal ----------------

function openAccessModal(rigId, accessId) {
    editingAccess = { rigId: rigId, accessId: accessId };
    const rigSelect = document.getElementById('accessRigSelect');
    // When creating a new access, allow creating a brand-new rig inline
    const rigOptions = (clusterData && clusterData.rigs ? clusterData.rigs : [])
        .map(r => '<option value="' + r.id + '">' + escapeHtml(r.name || r.id) + '</option>').join('');
    rigSelect.innerHTML = (accessId ? '' : '<option value="__new__">+ New rig</option>') + rigOptions;

    const access = (rigId && accessId && clusterData)
        ? (clusterData.rigs.find(r => r.id === rigId) || { accesses: [] }).accesses.find(a => a.id === accessId)
        : null;

    document.getElementById('accessModalTitle').textContent = access ? 'Edit SSH Access' : 'Add SSH Access';
    document.getElementById('accessNameInput').value = access ? access.name : '';
    document.getElementById('accessHostInput').value = access ? access.host : '';
    document.getElementById('accessPortInput').value = access ? access.port : 22;
    document.getElementById('accessUserInput').value = access ? access.user : '';
    document.getElementById('accessTypeSelect').value = access ? access.type : 'direct';
    document.getElementById('accessAuthSelect').value = access ? access.auth : 'password';
    document.getElementById('accessPasswordInput').value = access && access.auth === 'password' ? (access.password || '********') : '';
    document.getElementById('accessKeyPathInput').value = access && access.key_path ? access.key_path : '';

    // Jump server dropdown from the shared library
    const jumpSelect = document.getElementById('accessJumpSelect');
    const jumps = clusterData && clusterData.jump_hosts ? clusterData.jump_hosts : [];
    jumpSelect.innerHTML = jumps.length
        ? jumps.map(j => '<option value="' + j.id + '">' + escapeHtml(j.name + ' (' + j.host + ')') + '</option>').join('')
        : '<option value="">No jump servers - add one first</option>';
    if (access && access.jump_id) jumpSelect.value = access.jump_id;

    if (rigId && access) rigSelect.value = rigId;
    else if (!accessId) rigSelect.value = (clusterData && clusterData.rigs && clusterData.rigs.length) ? clusterData.rigs[0].id : '__new__';
    document.getElementById('accessNewRigName').value = '';
    document.getElementById('accessNewRigPassword').value = '';
    toggleAccessModalFields();
    document.getElementById('accessTestResult').textContent = '';
    new bootstrap.Modal(document.getElementById('accessModal')).show();
}

window.openAccessModal = openAccessModal;

function toggleAccessModalFields() {
    const type = document.getElementById('accessTypeSelect').value;
    const auth = document.getElementById('accessAuthSelect').value;
    const isNewRig = document.getElementById('accessRigSelect').value === '__new__';
    document.getElementById('jumpSettingsBlock').classList.toggle('d-none', type !== 'jump');
    document.getElementById('accessPasswordBlock').classList.toggle('d-none', auth !== 'password');
    document.getElementById('accessKeyBlock').classList.toggle('d-none', auth !== 'key');
    document.getElementById('accessNewRigBlock').classList.toggle('d-none', !isNewRig);
}

document.getElementById('accessTypeSelect').addEventListener('change', toggleAccessModalFields);
document.getElementById('accessAuthSelect').addEventListener('change', toggleAccessModalFields);
document.getElementById('accessRigSelect').addEventListener('change', toggleAccessModalFields);

function collectAccessPayload() {
    const type = document.getElementById('accessTypeSelect').value;
    const payload = {
        id: editingAccess && editingAccess.accessId ? editingAccess.accessId : '',
        name: document.getElementById('accessNameInput').value.trim(),
        type: type,
        host: document.getElementById('accessHostInput').value.trim(),
        port: parseInt(document.getElementById('accessPortInput').value, 10) || 22,
        user: document.getElementById('accessUserInput').value.trim(),
        auth: document.getElementById('accessAuthSelect').value,
        password: document.getElementById('accessPasswordInput').value,
        key_path: document.getElementById('accessKeyPathInput').value.trim()
    };
    if (type === 'jump') {
        payload.jump_id = document.getElementById('accessJumpSelect').value;
    }
    return payload;
}

window.saveAccessModal = async function() {
    let rigId = document.getElementById('accessRigSelect').value;
    // Create a brand-new rig first when "+ New rig" is selected
    if (rigId === '__new__') {
        const newName = document.getElementById('accessNewRigName').value.trim();
        const newPw = document.getElementById('accessNewRigPassword').value.trim();
        if (!newName) { showToast('Enter the new rig name.', false); return; }
        if (newPw.length < 4) { showToast('New rig dashboard password must be at least 4 characters.', false); return; }
        try {
            const rigRes = await fetch('/api/cluster/rig', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ name: newName, password: newPw, host_label: '' })
            });
            const rigData = await rigRes.json();
            if (!(rigRes.ok && rigData.success)) {
                showToast(rigData.message || 'Failed to create the rig.', false);
                return;
            }
            rigId = rigData.rig_id;
            showToast('Rig "' + newName + '" created.', true);
        } catch (e) {
            showToast('Network error creating the rig.', false);
            return;
        }
    }
    const payload = collectAccessPayload();
    try {
        const response = await fetch('/api/cluster/access', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rig_id: rigId, access: payload })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            bootstrap.Modal.getInstance(document.getElementById('accessModal')).hide();
            loadClusterData(true).then(() => {
                if (editingRigId) renderRigModalAccesses(clusterData ? clusterData.rigs.find(r => r.id === editingRigId) : null);
            });
            loadAccessList();
        } else {
            showToast(data.message || 'Failed to save SSH access.', false);
        }
    } catch (e) {
        showToast('Network error saving SSH access.', false);
    }
};
document.getElementById('accessSaveBtn').addEventListener('click', saveAccessModal);

document.getElementById('accessTestBtn').addEventListener('click', async function() {
    const btn = this;
    const resultEl = document.getElementById('accessTestResult');
    btn.disabled = true;
    btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i> Testing...';
    resultEl.textContent = '';
    try {
        const response = await fetch('/api/cluster/access/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rig_id: document.getElementById('accessRigSelect').value, access: collectAccessPayload() })
        });
        const data = await response.json();
        resultEl.innerHTML = '<span class="' + (data.success ? 'text-success' : 'text-danger') + '">' +
            escapeHtml(data.message || (data.success ? 'Connection OK' : 'Connection failed')) + '</span>';
    } catch (e) {
        resultEl.innerHTML = '<span class="text-danger">Network error.</span>';
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plug"></i> Test Connection';
    }
});

window.deleteAccess = async function(rigId, accessId) {
    if (!confirm('Delete this SSH access?')) return;
    try {
        const response = await fetch('/api/cluster/access/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rig_id: rigId, access_id: accessId })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Deleted.' : 'Failed to delete access.'), !!data.success);
        if (data.success) {
            loadClusterData(true).then(() => {
                if (editingRigId) renderRigModalAccesses(clusterData ? clusterData.rigs.find(r => r.id === editingRigId) : null);
            });
            loadAccessList();
        }
    } catch (e) {
        showToast('Network error deleting access.', false);
    }
};

// ---------------- Jump server modal ----------------

function openJumpModal(jumpId) {
    editingJumpId = jumpId || null;
    const jump = jumpId && clusterData && clusterData.jump_hosts
        ? clusterData.jump_hosts.find(j => j.id === jumpId) : null;
    document.getElementById('jumpModalTitle').textContent = jump ? 'Edit Jump Server' : 'Add Jump Server';
    document.getElementById('jumpNameInput').value = jump ? jump.name : '';
    document.getElementById('jumpServerHostInput').value = jump ? jump.host : '';
    document.getElementById('jumpServerPortInput').value = jump ? jump.port : 22;
    document.getElementById('jumpServerUserInput').value = jump ? jump.user : '';
    document.getElementById('jumpServerAuthSelect').value = jump ? jump.auth : 'password';
    document.getElementById('jumpServerPasswordInput').value = jump && jump.auth === 'password' ? (jump.password || '********') : '';
    document.getElementById('jumpServerKeyPathInput').value = jump && jump.key_path ? jump.key_path : '';
    document.getElementById('jumpServerPasswordBlock').classList.toggle('d-none', document.getElementById('jumpServerAuthSelect').value !== 'password');
    document.getElementById('jumpServerKeyBlock').classList.toggle('d-none', document.getElementById('jumpServerAuthSelect').value !== 'key');
    new bootstrap.Modal(document.getElementById('jumpModal')).show();
}
window.openJumpModal = openJumpModal;

async function saveJumpModal() {
    const payload = {
        jump: {
            id: editingJumpId || '',
            name: document.getElementById('jumpNameInput').value.trim(),
            host: document.getElementById('jumpServerHostInput').value.trim(),
            port: parseInt(document.getElementById('jumpServerPortInput').value, 10) || 22,
            user: document.getElementById('jumpServerUserInput').value.trim(),
            auth: document.getElementById('jumpServerAuthSelect').value,
            password: document.getElementById('jumpServerPasswordInput').value,
            key_path: document.getElementById('jumpServerKeyPathInput').value.trim()
        }
    };
    try {
        const response = await fetch('/api/cluster/jump', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            bootstrap.Modal.getInstance(document.getElementById('jumpModal')).hide();
            loadClusterData(true);
            loadAccessList();
        } else {
            showToast(data.message || 'Failed to save jump server.', false);
        }
    } catch (e) {
        showToast('Network error saving jump server.', false);
    }
}

window.deleteJump = async function(jumpId) {
    if (!confirm('Delete this jump server? Accesses referencing it will fall back to stored settings.')) return;
    try {
        const response = await fetch('/api/cluster/jump/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ jump_id: jumpId })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Deleted.' : 'Failed to delete jump server.'), !!data.success);
        if (data.success) {
            loadClusterData(true);
            loadAccessList();
        }
    } catch (e) {
        showToast('Network error deleting jump server.', false);
    }
};

// ---------------- Flight sheets & wallets (cloud-style) ----------------

// Bundled HiveOS catalogs (miners with N/A/C platforms, pools per coin, coins)
const CATALOG = { miners: [], minerById: {}, pools: [], coins: [], loaded: false, promise: null };

async function loadCatalog() {
    if (CATALOG.loaded) return CATALOG;
    if (!CATALOG.promise) {
        CATALOG.promise = Promise.all([
            fetch('static/data/hive-miners.json').then(r => r.json()),
            fetch('static/data/hive-pools.json').then(r => r.json()),
            fetch('static/data/hive-coins.json').then(r => r.json())
        ]).then(([m, p, c]) => {
            CATALOG.miners = (m.miners || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
            CATALOG.minerById = {};
            CATALOG.miners.forEach(x => { CATALOG.minerById[x.id] = x; });
            CATALOG.pools = p.pools || [];
            CATALOG.coins = c.coins || [];
            CATALOG.loaded = true;
            fillCoinDatalists();
            rebuildFsItemsContainer();
        }).catch(() => { CATALOG.promise = null; });
    }
    await CATALOG.promise;
    return CATALOG;
}

function fillCoinDatalists() {
    const dl = document.getElementById('walletCoinList');
    if (dl) dl.innerHTML = CATALOG.coins.map(c => '<option value="' + escapeHtml(c) + '"></option>').join('');
}

function minerBadgesHtml(minerId) {
    let m = CATALOG.minerById[minerId];
    if (!m && /_custom$/.test(minerId)) m = CATALOG.minerById[minerId.replace(/_custom$/, '')];
    if (!m) return '';
    let out = '';
    if (m.nvidia) out += '<span class="fs-badge n" title="NVIDIA">N</span>';
    if (m.amd) out += '<span class="fs-badge a" title="AMD">A</span>';
    if (m.cpu) out += '<span class="fs-badge c" title="CPU">C</span>';
    return out;
}

function coinHue(ticker) {
    let h = 0;
    const s = String(ticker || '?');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return h;
}

function coinAvatarHtml(coin, extraCls) {
    const c = String(coin || '').trim().toUpperCase();
    const label = c ? escapeHtml(c.slice(0, 3)) : '—';
    return '<span class="coin-av ' + (extraCls || '') + '" style="--h:' + coinHue(c) + '" title="' + escapeHtml(c || 'no coin') + '">' + label + '</span>';
}

// ---------------- Flight sheet builder (HiveOS worker page style) ----------------

// Hive: the Wallet dropdown lists the wallets from the Wallets page (all of them)
function walletOptionsHtml(selected) {
    const wallets = (window._fsData && window._fsData.wallets) || [];
    const isRef = wallets.some(w => w.id === selected);
    const isCustom = !!selected && !isRef;
    let opts = '<option value=""' + (!selected ? ' selected' : '') + '>Select wallet...</option>';
    opts += wallets.map(w =>
        '<option value="' + escapeHtml(w.id) + '"' + (selected === w.id ? ' selected' : '') + '>' +
        escapeHtml(w.name) + (w.coin ? ' · ' + escapeHtml(w.coin) : '') + '</option>').join('');
    opts += '<option value="__custom__"' + (isCustom ? ' selected' : '') + '>Custom address...</option>';
    return opts;
}

function minerOptionsHtml(selected) {
    const sel = selected || 'none';
    let opts = '<option value="none"' + (sel === 'none' ? ' selected' : '') + '>Select miner...</option>';
    opts += CATALOG.miners.map(m =>
        '<option value="' + escapeHtml(m.id) + '"' + (sel === m.id ? ' selected' : '') + '>' +
        escapeHtml(m.name) + '</option>').join('');
    const known = CATALOG.minerById[sel];
    if (sel && !known && sel !== 'custom' && sel !== 'none') {
        opts = '<option value="' + escapeHtml(sel) + '" selected>' + escapeHtml(sel) + '</option>' + opts;
    }
    opts += '<option value="custom"' + (sel === 'custom' ? ' selected' : '') + '>Custom miner...</option>';
    return opts;
}

// Miner config values live in hidden inputs inside the row; the Setup modal edits them
function builderRowHtml(idx, item) {
    item = item || {};
    const isRef = ((window._fsData && window._fsData.wallets) || []).some(w => w.id === item.wallet);
    const removable = idx > 0;
    const showCustom = !!item.wallet && !isRef;
    const cfgVal = (v, def) => v ? escapeHtml(v) : escapeHtml(def || '');
    return '<div class="fs-item-row' + (removable ? ' has-remove' : '') + '" data-idx="' + idx + '">' +
        (removable ? '<button type="button" class="btn btn-outline-danger btn-sm fs-item-remove" data-action="remove-item" title="Remove this miner item">&times;</button>' : '') +
        '<div class="fs-grid">' +
            '<div class="fs-cell-num"><span class="fs-item-num">' + (idx + 1) + '</span></div>' +
            '<div class="fs-field">' +
                '<label class="fs-flabel">Coin</label>' +
                '<div class="fs-coin-wrap">' +
                    '<span class="coin-av fs-coin-av" style="--h:' + coinHue(item.coin) + '">' + escapeHtml((item.coin || '').slice(0, 3) || '—') + '</span>' +
                    '<input type="text" class="form-control form-control-sm bg-dark-input text-white border-secondary-subtle text-uppercase fs-coin" list="walletCoinList" placeholder="Coin" value="' + escapeHtml(item.coin || '') + '">' +
                '</div>' +
            '</div>' +
            '<div class="fs-field">' +
                '<label class="fs-flabel">Wallet</label>' +
                '<div class="fs-inline">' +
                    '<select class="form-select form-select-sm bg-dark-input text-white border-secondary-subtle fs-wallet">' + walletOptionsHtml(item.wallet) + '</select>' +
                    '<button type="button" class="btn btn-sm btn-outline-primary flex-shrink-0" data-action="wallet-add" title="Add a new wallet to the library">Add</button>' +
                '</div>' +
                '<input type="text" class="form-control form-control-sm bg-dark-input text-white border-secondary-subtle font-monospace mt-1 fs-wallet-custom' + (showCustom ? '' : ' d-none') + '" placeholder="Custom wallet address" value="' + escapeHtml(showCustom ? item.wallet : '') + '">' +
            '</div>' +
            '<div class="fs-field">' +
                '<label class="fs-flabel">Pool</label>' +
                '<input type="text" class="form-control form-control-sm bg-dark-input text-white border-secondary-subtle font-monospace fs-pool" list="fsPoolList" placeholder="pool:port" value="' + escapeHtml(item.pool || '') + '">' +
            '</div>' +
            '<div class="fs-field">' +
                '<label class="fs-flabel">Miner</label>' +
                '<div class="fs-inline">' +
                    '<select class="form-select form-select-sm bg-dark-input text-white border-secondary-subtle fs-miner">' + minerOptionsHtml(item.miner) + '</select>' +
                    '<button type="button" class="btn btn-sm btn-outline-secondary flex-shrink-0" data-action="miner-setup" title="Miner setup">Setup</button>' +
                '</div>' +
            '</div>' +
        '</div>' +
        // hidden miner-config storage edited through the Setup modal
        '<div class="fs-cfg-data d-none">' +
            '<input type="text" class="fs-alt" value="' + cfgVal(item.miner_alt) + '">' +
            '<input type="text" class="fs-url" value="' + cfgVal(item.install_url) + '">' +
            '<input type="text" class="fs-algo" value="' + cfgVal(item.algo) + '">' +
            '<input type="text" class="fs-template" value="' + cfgVal(item.template, '%WAL%.%WORKER_NAME%') + '">' +
            '<input type="text" class="fs-pass" value="' + cfgVal(item.pass, 'x') + '">' +
            '<textarea class="fs-uc">' + escapeHtml(item.user_config || '') + '</textarea>' +
        '</div>' +
        '<div class="fs-cfg-summary small text-muted d-none"></div>' +
    '</div>';
}

function updateFsCfgSummary(row) {
    if (!row) return;
    const el = row.querySelector('.fs-cfg-summary');
    if (!el) return;
    const get = (cls) => { const i = row.querySelector(cls); return i ? i.value.trim() : ''; };
    const miner = row.querySelector('.fs-miner').value;
    const alt = get('.fs-alt'), algo = get('.fs-algo'), url = get('.fs-url'), uc = get('.fs-uc');
    const parts = [];
    if (alt) parts.push(alt);
    if (algo) parts.push(algo);
    let text = '';
    if (miner === 'custom') {
        text = 'Custom config' + (parts.length ? ' — ' + parts.join(' · ') : ' — press Setup to edit');
    } else if (parts.length || url || uc) {
        text = 'Miner config' + (parts.length ? ' — ' + parts.join(' · ') : ' — press Setup to edit');
    }
    if (text) {
        el.innerHTML = '<i class="bi bi-gear-fill me-1"></i>' + escapeHtml(text);
        el.classList.remove('d-none');
    } else {
        el.classList.add('d-none');
    }
}

function rebuildFsItemsContainer() {
    const container = document.getElementById('fsItemsContainer');
    if (!container) return;
    const rows = window._fsBuilderItems || [{}];
    container.innerHTML = rows.map((it, i) => builderRowHtml(i, it)).join('');
    container.querySelectorAll('.fs-item-row').forEach(updateFsCfgSummary);
    updatePoolDatalist('');
}

function builderAddRow(item) {
    const rows = window._fsBuilderItems || (window._fsBuilderItems = [{}]);
    if (rows.length >= 5) { showToast('Up to 5 miner items per flight sheet.', false); return; }
    rows.push(item || {});
    rebuildFsItemsContainer();
    const container = document.getElementById('fsItemsContainer');
    const last = container.lastElementChild;
    if (last) last.querySelector('.fs-coin').focus();
}

function resetFsheetBuilder() {
    window._fsBuilderItems = [{}];
    window._editingFsheetId = null;
    document.getElementById('fsName').value = '';
    document.getElementById('fsCreateBtn').textContent = 'Add';
    rebuildFsItemsContainer();
}

// "Add flight sheet": show the (empty) Hive builder and scroll to it
function openFsBuilder() {
    const form = document.getElementById('saveFsheetForm');
    form.classList.remove('d-none');
    resetFsheetBuilder();
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    const coin = form.querySelector('.fs-coin');
    if (coin) coin.focus();
}

function closeFsBuilder() {
    document.getElementById('saveFsheetForm').classList.add('d-none');
    resetFsheetBuilder();
}

function updatePoolDatalist(coinFilter) {
    const dl = document.getElementById('fsPoolList');
    if (!dl) return;
    let coin = coinFilter;
    if (typeof coinFilter !== 'string') {
        const active = document.activeElement;
        const row = active && active.classList.contains('fs-pool') ? active.closest('.fs-item-row') : null;
        coin = row ? (row.querySelector('.fs-coin').value.trim().toUpperCase()) : '';
    }
    let pools = CATALOG.pools;
    if (coin) {
        const matched = pools.filter(p => (p.coins || []).some(c => String(c).toUpperCase() === coin));
        if (matched.length) pools = matched;
    }
    dl.innerHTML = pools.slice(0, 400).map(p => '<option value="' + escapeHtml(p.name) + '"></option>').join('');
}

function collectBuilderItems(root) {
    const scope = root || document;
    const items = [];
    scope.querySelectorAll('.fs-item-row').forEach(row => {
        const walletSel = row.querySelector('.fs-wallet').value;
        const wallet = walletSel === '__custom__' ? row.querySelector('.fs-wallet-custom').value.trim() : walletSel;
        const miner = row.querySelector('.fs-miner').value;
        const item = {
            coin: row.querySelector('.fs-coin').value.trim().toUpperCase(),
            wallet: wallet,
            pool: row.querySelector('.fs-pool').value.trim(),
            miner: miner || 'none',
            miner_alt: '', install_url: '', algo: '', user_config: '', template: '', pass: ''
        };
        const cfg = row.querySelector('.fs-cfg-data');
        if (cfg) {
            item.miner_alt = cfg.querySelector('.fs-alt').value.trim().toLowerCase();
            item.install_url = cfg.querySelector('.fs-url').value.trim();
            item.algo = cfg.querySelector('.fs-algo').value.trim().toLowerCase();
            item.user_config = cfg.querySelector('.fs-uc').value.trim();
            const tpl = cfg.querySelector('.fs-template').value.trim();
            const pass = cfg.querySelector('.fs-pass').value.trim();
            item.template = tpl === '%WAL%.%WORKER_NAME%' ? '' : tpl;
            item.pass = pass === 'x' ? '' : pass;
        }
        items.push(item);
    });
    return items;
}

// Hive requires coin / wallet / pool in every miner item
function validateFsItems(items) {
    for (let i = 0; i < items.length; i++) {
        const it = items[i];
        const n = items.length > 1 ? (' #' + (i + 1)) : '';
        if (!it.coin) return 'Coin is required in miner item' + n + '.';
        if (!it.wallet) return 'Wallet is required in miner item' + n + '.';
        if (!it.pool) return 'Pool is required in miner item' + n + '.';
        if (it.miner === 'none') return 'Select a miner in miner item' + n + '.';
    }
    return '';
}

async function saveFsheetFromBuilder(e) {
    e.preventDefault();
    const items = collectBuilderItems(document.getElementById('fsItemsContainer'));
    if (!items.length) { showToast('Add at least one miner item.', false); return; }
    const name = document.getElementById('fsName').value.trim();
    if (!name) { showToast('Flight sheet name is required.', false); return; }
    const err = validateFsItems(items);
    if (err) { showToast(err, false); return; }
    const payload = {
        fsheet: {
            id: window._editingFsheetId || '',
            name: name,
            coin: items[0].coin,
            items: items
        }
    };
    const submitBtn = document.getElementById('fsCreateBtn');
    submitBtn.disabled = true;
    try {
        const response = await apiFetch('/api/fsheets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Flight sheet saved.' : 'Failed to save flight sheet.'), !!data.success);
        if (data.success) {
            closeFsBuilder();
            loadFsheets();
        }
    } catch (error) {
        showToast("Network error saving flight sheet.", false);
    } finally {
        submitBtn.disabled = false;
    }
}

// ---- shared per-row behavior (used by the top builder and inline edit panels) ----

function updateFsCoinUi(coinInput) {
    const row = coinInput.closest('.fs-item-row');
    if (!row) return;
    const coin = coinInput.value.trim().toUpperCase();
    const av = row.querySelector('.fs-coin-av');
    if (av) {
        av.style.setProperty('--h', coinHue(coin));
        av.textContent = coin.slice(0, 3) || '—';
    }
    updatePoolDatalist(coin);
}

// Refresh every Wallet dropdown in place (called after the wallet library changes,
// keeping the current selections)
function refreshFsWalletOptions() {
    document.querySelectorAll('.fs-wallet').forEach(sel => {
        const cur = sel.value;
        const isRef = ((window._fsData && window._fsData.wallets) || []).some(w => w.id === cur);
        sel.innerHTML = walletOptionsHtml(cur);
        if (isRef) sel.value = cur;
        const row = sel.closest('.fs-item-row');
        const custom = row && row.querySelector('.fs-wallet-custom');
        if (custom) custom.classList.toggle('d-none', sel.value !== '__custom__');
    });
}

function onFsRowChange(e) {
    const row = e.target.closest('.fs-item-row');
    if (!row) return;
    if (e.target.classList.contains('fs-wallet')) {
        const custom = row.querySelector('.fs-wallet-custom');
        if (custom) custom.classList.toggle('d-none', e.target.value !== '__custom__');
    } else if (e.target.classList.contains('fs-miner')) {
        updateFsCfgSummary(row);
    }
}

function onFsRowInput(e) {
    if (e.target.classList && e.target.classList.contains('fs-coin')) updateFsCoinUi(e.target);
}

// Miner Setup modal (Hive custom config form) — edits the hidden cfg inputs of a row
let _fsMinerCfgRow = null;

function openFsMinerCfgModal(row) {
    if (!row) return;
    _fsMinerCfgRow = row;
    const get = (cls, def) => { const i = row.querySelector(cls); return i ? i.value : (def || ''); };
    document.getElementById('fsmMinerName').value = get('.fs-alt');
    document.getElementById('fsmInstallUrl').value = get('.fs-url');
    document.getElementById('fsmAlgo').value = get('.fs-algo');
    document.getElementById('fsmTemplate').value = get('.fs-template', '%WAL%.%WORKER_NAME%');
    document.getElementById('fsmPool').value = get('.fs-pool');
    document.getElementById('fsmPass').value = get('.fs-pass', 'x');
    document.getElementById('fsmUserConfig').value = get('.fs-uc');
    const miner = row.querySelector('.fs-miner').value;
    const m = CATALOG.minerById[miner];
    document.getElementById('fsMinerCfgTitle').innerHTML =
        '<i class="bi bi-gear-fill text-warning me-2"></i>Miner setup' + (m ? ' — ' + escapeHtml(m.name) : '');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('fsMinerCfgModal')).show();
}

function applyFsMinerCfgFromModal() {
    const row = _fsMinerCfgRow;
    if (row && document.body.contains(row)) {
        const set = (sel, id) => { const el = row.querySelector(sel); if (el) el.value = document.getElementById(id).value; };
        set('.fs-alt', 'fsmMinerName');
        set('.fs-url', 'fsmInstallUrl');
        set('.fs-algo', 'fsmAlgo');
        set('.fs-template', 'fsmTemplate');
        set('.fs-pass', 'fsmPass');
        set('.fs-uc', 'fsmUserConfig');
        const pool = row.querySelector('.fs-pool');
        if (pool) pool.value = document.getElementById('fsmPool').value;
        updateFsCfgSummary(row);
    }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('fsMinerCfgModal')).hide();
}

function clearFsMinerCfgModal() {
    document.getElementById('fsmMinerName').value = '';
    document.getElementById('fsmInstallUrl').value = '';
    document.getElementById('fsmAlgo').value = '';
    document.getElementById('fsmTemplate').value = '%WAL%.%WORKER_NAME%';
    document.getElementById('fsmPass').value = 'x';
    document.getElementById('fsmUserConfig').value = '';
}

// Hive 'Clear': reset the miner config of one miner item to defaults
function clearFsExpertFields(row) {
    if (!row) return;
    const set = (cls, val) => { const el = row.querySelector(cls); if (el) el.value = val; };
    set('.fs-alt', ''); set('.fs-url', ''); set('.fs-algo', ''); set('.fs-uc', '');
    set('.fs-template', '%WAL%.%WORKER_NAME%'); set('.fs-pass', 'x');
    updateFsCfgSummary(row);
}

function renumberFsRows(scope) {
    scope.querySelectorAll('.fs-item-row').forEach((row, i) => {
        row.dataset.idx = i;
        const num = row.querySelector('.fs-item-num');
        if (num) num.textContent = String(i + 1);
        const rem = row.querySelector('[data-action="remove-item"]');
        if (rem) rem.classList.toggle('d-none', i === 0);
    });
}

async function loadFsheets() {
    try {
        const response = await apiFetch('/api/fsheets');
        const data = await response.json();
        if (!data.success) return;
        window._fsData = {
            fsheets: data.fsheets || [],
            wallets: data.wallets || [],
            active: data.active || {},
            rig_config: data.rig_config || {}
        };
        // The builder opens empty via "Add flight sheet" (no live-config prefill);
        // keep the wallet dropdowns in sync with the library
        refreshFsWalletOptions();
        // While an inline editor is open, keep its unsaved edits on screen
        if (!window._fsExpandedId) renderFsheets();
    } catch (e) {
        console.error('Failed to load flight sheets:', e);
    }
}

function resolveItemWallet(item, wallets) {
    if (!item) return '';
    const w = (wallets || []).find(x => x.id === item.wallet);
    return w ? w.address : (item.wallet || '');
}

function fsMatchesLive(f, wallets, active, rc) {
    // Applied sheets are attached by id (FS_ID in wallet.conf, like the Hive worker FS);
    // fall back to the name marker, then to the live config comparison
    if (active.fs_id) return String(f.id || '') === String(active.fs_id);
    if (active.fs_name) return String(f.name || '') === String(active.fs_name);
    const it = (f.items && f.items[0]) || {};
    const liveCoin = String(active.coin || rc.coin || '').toLowerCase();
    const liveWallet = active.wallet || rc.wallet || '';
    const livePool = active.pool || rc.pool || '';
    const liveMiner = String(active.miner || rc.miner || '').toLowerCase();
    return liveCoin !== '' &&
        String(it.coin || '').toLowerCase() === liveCoin &&
        String(it.pool || '') === livePool &&
        String(it.miner || '').toLowerCase() === liveMiner &&
        resolveItemWallet(it, wallets) === liveWallet;
}

function renderFsheets() {
    const container = document.getElementById('fsheetsContainer');
    const data = window._fsData;
    if (!data) return;
    const wallets = data.wallets;
    const active = data.active;
    const rc = data.rig_config || {};

    // Live rig config shown as a pseudo-row when it does not match any saved sheet
    const rcHasContent = rc.wallet || rc.pool;
    const anyMatch = data.fsheets.some(f => fsMatchesLive(f, wallets, active, rc));
    let entries = data.fsheets.map(f => ({ f: f, applied: fsMatchesLive(f, wallets, active, rc), live: false }));
    if (rcHasContent && !anyMatch) {
        entries.unshift({ f: {
            id: '__rig__', name: rc.name || 'Current mining config',
            coin: rc.coin || '?', items: [{ coin: rc.coin || '', wallet: rc.wallet || '', pool: rc.pool || '—', miner: rc.miner || 'none' }],
            fav: false
        }, applied: true, live: true });
    }

    renderFsChips(entries);

    // Chip filters (coins / wallets / pools)
    const chips = window._fsChips || {};
    const shown = entries.filter(e => fsMatchesChips(e, chips));
    // Favorites first, then by name
    shown.sort((a, b) => (b.f.fav ? 1 : 0) - (a.f.fav ? 1 : 0) ||
        String(a.f.name).localeCompare(String(b.f.name)));

    const counter = document.getElementById('fsheetsCount');
    if (counter) counter.textContent = shown.length + ' of ' + entries.length + ' shown';

    if (!shown.length) {
        container.innerHTML = '<div class="text-center text-muted small py-3">' +
            (entries.length ? 'No flight sheets match this filter.' : 'No flight sheets yet. Build one above or import.') + '</div>';
        return;
    }

    container.innerHTML = shown.map(fsRowHtml).join('');
}

function fsMatchesChips(entry, chips) {
    if (!chips) return true;
    const wallets = (window._fsData && window._fsData.wallets) || [];
    const items = entry.f.items || [];
    if (chips.coin &&
        !items.some(x => String(x.coin || '').toUpperCase() === chips.coin) &&
        String(entry.f.coin || '').toUpperCase() !== chips.coin) return false;
    if (chips.wallet && !items.some(x => walletAddressLabel(x.wallet, wallets) === chips.wallet)) return false;
    if (chips.pool && !items.some(x => String(x.pool || '') === chips.pool)) return false;
    return true;
}

// Chip filter rows: coins / wallets / pools used by the sheets (Hive cloud style)
function renderFsChips(entries) {
    const boxes = {
        coin: document.getElementById('fsChipsCoins'),
        wallet: document.getElementById('fsChipsWallets'),
        pool: document.getElementById('fsChipsPools')
    };
    const wallets = (window._fsData && window._fsData.wallets) || [];
    const byCoin = {}, byWallet = {}, byPool = {};
    entries.forEach(({ f }) => {
        const items = f.items || [];
        new Set(items.map(x => String(x.coin || '').toUpperCase()).filter(Boolean)
            .concat(f.coin ? [String(f.coin).toUpperCase()] : []))
            .forEach(c => { byCoin[c] = (byCoin[c] || 0) + 1; });
        const it = items[0] || {};
        if (it.wallet) {
            const l = walletAddressLabel(it.wallet, wallets);
            if (l) byWallet[l] = (byWallet[l] || 0) + 1;
        }
        if (it.pool) byPool[it.pool] = (byPool[it.pool] || 0) + 1;
    });
    window._fsChipsExp = window._fsChipsExp || {};
    window._fsChips = window._fsChips || {};
    const chip = (kind, value, label, count, av) =>
        '<button type="button" class="chip-btn' + (window._fsChips[kind] === value ? ' active' : '') + '" data-chip="' + kind + '" data-value="' + escapeHtml(value) + '" title="Filter flight sheets">' +
        (av || '') + '<span>' + escapeHtml(label) + '</span>' + (count !== undefined ? ' <span class="chip-count">' + count + '</span>' : '') + '</button>';
    const fill = (box, kind, dict, avFn, limit, moreLabel) => {
        if (!box) return;
        const keys = Object.keys(dict).sort();
        if (!keys.length) { box.innerHTML = ''; return; }
        let shown = keys;
        let html = '';
        if (keys.length > limit && !window._fsChipsExp[kind]) shown = keys.slice(0, limit);
        html = shown.map(k => chip(kind, k, k, dict[k], avFn ? avFn(k) : '')).join('');
        if (keys.length > shown.length) {
            html += '<button type="button" class="chip-btn" data-chip="' + kind + '" data-action="more" title="Show all">… ' + (keys.length - shown.length) + ' ' + moreLabel + '</button>';
        }
        box.innerHTML = html;
    };
    fill(boxes.coin, 'coin', byCoin, c => coinAvatarHtml(c), 10, 'more coins');
    fill(boxes.wallet, 'wallet', byWallet, null, 8, 'more wallets');
    fill(boxes.pool, 'pool', byPool, null, 8, 'more pools');
}

// One flight sheet row (Hive worker page style).
// Collapsed by default; a click (chevron or the row itself) expands the detailed
// card — wallet address with copy, pool URLs, miner config details. Several rows
// can be expanded at once. Edit (kebab) opens the inline editor panel instead.
function fsRowHtml({ f, applied, live }) {
    const items = (f.items && f.items.length ? f.items : [{}]);
    const wallets = (window._fsData && window._fsData.wallets) || [];
    const coins = Array.from(new Set(items.map(x => String(x.coin || '').toUpperCase()).filter(Boolean)));
    const it0 = items[0] || {};
    const fid = escapeHtml(f.id);
    const extra = items.length > 1 ? ' <span class="fs-multi" title="' + items.length + ' miner items">+' + (items.length - 1) + '</span>' : '';
    const infoOpen = !!(window._fsInfoOpen && window._fsInfoOpen.has(f.id));
    const editOpen = window._fsExpandedId === f.id;
    const star = live ? '' :
        '<button type="button" class="fs-star' + (f.fav ? ' on' : '') + '" data-action="fav" data-id="' + fid + '" title="To favorites">' +
        '<i class="bi ' + (f.fav ? 'bi-star-fill' : 'bi-star') + '"></i></button>';
    const walletLabel = it0.wallet ? walletAddressLabel(it0.wallet, wallets) : 'Configured in miner';
    const poolLabel = it0.pool || 'Configured in miner';
    const minerLabel = (it0.miner && it0.miner !== 'none') ? it0.miner : 'none';
    const run = '<button type="button" class="fs-run' + (applied ? ' active' : '') + '" data-action="apply" data-id="' + fid + '" title="' + (applied ? 'Re-apply this flight sheet' : 'Run this flight sheet') + '">' +
        '<i class="bi bi-rocket-takeoff' + (applied ? '-fill' : '') + '"></i></button>';
    const chevron = '<button type="button" class="fs-details' + (infoOpen ? ' open' : '') + '" data-action="info" data-id="' + fid + '" title="Details">' +
        '<i class="bi bi-chevron-down"></i></button>';
    // Kebab menu: live rig config vs saved sheet (active sheets have no Delete)
    let kebabItems;
    if (live) {
        kebabItems =
            '<li><button type="button" class="dropdown-item" data-action="edit" data-id="' + fid + '"><i class="bi bi-pencil me-2"></i>Edit</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="duplicate" data-id="' + fid + '"><i class="bi bi-files me-2"></i>Duplicate</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="export" data-id="' + fid + '"><i class="bi bi-download me-2"></i>Export</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="copy" data-id="' + fid + '"><i class="bi bi-clipboard me-2"></i>To clipboard</button></li>' +
            '<li><hr class="dropdown-divider"></li>' +
            '<li><button type="button" class="dropdown-item text-warning" data-action="unset"><i class="bi bi-eject me-2"></i>Unset</button></li>';
    } else {
        const del = applied
            ? '<li><button type="button" class="dropdown-item text-warning" data-action="unset"><i class="bi bi-eject me-2"></i>Unset</button></li>'
            : '<li><button type="button" class="dropdown-item text-danger" data-action="delete" data-id="' + fid + '"><i class="bi bi-trash me-2"></i>Delete</button></li>';
        kebabItems =
            '<li><button type="button" class="dropdown-item" data-action="edit" data-id="' + fid + '"><i class="bi bi-pencil me-2"></i>Edit</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="duplicate" data-id="' + fid + '"><i class="bi bi-files me-2"></i>Duplicate</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="export" data-id="' + fid + '"><i class="bi bi-download me-2"></i>Export</button></li>' +
            '<li><button type="button" class="dropdown-item" data-action="copy" data-id="' + fid + '"><i class="bi bi-clipboard me-2"></i>To clipboard</button></li>' +
            '<li><hr class="dropdown-divider"></li>' + del;
    }
    const kebab = '<div class="dropdown">' +
        '<button type="button" class="fs-kebab" data-bs-toggle="dropdown" aria-expanded="false" title="More actions"><i class="bi bi-three-dots-vertical"></i></button>' +
        '<ul class="dropdown-menu dropdown-menu-end">' + kebabItems + '</ul>' +
        '</div>';

    let html = '<div class="fsheet-row2 fs-row' + (applied ? ' is-active' : '') + '">' +
        '<div class="fs-row-left" data-action="info" data-id="' + fid + '" role="button">' +
            '<div class="fs-row-coins">' +
                (coins.length
                    ? coins.map(c => coinAvatarHtml(c) + '<span class="fs-ticker">' + escapeHtml(c) + '</span>').join('<span class="fs-plus">+</span>')
                    : coinAvatarHtml('') + '<span class="fs-ticker text-muted">—</span>') +
            '</div>' +
            '<div class="fs-row-info min-w-0">' +
                '<div class="fs-info-line fw-semibold text-truncate" title="' + escapeHtml(walletLabel) + '">' + escapeHtml(walletLabel) + '</div>' +
                '<div class="fs-info-line text-muted text-truncate" title="' + escapeHtml(poolLabel) + '">' + escapeHtml(poolLabel) + '</div>' +
                '<div class="fs-info-line text-truncate"><span class="font-monospace small">' + escapeHtml(minerLabel) + '</span>' + minerBadgesHtml(it0.miner) + '</div>' +
            '</div>' +
        '</div>' +
        '<div class="fs-row-right">' +
            '<div class="fs-name text-end text-truncate">' + escapeHtml(f.name) +
                (live ? ' <span class="badge bg-secondary small" title="Running from the rig config, not saved in the library">live</span>' : '') +
                extra +
            '</div>' +
            '<div class="fs-actions d-flex gap-1 align-items-center justify-content-end">' + run + chevron + star + kebab + '</div>' +
        '</div>' +
    '</div>';

    // Expanded info card (Hive details view) — available for every sheet
    if (infoOpen) {
        const walletW = resolveItemWallet(it0, wallets);
        const walletName = live ? 'Configured in miner' : (it0.wallet ? walletAddressLabel(it0.wallet, wallets) : 'Configured in miner');
        const isCustom = it0.miner === 'custom';
        const tpl = it0.template || '%WAL%.%WORKER_NAME%';
        const pass = it0.pass || 'x';
        const details = [];
        if (it0.pool) details.push(['Url', it0.pool]);
        if (it0.algo) details.push(['Algo', it0.algo]);
        if (isCustom || it0.pass) details.push(['Pass', pass]);
        details.push(['Miner', it0.miner_alt || minerLabel]);
        if (isCustom) details.push(['Template', tpl]);
        if (it0.install_url) details.push(['Install Url', it0.install_url]);
        if (it0.user_config) details.push(['User Config', it0.user_config]);
        const detailsHtml = details.map(([k, v]) =>
            '<div class="fs-act-line"><span class="fs-act-k">' + k + '</span> <span class="fs-act-v font-monospace">' + escapeHtml(v) + '</span></div>'
        ).join('');
        html += '<div class="fs-info-card">' +
            '<div class="fs-active-grid">' +
                '<div class="fs-act-col fs-act-coins">' +
                    (coins.length
                        ? coins.map(c => coinAvatarHtml(c) + '<span class="fs-ticker">' + escapeHtml(c) + '</span>').join('<span class="fs-plus">+</span>')
                        : coinAvatarHtml('') + '<span class="fs-ticker text-muted">—</span>') +
                '</div>' +
                '<div class="fs-act-col min-w-0">' +
                    '<div class="fs-act-name text-truncate">' + escapeHtml(walletName) + extra + '</div>' +
                    '<div class="fs-act-label">Wallet address</div>' +
                    (walletW
                        ? '<div class="fs-act-val"><span class="font-monospace">' + escapeHtml(walletW) + '</span>' +
                          '<button type="button" class="fs-copy" data-action="copy-wallet" data-wallet="' + escapeHtml(walletW) + '" title="Copy wallet address"><i class="bi bi-clipboard"></i></button></div>'
                        : '<div class="fs-act-val text-muted">—</div>') +
                '</div>' +
                '<div class="fs-act-col min-w-0">' +
                    '<div class="fs-act-label">' + (it0.pool ? 'Pool URLs' : 'Configured in miner') + '</div>' +
                    (it0.pool
                        ? '<div class="fs-act-val"><span class="font-monospace">' + escapeHtml(it0.pool) + '</span></div>'
                        : '<div class="fs-act-val text-muted">Configured in miner</div>') +
                '</div>' +
                '<div class="fs-act-col min-w-0">' +
                    '<div class="fs-act-name text-truncate"><span class="font-monospace">' + escapeHtml(it0.miner_alt || minerLabel) +
                        (isCustom ? ' (c)' : '') + '</span>' + minerBadgesHtml(it0.miner) +
                    '</div>' +
                    (details.length ? '<div class="fs-act-details">' + detailsHtml + '</div>' : '') +
                '</div>' +
            '</div>' +
            '<div class="fs-active-foot">' + escapeHtml(f.name) + '</div>' +
        '</div>';
    }

    // Inline editor panel (kebab -> Edit); not for the live rig config (it is not a saved sheet)
    if (editOpen && !live) html += fsExpandHtml(f);
    return html;
}

// Inline edit panel revealed by the Details chevron (Hive expands the row in place)
function fsExpandHtml(f) {
    const fid = escapeHtml(f.id);
    const items = (f.items && f.items.length ? f.items : [{}]);
    return '<div class="fs-expand" data-expand="' + fid + '">' +
        '<div class="fs-expand-items">' + items.map((it, i) => builderRowHtml(i, it)).join('') + '</div>' +
        '<div class="d-flex justify-content-end gap-2 mt-2">' +
            '<button type="button" class="btn btn-sm btn-outline-secondary" data-action="expand-clear" data-id="' + fid + '">Clear</button>' +
            '<button type="button" class="btn btn-sm btn-outline-secondary" data-action="expand-cancel" data-id="' + fid + '">Cancel</button>' +
            '<button type="button" class="btn btn-sm btn-warning fw-semibold px-3" data-action="expand-save" data-id="' + fid + '">Apply changes</button>' +
        '</div>' +
    '</div>';
}

async function saveExpandedFsheet(fid) {
    const f = findFsheet(fid);
    const container = document.getElementById('fsheetsContainer');
    const panel = container && container.querySelector('[data-expand="' + fid + '"]');
    if (!f || !panel) return;
    const items = collectBuilderItems(panel);
    if (!items.length) { showToast('Add at least one miner item.', false); return; }
    const err = validateFsItems(items);
    if (err) { showToast(err, false); return; }
    try {
        const response = await apiFetch('/api/fsheets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ fsheet: { id: f.id, name: f.name, coin: items[0].coin, fav: f.fav, items: items } })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Flight sheet updated.' : 'Failed to save flight sheet.'), !!data.success);
        if (data.success) {
            window._fsExpandedId = null;
            loadFsheets();
        }
    } catch (e) {
        showToast('Network error saving flight sheet.', false);
    }
}

function walletAddressLabel(walletRef, wallets) {
    const w = (wallets || []).find(x => x.id === walletRef);
    if (w) return w.name;
    if (!walletRef) return 'Configured in miner';
    return walletRef.length > 18 ? walletRef.slice(0, 18) + '…' : walletRef;
}

// Hive 'Unset': leave the rig without a flight sheet (miner stopped)
window.unsetFsheet = async function() {
    if (!confirm('Unset the active flight sheet? The rig will have no flight sheet and the miner will be stopped.')) return;
    try {
        const response = await apiFetch('/api/fsheets/unset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({})
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Flight sheet unset.' : 'Failed to unset flight sheet.'), !!data.success);
        if (data.success) setTimeout(() => { fetchStats(); loadFsheets(); }, 1500);
    } catch (e) {
        showToast('Network error unsetting flight sheet.', false);
    }
};

async function copyWalletAddress(addr) {
    if (!addr) return;
    let ok = false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(addr);
            ok = true;
        } else {
            ok = copyTextFallback(addr);
        }
    } catch (e) {
        ok = copyTextFallback(addr);
    }
    showToast(ok ? 'Wallet address copied.' : 'Copy failed.', ok);
}

window.applyFsheet = async function(fid) {
    try {
        const response = await apiFetch('/api/fsheets/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: fid })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Applied.' : 'Failed to apply.'), !!data.success);
        if (data.success) setTimeout(() => { fetchStats(); loadFsheets(); }, 2500);
    } catch (e) {
        showToast('Network error applying flight sheet.', false);
    }
};

function findFsheet(fid) {
    const data = window._fsData;
    if (data && fid === '__rig__') return liveConfigAsSheet();
    return data ? (data.fsheets || []).find(x => x.id === fid) : null;
}

// The rig's live mining config shaped like a flight sheet (for Edit/Duplicate/Export)
function liveConfigAsSheet() {
    const rc = (window._fsData && window._fsData.rig_config) || {};
    return {
        id: '__rig__',
        name: rc.name || 'Current mining config',
        coin: rc.coin || '',
        fav: false,
        items: [{
            coin: rc.coin || '', wallet: rc.wallet || '', pool: rc.pool || '',
            miner: rc.miner || 'none', miner_alt: '', install_url: '', algo: '',
            user_config: '', template: '', pass: ''
        }]
    };
}

// Kebab 'Edit' on the live rig config: open the builder prefilled with it
function editLiveConfig() {
    const live = liveConfigAsSheet();
    openFsBuilder();
    window._fsBuilderItems = JSON.parse(JSON.stringify(live.items));
    rebuildFsItemsContainer();
    if (live.name && live.name !== 'Current mining config') document.getElementById('fsName').value = live.name;
}

// Kebab 'Duplicate' on the live rig config: save it into the library
async function duplicateLiveConfig() {
    const live = liveConfigAsSheet();
    const name = (live.name && live.name !== 'Current mining config') ? live.name + ' (copy)' : 'Rig config ' + new Date().toISOString().slice(0, 10);
    try {
        const response = await apiFetch('/api/fsheets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ fsheet: { id: '', name: name, coin: live.coin, fav: false, items: live.items } })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Saved to the library.' : 'Failed to save.'), !!data.success);
        if (data.success) loadFsheets();
    } catch (e) {
        showToast('Network error saving flight sheet.', false);
    }
}

window.deleteFsheet = async function(fid) {
    if (!confirm('Delete this flight sheet?')) return;
    try {
        const response = await apiFetch('/api/fsheets/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: fid })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Deleted.' : 'Failed to delete.'), !!data.success);
        if (data.success) loadFsheets();
    } catch (e) {
        showToast('Network error deleting flight sheet.', false);
    }
};

window.duplicateFsheet = async function(fid) {
    const f = findFsheet(fid);
    if (!f) return;
    try {
        const response = await apiFetch('/api/fsheets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ fsheet: {
                id: '', name: (f.name || 'flight sheet') + ' (copy)',
                coin: f.coin || '', fav: false, items: f.items || []
            } })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Duplicated.' : 'Failed to duplicate.'), !!data.success);
        if (data.success) loadFsheets();
    } catch (e) {
        showToast('Network error duplicating flight sheet.', false);
    }
};

function fsheetExportJson(f) {
    return JSON.stringify({ name: f.name, coin: f.coin, fav: !!f.fav, items: f.items || [] }, null, 2);
}

window.exportFsheet = function(fid) {
    const f = findFsheet(fid);
    if (!f) return;
    try {
        const blob = new Blob([fsheetExportJson(f)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'fsheet_' + String(f.name || 'export').replace(/[^\w\-]+/g, '_').slice(0, 40) + '.json';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1500);
    } catch (e) {
        showToast('Export failed.', false);
    }
};

function copyTextFallback(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    return ok;
}

window.copyFsheet = async function(fid) {
    const f = findFsheet(fid);
    if (!f) return;
    const json = fsheetExportJson(f);
    let ok = false;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(json);
            ok = true;
        } else {
            ok = copyTextFallback(json);
        }
    } catch (e) {
        ok = copyTextFallback(json);
    }
    showToast(ok ? 'Flight sheet JSON copied to clipboard.' : 'Copy failed — use Export instead.', ok);
};

window.toggleFsheetFav = async function(fid) {
    const f = findFsheet(fid);
    if (!f) return;
    try {
        const response = await apiFetch('/api/fsheets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ fsheet: {
                id: f.id, name: f.name, coin: f.coin || '', fav: !f.fav, items: f.items || []
            } })
        });
        const data = await response.json();
        if (data.success) { f.fav = !f.fav; renderFsheets(); }
        else showToast(data.message || 'Failed to update favorite.', false);
    } catch (e) {
        showToast('Network error updating favorite.', false);
    }
};

// Delegated handlers for all flight sheet row + inline editor actions
function setupFsheetsContainer() {
    const container = document.getElementById('fsheetsContainer');
    if (!container) return;
    container.classList.add('fs-scroll');
    container.addEventListener('click', (e) => {
        const target = e.target.closest('[data-action]');
        if (!target || !container.contains(target)) return;
        const action = target.dataset.action;
        const fid = target.dataset.id;
        if (action === 'fav' || action === 'apply' || action === 'duplicate' ||
            action === 'export' || action === 'copy' || action === 'delete' || action === 'unset') {
            if (!fid && action !== 'unset') return;
            if (action === 'fav') toggleFsheetFav(fid);
            else if (action === 'apply') applyFsheet(fid);
            else if (action === 'duplicate') (fid === '__rig__') ? duplicateLiveConfig() : duplicateFsheet(fid);
            else if (action === 'export') exportFsheet(fid);
            else if (action === 'copy') copyFsheet(fid);
            else if (action === 'delete') deleteFsheet(fid);
            else if (action === 'unset') unsetFsheet();
            return;
        }
        if (action === 'copy-wallet') {
            copyWalletAddress(target.dataset.wallet || '');
            return;
        }
        if (action === 'info') {
            // Hive: click toggles the detailed view; several sheets can stay expanded
            if (!fid) return;
            window._fsInfoOpen = window._fsInfoOpen || new Set();
            if (window._fsInfoOpen.has(fid)) window._fsInfoOpen.delete(fid);
            else window._fsInfoOpen.add(fid);
            renderFsheets();
        } else if (action === 'edit') {
            if (!fid) return;
            if (fid === '__rig__') editLiveConfig();
            else window._fsExpandedId = (window._fsExpandedId === fid) ? null : fid;
            renderFsheets();
        } else if (action === 'expand-cancel') {
            window._fsExpandedId = null;
            renderFsheets();
        } else if (action === 'expand-save') {
            if (fid) saveExpandedFsheet(fid);
        } else if (action === 'expand-clear') {
            const panel = container.querySelector('[data-expand="' + fid + '"]');
            if (panel) panel.querySelectorAll('.fs-item-row').forEach(clearFsExpertFields);
        } else if (action === 'wallet-add') {
            const row = target.closest('.fs-item-row');
            const coin = row ? row.querySelector('.fs-coin').value.trim().toUpperCase() : '';
            showWalletModal({ coin: coin });
        } else if (action === 'miner-setup') {
            openFsMinerCfgModal(target.closest('.fs-item-row'));
        } else if (action === 'remove-item') {
            const panel = target.closest('.fs-expand');
            const row = target.closest('.fs-item-row');
            if (panel && row) {
                if (panel.querySelectorAll('.fs-item-row').length <= 1) {
                    row.outerHTML = builderRowHtml(0, {});
                } else {
                    row.remove();
                }
                renumberFsRows(panel);
            }
        }
    });
    // Inline editor rows behave like the builder rows
    container.addEventListener('change', onFsRowChange);
    container.addEventListener('input', onFsRowInput);
    container.addEventListener('focusin', function(e) {
        if (e.target.classList.contains('fs-pool')) updatePoolDatalist();
    });
    // Keep dropdown menus visible above the scroll container while open
    document.addEventListener('show.bs.dropdown', (e) => {
        if (container.contains(e.target)) container.classList.add('dd-open');
    });
    document.addEventListener('hidden.bs.dropdown', (e) => {
        if (container.contains(e.target)) container.classList.remove('dd-open');
    });
}

// Chip filter clicks: coins / wallets / pools
function setupFsChips() {
    const wrap = document.querySelector('.fs-chips');
    if (!wrap) return;
    wrap.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-chip]');
        if (!btn) return;
        const kind = btn.dataset.chip;
        if (btn.dataset.action === 'more') {
            window._fsChipsExp = window._fsChipsExp || {};
            window._fsChipsExp[kind] = true;
            renderFsheets();
            return;
        }
        window._fsChips = window._fsChips || {};
        window._fsChips[kind] = (window._fsChips[kind] === btn.dataset.value) ? '' : btn.dataset.value;
        renderFsheets();
    });
}

// Normalize an imported flight sheet (ours, or a HiveOS export with nested
// wallet/pool objects and an items[] array) into the items[]-based model
function normalizeFsheetItems(parsed) {
    const roots = Array.isArray(parsed) ? parsed : [parsed];
    const out = [];
    for (const root of roots) {
        if (!root || typeof root !== 'object') continue;
        if (Array.isArray(root.items)) {
            // items[] export: ours {name, items[]} or HiveOS {name, items:[{coin, pool_urls, wal_id, miner, miner_alt, miner_config}]}
            const items = root.items.map(it => {
                if (!it || typeof it !== 'object') return null;
                const mc = it.miner_config || {};
                let wallet = it.wallet;
                if (wallet && typeof wallet === 'object') wallet = wallet.address || wallet.id || '';
                if (wallet && typeof wallet !== 'string') wallet = '';
                return {
                    coin: String(it.coin || ''),
                    wallet: String(wallet || ''),
                    pool: String(it.pool || mc.url || (it.pool_urls && it.pool_urls[0]) || ''),
                    miner: String(it.miner || 'none'),
                    miner_alt: String(it.miner_alt || ''),
                    install_url: String(mc.install_url || ''),
                    algo: String(mc.algo || ''),
                    user_config: String(mc.user_config || '')
                };
            }).filter(Boolean);
            if (!items.length) continue;
            out.push({
                id: '', name: String(root.name || ''), coin: String(root.coin || items[0].coin || ''),
                fav: !!root.fav, items: items
            });
        } else {
            let wallet = root.wallet, pool = root.pool;
            if (wallet && typeof wallet === 'object') wallet = wallet.address || wallet.url || '';
            if (pool && typeof pool === 'object') pool = pool.url || (pool.host ? pool.host + ':' + (pool.port || '') : '');
            out.push({
                id: root.id || '', name: String(root.name || ''), coin: String(root.coin || ''),
                fav: !!root.fav,
                items: [{
                    coin: String(root.coin || ''), wallet: String(wallet || ''), pool: String(pool || ''),
                    miner: String(root.miner || 'none'), miner_alt: String(root.miner_alt || ''),
                    install_url: String(root.install_url || ''), algo: String(root.algo || ''),
                    user_config: String(root.user_config || '')
                }]
            });
        }
    }
    return out;
}

async function importFsheets() {
    const text = document.getElementById('fsheetImportText').value.trim();
    if (!text) { showToast('Nothing to import.', false); return; }
    let items;
    try {
        items = normalizeFsheetItems(JSON.parse(text));
    } catch (e) {
        showToast('Invalid JSON.', false);
        return;
    }
    if (!items.length) { showToast('Nothing to import.', false); return; }
    let ok = 0;
    for (const item of items) {
        try {
            const response = await apiFetch('/api/fsheets/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify({ fsheet: item })
            });
            const data = await response.json();
            if (data.success) ok += 1; else showToast(data.message || 'Import failed for one entry.', false);
        } catch (e) { /* keep going */ }
    }
    if (ok) showToast('Imported ' + ok + ' flight sheet' + (ok === 1 ? '' : 's') + '.', true);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('fsheetImportModal')).hide();
    loadFsheets();
}

async function importFromClipboard() {
    let text = '';
    try {
        if (navigator.clipboard && window.isSecureContext) {
            text = await navigator.clipboard.readText();
        }
    } catch (e) { text = ''; }
    if (!text) {
        showToast('Clipboard is not available over plain HTTP — paste with Ctrl+V.', false);
        return;
    }
    document.getElementById('fsheetImportText').value = text;
}

// Switch the dashboard section tabs (GPUs / Wallets / Flight Sheets)
function showDashTab(tab) {
    activeDashTab = tab;
    const isGpus = tab === 'gpus';
    const isGpusCards = isGpus && activeHardwareTab === 'gpus';
    const isIgpu = isGpus && activeHardwareTab !== 'gpus';
    [['dashTabGpusBtn', isGpus], ['dashTabFansBtn', tab === 'fans'], ['dashTabWalletsBtn', tab === 'wallets'],
     ['dashTabFsheetsBtn', tab === 'fsheets'], ['dashTabPresetsBtn', tab === 'presets'],
     ['dashTabServicesBtn', tab === 'services'], ['dashTabStatsBtn', tab === 'stats']].forEach(([id, on]) => {
        const b = document.getElementById(id);
        b.classList.toggle('btn-primary', on);
        b.classList.toggle('btn-outline-primary', !on);
        b.classList.toggle('active', on);
    });
    // GPUs tab shows the gpu or igpu grid per the hardware sub-switch
    document.getElementById('gpuContainer').classList.toggle('d-none', !isGpusCards);
    document.getElementById('igpuContainer').classList.toggle('d-none', !isIgpu);
    // CPU Mining card lives in the CPU iGPU sub-view, OC guide in the GPU Cards sub-view
    document.getElementById('cpuCardContainer').classList.toggle('d-none', !isIgpu);
    document.getElementById('ocGuideContainer').classList.toggle('d-none', !isGpusCards);
    document.getElementById('ocAllContainer').classList.toggle('d-none', !isGpusCards);
    document.getElementById('walletsTabContainer').classList.toggle('d-none', tab !== 'wallets');
    document.getElementById('fansTabContainer').classList.toggle('d-none', tab !== 'fans');
    document.getElementById('presetsTabContainer').classList.toggle('d-none', tab !== 'presets');
    document.getElementById('servicesTabContainer').classList.toggle('d-none', tab !== 'services');
    document.getElementById('fsheetsTabContainer').classList.toggle('d-none', tab !== 'fsheets');
    document.getElementById('statsTabContainer').classList.toggle('d-none', tab !== 'stats');
    // Stats refresh + hardware sub-switch only make sense on the GPUs tab
    document.getElementById('gpusTabControls').classList.toggle('d-none', !isGpus);
    if (isWalletsTab(tab)) renderWallets();
    if (tab === 'fsheets') loadFsheets();
    if (tab === 'fans') { loadAutofan(); loadFans(); }
    if (tab === 'presets') loadOcPresetsList();
    if (tab === 'stats') loadMetricsTab();
}

function isWalletsTab(tab) { return tab === 'wallets'; }

function openWalletModal() {
    // Legacy entry point: open the modal in "add" mode
    showWalletModal(null);
}

function showWalletModal(entry) {
    const isEdit = !!(entry && entry.id);
    window._editingWalletId = isEdit ? entry.id : '';
    document.getElementById('walletEditTitle').innerHTML = isEdit
        ? '<i class="bi bi-wallet2 text-warning me-2"></i>Edit Wallet'
        : '<i class="bi bi-wallet2 text-warning me-2"></i>New Wallet';
    document.getElementById('walletCoinInput').value = entry ? (entry.coin || '') : '';
    document.getElementById('walletAddressInput2').value = isEdit ? (entry.address || '') : '';
    document.getElementById('walletNameInput2').value = isEdit ? (entry.name || '') : '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('walletEditModal')).show();
}

async function saveWalletFromModal() {
    const payload = {
        wallet: {
            id: window._editingWalletId || '',
            coin: document.getElementById('walletCoinInput').value.trim().toUpperCase(),
            name: document.getElementById('walletNameInput2').value.trim(),
            address: document.getElementById('walletAddressInput2').value.trim()
        }
    };
    const btn = document.getElementById('walletEditSaveBtn');
    btn.disabled = true;
    try {
        const response = await apiFetch('/api/wallets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Wallet saved.' : 'Failed to save wallet.'), !!data.success);
        if (data.success) {
            bootstrap.Modal.getOrCreateInstance(document.getElementById('walletEditModal')).hide();
            renderWallets();
            refreshFsWalletOptions();
            loadFsheets();
        }
    } catch (err) {
        showToast('Network error saving wallet.', false);
    } finally {
        btn.disabled = false;
    }
}

window.deleteWallet = async function(wid) {
    if (!confirm('Delete this wallet?')) return;
    try {
        const response = await apiFetch('/api/wallets/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ id: wid })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Wallet removed.' : 'Failed to delete wallet.'), !!data.success);
        if (data.success) { renderWallets(); refreshFsWalletOptions(); loadFsheets(); }
    } catch (e) {
        showToast('Network error deleting wallet.', false);
    }
};

async function renderWallets() {
    try {
        const response = await apiFetch('/api/wallets');
        const data = await response.json();
        window._walletsData = { wallets: data.wallets || [], rig_config: data.rig_config || {} };
        // keep the fsheet builder wallet selects in sync with the library
        window._fsData = Object.assign({}, window._fsData || {}, { wallets: window._walletsData.wallets });
        renderWalletChips();
        renderWalletTable();
    } catch (e) {
        showToast('Failed to load wallets.', false);
    }
}

function renderWalletChips() {
    const box = document.getElementById('walletCoinChips');
    const data = window._walletsData;
    if (!box || !data) return;
    const wallets = data.wallets;
    const byCoin = {};
    wallets.forEach(w => {
        const c = (w.coin || '').toUpperCase();
        if (c) byCoin[c] = (byCoin[c] || 0) + 1;
    });
    const coins = Object.keys(byCoin).sort();
    const selected = window._walletCoinFilter || '';
    const expanded = window._walletChipsExpanded;
    let chipCoins = coins;
    let moreChip = '';
    if (coins.length > 8 && !expanded) {
        chipCoins = coins.slice(0, 7);
        moreChip = '<button type="button" class="chip-btn" data-action="more" title="Show all coins">… ' + (coins.length - 7) + ' more coins</button>';
    }
    const chip = (value, label, count, av) =>
        '<button type="button" class="chip-btn' + (selected === value ? ' active' : '') + '" data-action="coin" data-coin="' + escapeHtml(value) + '" title="Filter wallets by coin">' +
        (av || '') + '<span>' + escapeHtml(label) + '</span>' + (count !== undefined ? ' <span class="chip-count">' + count + '</span>' : '') + '</button>';
    let html = chip('', 'All', wallets.length);
    html += chipCoins.map(c => chip(c, c, byCoin[c], coinAvatarHtml(c))).join('');
    if (moreChip) html += moreChip;
    box.innerHTML = html;
}

function renderWalletTable() {
    const data = window._walletsData;
    if (!data) return;
    const tbody = document.getElementById('walletsTableBody');
    const wallets = data.wallets;
    const rc = data.rig_config || {};
    const filter = window._walletCoinFilter || '';
    // Live wallet currently used by the rig's mining config (may not exist in the library)
    const liveWallet = (rc.wallet && !wallets.some(w => walletMatchesLive(w.address, rc.wallet)))
        ? [{ id: '__rig__', coin: rc.coin || '', name: (rc.coin ? rc.coin + ' wallet' : 'Active wallet') + ' (rig)', address: rc.wallet, live: true, active: true }]
        : [];
    const rows = liveWallet.concat(wallets.filter(w => !filter || (w.coin || '').toUpperCase() === filter));
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted small py-3">' +
            (wallets.length ? 'No wallets for this coin.' : 'No wallets saved yet. Click Add.') + '</td></tr>';
        return;
    }
    tbody.innerHTML = rows.map(w => {
        const isActive = !!(w.active || w.live);
        const used = w.used_in || 0;
        const status = isActive
            ? '<span class="badge bg-warning-glow text-warning small" title="Wallet of the active flight sheet / live mining config">ACTIVE</span>'
            : '<span class="text-muted small">&mdash;</span>';
        const sheets = used > 0
            ? '<span class="fs-used-badge" title="Used in ' + used + ' flight sheet' + (used > 1 ? 's' : '') + '"><i class="bi bi-rocket-takeoff-fill"></i>' + used + '</span>'
            : '<span class="fs-used-badge fs-used-none" title="Not referenced by any flight sheet"><i class="bi bi-rocket-takeoff"></i>0</span>';
        return '<tr class="' + (isActive ? 'wallet-row-active' : '') + '">' +
            '<td>' + coinAvatarHtml(w.coin) + '</td>' +
            '<td>' + status + '</td>' +
            '<td>' + sheets + '</td>' +
            '<td><span class="small fw-semibold">' + escapeHtml(w.name) + '</span></td>' +
            '<td><span class="small font-monospace text-muted d-inline-block text-truncate align-middle" style="max-width: 100%;" title="' + escapeHtml(w.address) + '">' + escapeHtml(w.address) + '</span></td>' +
            '<td class="text-end">' + (w.live ? '<span class="small text-muted">running</span>' :
                '<button type="button" class="btn btn-xs btn-outline-primary py-0 px-2" data-action="wallet-edit" data-id="' + escapeHtml(w.id) + '" title="Edit"><i class="bi bi-pencil"></i></button>' +
                '<button type="button" class="btn btn-xs btn-outline-danger py-0 px-2" data-action="wallet-delete" data-id="' + escapeHtml(w.id) + '" title="Delete"><i class="bi bi-trash"></i></button>') +
            '</td>' +
        '</tr>';
    }).join('');
}

// Robust match of a library wallet address against the live mining wallet
// (live templates may carry a worker suffix: 'addr.WORKER')
function walletMatchesLive(addr, live) {
    addr = String(addr || '').trim();
    live = String(live || '').trim();
    if (!addr || !live) return false;
    if (addr === live) return true;
    if (live.startsWith(addr + '.')) return true;
    const base = live.split('.')[0];
    return base.length >= 8 && addr.startsWith(base);
}

function setupWalletsContainer() {
    const chips = document.getElementById('walletCoinChips');
    if (chips) {
        chips.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            if (btn.dataset.action === 'more') {
                window._walletChipsExpanded = true;
                renderWalletChips();
            } else if (btn.dataset.action === 'coin') {
                const c = btn.dataset.coin;
                window._walletCoinFilter = (window._walletCoinFilter === c) ? '' : c;
                renderWalletChips();
                renderWalletTable();
            }
        });
    }
    const tbody = document.getElementById('walletsTableBody');
    if (tbody) {
        tbody.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-action]');
            if (!btn || !tbody.contains(btn)) return;
            if (btn.dataset.action === 'wallet-edit') {
                const w = ((window._walletsData || {}).wallets || []).find(x => x.id === btn.dataset.id);
                if (w) showWalletModal(w);
            } else if (btn.dataset.action === 'wallet-delete') {
                deleteWallet(btn.dataset.id);
            }
        });
    }
}

// ---------------- Extra fans control ----------------

async function loadFans() {
    const container = document.getElementById('fansContainer');
    if (!container) return;
    try {
        const response = await apiFetch('/api/fans');
        const data = await response.json();
        const fans = (data && data.fans) || [];
        const mk = data && data.mknet;
        let html = '';
        if (mk && mk.present) html += renderMknet(mk);
        if (fans.length) html += fans.map(f => {
            const id = f.hwmon + '_' + f.pwm;
            return '<div class="d-flex align-items-center gap-2 border border-secondary-subtle rounded px-2 py-1 mb-1">' +
                '<span class="fw-semibold small" style="width: 130px;" title="' + escapeHtml(f.chip) + ' pwm' + f.pwm + '">' + escapeHtml(f.label) + '</span>' +
                '<span class="small text-muted font-monospace" style="width: 80px;">' + (f.rpm !== null ? f.rpm + ' rpm' : '—') + '</span>' +
                '<input type="range" class="form-range fan-slider" min="0" max="100" value="' + f.duty + '" id="fan_' + id + '"' + (f.mode === 'auto' ? ' disabled' : '') + '>' +
                '<span class="small font-monospace" style="width: 42px;" id="fanval_' + id + '">' + f.duty + '%</span>' +
                '<div class="btn-group btn-group-sm" role="group">' +
                    '<button class="btn btn-xs ' + (f.mode === 'manual' ? 'btn-warning' : 'btn-outline-secondary') + ' py-0 px-2" onclick="setFanMode(\'' + f.hwmon + '\',' + f.pwm + ',\'manual\')" title="Manual control">M</button>' +
                    '<button class="btn btn-xs ' + (f.mode === 'auto' ? 'btn-success' : 'btn-outline-secondary') + ' py-0 px-2" onclick="setFanMode(\'' + f.hwmon + '\',' + f.pwm + ',\'auto\')" title="Automatic">A</button>' +
                '</div>' +
            '</div>';
        }).join('');
        container.innerHTML = html;
        container.querySelectorAll('.fan-slider').forEach(sl => {
            sl.addEventListener('input', function() {
                document.getElementById(this.id.replace('fan_', 'fanval_')).textContent = this.value + '%';
            });
            sl.addEventListener('change', function() {
                const parts = this.id.replace('fan_', '').split('_');
                setFanDuty(parts[0], parts[1], parseInt(this.value, 10));
            });
        });
        if (mk && mk.present) bindMknet(mk);
        if (!fans.length && !(mk && mk.present)) {
            container.innerHTML = '<div class="text-muted small">No controllable fans detected on this rig (motherboard fan headers via hwmon).</div>';
        }
    } catch (e) {
        container.innerHTML = '<div class="text-muted small">Fan control not available.</div>';
    }
}

// ---------------- 8MK_NET USB fan controller (auto/static with min/max/target) ----------------
// Untyped edits survive re-renders (auto-refresh, tab entry, manual refresh):
// they live in window._mknetUI and are dropped after a successful Apply or a rig switch.

function mknetUi() {
    if (!window._mknetUI) window._mknetUI = { mode: 'auto' };
    return window._mknetUI;
}

const MKNET_FIELD_KEYS = { mkTargetTemp: 'target_temp', mkTargetMem: 'target_mem_temp',
    mkMinFan: 'min_fan', mkMaxFan: 'max_fan', mkStaticSpeed: 'static_speed' };

const MKNET_DEFAULTS = { target_temp: 60, target_mem_temp: 90, min_fan: 5, max_fan: 100, static_speed: 70 };

function renderMknet(mk) {
    const st = mk.stats || {};
    const cfg = mk.config || {};
    const ui = window._mknetUI;
    const mode = (ui && ui.mode) ? ui.mode : (cfg.auto === false ? 'static' : 'auto');
    const val = (k, d) => (ui && ui[k] !== undefined && ui[k] !== null && ui[k] !== '')
        ? ui[k] : ((cfg[k] !== undefined && cfg[k] !== null) ? cfg[k] : d);
    const fans = st.casefan || [];
    const sensor = (st.thermosensors && st.thermosensors.length) ? st.thermosensors[0] : null;
    // Speed color: green -> yellow -> red (hue 130 -> 0)
    const speedColor = v => {
        const h = Math.round(130 - v * 1.3);
        return ' style="color:hsl(' + h + ',85%,70%);border-color:hsla(' + h + ',85%,60%,0.45);background:hsla(' + h + ',85%,60%,0.08);"';
    };
    const chips = (sensor !== null
            ? '<span class="mk-chip mk-temp" title="Controller thermosensor"><i class="bi bi-thermometer-half"></i> ' + sensor + '°C</span>'
            : '')
        + fans.map((v, i) =>
            '<span class="mk-chip' + (v > 0 ? '' : ' mk-off') + '"' + (v > 0 ? speedColor(v) : '') +
            ' title="Fan channel ' + (i + 1) + ' speed">F' + (i + 1) + ' ' + (v > 0 ? v + '%' : '—') + '</span>'
        ).join('');
    const num = (v, d) => (v === null || v === undefined || v === '' || isNaN(v)) ? d : v;
    const fld = (id, label, v, def, extra) =>
        '<div><label class="form-label small text-muted mb-1" for="' + id + '">' + label + '</label>' +
        '<input type="number" class="form-control form-control-sm bg-dark-input" style="width: 88px;" id="' + id + '" value="' + num(v, def) + '"' + (extra || '') + '></div>';
    return '<div class="border border-secondary-subtle rounded p-3 mb-2" id="mknetBlock" data-mode="' + mode + '">' +
        '<div class="d-flex justify-content-between align-items-center mb-3">' +
            '<span class="fw-semibold small"><i class="bi bi-usb-plug text-info"></i> 8MK_NET USB controller</span>' +
        '</div>' +
        (chips
            ? '<div class="d-flex flex-wrap gap-2 mb-3">' + chips + '</div>'
            : '<div class="text-muted small mb-3">No data from the controller yet (it reports every ~2 min).</div>') +
        '<div class="d-flex align-items-center gap-3 mb-3">' +
            '<div class="btn-group btn-group-sm" role="group">' +
                '<button type="button" class="btn btn-xs py-0 px-2" id="mkAutoBtn">Auto</button>' +
                '<button type="button" class="btn btn-xs py-0 px-2" id="mkStaticBtn">Static</button>' +
            '</div>' +
            '<span class="small text-muted">Auto keeps the lowest speed within the range to hold the target temperature.</span>' +
        '</div>' +
        '<div class="d-flex flex-wrap gap-3 mb-3 mk-auto-field">' +
            fld('mkTargetTemp', 'Target temp, °C', val('target_temp'), MKNET_DEFAULTS.target_temp) +
            fld('mkTargetMem', 'Target MEM, °C', val('target_mem_temp'), MKNET_DEFAULTS.target_mem_temp) +
            fld('mkMinFan', 'Min fan, %', val('min_fan'), MKNET_DEFAULTS.min_fan) +
            fld('mkMaxFan', 'Max fan, %', val('max_fan'), MKNET_DEFAULTS.max_fan) +
        '</div>' +
        '<div class="d-flex flex-wrap gap-3 mb-3 mk-static-field">' +
            fld('mkStaticSpeed', 'Static speed, %', val('static_speed'), MKNET_DEFAULTS.static_speed, ' min="0" max="100"') +
        '</div>' +
        '<div class="d-flex justify-content-end gap-2">' +
            '<button type="button" class="btn btn-sm btn-outline-secondary px-3" id="mkResetBtn">Reset</button>' +
            '<button type="button" class="btn btn-sm btn-primary px-4" id="mkApplyBtn">Apply</button>' +
        '</div>' +
    '</div>';
}

function mknetSyncModeUI() {
    const block = document.getElementById('mknetBlock');
    if (!block) return;
    const mode = block.dataset.mode === 'static' ? 'static' : 'auto';
    block.querySelectorAll('.mk-auto-field').forEach(el => el.classList.toggle('d-none', mode !== 'auto'));
    block.querySelectorAll('.mk-static-field').forEach(el => el.classList.toggle('d-none', mode !== 'static'));
    const a = document.getElementById('mkAutoBtn'), s = document.getElementById('mkStaticBtn');
    a.classList.toggle('btn-success', mode === 'auto');
    a.classList.toggle('btn-outline-secondary', mode !== 'auto');
    s.classList.toggle('btn-warning', mode === 'static');
    s.classList.toggle('btn-outline-secondary', mode !== 'static');
}

function bindMknet() {
    const block = document.getElementById('mknetBlock');
    if (!block) return;
    mknetSyncModeUI();
    document.getElementById('mkAutoBtn').addEventListener('click', () => {
        mknetUi().mode = 'auto'; block.dataset.mode = 'auto'; mknetSyncModeUI();
    });
    document.getElementById('mkStaticBtn').addEventListener('click', () => {
        mknetUi().mode = 'static'; block.dataset.mode = 'static'; mknetSyncModeUI();
    });
    document.getElementById('mkResetBtn').addEventListener('click', () => {
        // Reset the form fields to the defaults (target 60/90, min 5, max 100,
        // static 70) keeping the currently selected mode; press Apply to apply
        const ui = mknetUi();
        ui.mode = block.dataset.mode === 'static' ? 'static' : 'auto';
        Object.assign(ui, MKNET_DEFAULTS);
        loadFans();
    });
    Object.keys(MKNET_FIELD_KEYS).forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => { mknetUi()[MKNET_FIELD_KEYS[id]] = el.value; });
    });
    document.getElementById('mkApplyBtn').addEventListener('click', async function() {
        const mode = block.dataset.mode;
        const val = id => document.getElementById(id) ? document.getElementById(id).value : '';
        const payload = {
            mode: mode,
            target_temp: val('mkTargetTemp'),
            target_mem_temp: val('mkTargetMem'),
            min_fan: val('mkMinFan'),
            max_fan: val('mkMaxFan'),
            static_speed: val('mkStaticSpeed')
        };
        const btn = this;
        btn.disabled = true;
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
        try {
            const response = await apiFetch('/api/fans/mknet', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
                body: JSON.stringify(payload)
            });
            const data = await response.json();
            showToast(data.message || (data.success ? 'Applied.' : 'Failed to apply.'), !!data.success);
            if (data.success) {
                window._mknetUI = null; // config now matches the form - reload it
                loadFans();
            }
        } catch (e) {
            showToast('Network error applying 8MK_NET settings.', false);
        } finally {
            btn.disabled = false;
            btn.innerHTML = orig;
        }
    });
}

async function setFanMode(hwmon, pwm, mode) {
    try {
        const response = await apiFetch('/api/fans', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ hwmon, pwm, mode })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Fan updated.' : 'Failed to update fan.'), !!data.success);
        loadFans();
    } catch (e) {
        showToast('Network error controlling fan.', false);
    }
}

async function setFanDuty(hwmon, pwm, duty) {
    try {
        const response = await apiFetch('/api/fans', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ hwmon, pwm, mode: 'manual', duty })
        });
        const data = await response.json();
        if (!data.success) {
            showToast(data.message || 'Failed to set fan speed.', false);
            loadFans();
        }
    } catch (e) {
        showToast('Network error setting fan speed.', false);
    }
}

// ---------------- AutoFan (Fans tab, 1:1 HiveOS autofan page copy) ----------------
// State model mirrors the Hive cloud SPA (module 89230 constants):
//   items[i] = {mode: 'auto'|'static', static_fan, min_fan, max_fan,
//               target_temp, target_mem_temp, critical_temp}
//   common (set-all row template) = {mode:'auto', static_fan:80, min_fan:30,
//               max_fan:100, target_temp:65, target_mem_temp:90, critical_temp:90}
// Field mapping (verified against the rig's /hive/sbin/autofan + autofan.conf
// and the cloud worker.autofan JSON — keys/values are identical):
//   mode        -> CUSTOM_MODE[i] (1 = static)      cloud: items[].mode
//   static_fan  -> nvidia-oc.conf FAN[i]            cloud: items[].static_fan
//   min/max_fan -> CUSTOM_MIN_FAN/CUSTOM_MAX_FAN    cloud: items[].min_fan/max_fan
//   target_temp/target_mem_temp -> CUSTOM_TARGET_TEMP/CUSTOM_TARGET_MEM_TEMP
//   critical_temp -> CUSTOM_CRITICAL_TEMP           cloud: items[].critical_temp
//   enabled/critical_temp_action/reboot_on_errors/smart_mode -> ENABLED/
//               CRITICAL_TEMP_ACTION/REBOOT_ON_ERROR/SMART_MODE (same names in cloud)

const AF_DEFAULTS = { mode: 'auto', static_fan: 80, min_fan: 30, max_fan: 100, target_temp: 60, target_mem_temp: 90, critical_temp: 70 };
// Keys allowed per mode (Hive: Zo = auto keys, Iq = static keys)
const AF_AUTO_KEYS = ['mode', 'target_temp', 'target_mem_temp', 'min_fan', 'max_fan', 'critical_temp'];
const AF_STATIC_KEYS = ['mode', 'static_fan', 'critical_temp'];
const AF_ADV_FIELDS = [
    { id: 'afAdvStatic', key: 'static_fan' },
    { id: 'afAdvCore', key: 'target_temp' },
    { id: 'afAdvMem', key: 'target_mem_temp' },
    { id: 'afAdvMin', key: 'min_fan' },
    { id: 'afAdvMax', key: 'max_fan' },
    { id: 'afAdvCrit', key: 'critical_temp' }
];

function afEsc(v) { return escapeHtml(String(v === null || v === undefined ? '' : v)); }

function afGpuInfo(idx) {
    const g = ((lastStatsData && lastStatsData.gpus) || []).find(x => x.index === idx) || {};
    // Hive shows card names without the vendor prefix ("GeForce RTX 3070")
    const model = (g.model || '').replace(/^NVIDIA\s+/i, '') || ('GPU ' + idx);
    return {
        model: model,
        brand: g.brand || '',
        bus: g.bus_id || '',
        vram: g.vram_mb ? g.vram_mb + ' MB' : '',
        subvendor: g.subvendor || ''
    };
}

function afModeSelectHtml(mode, rowKey) {
    return '<span class="af-mode-wrap"><span class="af-ico ' + (mode === 'static' ? 'af-ico-s">S' : 'af-ico-a">A') + '</span>' +
        '<select class="gpu-fan-mode" data-row="' + rowKey + '" title="Fan mode">' +
        '<option value="auto"' + (mode !== 'static' ? ' selected' : '') + '>Auto</option>' +
        '<option value="static"' + (mode === 'static' ? ' selected' : '') + '>Static</option>' +
        '</select></span>';
}

// Applicable/blanked rule (Hive): in auto mode the static input is hidden+disabled,
// in static mode min/max/targets are hidden+disabled, critical is always editable.
function afFieldApplicable(item, field) {
    if (field === 'critical_temp' || field === 'mode') return true;
    if (field === 'static_fan') return item.mode === 'static';
    return item.mode === 'auto';
}

function afNumInput(field, rowKey, item) {
    const show = afFieldApplicable(item, field);
    const val = show ? afEsc(item[field]) : '';
    return '<input type="text" inputmode="numeric" autocomplete="off" spellcheck="false" class="af-in gpu-fan-' + field +
        '" data-row="' + rowKey + '" data-field="' + field + '" value="' + val + '"' + (show ? '' : ' disabled') + '>';
}

function afValueTds(item, rowKey) {
    return '<td><div class="af-cells">' +
            '<div class="af-cell">' + afNumInput('static_fan', rowKey, item) + '</div>' +
            '<div class="af-cell">' + afNumInput('min_fan', rowKey, item) + '</div>' +
            '<div class="af-cell">' + afNumInput('max_fan', rowKey, item) + '</div>' +
        '</div></td>' +
        '<td><div class="af-cells">' +
            '<div class="af-cell">' + afNumInput('target_temp', rowKey, item) + '</div>' +
            '<div class="af-cell">' + afNumInput('target_mem_temp', rowKey, item) + '</div>' +
        '</div></td>' +
        '<td><div class="af-cells">' +
            '<div class="af-cell">' + afNumInput('critical_temp', rowKey, item) + '</div>' +
        '</div></td>';
}

// The set-all row mirrors the per-GPU field rules: in auto mode its Static input
// is blanked+disabled, in static mode Min/Max/Target/MEM are (Critical stays editable)
function afSyncSetAllRow(mode) {
    const rules = { static_fan: mode === 'static', min_fan: mode === 'auto', max_fan: mode === 'auto',
                    target_temp: mode === 'auto', target_mem_temp: mode === 'auto', critical_temp: true };
    document.querySelectorAll('#afTableBody tr[data-gpu-row="all"] input[data-field]').forEach(el => {
        const show = rules[el.dataset.field];
        el.disabled = !show;
        if (!show) el.value = '';
    });
}

function renderAfTable() {
    const body = document.getElementById('afTableBody');
    const af = window._af;
    if (!body || !af) return;
    const d = AF_DEFAULTS;

    // Header sub-labels row (same rhythm as the input rows below it)
    const labelsRow =
        '<tr class="af-labels-row">' +
            '<td><div class="af-split">' +
                '<span>GPU</span><span>Name</span><span></span>' +
            '</div></td>' +
            '<td><div class="af-cells">' +
                '<div class="af-cell-label">Static</div>' +
                '<div class="af-cell-label">Min</div>' +
                '<div class="af-cell-label">Max</div>' +
            '</div></td>' +
            '<td><div class="af-cells">' +
                '<div class="af-cell-label">Core</div>' +
                '<div class="af-cell-label">Memory</div>' +
            '</div></td>' +
            '<td><div class="af-cells"><div class="af-cell-label">Core</div></div></td>' +
        '</tr>';

    // "Set settings for all GPU" template row (Hive keeps the MH defaults here and
    // distributes typed values into every per-GPU item on the fly)
    const setAllItem = { mode: 'auto', static_fan: '', min_fan: '', max_fan: '', target_temp: '', target_mem_temp: '', critical_temp: '' };
    const setAllRow =
        '<tr data-gpu-row="all">' +
            '<td><div class="af-split">' +
                '<span class="af-setall-text" style="grid-column: 1 / 3;">Set settings for all GPU of your worker</span>' +
                '<span>' + afModeSelectHtml('auto', 'all') + '</span>' +
            '</div></td>' +
            '<td><div class="af-cells">' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllStatic" data-field="static_fan" placeholder="' + d.static_fan + '"></div>' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllMin" data-field="min_fan" placeholder="' + d.min_fan + '"></div>' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllMax" data-field="max_fan" placeholder="' + d.max_fan + '"></div>' +
            '</div></td>' +
            '<td><div class="af-cells">' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllCore" data-field="target_temp" placeholder="' + d.target_temp + '"></div>' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllMem" data-field="target_mem_temp" placeholder="' + d.target_mem_temp + '"></div>' +
            '</div></td>' +
            '<td><div class="af-cells">' +
                '<div class="af-cell"><input type="text" inputmode="numeric" autocomplete="off" class="af-in af-all-input" id="afAllCrit" data-field="critical_temp" placeholder="' + d.critical_temp + '"></div>' +
            '</div></td>' +
        '</tr>' +
        '<tr class="af-sep"><td colspan="4"></td></tr>';

    const rows = (af.items || []).map(item => {
        const info = afGpuInfo(item.index);
        const meta = [info.vram, info.subvendor].filter(Boolean).join(' · ');
        return '<tr data-gpu-row="' + item.index + '">' +
            '<td><div class="af-split">' +
                '<span><div class="af-gpu-title">GPU ' + item.index + '</div>' +
                    (info.bus ? '<div class="af-gpu-sub">' + afEsc(info.bus) + '</div>' : '') + '</span>' +
                '<span><div class="af-name-title">' + afEsc(info.model) + '</div>' +
                    (meta ? '<div class="af-name-sub">' + afEsc(meta) + '</div>' : '') + '</span>' +
                '<span>' + afModeSelectHtml(item.mode, item.index) + '</span>' +
            '</div></td>' +
            afValueTds(item, item.index) +
        '</tr>';
    }).join('');

    body.innerHTML = labelsRow + setAllRow + rows;
    afSyncSetAllRow('auto');
    bindAfTableEvents();
}

// Re-sync one row's value cells after a mode change (values stay in state,
// non-applicable inputs get blanked+disabled exactly like the Hive page)
function afSyncRow(rowKey) {
    const af = window._af;
    const tr = document.querySelector('#afTableBody tr[data-gpu-row="' + rowKey + '"]');
    if (!tr || !af) return;
    const item = rowKey === 'all' ? null : af.items.find(x => String(x.index) === String(rowKey));
    if (!item) return;
    const wrap = document.createElement('div');
    wrap.innerHTML = '<table><tr>' + afValueTds(item, rowKey) + '</tr></table>';
    const tds = wrap.querySelectorAll('td');
    const rowTds = tr.querySelectorAll('td');
    for (let i = 0; i < tds.length; i++) {
        rowTds[i + 1].innerHTML = tds[i].innerHTML;
    }
    bindRowInputEvents(tr);
}

function afUpdateModeIcon(sel) {
    // Keep the A/S letter badge in sync with the select value
    const ico = sel.parentElement.querySelector('.af-ico');
    if (ico) {
        ico.className = 'af-ico ' + (sel.value === 'static' ? 'af-ico-s' : 'af-ico-a');
        ico.textContent = sel.value === 'static' ? 'S' : 'A';
    }
}

function bindRowInputEvents(scope) {
    scope.querySelectorAll('input.gpu-fan-static_fan, input.gpu-fan-min_fan, input.gpu-fan-max_fan, input.gpu-fan-target_temp, input.gpu-fan-target_mem_temp, input.gpu-fan-critical_temp').forEach(inp => {
        inp.addEventListener('input', () => {
            inp.value = inp.value.replace(/[^\d]/g, '');
            const item = (window._af.items || []).find(x => String(x.index) === inp.dataset.row);
            if (item) item[inp.dataset.field] = inp.value;
        });
    });
    scope.querySelectorAll('select.gpu-fan-mode').forEach(sel => {
        sel.addEventListener('change', () => {
            const rowKey = sel.dataset.row;
            if (rowKey === 'all') {
                (window._af.items || []).forEach(it => {
                    it.mode = sel.value;
                    const tr = document.querySelector('#afTableBody tr[data-gpu-row="' + it.index + '"]');
                    const s = tr && tr.querySelector('select.gpu-fan-mode');
                    if (s) {
                        s.value = sel.value;
                        afUpdateModeIcon(s);
                    }
                    afSyncRow(String(it.index));
                });
                afUpdateModeIcon(sel);
                afSyncSetAllRow(sel.value);
            } else {
                const item = (window._af.items || []).find(x => String(x.index) === rowKey);
                if (item) item.mode = sel.value;
                afUpdateModeIcon(sel);
                afSyncRow(rowKey);
            }
        });
    });
}

function bindAfTableEvents() {
    const body = document.getElementById('afTableBody');
    if (!body || !window._af) return;
    bindRowInputEvents(body);
    // The set-all row copies typed values into every GPU item at once (like Hive
    // handleCommonChange) — values land in the state even for blanked inputs
    body.querySelectorAll('.af-all-input').forEach(inp => {
        inp.addEventListener('input', () => {
            inp.value = inp.value.replace(/[^\d]/g, '');
            const field = inp.dataset.field;
            (window._af.items || []).forEach(it => { it[field] = inp.value; });
            document.querySelectorAll('#afTableBody tr[data-gpu-row]:not([data-gpu-row="all"]) input.gpu-fan-' + field).forEach(target => {
                if (!target.disabled) target.value = inp.value;
            });
        });
    });
}

// ---------------- Advanced mode: space-separated per-GPU lists ----------------

function renderAfAdvanced() {
    const af = window._af;
    if (!af) return;
    AF_ADV_FIELDS.forEach(({ id, key }) => {
        const el = document.getElementById(id);
        if (el) el.value = (af.items || []).map(it => it[key] === null || it[key] === undefined ? '' : it[key]).join(' ');
    });
    bindAfAdvancedEvents();
}

function afCaretTokenIndex(value, caret) {
    const tokens = value.split(' ');
    let pos = caret, idx = 0;
    for (let i = 0; i < tokens.length; i++) {
        if (pos <= tokens[i].length) { idx = i; break; }
        pos -= tokens[i].length + 1;
        idx = i + 1;
    }
    return { tokens, idx };
}

function updateAfCaret(input) {
    const helper = document.querySelector('.af-caret[data-caret-for="' + input.id + '"]');
    if (!helper) return;
    const af = window._af;
    const count = (af && af.items || []).length;
    const { tokens, idx } = afCaretTokenIndex(input.value, input.selectionStart || 0);
    const singleValue = tokens.length < 2;
    // Hive warns only when the token at the caret has no value while the list
    // already ends with a number ("fewer values than GPUs"); a single non-empty
    // value applies to all GPUs, so there is nothing "missed" in that case
    const nonEmptyCount = tokens.filter(t => t !== '').length;
    const warn = count > 0 && nonEmptyCount > 1 && (tokens[idx] === undefined || tokens[idx] === '') &&
        tokens.length >= 1 && tokens[tokens.length - 1] !== '' &&
        !Number.isNaN(parseInt(tokens[tokens.length - 1], 10));
    let html = '';
    if (singleValue) {
        html = count > 1 ? 'GPU 0...GPU ' + (count - 1) : 'GPU 0';
    } else {
        const info = afGpuInfo(idx);
        if (idx < count) {
            html = '<span>GPU ' + idx + '</span>' +
                (info.bus ? '<span class="af-caret-bus">' + escapeHtml(info.bus) + '</span>' : '') +
                '<span>' + escapeHtml(info.model) + (info.vram ? ' ' + escapeHtml(info.vram) : '') + '</span>';
        }
        if (warn) {
            const warnText = idx >= count ? 'Out of GPU count range' : 'GPU missed';
            html += '<span class="af-caret-warn">' + warnText + '</span>';
        }
    }
    helper.innerHTML = html;
    helper.classList.toggle('d-none', false);
}

function bindAfAdvancedEvents() {
    const af = window._af;
    if (!af) return;
    AF_ADV_FIELDS.forEach(({ id, key }) => {
        const el = document.getElementById(id);
        if (!el) return;
        // The advanced inputs are static DOM nodes rendered once - drop the
        // previous event bundle before re-binding, otherwise listeners pile up
        if (el._afHandlers) {
            el.removeEventListener('input', el._afHandlers.input);
            el.removeEventListener('focus', el._afHandlers.focus);
            el.removeEventListener('click', el._afHandlers.click);
            el.removeEventListener('keyup', el._afHandlers.keyup);
            el.removeEventListener('blur', el._afHandlers.blur);
        }
        const h = {};
        h.input = () => {
            const tokens = el.value.split(' ').map(t => t.replace(/[^\d]/g, ''));
            const nonEmpty = tokens.filter(t => t !== '');
            (window._af.items || []).forEach((it, i) => {
                if (nonEmpty.length === 1) {
                    // "150 - one value for all GPUs" (the documented Hive hint):
                    // a single typed value applies to every card
                    it[key] = nonEmpty[0];
                } else if (i < tokens.length) {
                    // per-GPU list: token index maps to GPU index; tokens beyond
                    // the GPU count are dropped (our backend rejects unknown
                    // GPU indices, the caret helper warns about them)
                    it[key] = tokens[i];
                }
            });
        };
        h.focus = () => updateAfCaret(el);
        h.click = h.focus;
        h.keyup = h.focus;
        h.blur = () => {
            const helper = document.querySelector('.af-caret[data-caret-for="' + id + '"]');
            if (helper) helper.classList.add('d-none');
        };
        el._afHandlers = h;
        el.addEventListener('input', h.input);
        el.addEventListener('focus', h.focus);
        el.addEventListener('click', h.click);
        el.addEventListener('keyup', h.keyup);
        el.addEventListener('blur', h.blur);
    });
}

// ---------------- Shared: load / reset / collect / save ----------------

function afSetEnabledVisibility() {
    const on = document.getElementById('afEnabledSwitch').checked;
    if (window._af) window._af.enabled = on;
    document.getElementById('afEditorWrap').classList.toggle('d-none', !on);
}

function applyAutofanData(data) {
    // Per-GPU effective values: fall back to the global scalar, then the default
    // (the Hive cloud SPA merges globals into every item the same way)
    const items = (data.gpus || []).map(g => ({
        index: g.index,
        mode: g.mode === 1 ? 'static' : 'auto',
        static_fan: (g.static && g.static > 0) ? g.static : AF_DEFAULTS.static_fan,
        min_fan: g.min !== null && g.min !== undefined ? g.min : (data.min_fan ? parseInt(data.min_fan, 10) || AF_DEFAULTS.min_fan : AF_DEFAULTS.min_fan),
        max_fan: g.max !== null && g.max !== undefined ? g.max : (data.max_fan ? parseInt(data.max_fan, 10) || AF_DEFAULTS.max_fan : AF_DEFAULTS.max_fan),
        target_temp: g.target_core !== null && g.target_core !== undefined ? g.target_core : (data.target_temp ? parseInt(data.target_temp, 10) || AF_DEFAULTS.target_temp : AF_DEFAULTS.target_temp),
        target_mem_temp: g.target_mem !== null && g.target_mem !== undefined ? g.target_mem : (data.target_mem_temp ? parseInt(data.target_mem_temp, 10) || AF_DEFAULTS.target_mem_temp : AF_DEFAULTS.target_mem_temp),
        critical_temp: g.critical !== null && g.critical !== undefined ? g.critical : (data.critical_temp ? parseInt(data.critical_temp, 10) || AF_DEFAULTS.critical_temp : AF_DEFAULTS.critical_temp)
    }));
    afApplyState({
        enabled: data.enabled === '1',
        critical_temp: data.critical_temp,
        critical_action: data.critical_action || '',
        reboot_on_errors: data.reboot_on_errors === '1',
        smart_mode: data.smart_mode === '1',
        no_amd: data.no_amd === '1',
        items: items
    });
}

function afApplyState(state) {
    window._af = state;
    window._afInitial = JSON.stringify(state);

    document.getElementById('afCriticalTempRO').value = state.critical_temp;
    document.getElementById('afCriticalAction').value = state.critical_action;
    document.getElementById('afCriticalActionAdv').value = state.critical_action;
    document.getElementById('afEnabledSwitch').checked = state.enabled;
    document.getElementById('afRebootOnError').checked = state.reboot_on_errors;
    document.getElementById('afSmartMode').checked = state.smart_mode;
    afSetEnabledVisibility();
    renderAfTable();
    renderAfAdvanced();
}

async function loadAutofan() {
    try {
        const response = await apiFetch('/api/autofan');
        const data = await response.json();
        if (response.ok && data.success) {
            window._afData = data;
            applyAutofanData(data);
        }
    } catch (e) { /* silent */ }
}

function afCollectPayload() {
    const af = window._af;
    if (!af) return null;
    const gpus = [];
    for (const it of (af.items || [])) {
        const num = v => {
            if (v === null || v === undefined || v === '') return 0;
            const n = parseInt(v, 10);
            return Number.isNaN(n) ? NaN : n;
        };
        const entry = { index: it.index, mode: it.mode };
        // Static speed is sent for every GPU: the backend stores the whole list
        // (CUSTOM_STATIC_FAN) so typed values stick even for GPUs still in auto
        const st = num(it.static_fan);
        if (it.mode === 'static') {
            if (Number.isNaN(st) || st < 1 || st > 100) {
                showToast('GPU ' + it.index + ': static fan speed must be 1-100% in Static mode.', false);
                return null;
            }
        } else if (!Number.isNaN(st) && (st < 0 || st > 100)) {
            showToast('GPU ' + it.index + ': static fan speed must be 0-100%.', false);
            return null;
        }
        entry.static = Number.isNaN(st) ? 0 : st;
        const pairs = [['min_fan', 'min', 0, 99], ['max_fan', 'max', 1, 100],
                       ['target_temp', 'target_core', 5, 120], ['target_mem_temp', 'target_mem', 10, 120],
                       ['critical_temp', 'critical', 30, 120]];
        for (const [key, out, lo, hi] of pairs) {
            const v = num(it[key]);
            if (Number.isNaN(v) || (v !== 0 && (v < lo || v > hi))) {
                showToast('GPU ' + it.index + ': ' + key.replace('_fan', ' fan').replace(/_/g, ' ') + ' must be between ' + lo + ' and ' + hi + ' (or 0 to use the global default).', false);
                return null;
            }
            entry[out] = v;
        }
        if (entry.min > 0 && entry.max > 0 && entry.min > entry.max) {
            showToast('GPU ' + it.index + ': min fan speed cannot be greater than max.', false);
            return null;
        }
        gpus.push(entry);
    }
    return {
        enabled: af.enabled ? '1' : '0',
        critical_action: document.getElementById('afCriticalAction').value,
        reboot_on_errors: document.getElementById('afRebootOnError').checked ? '1' : '0',
        smart_mode: document.getElementById('afSmartMode').checked ? '1' : '0',
        gpus: gpus
    };
}

// ---------------- Password change ----------------

async function changePassword() {
    const current = document.getElementById('currentPasswordInput').value;
    const newPw = document.getElementById('newPasswordInput').value;
    const confirmPw = document.getElementById('confirmPasswordInput').value;
    const btn = document.getElementById('passwordSaveBtn');

    if (newPw !== confirmPw) {
        showToast('New passwords do not match.', false);
        return;
    }
    if (newPw.trim().length < 4) {
        showToast('New password must be at least 4 characters.', false);
        return;
    }

    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Saving...';
    try {
        const response = await fetch('/api/auth/password', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({
                current_password: current,
                new_password: newPw.trim(),
                apply_to_cluster: document.getElementById('applyToClusterCheck').checked
            })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Password updated.' : 'Failed to change password.'), !!data.success);
        if (response.ok && data.success) {
            bootstrap.Modal.getInstance(document.getElementById('passwordModal')).hide();
        }
    } catch (e) {
        showToast('Network error changing password.', false);
    } finally {
        btn.disabled = false;
        btn.innerHTML = 'Update Password';
    }
}

// ---------------- Helpers ----------------

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ---------------- Statistics tab (HiveOS worker Stats parity) ----------------

const STATS_GPU_COLORS = ['#1993eb', '#86236d', '#00b091', '#d97b00', '#b43e5a', '#80aa19', '#2f69b7', '#9c5935', '#e377c2', '#17becf', '#bcbd22', '#7f7f7f'];
const STATS_EVENT_COLORS = { info: '#2392dc', file: '#c6ccd2', danger: '#ff3733', warning: '#ffae00', success: '#84bf40' };
const statsState = { date: null, days: 1, filter: 'all', data: null, charts: {}, detailChart: null, loading: false };

function statsTodayStr() {
    const d = new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}

function statsAddDaysStr(dateStr, n) {
    const [y, m, d] = dateStr.split('-').map(Number);
    const dt = new Date(y, m - 1, d + n);
    return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
}

function statsDateToTs(dateStr) {
    const [y, m, d] = dateStr.split('-').map(Number);
    return new Date(y, m - 1, d).getTime();
}

function statsTimeLabel(ts, days) {
    const d = new Date(ts * 1000);
    const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    if (days === 3) return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + ' ' + hm;
    return hm;
}

function statsFullLabel(ts) {
    const d = new Date(ts * 1000);
    return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0') + '.' + d.getFullYear() +
        ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function fmtHashRate(mh, withPerS) {
    // Hashrate samples are stored in MH/s (same as /api/stats total_hashrate_mh)
    const units = ['MH', 'GH', 'TH', 'PH'];
    let v = Math.abs(Number(mh) || 0), i = 0;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    const s = v >= 100 ? v.toFixed(1) : v.toFixed(2);
    return s + ' ' + units[i] + (withPerS ? '/s' : '');
}

function statsGradient(ctx, color) {
    const area = ctx.chart.chartArea;
    if (!area) return color + '22';
    const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, color + '4D');
    g.addColorStop(1, color + '0D');
    return g;
}

function statsDownsample(samples, maxPoints) {
    if (samples.length <= maxPoints) return samples;
    const step = Math.ceil(samples.length / maxPoints);
    const out = [];
    for (let i = 0; i < samples.length; i += step) out.push(samples[i]);
    if (out[out.length - 1][0] !== samples[samples.length - 1][0]) out.push(samples[samples.length - 1]);
    return out;
}

function statsBaseOptions(unitFmt, beginAtZero, tickFmt) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: { display: false },
            tooltip: {
                callbacks: {
                    title: (items) => statsFullLabel(statsState._labelTs[items[0].dataIndex] || 0),
                    label: (ctx) => ' ' + ctx.dataset.label + ': ' + unitFmt(ctx.parsed.y)
                }
            }
        },
        scales: {
            x: {
                ticks: { color: 'rgba(198,204,210,0.55)', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
                grid: { display: false },
                border: { color: 'rgba(198,204,210,0.18)' }
            },
            y: {
                beginAtZero: beginAtZero,
                ticks: { color: 'rgba(198,204,210,0.55)', font: { size: 10 }, callback: tickFmt },
                grid: { color: 'rgba(198,204,210,0.07)' },
                border: { display: false }
            }
        }
    };
}

function statsSeriesDatasets(samples, col) {
    let gpuCount = 0;
    samples.forEach(s => { gpuCount = Math.max(gpuCount, (s[col] || []).length); });
    const ds = [];
    for (let i = 0; i < gpuCount; i++) {
        const color = STATS_GPU_COLORS[i % STATS_GPU_COLORS.length];
        ds.push({
            label: 'GPU ' + i,
            data: samples.map(s => (s[col] || [])[i] !== undefined ? (s[col] || [])[i] : null),
            borderColor: color,
            backgroundColor: (c) => statsGradient(c, color),
            fill: true, borderWidth: 1.5, pointRadius: 0, pointHitRadius: 8, tension: 0.25, spanGaps: true
        });
    }
    return ds;
}

function statsSingleDataset(samples, col, color, label) {
    return [{
        label: label,
        data: samples.map(s => s[col]),
        borderColor: color,
        backgroundColor: (c) => statsGradient(c, color),
        fill: true, borderWidth: 1.5, pointRadius: 0, pointHitRadius: 8, tension: 0.25
    }];
}

function statsMakeChart(key, canvasId, labels, datasets, options) {
    if (statsState.charts[key]) { statsState.charts[key].destroy(); statsState.charts[key] = null; }
    const el = document.getElementById(canvasId);
    if (!el || typeof Chart === 'undefined') return;
    statsState.charts[key] = new Chart(el.getContext('2d'), { type: 'line', data: { labels: labels, datasets: datasets }, options: options });
}

function statsNoChartFallback() {
    document.querySelectorAll('#statsTabContainer .stats-chart-box').forEach(box => {
        if (!box.dataset.fallback) {
            box.dataset.fallback = '1';
            box.innerHTML = '<div class="stats-nochart">Charts unavailable: Chart.js could not be loaded</div>';
        }
    });
}

async function loadMetricsTab() {
    if (!statsState.date) statsState.date = statsTodayStr();
    if (statsState.loading) return;
    statsState.loading = true;
    try {
        const res = await apiFetch('/api/metrics/history?date=' + statsState.date + '&days=' + statsState.days);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        statsState.data = await res.json();
        renderStatsTab();
    } catch (e) {
        console.error('Metrics history load failed:', e);
    } finally {
        statsState.loading = false;
    }
}

function renderStatsTab() {
    const data = statsState.data;
    if (!data) return;
    const allSamples = data.samples || [];
    const samples = statsDownsample(allSamples, 400);
    statsState._labelTs = samples.map(s => s[0]);
    const labels = samples.map(s => statsTimeLabel(s[0], data.days));

    // Date navigation + period buttons
    document.getElementById('statsDateLabel').textContent = statsRangeLabel(data.start, data.days);
    document.getElementById('statsSeparatorDate').textContent = statsRangeLabel(statsAddDaysStr(data.start, data.days - 1), 1);
    document.getElementById('stats1dBtn').classList.toggle('active', data.days === 1);
    document.getElementById('stats3dBtn').classList.toggle('active', data.days === 3);
    document.getElementById('statsNextBtn').disabled = statsAddDaysStr(data.start, data.days - 1) >= statsTodayStr();

    // Algo titles
    const algo = data.algo || 'Hashrate';
    document.getElementById('statsHashTitle').textContent = algo;
    document.getElementById('statsHashTotalTitle').textContent = algo;

    // Current-value chips from the last raw sample
    const last = allSamples.length ? allSamples[allSamples.length - 1] : null;
    statsRenderChips('statsTempChips', last ? last[1] : [], v => v + '\u00B0');
    statsRenderChips('statsFanChips', last ? last[2] : [], v => v + '%');
    statsRenderChips('statsPowerChips', last ? last[3] : [], v => Math.round(v) + 'W');
    statsRenderChips('statsHashChips', last ? last[4] : [], v => fmtHashRate(v, true));

    // Totals + min/mean/max
    statsRenderTotals();
    const hrs = allSamples.map(s => s[6] || 0).filter(v => v > 0);
    if (hrs.length) {
        const min = Math.min.apply(null, hrs), max = Math.max.apply(null, hrs);
        const mean = hrs.reduce((a, b) => a + b, 0) / hrs.length;
        document.getElementById('statsHashMin').textContent = fmtHashRate(min, true);
        document.getElementById('statsHashMean').textContent = fmtHashRate(mean, true);
        document.getElementById('statsHashMax').textContent = fmtHashRate(max, true);
    } else {
        document.getElementById('statsHashMin').textContent = '0';
        document.getElementById('statsHashMean').textContent = '0';
        document.getElementById('statsHashMax').textContent = '0';
    }

    if (typeof Chart === 'undefined') { statsNoChartFallback(); return; }

    // Per-GPU charts
    statsMakeChart('temp', 'statsTempChart', labels, statsSeriesDatasets(samples, 1),
        statsBaseOptions(v => v + '\u00B0C', false, v => v + '\u00B0'));
    statsMakeChart('fan', 'statsFanChart', labels, statsSeriesDatasets(samples, 2),
        statsBaseOptions(v => v + '%', true, v => v + '%'));
    statsMakeChart('power', 'statsPowerChart', labels, statsSeriesDatasets(samples, 3),
        statsBaseOptions(v => v + 'W', true, v => v + 'W'));
    statsMakeChart('hashrate', 'statsHashChart', labels, statsSeriesDatasets(samples, 4),
        statsBaseOptions(v => fmtHashRate(v, false), false, v => fmtHashRate(v, false)));
    statsMakeChart('powertotal', 'statsPowerTotalChart', labels, statsSingleDataset(samples, 5, '#2392dc', 'Power'),
        statsBaseOptions(v => v + 'W', true, v => v + 'W'));
    statsMakeChart('hashtotal', 'statsHashTotalChart', labels, statsSingleDataset(samples, 6, '#00b091', algo),
        statsBaseOptions(v => fmtHashRate(v, false), false, v => fmtHashRate(v, false)));

    statsRenderActivity();
}

function statsRangeLabel(startStr, days) {
    const fmt = (s) => {
        const [y, m, d] = s.split('-').map(Number);
        return new Date(y, m - 1, d).toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
    };
    if (days === 1) return fmt(startStr);
    return fmt(startStr) + ' \u2013 ' + fmt(statsAddDaysStr(startStr, days - 1));
}

function statsRenderChips(containerId, values, fmt) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!values || !values.length) {
        el.innerHTML = '<span class="text-muted small">No samples for this range yet</span>';
        return;
    }
    el.innerHTML = values.map((v, i) =>
        '<span class="stat-chip"><span class="stat-chip-val">' + escapeHtml(fmt(v)) + '</span>' +
        '<span class="stat-chip-dot" style="background:' + STATS_GPU_COLORS[i % STATS_GPU_COLORS.length] + '"></span>' +
        '<small>' + i + '</small></span>'
    ).join('');
}

function statsRenderTotals() {
    const data = statsState.data;
    const samples = data.samples || [];
    let kwh = 0;
    for (let i = 1; i < samples.length; i++) {
        const dt = samples[i][0] - samples[i - 1][0];
        // 60s sampler; bridge gaps up to 10 min (missed cycles / service restart)
        if (dt > 0 && dt <= 600) kwh += ((samples[i - 1][5] || 0) * dt) / 3600000;
    }
    const rate = Number(data.rate || 0);
    document.getElementById('statsKwhValue').innerHTML = kwh.toFixed(2) + '<small>kWh</small>';
    document.getElementById('statsRateValue').textContent = String(rate);
    document.getElementById('statsCostValue').textContent = (kwh * rate).toFixed(2);
    const last = samples.length ? samples[samples.length - 1] : null;
    document.getElementById('statsPowerTotalValue').innerHTML = last
        ? ((last[5] || 0) / 1000).toFixed(3) + '<small>kW</small>'
        : '&mdash;';
    document.getElementById('statsHashTotalValue').innerHTML = last
        ? escapeHtml(fmtHashRate(last[6] || 0, true))
        : '&mdash;';
}

function statsActivityBuckets() {
    const data = statsState.data;
    const startTs = statsDateToTs(data.start);
    const bucketMs = data.days === 3 ? 4 * 3600 * 1000 : 3600 * 1000;
    const nBuckets = Math.ceil((data.days * 86400 * 1000) / bucketMs);
    return { startTs, bucketMs, nBuckets };
}

function statsRenderActivity() {
    const data = statsState.data;
    if (statsState.charts.activity) { statsState.charts.activity.destroy(); statsState.charts.activity = null; }
    const canvas = document.getElementById('statsActivityChart');
    if (!canvas || typeof Chart === 'undefined') return;
    const { startTs, bucketMs, nBuckets } = statsActivityBuckets();
    const counts = {};
    Object.keys(STATS_EVENT_COLORS).forEach(lv => { counts[lv] = new Array(nBuckets).fill(0); });
    (data.events || []).forEach(e => {
        const idx = Math.floor((e.ts * 1000 - startTs) / bucketMs);
        if (idx >= 0 && idx < nBuckets && counts[e.level]) counts[e.level][idx]++;
    });
    const levels = statsState.filter === 'all' ? Object.keys(STATS_EVENT_COLORS) : [statsState.filter];
    const labels = Array.from({ length: nBuckets }, (_, i) => statsTimeLabel((startTs + i * bucketMs) / 1000, data.days));
    const datasets = levels.map(lv => ({
        label: lv,
        data: counts[lv],
        backgroundColor: STATS_EVENT_COLORS[lv],
        stack: 'events', barPercentage: 0.6, categoryPercentage: 0.9, borderWidth: 0
    }));
    const options = {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: { display: false },
            tooltip: {
                callbacks: {
                    title: (items) => statsFullLabel((startTs + items[0].dataIndex * bucketMs) / 1000),
                    label: (ctx) => ' ' + ctx.dataset.label + ': ' + ctx.parsed.y
                }
            }
        },
        scales: {
            x: {
                stacked: true,
                ticks: { color: 'rgba(198,204,210,0.55)', font: { size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 },
                grid: { display: false },
                border: { color: 'rgba(198,204,210,0.18)' }
            },
            y: {
                stacked: true, beginAtZero: true,
                ticks: { color: 'rgba(198,204,210,0.55)', font: { size: 10 }, precision: 0 },
                grid: { color: 'rgba(198,204,210,0.07)' },
                border: { display: false }
            }
        }
    };
    statsState.charts.activity = new Chart(canvas.getContext('2d'), { type: 'bar', data: { labels: labels, datasets: datasets }, options: options });
}

function setActivityFilter(filter) {
    statsState.filter = filter || 'all';
    document.querySelectorAll('#statsActivityFilters .stats-filter-link').forEach(a => {
        a.classList.toggle('active', a.dataset.filter === statsState.filter);
    });
    if (statsState.data) statsRenderActivity();
}

function statsRenderEventList(container, events) {
    if (!events.length) {
        container.innerHTML = '<div class="text-muted small">No events in this range</div>';
        return;
    }
    container.innerHTML = events.slice().reverse().map(e =>
        '<div class="stats-event-row">' +
        '<span class="stats-legend-dot" style="background:' + (STATS_EVENT_COLORS[e.level] || '#c6ccd2') + '"></span>' +
        '<span class="stats-event-time">' + escapeHtml(statsFullLabel(e.ts)) + '</span>' +
        '<span class="stats-event-msg">' + escapeHtml(e.message) + '</span></div>'
    ).join('');
}

function openStatsDetail(key) {
    const data = statsState.data;
    if (!data || typeof Chart === 'undefined') return;
    const algo = data.algo || 'Hashrate';
    const titles = { activity: 'Activity', temp: 'TEMP', fan: 'FAN', power: 'POWER', hashrate: algo, powertotal: 'Power', hashtotal: algo + ' (total)' };
    document.getElementById('statsDetailTitle').textContent = titles[key] || 'Detail view';
    const eventsBox = document.getElementById('statsDetailEvents');
    const isActivity = key === 'activity';
    eventsBox.classList.toggle('d-none', !isActivity);
    if (isActivity) {
        statsRenderEventList(eventsBox, (data.events || []).filter(e => statsState.filter === 'all' || e.level === statsState.filter));
    }
    if (statsState.detailChart) { statsState.detailChart.destroy(); statsState.detailChart = null; }

    const samples = statsDownsample(data.samples || [], 1200);
    statsState._labelTs = samples.map(s => s[0]);
    const labels = samples.map(s => statsTimeLabel(s[0], data.days));
    let datasets, options;
    if (isActivity) {
        const { startTs, bucketMs, nBuckets } = statsActivityBuckets();
        const counts = {};
        Object.keys(STATS_EVENT_COLORS).forEach(lv => { counts[lv] = new Array(nBuckets).fill(0); });
        (data.events || []).forEach(e => {
            const idx = Math.floor((e.ts * 1000 - startTs) / bucketMs);
            if (idx >= 0 && idx < nBuckets && counts[e.level]) counts[e.level][idx]++;
        });
        const levels = statsState.filter === 'all' ? Object.keys(STATS_EVENT_COLORS) : [statsState.filter];
        const alabels = Array.from({ length: nBuckets }, (_, i) => statsTimeLabel((startTs + i * bucketMs) / 1000, data.days));
        datasets = levels.map(lv => ({
            label: lv, data: counts[lv], backgroundColor: STATS_EVENT_COLORS[lv],
            stack: 'events', barPercentage: 0.6, categoryPercentage: 0.9, borderWidth: 0
        }));
        options = {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => ' ' + ctx.dataset.label + ': ' + ctx.parsed.y } } },
            scales: {
                x: { stacked: true, ticks: { color: 'rgba(198,204,210,0.6)', font: { size: 11 }, autoSkip: true, maxTicksLimit: 12 }, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, ticks: { color: 'rgba(198,204,210,0.6)', precision: 0 }, grid: { color: 'rgba(198,204,210,0.07)' } }
            }
        };
        statsState.detailChart = new Chart(document.getElementById('statsDetailChart'), { type: 'bar', data: { labels: alabels, datasets: datasets }, options: options });
    } else {
        const cfg = {
            temp: () => [statsSeriesDatasets(samples, 1), statsBaseOptions(v => v + '\u00B0C', false, v => v + '\u00B0'), 'line'],
            fan: () => [statsSeriesDatasets(samples, 2), statsBaseOptions(v => v + '%', true, v => v + '%'), 'line'],
            power: () => [statsSeriesDatasets(samples, 3), statsBaseOptions(v => v + 'W', true, v => v + 'W'), 'line'],
            hashrate: () => [statsSeriesDatasets(samples, 4), statsBaseOptions(v => fmtHashRate(v, false), false, v => fmtHashRate(v, false)), 'line'],
            powertotal: () => [statsSingleDataset(samples, 5, '#2392dc', 'Power'), statsBaseOptions(v => v + 'W', true, v => v + 'W'), 'line'],
            hashtotal: () => [statsSingleDataset(samples, 6, '#00b091', algo), statsBaseOptions(v => fmtHashRate(v, false), false, v => fmtHashRate(v, false)), 'line']
        };
        if (!cfg[key]) return;
        const built = cfg[key]();
        datasets = built[0]; options = built[1];
        statsState.detailChart = new Chart(document.getElementById('statsDetailChart'), { type: built[2], data: { labels: labels, datasets: datasets }, options: options });
    }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDetailModal')).show();
}

async function exportMetricsCsv() {
    try {
        const res = await apiFetch('/api/metrics/export?date=' + statsState.date + '&days=' + statsState.days);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'metrics_' + statsState.date + '_' + statsState.days + 'd.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e) {
        showToast('CSV export failed.', false);
    }
}

function openRateEdit() {
    document.getElementById('statsRateInput').value = statsState.data ? Number(statsState.data.rate || 0) : '';
    document.getElementById('statsRateEditBlock').classList.remove('d-none');
    document.getElementById('statsRateInput').focus();
}

async function saveMetricsRate() {
    const input = document.getElementById('statsRateInput');
    const v = parseFloat(input.value);
    if (!Number.isFinite(v) || v < 0) {
        showToast('Rate must be a non-negative number.', false);
        return;
    }
    const btn = document.getElementById('statsRateSaveBtn');
    btn.disabled = true;
    try {
        const res = await apiFetch('/api/metrics/rate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rate: v })
        });
        const data = await res.json().catch(() => ({}));
        showToast(data.message || (data.success ? 'Rate saved.' : 'Failed to save rate.'), !!data.success);
        if (data.success) {
            document.getElementById('statsRateEditBlock').classList.add('d-none');
            if (statsState.data) statsState.data.rate = data.rate;
            statsRenderTotals();
        }
    } catch (e) {
        showToast('Network error saving rate.', false);
    } finally {
        btn.disabled = false;
    }
}

function setStatsPeriod(days) {
    statsState.days = days;
    statsState.date = statsAddDaysStr(statsTodayStr(), -(days - 1));
    loadMetricsTab();
}

function statsShiftDate(delta) {
    statsState.date = statsAddDaysStr(statsState.date, delta * statsState.days);
    loadMetricsTab();
}

function initStatsTab() {
    document.getElementById('statsPrevBtn').addEventListener('click', () => statsShiftDate(-1));
    document.getElementById('statsNextBtn').addEventListener('click', () => statsShiftDate(1));
    document.getElementById('stats1dBtn').addEventListener('click', () => setStatsPeriod(1));
    document.getElementById('stats3dBtn').addEventListener('click', () => setStatsPeriod(3));
    document.getElementById('statsRefreshBtn').addEventListener('click', () => loadMetricsTab());
    document.getElementById('statsExportBtn').addEventListener('click', exportMetricsCsv);
    document.getElementById('statsRateEditBtn').addEventListener('click', openRateEdit);
    document.getElementById('statsRateSaveBtn').addEventListener('click', saveMetricsRate);
    document.getElementById('statsRateCancelBtn').addEventListener('click', () => {
        document.getElementById('statsRateEditBlock').classList.add('d-none');
    });
    document.querySelectorAll('#statsActivityFilters .stats-filter-link').forEach(a => {
        a.addEventListener('click', (ev) => { ev.preventDefault(); setActivityFilter(a.dataset.filter); });
    });
    document.querySelectorAll('#statsTabContainer .stats-detail-btn').forEach(b => {
        b.addEventListener('click', () => openStatsDetail(b.dataset.detail));
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initStatsTab);
} else {
    initStatsTab();
}
