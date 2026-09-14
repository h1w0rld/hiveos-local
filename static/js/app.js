// Global state to store overclock configurations
let activeOverclocks = {};
let csrfToken = '';
let activeHardwareTab = 'gpus';
let lastStatsData = null;
let lastHugepagesEnabled = false;

// ---- Cluster / remote rig state ----
let currentRigId = 'self';
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
        speedEl.innerHTML = fmtSpeedHtml(totalHashrate, data.miner_algo || '');
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

function fmtSpeedHtml(mh, algo) {
    const base = '<span class="text-primary-gradient fw-bold">' + fmtSpeed(mh) + '</span>';
    return algo ? base + '<div class="small text-muted">' + escapeHtml(algo) + '</div>' : base;
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
                revertBtn.classList.remove('d-none');
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
    const statsInterval = setInterval(fetchStats, 5000); // Poll every 5s
    const updateInterval = setInterval(checkUpdate, 600000); // Check updates every 10m

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

    // Reboot / Shutdown bindings
    document.getElementById('rigRebootBtn').addEventListener('click', async () => {
        if (confirm("Are you sure you want to REBOOT this rig? Mining operations will be suspended during reboot.")) {
            sendSystemPowerAction('/api/system/reboot', 'rigRebootBtn');
        }
    });
    
    document.getElementById('rigShutdownBtn').addEventListener('click', async () => {
        if (confirm("Are you sure you want to SHUTDOWN this rig? Power will be cut from the hardware.")) {
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
    
    // Flight Sheets: wallets select and quick-save form
    populateFsheetMiners();
    document.getElementById('saveFsheetForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        const walletVal = document.getElementById('fsWalletSelect').value;
        const payload = {
            fsheet: {
                id: window._editingFsheetId || '',
                name: document.getElementById('fsName').value.trim(),
                coin: document.getElementById('fsCoin').value.trim(),
                wallet: walletVal === '__custom__' ? document.getElementById('fsWalletCustom').value.trim() : walletVal,
                pool: document.getElementById('fsPool').value.trim(),
                miner: document.getElementById('fsMinerSelect').value
            }
        };
        const submitBtn = this.querySelector('button[type="submit"]');
        const origHTML = submitBtn.innerHTML;
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
                this.reset();
                window._editingFsheetId = null;
                document.getElementById('fsWalletCustom').classList.add('d-none');
                populateFsheetMiners();
                loadFsheets();
            }
        } catch (error) {
            showToast("Network error saving flight sheet.", false);
        } finally {
            submitBtn.disabled = false;
            submitBtn.innerHTML = origHTML;
        }
    });

    document.getElementById('fsWalletSelect').addEventListener('change', function() {
        document.getElementById('fsWalletCustom').classList.toggle('d-none', this.value !== '__custom__');
    });

    document.getElementById('manageWalletsBtn').addEventListener('click', openWalletModal);
    document.getElementById('saveWalletForm').addEventListener('submit', addWallet);
    document.getElementById('importFsheetsBtn').addEventListener('click', () => {
        document.getElementById('fsheetImportText').value = '';
        new bootstrap.Modal(document.getElementById('fsheetImportModal')).show();
    });
    document.getElementById('fsheetImportFile').addEventListener('change', function() {
        const file = this.files && this.files[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => { document.getElementById('fsheetImportText').value = reader.result; };
        reader.readAsText(file);
    });
    document.getElementById('fsheetImportSaveBtn').addEventListener('click', importFsheets);
    document.getElementById('fansRefreshBtn').addEventListener('click', loadFans);

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
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
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
    
    // Autofan form submit
    document.getElementById('autofanForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        const btn = this.querySelector('button[type="submit"]');
        btn.disabled = true;
        
        const payload = {
            enabled: document.getElementById('afEnabled').value,
            target_temp: document.getElementById('afTargetCore').value,
            target_mem_temp: document.getElementById('afTargetMem').value,
            min_fan: document.getElementById('afMinFan').value,
            max_fan: document.getElementById('afMaxFan').value,
            critical_temp: document.getElementById('afCriticalTemp').value
        };
        
        try {
            const response = await apiFetch('/api/autofan', {
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
                showToast(data.message || "Failed to save autofan settings.", false);
            }
        } catch (error) {
            showToast("Network error saving autofan settings.", false);
        } finally {
            btn.disabled = false;
        }
    });

    // Save Preset form submit
    document.getElementById('savePresetForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        const input = document.getElementById('newPresetName');
        const name = input.value.trim();
        const btn = this.querySelector('button[type="submit"]');
        btn.disabled = true;
        
        try {
            const response = await apiFetch('/api/presets/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken
                },
                body: JSON.stringify({ name: name })
            });
            const data = await response.json();
            if (response.ok && data.success) {
                showToast(data.message, true);
                input.value = '';
                loadPresetsList();
            } else {
                showToast(data.message || "Failed to save preset.", false);
            }
        } catch (error) {
            showToast("Network error saving preset profile.", false);
        } finally {
            btn.disabled = false;
        }
    });

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
        new bootstrap.Modal(document.getElementById('passwordModal')).show();
    });
    document.getElementById('passwordSaveBtn').addEventListener('click', changePassword);

    // Cluster page bindings
    document.getElementById('syncNowBtn').addEventListener('click', syncNow);
    document.getElementById('clusterNameSaveBtn').addEventListener('click', saveClusterSettings);
    document.getElementById('addRigBtn').addEventListener('click', () => openRigModal(null));

    // Access page bindings
    document.getElementById('accessRefreshBtn').addEventListener('click', loadAccessList);
    document.getElementById('addAccessBtn').addEventListener('click', () => openAccessModal(null, null));
    document.getElementById('addJumpBtn').addEventListener('click', () => openJumpModal(null));
    document.getElementById('jumpSaveBtn').addEventListener('click', saveJumpModal);
    document.getElementById('jumpServerAuthSelect').addEventListener('change', function() {
        document.getElementById('jumpServerPasswordBlock').classList.toggle('d-none', this.value !== 'password');
        document.getElementById('jumpServerKeyBlock').classList.toggle('d-none', this.value !== 'key');
    });

    // 7. Cluster polling (only while the cluster view is active)
    setInterval(() => {
        if (activeView === 'cluster') {
            loadClusterData(true);
        }
    }, 5000);

    // Initial view from URL hash
    const initialView = (location.hash || '').replace('#', '');
    showView(['cluster', 'accesses', 'dashboard'].includes(initialView) ? initialView : 'cluster');
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
    lastStatsData = null;
    updateRigScopeUi();
    fetchStats();
    loadTuningSettings();
    loadPresetsList();
    loadFsheets();
    loadFans();
    showToast('Switched to ' + (rigId === 'self' ? 'the local rig' : getRigName(rigId)), true);
}

function getRigName(rigId) {
    if (clusterData && clusterData.rigs) {
        const rig = clusterData.rigs.find(r => r.id === rigId);
        if (rig) return rig.name;
    }
    return rigId === 'self' ? 'This rig' : rigId;
}

function updateRigScopeUi() {
    // Top-bar rig selector: shows the managed rig with a REMOTE marker
    const isSelf = currentRigId === 'self' || !currentRigId;
    document.getElementById('rigScopeName').textContent = isSelf ? getRigName(currentRigId) : getRigName(currentRigId);
    document.getElementById('rigScopeBadge').classList.toggle('d-none', isSelf);
    renderRigScopeMenu();
}

function renderRigScopeMenu() {
    const menu = document.getElementById('rigScopeMenu');
    if (!menu || !clusterData || !clusterData.rigs) return;
    const items = [];
    const selfRig = clusterData.rigs.find(r => r.is_self);
    if (selfRig) {
        items.push('<li><button class="dropdown-item' + (currentRigId === 'self' ? ' active' : '') + '" onclick="switchRigGlobal(\'self\')">' +
            '<i class="bi bi-hdd-network me-2"></i>' + escapeHtml(selfRig.name) + ' <span class="small text-muted">(local)</span></button></li>');
        items.push('<li><hr class="dropdown-divider"></li>');
    }
    clusterData.rigs.filter(r => !r.is_self).forEach(rig => {
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

// Fetch stats from backend API
async function fetchStats() {
    try {
        const response = await apiFetch('/api/stats');
        
        // Handle 401 Unauthorized status
        if (response.status === 401) {
            document.getElementById('loginOverlay').classList.remove('d-none');
            document.getElementById('revertSettingsBtn').classList.add('d-none');
            document.getElementById('emergencyResetClocksBtn').classList.add('d-none');
            return;
        }
        
        if (!response.ok) {
            const errText = await response.text();
            throw new Error(`HTTP ${response.status}: ${errText}`);
        }
        
        const data = await response.json();
        
        // Hide login if active
        if (!document.getElementById('loginOverlay').classList.contains('d-none')) {
            document.getElementById('loginOverlay').classList.add('d-none');
            document.getElementById('revertSettingsBtn').classList.remove('d-none');
            document.getElementById('emergencyResetClocksBtn').classList.remove('d-none');
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
        document.getElementById('statCoin').textContent = data.system.coin || 'Unknown';
        
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
        const nvMem = activeOverclocks.nvidia?.mem?.[index] || "";
        const nvPl = activeOverclocks.nvidia?.pl?.[index] || "";
        const nvFan = activeOverclocks.nvidia?.fan?.[index] || "";
        
        document.getElementById('nvCore').value = nvCore === "0" ? "" : nvCore;
        document.getElementById('nvMem').value = nvMem === "0" ? "" : nvMem;
        document.getElementById('nvPl').value = nvPl === "0" ? "" : nvPl;
        document.getElementById('nvFan').value = nvFan === "0" ? "" : nvFan;

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
                document.getElementById('afEnabled').value = afData.enabled;
                document.getElementById('afTargetCore').value = afData.target_temp;
                document.getElementById('afTargetMem').value = afData.target_mem_temp;
                document.getElementById('afMinFan').value = afData.min_fan;
                document.getElementById('afMaxFan').value = afData.max_fan;
                document.getElementById('afCriticalTemp').value = afData.critical_temp;
            }
        }
        
        
        loadFsheets();
        loadPresetsList();
    } catch (error) {
        console.error("Failed to load tuning configs:", error);
    }
}

// Load list of saved configuration presets
async function loadPresetsList() {
    const container = document.getElementById('presetsContainer');
    try {
        const response = await apiFetch('/api/presets');
        const data = await response.json();
        if (response.ok && data.success) {
            if (data.presets.length === 0) {
                container.innerHTML = `<div class="text-center text-muted small py-4">No profiles saved yet. Save active flight sheet as a preset.</div>`;
                return;
            }
            
            let html = '<div class="list-group list-group-flush border border-secondary-subtle rounded bg-dark-card">';
            data.presets.forEach(name => {
                html += `
                    <div class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-2 px-3">
                        <span class="fw-semibold text-white small">${name}</span>
                        <div class="d-flex gap-2">
                            <button class="btn btn-xs btn-success fw-semibold py-1 px-2 apply-preset-btn" data-preset="${name}">
                                <i class="bi bi-play-circle-fill"></i> Swap
                            </button>
                            <button class="btn btn-xs btn-outline-danger py-1 px-2 delete-preset-btn" data-preset="${name}">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </div>`;
            });
            html += '</div>';
            container.innerHTML = html;
            
            document.querySelectorAll('.apply-preset-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const name = this.getAttribute('data-preset');
                    if (confirm(`Are you sure you want to load profile preset "${name}"? Active miner config files will be overwritten and miner restarted.`)) {
                        applyPreset(name);
                    }
                });
            });
            
            document.querySelectorAll('.delete-preset-btn').forEach(btn => {
                btn.addEventListener('click', function() {
                    const name = this.getAttribute('data-preset');
                    if (confirm(`Are you sure you want to delete profile preset "${name}"?`)) {
                        deletePreset(name);
                    }
                });
            });
        } else {
            container.innerHTML = `<div class="text-danger small py-3 text-center">Failed to load presets.</div>`;
        }
    } catch (error) {
        container.innerHTML = `<div class="text-danger small py-3 text-center">Connection error.</div>`;
    }
}

async function applyPreset(name) {
    try {
        const response = await apiFetch('/api/presets/apply', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify({ name: name })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            fetchStats();
        } else {
            showToast(data.message || "Failed to apply profile preset.", false);
        }
    } catch (error) {
        showToast("Network error applying preset.", false);
    }
}

async function deletePreset(name) {
    try {
        const response = await apiFetch('/api/presets/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken
            },
            body: JSON.stringify({ name: name })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showToast(data.message, true);
            loadPresetsList();
        } else {
            showToast(data.message || "Failed to delete preset.", false);
        }
    } catch (error) {
        showToast("Network error deleting preset.", false);
    }
}

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
            '<div class="col-12 text-center py-5"><div class="spinner-border text-primary" role="status"></div><p class="mt-2 text-muted">Loading cluster rigs...</p></div>';
    }
    try {
        const response = await fetch('/api/cluster/rigs');
        if (response.status === 401) {
            document.getElementById('loginOverlay').classList.remove('d-none');
            return;
        }
        const data = await response.json();
        if (response.ok && data.success) {
            clusterData = data;
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

function renderCluster() {
    if (!clusterData || !clusterData.rigs) return;
    const container = document.getElementById('clusterRigsContainer');
    container.dataset.loaded = '1';

    // Cluster settings
    document.getElementById('clusterNameBadge').textContent = clusterData.cluster_name || '';
    document.getElementById('clusterNameBadge').classList.toggle('d-none', !clusterData.cluster_name);
    const nameInput = document.getElementById('clusterNameInput');
    if (document.activeElement !== nameInput) {
        nameInput.value = clusterData.cluster_name || '';
    }
    document.getElementById('clusterSyncStatus').textContent = clusterData.last_sync_message || '';

    const online = clusterData.rigs.filter(r => r.online).length;
    document.getElementById('clusterOnline').textContent = online + ' / ' + clusterData.rigs.length;

    if (clusterData.last_sync > 0) {
        const ago = Math.max(0, Math.round((Date.now() / 1000) - clusterData.last_sync));
        document.getElementById('clusterLastSync').textContent = ago < 60 ? ago + 's ago' : Math.round(ago / 60) + 'm ago';
        document.getElementById('clusterLastSync').className = 'stat-value ' + (clusterData.last_sync_ok ? 'text-success' : 'text-danger');
    }

    // Farm-wide totals: only online rigs count (offline stats are stale)
    let totalPower = 0, totalGpus = 0, tempSum = 0, tempCount = 0;
    const speedByAlgo = {};
    clusterData.rigs.forEach(rig => {
        const stats = rig.stats;
        if (!stats || !rig.online) return;
        const mh = (stats.total_hashrate_mh || 0) + (stats.system && stats.system.cpu ? stats.system.cpu.hashrate / 1000 : 0);
        const key = stats.miner_algo || (stats.system && stats.system.coin) || '';
        speedByAlgo[key] = (speedByAlgo[key] || 0) + mh;
        (stats.gpus || []).forEach(g => {
            totalPower += g.power || 0;
            tempSum += g.temp || 0;
            tempCount += 1;
        });
        totalGpus += (stats.gpus || []).length;
    });
    const algoKeys = Object.keys(speedByAlgo).filter(k => speedByAlgo[k] > 0);
    let speedHtml;
    if (algoKeys.length === 0) {
        speedHtml = '0 MH/s';
    } else if (algoKeys.length === 1) {
        speedHtml = fmtSpeedHtml(speedByAlgo[algoKeys[0]], algoKeys[0] !== '' ? algoKeys[0] : '');
    } else {
        speedHtml = '<div class="small text-primary-gradient fw-bold">' + algoKeys.map(k =>
            fmtSpeed(speedByAlgo[k]) + ' <span class="text-muted fw-normal">' + escapeHtml(k) + '</span>').join('<br>') + '</div>';
    }
    document.getElementById('clusterTotalHashrate').innerHTML = speedHtml;
    document.getElementById('clusterTotalPower').textContent = totalPower.toFixed(1) + ' W';
    document.getElementById('clusterTotalGpus').textContent = totalGpus;
    document.getElementById('clusterAvgTemp').textContent = (tempCount ? (tempSum / tempCount).toFixed(1) : 0) + ' °C';

    // Rig cards
    container.innerHTML = '';
    if (clusterData.rigs.length === 0) {
        container.innerHTML = '<div class="col-12 text-center py-4"><p class="text-muted">No rigs in the cluster yet. Add the first rig with the button above.</p></div>';
    }
    clusterData.rigs.forEach(rig => {
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

        const statusBadge = isSelf
            ? '<span class="badge bg-success-glow border border-success text-success">THIS RIG</span>'
            : (isOnline
                ? '<span class="badge bg-success-glow border border-success text-success"><span class="pulse-indicator"></span>ONLINE</span>'
                : '<span class="badge bg-danger-glow border border-danger text-danger" title="' + escapeHtml(rig.last_error || '') + '">OFFLINE</span>');

        const col = document.createElement('div');
        col.className = 'col-md-6 col-lg-4' + (isOnline ? '' : ' rig-offline');
        col.innerHTML = `
            <div class="card glass-card h-100">
                <div class="card-body d-flex flex-column">
                    <div class="d-flex justify-content-between align-items-center mb-2">
                        <h3 class="h6 fw-bold mb-0">${escapeHtml(rig.name || rig.id)}</h3>
                        ${statusBadge}
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
                            <div class="fw-semibold small text-primary-gradient fw-bold">${totalHash}${isOnline && stats && stats.miner_algo ? '<div class="small text-muted fw-normal">' + escapeHtml(stats.miner_algo) + '</div>' : ''}</div>
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
                            <div class="fw-semibold small text-warning">${isOnline ? escapeHtml(system.coin || 'None') : '—'}</div>
                        </div>
                        <div class="col-4">
                            <div class="small text-muted">Power</div>
                            <div class="fw-semibold small text-danger-emphasis">${power}</div>
                        </div>
                    </div>
                    <div class="mt-2 d-flex gap-2">
                        <button class="btn btn-sm btn-primary flex-grow-1 fw-semibold py-2 d-flex align-items-center justify-content-center gap-1" onclick="openRigDashboard('${rig.id}')" ${isSelf ? '' : (isOnline ? '' : 'disabled')}>
                            <i class="bi bi-gear-wide-connected"></i> Manage Rig
                        </button>
                        <button class="btn btn-sm btn-outline-secondary" title="Edit rig" onclick="openRigModal('${rig.id}')">
                            <i class="bi bi-pencil"></i>
                        </button>
                        ${isSelf ? '' : '<button class="btn btn-sm btn-outline-danger" title="Remove rig from cluster" onclick="deleteRig(\'' + rig.id + '\')"><i class="bi bi-trash"></i></button>'}
                    </div>
                </div>
            </div>`;
        container.appendChild(col);
    });
}

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
    setTimeout(() => loadClusterData(true), 2500);
};

function saveClusterSettings() {
    const name = document.getElementById('clusterNameInput').value.trim();
    fetch('/api/cluster/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
        body: JSON.stringify({ cluster_name: name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            showToast(data.message, true);
            loadClusterData(true);
        } else {
            showToast(data.message || 'Failed to save cluster settings.', false);
        }
    })
    .catch(() => showToast('Network error saving cluster settings.', false));
}

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

async function loadAccessList() {
    try {
        const response = await fetch('/api/cluster/rigs');
        if (response.status === 401) {
            document.getElementById('loginOverlay').classList.remove('d-none');
            return;
        }
        const data = await response.json();
        if (response.ok && data.success) {
            clusterData = data;
            document.getElementById('sshpassWarning').classList.toggle('d-none', !!data.sshpass_available);
            renderAccesses();
        }
    } catch (e) {
        console.error('Failed to load accesses:', e);
    }
}

function renderAccesses() {
    const body = document.getElementById('accessesTableBody');
    const jumpBody = document.getElementById('jumpTableBody');
    if (!clusterData || !clusterData.rigs) return;

    // Jump server library table
    const jumps = clusterData.jump_hosts || [];
    if (!jumps.length) {
        jumpBody.innerHTML = '<tr><td colspan="4" class="text-center text-muted small py-3">No jump servers yet. Add one and reuse it for any rig.</td></tr>';
    } else {
        jumpBody.innerHTML = jumps.map(j => {
            const auth = j.auth === 'key' ? '<i class="bi bi-file-earmark-key"></i> key' : '<i class="bi bi-shield-lock"></i> password';
            return '<tr>' +
                '<td class="fw-semibold">' + escapeHtml(j.name) + '</td>' +
                '<td class="font-monospace small">' + escapeHtml(j.user + '@' + j.host + ':' + j.port) + '</td>' +
                '<td class="small text-muted">' + auth + '</td>' +
                '<td class="text-end"><div class="btn-group btn-group-sm">' +
                '<button class="btn btn-outline-primary" title="Edit" onclick="openJumpModal(\'' + j.id + '\')"><i class="bi bi-pencil"></i></button>' +
                '<button class="btn btn-outline-danger" title="Delete" onclick="deleteJump(\'' + j.id + '\')"><i class="bi bi-trash"></i></button>' +
                '</div></td></tr>';
        }).join('');
    }

    // Access routes, grouped by rig
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
            rows.push('<tr' + (idx === 0 ? ' class="access-group-start"' : '') + '>' +
                (idx === 0 ? '<td rowspan="' + rig.accesses.length + '" class="fw-semibold align-middle">' + rigLabel + '</td>' : '') +
                '<td class="fw-semibold">' + escapeHtml(a.name) + '</td>' +
                '<td><span class="badge ' + (a.type === 'jump' ? 'bg-warning-glow text-warning' : 'bg-accent-glow text-primary') + '">' + (a.type === 'jump' ? 'JUMP' : 'DIRECT') + '</span></td>' +
                '<td class="font-monospace small">' + route + '</td>' +
                '<td class="small text-muted">' + auth + '</td>' +
                '<td class="text-end">' +
                '<div class="btn-group btn-group-sm">' +
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

function jumpNameById(jumpId) {
    const j = (clusterData && clusterData.jump_hosts || []).find(x => x.id === jumpId);
    return j ? j.name : '';
}

window.testAccess = async function(rigId, accessId, btn) {
    const rig = clusterData.rigs.find(r => r.id === rigId);
    const access = rig ? rig.accesses.find(a => a.id === accessId) : null;
    if (!access) return;
    if (btn) btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
    try {
        const response = await fetch('/api/cluster/access/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify({ rig_id: rigId, access: access })
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'OK' : 'Test failed'), !!data.success);
    } catch (e) {
        showToast('Network error testing access.', false);
    } finally {
        if (btn) btn.innerHTML = '<i class="bi bi-plug"></i>';
    }
};

// ---------------- Access modal ----------------

function openAccessModal(rigId, accessId) {
    editingAccess = { rigId: rigId, accessId: accessId };
    const rigSelect = document.getElementById('accessRigSelect');
    rigSelect.innerHTML = (clusterData && clusterData.rigs ? clusterData.rigs : [])
        .map(r => '<option value="' + r.id + '">' + escapeHtml(r.name || r.id) + '</option>').join('');

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
    toggleAccessModalFields();
    document.getElementById('accessTestResult').textContent = '';
    new bootstrap.Modal(document.getElementById('accessModal')).show();
}

window.openAccessModal = openAccessModal;

function toggleAccessModalFields() {
    const type = document.getElementById('accessTypeSelect').value;
    const auth = document.getElementById('accessAuthSelect').value;
    document.getElementById('jumpSettingsBlock').classList.toggle('d-none', type !== 'jump');
    document.getElementById('accessPasswordBlock').classList.toggle('d-none', auth !== 'password');
    document.getElementById('accessKeyBlock').classList.toggle('d-none', auth !== 'key');
}

document.getElementById('accessTypeSelect').addEventListener('change', toggleAccessModalFields);
document.getElementById('accessAuthSelect').addEventListener('change', toggleAccessModalFields);

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
    const rigId = document.getElementById('accessRigSelect').value;
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

// ---------------- Flight sheets & wallets ----------------

const MINER_OPTIONS = ["lolminer", "xmrig", "gminer", "rigel", "bzminer", "teamredminer",
    "hiveon", "srbminer", "wildrig-multi", "bminer", "ccminer", "t-rex", "none"];

function populateFsheetMiners() {
    const sel = document.getElementById('fsMinerSelect');
    const current = sel.value;
    sel.innerHTML = MINER_OPTIONS.map(m => '<option value="' + m + '">' + m + '</option>').join('');
    if (current) sel.value = current;
}

function populateWalletSelect(wallets) {
    const sel = document.getElementById('fsWalletSelect');
    const current = sel.value;
    let options = wallets.map(w => '<option value="' + w.id + '">' + escapeHtml(w.name) + '</option>');
    options.push('<option value="__custom__">Custom address...</option>');
    sel.innerHTML = options.join('') || '<option value="__custom__">Custom address...</option>';
    if (current) sel.value = current;
}

async function loadFsheets() {
    try {
        const response = await apiFetch('/api/fsheets');
        const data = await response.json();
        if (!data.success) return;
        renderFsheets(data.fsheets || [], data.wallets || [], data.active || {});
    } catch (e) {
        console.error('Failed to load flight sheets:', e);
    }
}

function renderFsheets(fsheets, wallets, active) {
    populateWalletSelect(wallets);
    window._fsWallets = wallets;
    // Prefill the quick-save form once with the active mining config
    if (!window._fsFormPrefilled) {
        window._fsFormPrefilled = true;
        document.getElementById('fsCoin').value = active.coin || '';
        document.getElementById('fsPool').value = active.pool || '';
        const sel = document.getElementById('fsWalletSelect');
        const w = wallets.find(x => x.address === active.wallet);
        if (w) { sel.value = w.id; }
        else {
            sel.value = '__custom__';
            const custom = document.getElementById('fsWalletCustom');
            custom.classList.remove('d-none');
            custom.value = active.wallet || '';
        }
        populateFsheetMiners();
        document.getElementById('fsMinerSelect').value = active.miner || 'none';
    }
    const container = document.getElementById('fsheetsContainer');
    if (!fsheets.length) {
        container.innerHTML = '<div class="text-center text-muted small py-3">No flight sheets saved yet. Save one below or import.</div>';
        return;
    }
    const walletLabel = (ref) => {
        const w = wallets.find(x => x.id === ref);
        if (w) return w.name;
        return ref ? ref.slice(0, 14) + (ref.length > 14 ? '…' : '') : '—';
    };
    container.innerHTML = fsheets.map(f => {
        const isWalletRef = wallets.some(x => x.id === f.wallet);
        const isActive = active.coin === f.coin && (isWalletRef || (active.wallet === f.wallet));
        return '<div class="d-flex justify-content-between align-items-center border border-secondary-subtle rounded px-2 py-1 mb-1 fsheet-row' + (isActive ? ' border-warning-subtle' : '') + '">' +
            '<div class="min-w-0">' +
                '<span class="fw-semibold small">' + escapeHtml(f.name) + '</span>' +
                (isActive ? ' <span class="badge bg-warning-glow text-warning small">ACTIVE</span>' : '') +
                '<div class="small text-muted text-truncate">' + escapeHtml(f.coin || '?') + ' • ' + escapeHtml(walletLabel(f.wallet)) + ' • ' + escapeHtml(f.pool || '—') + ' • ' + escapeHtml(f.miner) + '</div>' +
            '</div>' +
            '<div class="d-flex gap-1 flex-shrink-0">' +
                '<button class="btn btn-xs btn-outline-warning py-0 px-2" title="Apply" onclick="applyFsheet(\'' + f.id + '\', this)"><i class="bi bi-lightning-charge-fill"></i></button>' +
                '<button class="btn btn-xs btn-outline-primary py-0 px-2" title="Edit" onclick="editFsheet(\'' + f.id + '\')"><i class="bi bi-pencil"></i></button>' +
                '<button class="btn btn-xs btn-outline-danger py-0 px-2" title="Delete" onclick="deleteFsheet(\'' + f.id + '\')"><i class="bi bi-trash"></i></button>' +
            '</div>' +
        '</div>';
    }).join('');
}

window.applyFsheet = async function(fid, btn) {
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="bi bi-arrow-repeat spin-animation"></i>';
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
    } finally {
        setTimeout(() => { btn.innerHTML = orig; }, 800);
    }
};

window.editFsheet = function(fid) {
    apiFetch('/api/fsheets').then(r => r.json()).then(data => {
        const f = (data.fsheets || []).find(x => x.id === fid);
        if (!f) return;
        populateWalletSelect(data.wallets || []);
        document.getElementById('fsName').value = f.name;
        document.getElementById('fsCoin').value = f.coin;
        const sel = document.getElementById('fsWalletSelect');
        const isRef = (data.wallets || []).some(w => w.id === f.wallet);
        if (isRef) {
            sel.value = f.wallet;
            document.getElementById('fsWalletCustom').classList.add('d-none');
        } else {
            sel.value = '__custom__';
            const custom = document.getElementById('fsWalletCustom');
            custom.classList.remove('d-none');
            custom.value = f.wallet;
        }
        document.getElementById('fsPool').value = f.pool;
        populateFsheetMiners();
        document.getElementById('fsMinerSelect').value = f.miner;
        // Reuse the save form; saving updates by name+id when editing flag set
        window._editingFsheetId = fid;
        const form = document.getElementById('saveFsheetForm');
        form.scrollIntoView({ behavior: 'smooth', block: 'center' });
        document.getElementById('fsName').focus();
        showToast('Editing "' + f.name + '" - press Save to update.', true);
    });
};

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

async function importFsheets() {
    const text = document.getElementById('fsheetImportText').value.trim();
    if (!text) { showToast('Nothing to import.', false); return; }
    let items;
    try {
        const parsed = JSON.parse(text);
        items = Array.isArray(parsed) ? parsed : [parsed];
    } catch (e) {
        showToast('Invalid JSON.', false);
        return;
    }
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
    bootstrap.Modal.getInstance(document.getElementById('fsheetImportModal')).hide();
    loadFsheets();
}

function openWalletModal() {
    renderWallets();
    new bootstrap.Modal(document.getElementById('walletModal')).show();
}

async function renderWallets() {
    try {
        const response = await apiFetch('/api/wallets');
        const data = await response.json();
        const list = data.wallets || [];
        window._fsWallets = list;
        populateWalletSelect(list);
        const container = document.getElementById('walletsContainer');
        if (!list.length) {
            container.innerHTML = '<div class="text-center text-muted small py-3">No wallets saved yet.</div>';
            return;
        }
        container.innerHTML = list.map(w =>
            '<div class="d-flex justify-content-between align-items-center border border-secondary-subtle rounded px-2 py-1 mb-1">' +
                '<div class="min-w-0">' +
                    '<span class="fw-semibold small">' + escapeHtml(w.name) + '</span>' +
                    '<div class="small text-muted font-monospace text-truncate">' + escapeHtml(w.address) + '</div>' +
                '</div>' +
                '<button class="btn btn-xs btn-outline-danger py-0 px-2 flex-shrink-0" title="Delete" onclick="deleteWallet(\'' + w.id + '\')"><i class="bi bi-trash"></i></button>' +
            '</div>').join('');
    } catch (e) {
        showToast('Failed to load wallets.', false);
    }
}

async function addWallet(e) {
    e.preventDefault();
    const payload = {
        wallet: {
            name: document.getElementById('walletNameInput').value.trim(),
            address: document.getElementById('walletAddressInput').value.trim()
        }
    };
    try {
        const response = await apiFetch('/api/wallets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        showToast(data.message || (data.success ? 'Wallet saved.' : 'Failed to save wallet.'), !!data.success);
        if (data.success) {
            document.getElementById('walletNameInput').value = '';
            document.getElementById('walletAddressInput').value = '';
            renderWallets();
            loadFsheets();
        }
    } catch (err) {
        showToast('Network error saving wallet.', false);
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
        if (data.success) { renderWallets(); loadFsheets(); }
    } catch (e) {
        showToast('Network error deleting wallet.', false);
    }
};

// ---------------- Extra fans control ----------------

async function loadFans() {
    const container = document.getElementById('fansContainer');
    if (!container) return;
    try {
        const response = await apiFetch('/api/fans');
        const data = await response.json();
        const fans = (data && data.fans) || [];
        if (!fans.length) {
            container.innerHTML = '<div class="text-muted small">No controllable fans detected on this rig (motherboard fan headers via hwmon).</div>';
            return;
        }
        container.innerHTML = fans.map(f => {
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
        container.querySelectorAll('.fan-slider').forEach(sl => {
            sl.addEventListener('input', function() {
                document.getElementById(this.id.replace('fan_', 'fanval_')).textContent = this.value + '%';
            });
            sl.addEventListener('change', function() {
                const parts = this.id.replace('fan_', '').split('_');
                setFanDuty(parts[0], parts[1], parseInt(this.value, 10));
            });
        });
    } catch (e) {
        container.innerHTML = '<div class="text-muted small">Fan control not available.</div>';
    }
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
            body: JSON.stringify({ current_password: current, new_password: newPw.trim() })
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
