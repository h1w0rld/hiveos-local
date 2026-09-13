import os
import re
import json
import hmac
import uuid
import shlex
import socket
import shutil
import tempfile
import subprocess
import platform
import logging
import threading
import random
import time
import urllib.request
from flask import Flask, jsonify, request, render_template, session, Response

app = Flask(__name__)
# Secure randomly-generated key for session management
app.secret_key = os.urandom(24)
# Never cache static assets: guarantees JS/CSS updates are picked up on reload
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Constants and Configuration Paths
HIVE_CONFIG_DIR = "/hive-config"
RIG_CONF_PATH = os.path.join(HIVE_CONFIG_DIR, "rig.conf")
NVIDIA_OC_CONF = os.path.join(HIVE_CONFIG_DIR, "nvidia-oc.conf")
AMD_OC_CONF = os.path.join(HIVE_CONFIG_DIR, "amd-oc.conf")
WALLET_CONF_PATH = os.path.join(HIVE_CONFIG_DIR, "wallet.conf")
PIN_PATH = os.path.join(HIVE_CONFIG_DIR, "dashboard.key")
AUTOFAN_CONF = os.path.join(HIVE_CONFIG_DIR, "autofan.conf")
PRESETS_DIR = os.path.join(HIVE_CONFIG_DIR, "presets")
CLUSTER_CONF = os.path.join(HIVE_CONFIG_DIR, "cluster.json")
CLUSTER_CACHE = os.path.join(HIVE_CONFIG_DIR, "cluster-cache.json")
DEFAULT_SYNC_INTERVAL = 60
DASHBOARD_PORT = 1337

# Local Dashboard Release Version (kept in sync with version.txt used for update checks)
def _load_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "version.txt")) as f:
            v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    return "1.1.2"

VERSION = _load_version()

# systemd/sudo environments lack /hive/bin, so miner screen children (miner-run) can not be
# executed. Force an explicit PATH when invoking the hive miner wrapper.
HIVE_MINER_ENV = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/hive/bin"
MINER_START_CMD = f"sudo env {HIVE_MINER_ENV} /hive/bin/miner start"
MINER_STOP_CMD = f"sudo env {HIVE_MINER_ENV} /hive/bin/miner stop"
MINER_RESTART_CMD = f"sudo env {HIVE_MINER_ENV} /hive/bin/miner restart"

# Verify environments
IS_LINUX = platform.system() == "Linux"
HAS_HIVEOS = IS_LINUX and os.path.exists(HIVE_CONFIG_DIR)

# Thread safety lock for files access
config_lock = threading.Lock()

# IP failed logins tracker for rate-limiting
failed_login_attempts = {}

# Setup Structured Production Logging
log_file = '/var/log/hiveos-local.log' if HAS_HIVEOS else './hiveos-local.log'
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger()

# Console logger stream handler
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(console)

def get_local_ip():
    """Detects primary LAN IP interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

def make_self_rig_entry(state):
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    now = int(time.time())
    return {
        "id": state["self_id"],
        "name": rig_conf.get("RIG_ID", "") or socket.gethostname(),
        "host_label": get_local_ip(),
        "is_self": True,
        "password": str(app.config.get('ACCESS_PASSWORD', '')),
        "accesses": [],
        "updated_at": now,
        "added_at": now,
    }

def load_cluster_state():
    state = {
        "cluster_name": "",
        "self_id": "",
        "sync_interval": DEFAULT_SYNC_INTERVAL,
        "rigs": []
    }
    try:
        if os.path.exists(CLUSTER_CONF):
            with open(CLUSTER_CONF, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in ("cluster_name", "self_id", "sync_interval", "rigs"):
                    if k in data:
                        state[k] = data[k]
    except Exception as e:
        logging.error(f"Failed to read cluster config: {e}")

    if not state.get("self_id"):
        state["self_id"] = uuid.uuid4().hex
    rigs = [r for r in state.get("rigs", []) if isinstance(r, dict) and r.get("id")]
    if not any(r.get("id") == state["self_id"] for r in rigs):
        rigs.insert(0, make_self_rig_entry(state))
    state["rigs"] = rigs
    if not os.path.exists(CLUSTER_CONF):
        save_cluster_state(state)
    try:
        os.chmod(CLUSTER_CONF, 0o600)
    except Exception:
        pass
    # Normalize self flags and keep the self entry password in sync with the live dashboard key
    for r in state["rigs"]:
        if r.get("id") == state["self_id"]:
            r["is_self"] = True
            r["password"] = str(app.config.get('ACCESS_PASSWORD', ''))
        else:
            r["is_self"] = False
    return state

def save_cluster_state(state):
    try:
        with config_lock:
            with open(CLUSTER_CONF, 'w') as f:
                json.dump(state, f, indent=2)
            os.chmod(CLUSTER_CONF, 0o600)
        return True
    except Exception as e:
        logging.error(f"Failed to save cluster config: {e}")
        return False

def clean_access_entry(access):
    return {k: access[k] for k in ACCESS_ENTRY_FIELDS if k in access}

def clean_rig_entry(entry):
    """Keep only known rig fields; normalize is_self relative to the local self id."""
    clean = {k: entry[k] for k in RIG_ENTRY_FIELDS if k in entry}
    clean["is_self"] = (clean.get("id") == _CURRENT_SELF_ID.get("value", ""))
    accesses = clean.get("accesses")
    if not isinstance(accesses, list):
        clean["accesses"] = []
    else:
        clean["accesses"] = [clean_access_entry(a) for a in accesses if isinstance(a, dict)]
    return clean

def merge_rig_lists(base_rigs, incoming_rigs):
    """Merge rig entries by id; the entry with the newer updated_at wins."""
    by_id = {}
    for r in base_rigs:
        if isinstance(r, dict) and r.get("id"):
            by_id[r["id"]] = r
    for inc in incoming_rigs or []:
        if not isinstance(inc, dict) or not inc.get("id"):
            continue
        clean = clean_rig_entry(inc)
        rid = clean["id"]
        existing = by_id.get(rid)
        if existing is None:
            clean.setdefault("updated_at", 0)
            clean.setdefault("added_at", int(time.time()))
            by_id[rid] = clean
            logging.info(f"Cluster merge: discovered new rig '{clean.get('name', rid)}'")
        else:
            try:
                inc_ts = int(clean.get("updated_at") or 0)
                cur_ts = int(existing.get("updated_at") or 0)
            except (TypeError, ValueError):
                inc_ts, cur_ts = 0, 0
            if inc_ts > cur_ts:
                # Preserve the locally-resolved is_self flag; adopt newer remote edits
                clean["is_self"] = existing.get("is_self", False)
                by_id[rid] = clean
    return list(by_id.values())

# ---------------- SSH transport for cluster communication ----------------

VALID_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9\.\-]*$')
VALID_USER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_\.\-\@\$]*$')
VALID_KEYPATH_RE = re.compile(r'^[A-Za-z0-9\./_\-]{1,200}$')

_sshpass_install_attempted = False

def ensure_sshpass():
    """Install sshpass (SSH password auth helper) on demand. Returns True if available."""
    global _sshpass_install_attempted
    if shutil.which("sshpass"):
        return True
    if _sshpass_install_attempted:
        return False
    _sshpass_install_attempted = True
    devnull = subprocess.DEVNULL
    for cmd in (["sudo", "-n", "apt-get", "install", "-y", "sshpass"],
                ["apt-get", "install", "-y", "sshpass"]):
        try:
            res = subprocess.run(cmd, stdout=devnull, stderr=devnull, timeout=180)
            if res.returncode == 0 and shutil.which("sshpass"):
                logging.info("Installed sshpass package for cluster SSH password authentication")
                return True
        except Exception as e:
            logging.warning(f"sshpass auto-install attempt failed: {e}")
    return False

def validate_access_payload(access):
    """Validate an SSH access definition. Returns (clean_access, error_message)."""
    if not isinstance(access, dict):
        return None, "Access payload must be an object."
    a_type = str(access.get("type", "direct")).strip().lower()
    if a_type not in ("direct", "jump"):
        return None, "Access type must be 'direct' or 'jump'."
    name = str(access.get("name", "")).strip()
    if not name or len(name) > 60:
        return None, "Access name must be 1-60 characters."
    host = str(access.get("host", "")).strip()
    if not VALID_HOST_RE.match(host):
        return None, "Invalid target host (use IP address or hostname)."
    user = str(access.get("user", "")).strip()
    if not VALID_USER_RE.match(user):
        return None, "Invalid SSH user name."
    try:
        port = int(access.get("port", 22))
    except (TypeError, ValueError):
        return None, "SSH port must be an integer."
    if not (1 <= port <= 65535):
        return None, "SSH port must be between 1 and 65535."
    auth = str(access.get("auth", "password")).strip().lower()
    if auth not in ("password", "key"):
        return None, "SSH auth must be 'password' or 'key'."
    clean = {
        "id": str(access.get("id", "")).strip() or uuid.uuid4().hex[:12],
        "name": name,
        "type": a_type,
        "host": host,
        "port": port,
        "user": user,
        "auth": auth,
    }
    if auth == "password":
        password = str(access.get("password", ""))
        if len(password) > 128:
            return None, "SSH password is too long."
        clean["password"] = password
    else:
        key_path = str(access.get("key_path", "")).strip()
        if key_path:
            if not VALID_KEYPATH_RE.match(key_path):
                return None, "Invalid SSH private key path."
            clean["key_path"] = key_path
    if a_type == "jump":
        jhost = str(access.get("jump_host", "")).strip()
        if not VALID_HOST_RE.match(jhost):
            return None, "Invalid jump server host (use IP address or hostname)."
        juser = str(access.get("jump_user", "")).strip()
        if not VALID_USER_RE.match(juser):
            return None, "Invalid jump server SSH user name."
        try:
            jport = int(access.get("jump_port", 22))
        except (TypeError, ValueError):
            return None, "Jump server SSH port must be an integer."
        if not (1 <= jport <= 65535):
            return None, "Jump server SSH port must be between 1 and 65535."
        jauth = str(access.get("jump_auth", "password")).strip().lower()
        if jauth not in ("password", "key"):
            return None, "Jump server auth must be 'password' or 'key'."
        clean["jump_host"] = jhost
        clean["jump_port"] = jport
        clean["jump_user"] = juser
        clean["jump_auth"] = jauth
        if jauth == "password":
            jpassword = str(access.get("jump_password", ""))
            if len(jpassword) > 128:
                return None, "Jump server password is too long."
            clean["jump_password"] = jpassword
        else:
            jkey = str(access.get("jump_key_path", "")).strip()
            if jkey:
                if not VALID_KEYPATH_RE.match(jkey):
                    return None, "Invalid jump server SSH key path."
                clean["jump_key_path"] = jkey
    return clean, ""

def _write_temp_password(password, tmp_files):
    fd, path = tempfile.mkstemp(prefix="hvssh_", suffix=".pw", dir="/tmp")
    with os.fdopen(fd, "w") as f:
        f.write(str(password))
    os.chmod(path, 0o600)
    tmp_files.append(path)
    return path

_SSH_BASE_OPTS = ["-o", "StrictHostKeyChecking=no",
                  "-o", "UserKnownHostsFile=/dev/null",
                  "-o", "ConnectTimeout=12",
                  "-o", "ServerAliveInterval=5",
                  "-o", "ServerAliveCountMax=3",
                  "-o", "LogLevel=ERROR"]

def build_ssh_command(access, remote_cmd, tmp_files):
    """Build a safe argv list for ssh (direct connection or via jump server)."""
    args = []
    proxy_arg = None
    # Jump server hop via ProxyCommand
    if access.get("type") == "jump":
        jopts = " ".join(["-o StrictHostKeyChecking=no",
                          "-o UserKnownHostsFile=/dev/null",
                          "-o ConnectTimeout=12",
                          "-o LogLevel=ERROR"])
        if access.get("jump_auth") == "password":
            if not ensure_sshpass():
                return None, ("sshpass is not installed on this rig. Run "
                              "'sudo apt-get install -y sshpass' or use SSH key authentication.")
            jpw_file = _write_temp_password(access.get("jump_password", ""), tmp_files)
            prefix = "sshpass -f %s ssh" % shlex.quote(jpw_file)
        else:
            jkey = str(access.get("jump_key_path", "")).strip()
            prefix = ("ssh -i %s -o IdentitiesOnly=yes" % shlex.quote(jkey)) if jkey else "ssh"
        proxy = "%s -p %d %s -W %%h:%%p %s@%s" % (
            prefix, int(access.get("jump_port", 22)), jopts,
            shlex.quote(str(access.get("jump_user", ""))),
            shlex.quote(str(access.get("jump_host", ""))))
        proxy_arg = "ProxyCommand=" + proxy

    if access.get("auth") == "password":
        if not ensure_sshpass():
            return None, ("sshpass is not installed on this rig. Run "
                          "'sudo apt-get install -y sshpass' or use SSH key authentication.")
        pw_file = _write_temp_password(access.get("password", ""), tmp_files)
        args += ["sshpass", "-f", pw_file]
        args += ["ssh", "-o", "PreferredAuthentications=password",
                 "-o", "PubkeyAuthentication=no",
                 "-o", "NumberOfPasswordPrompts=1"]
    else:
        args += ["ssh", "-o", "BatchMode=yes"]
        key_path = str(access.get("key_path", "")).strip()
        if key_path:
            args += ["-i", key_path, "-o", "IdentitiesOnly=yes"]

    args += _SSH_BASE_OPTS
    if proxy_arg:
        args += ["-o", proxy_arg]
    args += ["-p", str(access.get("port", 22)),
             "%s@%s" % (access.get("user", ""), access.get("host", "")),
             remote_cmd]
    return args, None

def run_ssh_command(access, remote_cmd, timeout=35):
    """Execute a command on a remote rig over SSH. Returns (ok, output, error)."""
    tmp_files = []
    try:
        args, err = build_ssh_command(access, remote_cmd, tmp_files)
        if err:
            return False, "", err
        res = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, timeout=timeout)
        out = res.stdout.decode(errors="ignore")
        errout = res.stderr.decode(errors="ignore")
        if res.returncode != 0:
            msg = (errout or out).strip()
            detail = msg.splitlines()[-1] if msg else "exit code %d" % res.returncode
            return False, out, detail
        return True, out, ""
    except subprocess.TimeoutExpired:
        return False, "", "SSH connection timed out"
    except FileNotFoundError as e:
        return False, "", "%s not found on this rig" % e.filename
    except Exception as e:
        return False, "", str(e)
    finally:
        for p in tmp_files:
            try:
                os.remove(p)
            except Exception:
                pass

def build_curl_command(method, path, has_body, password):
    """HTTP request against the remote rig's local dashboard, executed over SSH."""
    parts = ["curl", "-s", "-m", "25",
             "-X", method,
             "-H", shlex.quote("Authorization: Bearer " + str(password)),
             "-H", shlex.quote("Content-Type: application/json"),
             "-w", shlex.quote("\n__HC:%{http_code}")]
    if has_body:
        parts.append("--data-binary @-")
    parts.append(shlex.quote("http://127.0.0.1:%d/%s" % (DASHBOARD_PORT, path.lstrip('/'))))
    return " ".join(parts)

def _parse_curl_output(output):
    """Split the curl -w status trailer from the JSON body. Returns (body, http_code)."""
    code = 200
    body = output
    m = re.search(r'__HC:(\d+)\s*$', output)
    if m:
        code = int(m.group(1))
        body = output[:m.start()].rstrip("\n")
    return body, code

def cluster_remote_api(rig, method, path, body=None, timeout=40):
    """Call a remote rig's dashboard API over its configured SSH accesses.

    Tries each access in order until one succeeds. Requires curl on the remote rig
    and sshpass locally for password-based accesses.
    Returns (ok, parsed_json_or_None, http_code, error_message, access_name).
    """
    password = str(rig.get("password", ""))
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    curl_cmd = build_curl_command(method.upper(), path, raw is not None, password)
    last_error = "No SSH accesses configured for this rig"
    for access in rig.get("accesses", []):
        access_name = access.get("name") or access.get("id", "?")
        ok, out, ssh_err = run_ssh_command(access, curl_cmd, timeout=timeout)
        if not ok:
            last_error = "%s: %s" % (access_name, ssh_err)
            continue
        body_text, http_code = _parse_curl_output(out)
        try:
            data = json.loads(body_text)
        except Exception:
            last_error = "%s: invalid response from remote dashboard (HTTP %d)" % (access_name, http_code)
            continue
        if http_code >= 400:
            msg = "HTTP %d" % http_code
            if isinstance(data, dict) and data.get("message"):
                msg += " - %s" % data.get("message")
            last_error = "%s: %s" % (access_name, msg)
            continue
        return True, data, http_code, "", access_name
    return False, None, 0, last_error, ""

# ---------------- Cluster stats cache ----------------

def load_cluster_cache():
    try:
        if os.path.exists(CLUSTER_CACHE):
            with open(CLUSTER_CACHE, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logging.error(f"Failed to read cluster cache: {e}")
    return {"rigs": {}}

def write_cluster_cache(cache):
    try:
        with config_lock:
            with open(CLUSTER_CACHE, 'w') as f:
                json.dump(cache, f)
            os.chmod(CLUSTER_CACHE, 0o600)
        return True
    except Exception as e:
        logging.error(f"Failed to save cluster cache: {e}")
        return False

def collect_stats_payload():
    """Full stats payload shared between /api/stats and the cluster cache."""
    hw = get_gpu_stats()
    total_mh, per_gpu = get_miner_hashrate()
    for g in hw["gpus"]:
        g["hashrate"] = round(per_gpu.get(g["index"], 0.0), 2)
    return {
        "system": get_system_stats(),
        "gpus": hw["gpus"],
        "igpus": hw["igpus"],
        "total_hashrate_mh": round(total_mh, 2),
        "overclocks": get_overclocks_formatted(),
        "csrf_token": session.get('csrf_token', '')
    }

_cluster_sync_lock = threading.Lock()
_cluster_last_sync = {"ts": 0, "ok": True, "message": "Not synced yet"}

def run_sync_cycle(triggered_by="auto"):
    """One cluster sync pass: exchange rig lists with every peer and refresh stats cache."""
    if not _cluster_sync_lock.acquire(blocking=False):
        return False, "Another sync cycle is already running"
    try:
        state = load_cluster()
        _CURRENT_SELF_ID["value"] = state["self_id"]
        cache = load_cluster_cache()
        cache.setdefault("rigs", {})

        # Cache the local rig's stats as well
        try:
            cache["rigs"][state["self_id"]] = {
                "stats": collect_stats_payload(),
                "fetched_at": int(time.time()),
                "online": True,
                "error": ""
            }
        except Exception as e:
            logging.error(f"Cluster cache: failed to collect local stats: {e}")

        for rig in list(state["rigs"]):
            if rig.get("id") == state["self_id"]:
                continue
            rig_id = rig["id"]
            entry = cache["rigs"].get(rig_id, {})

            # 1. Pull the peer's cluster state and merge (learns about new rigs)
            ok, data, _, err, _ = cluster_remote_api(rig, "GET", "api/cluster/state", timeout=45)
            if ok and isinstance(data, dict) and isinstance(data.get("rigs"), list):
                state["rigs"] = merge_rig_lists(state["rigs"], data["rigs"])
                if data.get("cluster_name") and not state.get("cluster_name"):
                    state["cluster_name"] = data["cluster_name"]
                entry["online"] = True
                entry["error"] = ""
            else:
                entry["online"] = False
                entry["error"] = err

            # 2. Push our merged state back to the peer (propagates new rigs/passwords)
            push_ok, _, _, push_err, _ = cluster_remote_api(
                rig, "POST", "api/cluster/sync",
                body={"cluster_name": state.get("cluster_name", ""),
                      "rigs": state["rigs"], "from_id": state["self_id"]},
                timeout=45)
            if not push_ok and not entry.get("online"):
                entry["error"] = push_err

            # 3. Refresh the live stats cache for this peer
            sok, sdata, _, serr, _ = cluster_remote_api(rig, "GET", "api/stats", timeout=45)
            if sok:
                entry["stats"] = sdata
                entry["fetched_at"] = int(time.time())
                entry["online"] = True
                entry["error"] = ""
            elif not entry.get("online"):
                entry["error"] = serr

            cache["rigs"][rig_id] = entry

        save_cluster_state(state)
        write_cluster_cache(cache)

        _cluster_last_sync["ts"] = int(time.time())
        offline = [r.get("name") or r.get("id") for r in state["rigs"]
                   if r.get("id") != state["self_id"]
                   and not cache["rigs"].get(r["id"], {}).get("online")]
        _cluster_last_sync["ok"] = not offline
        _cluster_last_sync["message"] = ("All peers reachable" if not offline
                                         else "Offline: " + ", ".join(str(x) for x in offline))
        if triggered_by != "auto":
            logging.info(f"Cluster sync cycle completed (triggered by {triggered_by})")
        return True, "Sync cycle finished"
    finally:
        _cluster_sync_lock.release()

def _cluster_sync_worker():
    # Give the service a moment to finish booting before the first cycle
    time.sleep(15)
    while True:
        interval = DEFAULT_SYNC_INTERVAL
        try:
            state = load_cluster_state()
            try:
                interval = max(15, int(state.get("sync_interval", DEFAULT_SYNC_INTERVAL)))
            except (TypeError, ValueError):
                interval = DEFAULT_SYNC_INTERVAL
            run_sync_cycle(triggered_by="auto")
        except Exception as e:
            logging.error(f"Cluster sync worker error: {e}")
        time.sleep(interval)

def start_cluster_worker():
    t = threading.Thread(target=_cluster_sync_worker, daemon=True, name="cluster-sync")
    t.start()
    logging.info("Cluster sync worker started")

# Load or generate dashboard access password (legacy 6-digit PINs stay valid as passwords)
def load_or_generate_access_key():
    with config_lock:
        if os.path.exists(PIN_PATH):
            try:
                with open(PIN_PATH, 'r') as f:
                    key = f.read().strip()
                    # Accept any non-empty password; legacy 6-digit PINs keep working
                    if key:
                        return key
            except Exception as e:
                logging.error(f"Failed to read access password: {e}")
        
        # Generate new 6-digit random password
        key = "".join([str(random.randint(0, 9)) for _ in range(6)])
        try:
            with open(PIN_PATH, 'w') as f:
                f.write(key)
            os.chmod(PIN_PATH, 0o600) # Read/write by root only
            logging.info(f"Generated new dashboard access password: {key}")
        except Exception as e:
            logging.error(f"Failed to write access password: {e}")
        return key

# Dashboard access password (loaded or generated once at import time)
ACCESS_PASSWORD = load_or_generate_access_key()
app.config['ACCESS_PASSWORD'] = ACCESS_PASSWORD

# ---------------- Cluster (multi-rig) state management ----------------

RIG_ENTRY_FIELDS = ["id", "name", "host_label", "is_self", "password", "accesses", "updated_at", "added_at"]
ACCESS_ENTRY_FIELDS = ["id", "name", "type", "host", "port", "user", "auth", "password", "key_path",
                       "jump_host", "jump_port", "jump_user", "jump_auth", "jump_password", "jump_key_path"]

# Helper carrying self id for entry cleaning (avoids passing it through call stacks)
_CURRENT_SELF_ID = {"value": ""}

# Parse shell-like config files with locking
def parse_shell_config(filepath):
    config = {}
    
    # Enforce strict path prefix validation to mitigate CodeQL Path Traversal alerts
    filepath = os.path.abspath(filepath)
    allowed_base = os.path.abspath(HIVE_CONFIG_DIR)
    if not filepath.startswith(allowed_base + os.sep) and filepath != allowed_base:
        logging.error(f"Security Alert: Blocked unauthorized config file parse attempt: {filepath}")
        return config

    with config_lock:
        if not os.path.exists(filepath):
            return config
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    match = re.match(r'^([A-Za-z0-9_]+)\s*=\s*(.*)$', line)
                    if match:
                        key = match.group(1)
                        val = match.group(2).strip()
                        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                            val = val[1:-1]
                        config[key] = val
        except Exception as e:
            logging.error(f"Error parsing config file {filepath}: {e}")
    return config

# Write shell-like config files with locking
def write_shell_config(filepath, config):
    # Enforce strict path prefix validation to mitigate CodeQL Path Traversal alerts
    filepath = os.path.abspath(filepath)
    allowed_base = os.path.abspath(HIVE_CONFIG_DIR)
    if not filepath.startswith(allowed_base + os.sep) and filepath != allowed_base:
        logging.error(f"Security Alert: Blocked unauthorized config file write attempt: {filepath}")
        return False

    with config_lock:
        try:
            with open(filepath, 'w') as f:
                for k, v in config.items():
                    f.write(f'{k}="{v}"\n')
            return True
        except Exception as e:
            logging.error(f"Error writing config file {filepath}: {e}")
            return False

# Strict regex parameter validation to block shell injection
def is_safe_parameter_value(value):
    val_str = str(value).strip()
    if not val_str:
        return True
    return re.match(r'^^[\-\+]?[0-9\s]+$$', val_str) is not None

# Overclock Parameters range check validation
def validate_overclock_ranges(brand, data):
    try:
        if brand == "NVIDIA":
            if "core" in data and data["core"] != "":
                val = int(data["core"])
                if val > 500:
                    if not (500 <= val <= 3000):
                        return False, "NVIDIA locked core clock must be between 500 and 3000 MHz."
                else:
                    if not (-1000 <= val <= 1000):
                        return False, "NVIDIA core clock offset must be between -1000 and 1000 MHz."
                        
            if "mem" in data and data["mem"] != "":
                val = int(data["mem"])
                if not (-2000 <= val <= 4000):
                    return False, "NVIDIA memory clock offset must be between -2000 and 4000 MHz."
                    
            if "pl" in data and data["pl"] != "":
                val = int(data["pl"])
                if not (0 <= val <= 600):
                    return False, "NVIDIA power limit must be between 0 and 600 Watts."
                    
            if "fan" in data and data["fan"] != "":
                val = int(data["fan"])
                if not (0 <= val <= 100):
                    return False, "NVIDIA fan speed must be between 0 and 100%."
                    
        elif brand == "AMD":
            if "core" in data and data["core"] != "":
                val = int(data["core"])
                if not (0 <= val <= 3000):
                    return False, "AMD core clock must be between 0 and 3000 MHz."
                    
            if "mem" in data and data["mem"] != "":
                val = int(data["mem"])
                if not (0 <= val <= 3000):
                    return False, "AMD memory clock must be between 0 and 3000 MHz."
                    
            if "vdd" in data and data["vdd"] != "":
                val = int(data["vdd"])
                if not (0 <= val <= 1500):
                    return False, "AMD core voltage (VDD) must be between 0 and 1500 mV."
                    
            if "vddci" in data and data["vddci"] != "":
                val = int(data["vddci"])
                if not (0 <= val <= 1500):
                    return False, "AMD VDDCI voltage must be between 0 and 1500 mV."
                    
            if "mvdd" in data and data["mvdd"] != "":
                val = int(data["mvdd"])
                if not (0 <= val <= 2000):
                    return False, "AMD memory voltage (MVDD) must be between 0 and 2000 mV."
                    
            if "fan" in data and data["fan"] != "":
                val = int(data["fan"])
                if not (0 <= val <= 100):
                    return False, "AMD fan speed must be between 0 and 100%."
                    
            if "pl" in data and data["pl"] != "":
                val = int(data["pl"])
                if not (0 <= val <= 500):
                    return False, "AMD power limit must be between 0 and 500 Watts."
                    
            if "dpm" in data and data["dpm"] != "":
                val = int(data["dpm"])
                if not (0 <= val <= 7):
                    return False, "AMD DPM state must be between 0 and 7."
                    
            if "ref" in data and data["ref"] != "":
                val = int(data["ref"])
                if not (0 <= val <= 100):
                    return False, "AMD memory refresh index (REF) must be between 0 and 100."
                    
        return True, ""
    except ValueError:
        return False, "Overclock parameters must be valid integers."

# Create backup files of current config files
def backup_configs():
    try:
        if os.path.exists(NVIDIA_OC_CONF):
            shutil.copy2(NVIDIA_OC_CONF, NVIDIA_OC_CONF + ".bak")
        if os.path.exists(AMD_OC_CONF):
            shutil.copy2(AMD_OC_CONF, AMD_OC_CONF + ".bak")
        return True
    except Exception as e:
        logging.error(f"Failed to create backups of configurations: {e}")
        return False

# CPU Mining Statistics Gatherers
def get_cpu_model():
    if IS_LINUX:
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.strip().startswith('model name'):
                        return line.split(':')[1].strip()
        except Exception as e:
            logging.error(f"Failed to parse /proc/cpuinfo: {e}")
    return "Unknown CPU"

def get_cpu_temp():
    if IS_LINUX:
        paths = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
            "/sys/class/hwmon/hwmon1/temp1_input"
        ]
        for p in paths:
            if os.path.exists(p):
                try:
                    with open(p, 'r') as f:
                        temp = int(f.read().strip())
                        if temp > 1000:
                            temp = int(temp / 1000)
                        return temp
                except Exception:
                    pass
    return 0

def check_hugepages_status():
    if IS_LINUX:
        try:
            with open('/proc/meminfo', 'r') as f:
                content = f.read()
            match = re.search(r'HugePages_Total:\s+(\d+)', content)
            if match and int(match.group(1)) > 0:
                return True
        except Exception:
            pass
    return False

def get_xmrig_hashrate():
    log_path = "/var/log/miner/xmrig/lastrun_noappend.log"
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r') as f:
                lines = f.readlines()[-50:]
            for line in reversed(lines):
                match = re.search(r'speed \S+ \s*([0-9\.]+)\s+([0-9\.]+)', line)
                if match:
                    return float(match.group(1))
        except Exception:
            pass
    return 0.0

# Resolve Hive OS client version (package version first, then /etc/hiveos-release)
def get_hive_version():
    stdout, _, code = run_command("dpkg -l hive 2>/dev/null | awk '/^ii/{print $3}'")
    if code == 0 and stdout.strip():
        return stdout.strip()
    try:
        with open('/etc/hiveos-release', 'r') as f:
            release = parse_kv(f.read())
        codename = release.get('CODENAME')
        build_date = release.get('BUILD_DATE')
        if codename:
            return f"{codename} ({build_date})" if build_date else codename
    except Exception:
        pass
    return "Not Found"

def parse_kv(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, val = line.partition('=')
        result[key.strip()] = val.strip().strip('"').strip("'")
    return result

# System stats query
def get_system_stats():
    stats = {
        "hostname": socket.gethostname(),
        "local_ip": get_local_ip(),
        "uptime": "Unknown",
        "cpu_load": [0.0, 0.0, 0.0],
        "ram_used_pct": 0.0,
        "ram_total_gb": 0.0,
        "rig_id": "Offline",
        "farm_hash": "Offline",
        "hive_version": "Local-1.0",
        "active_miner": "None",
        "dashboard_version": VERSION,
        "cpu": {
            "model": get_cpu_model(),
            "temp": get_cpu_temp(),
            "hugepages": check_hugepages_status(),
            "hashrate": get_xmrig_hashrate()
        }
    }

    rig_conf = parse_shell_config(RIG_CONF_PATH)
    stats["rig_id"] = rig_conf.get("RIG_ID", "Not Found")
    stats["farm_hash"] = rig_conf.get("FARM_HASH", "Not Found")
    stats["hive_version"] = get_hive_version() if IS_LINUX else rig_conf.get("HIVE_VERSION", "Not Found")
    stats["active_miner"] = rig_conf.get("MINER", "None")

    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    coin = wallet_conf.get("COIN", "")
    if not coin:
        # Cloud-managed flight sheets store the coin inside the META JSON
        # (e.g. META='{"fs_id":22415703,"rigel":{"coin":"QUAI"}}')
        try:
            meta = json.loads(wallet_conf.get("META", ""))
            if isinstance(meta, dict):
                miner_name = str(stats.get("active_miner") or "").strip().lower()
                blocks = []
                if miner_name and isinstance(meta.get(miner_name), dict):
                    blocks.append(meta[miner_name])
                blocks.extend(v for v in meta.values() if isinstance(v, dict) and v not in blocks)
                for block in blocks:
                    candidate = str(block.get("coin") or "").strip()
                    if candidate and candidate.lower() != "none":
                        coin = candidate
                        break
        except Exception:
            pass
    stats["miner_running"] = is_miner_screen_running()
    # "Mined Crypto" reflects the currently mined coin: hide it when the miner is stopped
    stats["coin"] = (coin or "None") if stats["miner_running"] else "None"

    if IS_LINUX:
        try:
            with open('/proc/uptime', 'r') as f:
                uptime_seconds = float(f.readline().split()[0])
                hours = int(uptime_seconds // 3600)
                minutes = int((uptime_seconds % 3600) // 60)
                stats["uptime"] = f"{hours}h {minutes}m"
        except Exception:
            pass

        try:
            with open('/proc/loadavg', 'r') as f:
                stats["cpu_load"] = [float(x) for x in f.readline().split()[:3]]
        except Exception:
            pass

        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.read()
                mem_total = int(re.search(r'MemTotal:\s+(\d+)', meminfo).group(1))
                mem_free = int(re.search(r'MemFree:\s+(\d+)', meminfo).group(1))
                mem_buffers = int(re.search(r'Buffers:\s+(\d+)', meminfo).group(1))
                mem_cached = int(re.search(r'Cached:\s+(\d+)', meminfo).group(1))
                mem_used = mem_total - (mem_free + mem_buffers + mem_cached)
                stats["ram_used_pct"] = round((mem_used / mem_total) * 100, 1)
                stats["ram_total_gb"] = round(mem_total / (1024 * 1024), 1)
        except Exception:
            pass

    return stats

# Shell command execution helper
def run_command(cmd):
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.stdout, res.stderr, res.returncode
    except Exception as e:
        return "", str(e), -1

# Safe numeric parsers for nvidia-smi output ([N/A] or empty values are treated as 0)
def safe_int(value, default=0):
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default

def safe_float(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default

# GPU metrics parser
def get_gpu_stats():
    gpus = []
    igpus = []  # Integrated graphics (CPU iGPU), shown in separate tab
    
    if HAS_HIVEOS or IS_LINUX:
        stdout, stderr, code = run_command("nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,utilization.gpu,clocks.current.graphics,clocks.current.memory,power.limit --format=csv,noheader,nounits")
        if code == 0 and stdout:
            lines = stdout.strip().split('\n')
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 9:
                    idx = safe_int(parts[0], 0)
                    gpus.append({
                        "id": f"NV_{idx}",
                        "index": idx,
                        "brand": "NVIDIA",
                        "model": parts[1],
                        "temp": safe_int(parts[2]),
                        "fan": safe_int(parts[3]),
                        "power": safe_float(parts[4]),
                        "power_limit": safe_float(parts[8]),
                        "utilization": safe_int(parts[5]),
                        "core_clock": safe_int(parts[6]),
                        "mem_clock": safe_int(parts[7]),
                        "hashrate": 0.0
                    })

    if HAS_HIVEOS or IS_LINUX:
        if os.path.exists("/sys/class/drm"):
            cards = [d for d in os.listdir("/sys/class/drm") if re.match(r'^card\d+$', d)]
            amd_idx = 0
            for card in sorted(cards):
                hwmon_path = f"/sys/class/drm/{card}/device/hwmon"
                if os.path.exists(hwmon_path):
                    hwmons = os.listdir(hwmon_path)
                    if not hwmons:
                        continue
                    hpath = f"{hwmon_path}/{hwmons[0]}"
                    
                    vendor_path = f"/sys/class/drm/{card}/device/vendor"
                    if os.path.exists(vendor_path):
                        with open(vendor_path, 'r') as f:
                            vendor = f.read().strip()
                        if "0x1002" not in vendor:
                            continue
                            
                    try:
                        temp = 0
                        if os.path.exists(f"{hpath}/temp1_input"):
                            with open(f"{hpath}/temp1_input", 'r') as f:
                                temp = int(int(f.read().strip()) / 1000)
                        
                        fan = 0
                        if os.path.exists(f"{hpath}/fan1_input"):
                            with open(f"{hpath}/fan1_input", 'r') as f:
                                rpm = int(f.read().strip())
                                fan = min(100, int(rpm / 30))
                        
                        power = 0.0
                        if os.path.exists(f"{hpath}/power1_average"):
                            with open(f"{hpath}/power1_average", 'r') as f:
                                power = round(float(f.read().strip()) / 1000000.0, 1)
                                
                        model = "AMD Radeon GPU"
                        device_path = f"/sys/class/drm/{card}/device/device"
                        if os.path.exists(device_path):
                            with open(device_path, 'r') as f:
                                dev_id = f.read().strip()
                            model = f"AMD GPU ({dev_id})"
                        
                        gpus.append({
                            "id": f"AMD_{amd_idx}",
                            "index": amd_idx,
                            "brand": "AMD",
                            "model": model,
                            "temp": temp,
                            "fan": fan,
                            "power": power,
                            "power_limit": 0.0,
                            "utilization": 0,
                            "core_clock": 0,
                            "mem_clock": 0,
                            "hashrate": 0.0
                        })
                        amd_idx += 1
                    except Exception as e:
                        logging.error(f"Error reading AMD sysfs indices: {e}")

    if HAS_HIVEOS or IS_LINUX:
        if os.path.exists("/sys/class/drm"):
            cards = [d for d in os.listdir("/sys/class/drm") if re.match(r'^card\d+$', d)]
            intel_idx = 0
            for card in sorted(cards):
                vendor_path = f"/sys/class/drm/{card}/device/vendor"
                if os.path.exists(vendor_path):
                    try:
                        with open(vendor_path, 'r') as f:
                            vendor = f.read().strip()
                        if "0x8086" not in vendor:
                            continue
                    except Exception:
                        continue
                else:
                    continue

                device_path = f"/sys/class/drm/{card}/device/device"
                dev_id = ""
                if os.path.exists(device_path):
                    try:
                        with open(device_path, 'r') as f:
                            dev_id = f.read().strip().lower()
                    except Exception:
                        dev_id = ""

                # Discrete Arc cards carry device ids 0x56xx (Alchemist) or
                # 0xE2xx (Battlemage); anything else is CPU integrated graphics
                is_arc = dev_id.startswith(("0x56", "0xe2"))
                
                hwmon_path = f"/sys/class/drm/{card}/device/hwmon"
                temp = 0
                fan = 0
                power = 0.0
                model = "Intel Arc GPU" if is_arc else "Intel Integrated Graphics"
                
                if os.path.exists(hwmon_path):
                    try:
                        hwmons = os.listdir(hwmon_path)
                        if hwmons:
                            hpath = f"{hwmon_path}/{hwmons[0]}"
                            if os.path.exists(f"{hpath}/temp1_input"):
                                with open(f"{hpath}/temp1_input", 'r') as f:
                                    temp = int(int(f.read().strip()) / 1000)
                            
                            if os.path.exists(f"{hpath}/fan1_input"):
                                with open(f"{hpath}/fan1_input", 'r') as f:
                                    rpm = int(f.read().strip())
                                    fan = min(100, int(rpm / 30))
                                    
                            if os.path.exists(f"{hpath}/power1_average"):
                                with open(f"{hpath}/power1_average", 'r') as f:
                                    power = round(float(f.read().strip()) / 1000000.0, 1)
                    except Exception as e:
                        logging.debug(f"Failed to read Intel sysfs hwmon stats: {e}")
                        
                if os.path.exists(device_path):
                    try:
                        with open(device_path, 'r') as f:
                            dev_id_full = f.read().strip()
                        model = f"{'Intel Arc GPU' if is_arc else 'Intel Integrated Graphics'} ({dev_id_full})"
                    except Exception:
                        pass

                entry = {
                    "id": f"{'INTEL_' if is_arc else 'IGPU_'}{intel_idx}",
                    "index": intel_idx,
                    "brand": "INTEL",
                    "model": model,
                    "temp": temp,
                    "fan": fan,
                    "power": power,
                    "power_limit": 0.0,
                    "utilization": 0,
                    "core_clock": 0,
                    "mem_clock": 0,
                    "hashrate": 0.0
                }
                if is_arc:
                    gpus.append(entry)
                else:
                    igpus.append(entry)
                intel_idx += 1

    return {"gpus": gpus, "igpus": igpus}

# ---- Miner hashrate resolution (fills GPU cards + Total Speed) ----
_HASHRATE_UNITS = {"KH": 1e-3, "MH": 1.0, "GH": 1e3}

def _parse_hashrate_str(text):
    match = re.search(r'([0-9.]+)\s*(KH|MH|GH)', str(text))
    if match:
        return float(match.group(1)) * _HASHRATE_UNITS[match.group(2)]
    return 0.0

def _to_mh(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    # Miner APIs report raw H/s (tens of millions); values already in MH/s pass through
    if v > 1_000_000:
        return v / 1_000_000.0
    return v

def _extract_api_hashrate(data):
    """Supports rigel / t-rex / gminer local JSON stats formats."""
    total = 0.0
    per_gpu = {}
    if not isinstance(data, dict):
        return total, per_gpu

    # rigel: {"name":"Rigel","hashrate":{"algo":H/s},"devices":[{"id":0,"hashrate":{"algo":H/s}}]}
    devices = data.get("devices")
    if isinstance(devices, list) and devices and isinstance(data.get("hashrate"), dict):
        total = max((_to_mh(v) for v in data["hashrate"].values()), default=0.0)
        for g in devices:
            if isinstance(g, dict):
                idx = safe_int(g.get("id", -1), -1)
                hr = g.get("hashrate")
                if idx >= 0 and isinstance(hr, dict) and hr:
                    per_gpu[idx] = max((_to_mh(v) for v in hr.values()), default=0.0)
        return total, per_gpu

    # rigel (older): {"miners":[{"hashrate": H/s, "gpus":[{"id":0,"hashrate":H/s}]}]}
    miners = data.get("miners")
    if isinstance(miners, list) and miners:
        m = miners[0]
        total = _to_mh(m.get("hashrate"))
        for g in m.get("gpus") or []:
            if isinstance(g, dict):
                idx = safe_int(g.get("id", g.get("gpu_id", -1)), -1)
                if idx >= 0:
                    per_gpu[idx] = _to_mh(g.get("hashrate"))
        return total, per_gpu

    # t-rex: {"hashrate": H/s, "gpus":[{"gpu_id":0,"hashrate":H/s}]}
    if "hashrate" in data and "gpus" in data:
        total = _to_mh(data.get("hashrate"))
        for g in data.get("gpus") or []:
            if isinstance(g, dict):
                idx = safe_int(g.get("gpu_id", g.get("id", -1)), -1)
                if idx >= 0:
                    per_gpu[idx] = _to_mh(g.get("hashrate"))
        return total, per_gpu

    # gminer: {"miner":{"total_speed":["44.5 MH"]},"per_device":["11.1 MH",...]}
    miner_block = data.get("miner")
    if isinstance(miner_block, dict):
        ts = miner_block.get("total_speed") or []
        if isinstance(ts, list) and ts:
            total = _parse_hashrate_str(ts[0])
        for idx, val in enumerate(data.get("per_device") or []):
            per_gpu[idx] = _parse_hashrate_str(val)
    return total, per_gpu

def is_miner_screen_running():
    """True when a HiveOS miner screen session (N.miner) is alive."""
    _, _, code = run_command("screen -ls 2>/dev/null | grep -qE '[0-9]+\\.miner'")
    return code == 0

def get_miner_hashrate():
    """Returns (total_mh, per_gpu dict) from local miner stats API, log fallback."""
    total_mh = 0.0
    per_gpu = {}

    # 1. Miner HTTP stats APIs (rigel 5000, t-rex 4067, gminer/xmrig 4068, lolminer 4028)
    for port in (5000, 4067, 4068, 4028):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "hiveos-local"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode(errors="ignore"))
            total_mh, per_gpu = _extract_api_hashrate(data)
            if total_mh > 0 or per_gpu:
                return total_mh, per_gpu
        except Exception:
            continue

    # 2. Fallback: parse rigel-style miner log. Only valid while the miner screen is alive,
    #    otherwise stale log entries keep reporting hashrate after the miner stops.
    if not is_miner_screen_running():
        return 0.0, {}

    miner_name = parse_shell_config(RIG_CONF_PATH).get("MINER", "").strip().lower()
    if miner_name and miner_name != "none":
        allowed_base = os.path.abspath("/var/log/miner")
        for fname in (f"{miner_name}.log", "lastrun_noappend.log", "lastrun.log"):
            path = os.path.abspath(os.path.join(allowed_base, miner_name, fname))
            if not path.startswith(allowed_base + os.sep) or not os.path.exists(path):
                continue
            try:
                with open(path, 'r', errors='ignore') as f:
                    tail = f.readlines()[-60:]
                for line in reversed(tail):
                    # rigel: "|  Total: 245.8 MH/s|..." or legacy "Total speed: 245.8 MH/s"
                    m = re.search(r'Total(?: speed)?:\s*([0-9.]+)\s*(KH|MH|GH)/s', line)
                    if m and total_mh <= 0:
                        total_mh = float(m.group(1)) * _HASHRATE_UNITS[m.group(2)]
                    # rigel table row: "|6|RTX 3070 Laptop GPU|30.43 MH/s|22.50 MH/s|..."
                    g = re.search(r'\|\s*(\d+)\s*\|[^|]*\|\s*([0-9.]+)\s*(KH|MH|GH)/s', line)
                    if g:
                        idx = int(g.group(1))
                        if idx not in per_gpu:
                            per_gpu[idx] = float(g.group(2)) * _HASHRATE_UNITS[g.group(3)]
                    # legacy plain: "GPU0: 55.00 MH/s"
                    g2 = re.search(r'GPU(\d+):\s*([0-9.]+)\s*(KH|MH|GH)/s', line)
                    if g2:
                        idx = int(g2.group(1))
                        if idx not in per_gpu:
                            per_gpu[idx] = float(g2.group(2)) * _HASHRATE_UNITS[g2.group(3)]
                if total_mh > 0 or per_gpu:
                    break
            except Exception:
                continue

    return total_mh, per_gpu

# Read configs formatted
def get_overclocks_formatted():
    nv_data = parse_shell_config(NVIDIA_OC_CONF)
    amd_data = parse_shell_config(AMD_OC_CONF)
    
    return {
        "nvidia": {
            "core": nv_data.get("CORE", "").split(),
            "mem": nv_data.get("MEM", "").split(),
            "pl": nv_data.get("PL", "").split(),
            "fan": nv_data.get("FAN", "").split()
        },
        "amd": {
            "core": amd_data.get("CORE", "").split(),
            "mem": amd_data.get("MEM", "").split(),
            "vdd": amd_data.get("VDD", "").split(),
            "vddci": amd_data.get("VDDCI", "").split(),
            "mvdd": amd_data.get("MVDD", "").split(),
            "fan": amd_data.get("FAN", "").split(),
            "pl": amd_data.get("PL", "").split(),
            "dpm": amd_data.get("DPM", "").split(),
            "ref": amd_data.get("REF", "").split()
        }
    }

# Require authentication and CSRF token validations for all endpoints
@app.before_request
def require_auth():
    if request.path in ['/', '/api/login'] or request.path.startswith('/static/'):
        return

    # Machine-to-machine cluster calls authenticate with a bearer token
    # carrying the rig's dashboard password (not subject to CSRF)
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:].strip()
        expected = str(app.config.get('ACCESS_PASSWORD', ''))
        if token and expected and hmac.compare_digest(token, expected):
            request.bearer_auth = True
            return
        return jsonify({"success": False, "authenticated": False, "message": "Unauthorized"}), 401

    if not session.get('authenticated'):
        return jsonify({"success": False, "authenticated": False, "message": "Unauthorized"}), 401
        
    # Enforce Anti-CSRF on all POST write actions
    if request.method == 'POST':
        token = request.headers.get('X-CSRF-Token')
        expected = session.get('csrf_token')
        if not token or not expected or token != expected:
            logging.warning(f"CSRF Alert: Invalid or missing token from IP {request.remote_addr}")
            return jsonify({"success": False, "message": "CSRF verification failed."}), 403

@app.route('/api/login', methods=['POST'])
def api_login():
    ip = request.remote_addr
    now = time.time()
    
    # Check if currently locked out
    if ip in failed_login_attempts:
        record = failed_login_attempts[ip]
        if record["blocked_until"] > now:
            remaining = int(record["blocked_until"] - now)
            logging.warning(f"Blocked login attempt from locked-out IP {ip}. Remaining lockout: {remaining}s")
            return jsonify({"success": False, "message": f"Too many failed attempts. Try again in {remaining} seconds."}), 429
            
    data = request.get_json()
    if not data or ('password' not in data and 'pin' not in data):
        return jsonify({"success": False, "message": "Missing password"}), 400
        
    user_key = str(data.get('password', data.get('pin', ''))).strip()
    if user_key and hmac.compare_digest(user_key, str(app.config['ACCESS_PASSWORD'])):
        # Reset failure record
        if ip in failed_login_attempts:
            del failed_login_attempts[ip]
            
        session['authenticated'] = True
        # Generate CSRF token
        csrf_token = os.urandom(16).hex()
        session['csrf_token'] = csrf_token
        
        logging.info(f"Authorized login request from IP: {ip}")
        return jsonify({"success": True, "message": "Authenticated successfully!", "csrf_token": csrf_token})
    
    # Log failure and increment counters
    if ip not in failed_login_attempts:
        failed_login_attempts[ip] = {"count": 1, "blocked_until": 0.0}
    else:
        failed_login_attempts[ip]["count"] += 1
        
    record = failed_login_attempts[ip]
    if record["count"] >= 5:
        record["blocked_until"] = now + 900.0  # 15 minutes lockout
        logging.warning(f"IP {ip} locked out for 15 minutes due to 5 failed attempts")
        return jsonify({"success": False, "message": "Too many failed attempts. Locked out for 15 minutes."}), 429
        
    logging.warning(f"Invalid access password attempt {record['count']}/5 from IP: {ip}")
    return jsonify({"success": False, "message": f"Invalid password. {5 - record['count']} attempts remaining."}), 401

@app.route('/api/auth/password', methods=['POST'])
def change_password():
    """Change the dashboard access password (replaces the legacy PIN concept)."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400

    current = str(data.get("current_password", ""))
    new_password = str(data.get("new_password", "")).strip()

    if not current or not hmac.compare_digest(current, str(app.config['ACCESS_PASSWORD'])):
        logging.warning(f"Failed password change verification from IP: {request.remote_addr}")
        return jsonify({"success": False, "message": "Current password is incorrect."}), 401

    if not (4 <= len(new_password) <= 64):
        return jsonify({"success": False, "message": "New password must be 4-64 characters long."}), 400
    if not re.match(r'^[A-Za-z0-9!@\#$%^\&\*\(\)_\-+=\[\]\{\};:,\.<>\?/~\s]+$', new_password):
        return jsonify({"success": False, "message": "Password contains unsupported characters."}), 400

    try:
        with config_lock:
            with open(PIN_PATH, 'w') as f:
                f.write(new_password)
            os.chmod(PIN_PATH, 0o600)
    except Exception as e:
        logging.error(f"Failed to write new access password: {e}")
        return jsonify({"success": False, "message": "Failed to save the new password."}), 500

    app.config['ACCESS_PASSWORD'] = new_password

    # Bump the self cluster entry so peers adopt the new password on next sync
    try:
        state = load_cluster_state()
        for r in state["rigs"]:
            if r.get("id") == state["self_id"]:
                r["password"] = new_password
                r["updated_at"] = int(time.time())
        save_cluster_state(state)
    except Exception as e:
        logging.error(f"Failed to propagate new password to cluster config: {e}")

    logging.info(f"Dashboard access password changed by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": "Access password updated successfully!"})

@app.route('/api/overclock', methods=['POST'])
def save_overclock():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON payload"}), 400
        
    brand = data.get("brand", "").upper()
    try:
        gpu_index = int(data.get("index", 0))
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "GPU index must be an integer."}), 400
        
    if not (0 <= gpu_index < 64):
        return jsonify({"success": False, "message": "GPU index out of acceptable bounds (0-63)."}), 400
    
    if brand not in ["NVIDIA", "AMD"]:
        return jsonify({"success": False, "message": "Invalid brand specification"}), 400

    # 1. Strict Shell-Injection checks
    for key, val in data.items():
        if key not in ["brand", "index"]:
            if not is_safe_parameter_value(val):
                logging.warning(f"Security Alert: Blocked shell injection signature on parameter {key}='{val}' from {request.remote_addr}")
                return jsonify({"success": False, "message": f"Security Alert: Malicious character detected inside value '{val}'"}), 400

    # 2. Clamping bounds check validation
    is_valid, err_msg = validate_overclock_ranges(brand, data)
    if not is_valid:
        logging.warning(f"Overclock range validation failed: {err_msg} from IP {request.remote_addr}")
        return jsonify({"success": False, "message": err_msg}), 400

    # Backup prior configs before editing
    backup_configs()
        
    if brand == "NVIDIA":
        filepath = NVIDIA_OC_CONF
        config = parse_shell_config(filepath)
        
        core = config.get("CORE", "").split()
        mem = config.get("MEM", "").split()
        pl = config.get("PL", "").split()
        fan = config.get("FAN", "").split()
        
        max_idx = max(3, gpu_index)
        core += ["0"] * (max_idx + 1 - len(core))
        mem += ["0"] * (max_idx + 1 - len(mem))
        pl += ["0"] * (max_idx + 1 - len(pl))
        fan += ["0"] * (max_idx + 1 - len(fan))
        
        if "core" in data:
            core[gpu_index] = str(data["core"])
        if "mem" in data:
            mem[gpu_index] = str(data["mem"])
        if "pl" in data:
            pl[gpu_index] = str(data["pl"])
        if "fan" in data:
            fan[gpu_index] = str(data["fan"])
            
        config["CORE"] = " ".join(core)
        config["MEM"] = " ".join(mem)
        config["PL"] = " ".join(pl)
        config["FAN"] = " ".join(fan)
        
        write_shell_config(filepath, config)
        logging.info(f"NVIDIA GPU {gpu_index} parameters updated: Core={data.get('core')}, Mem={data.get('mem')}, PL={data.get('pl')}, Fan={data.get('fan')}")
        
        stdout, stderr, code = run_command("sudo /hive/sbin/nvidia-oc")
        if code != 0:
            logging.error(f"NVIDIA OC script failed: {stderr}")
            return jsonify({"success": False, "message": "NVIDIA overclock script failed to apply settings."})
        
    elif brand == "AMD":
        filepath = AMD_OC_CONF
        config = parse_shell_config(filepath)
        
        fields = ["CORE", "MEM", "VDD", "VDDCI", "MVDD", "FAN", "PL", "DPM", "REF"]
        parsed_fields = {}
        for fld in fields:
            parsed_fields[fld] = config.get(fld, "").split()
            parsed_fields[fld] += ["0"] * (gpu_index + 1 - len(parsed_fields[fld]))
            
        for key in fields:
            payload_key = key.lower()
            if payload_key in data:
                parsed_fields[key][gpu_index] = str(data[payload_key])
                
        for key in fields:
            config[key] = " ".join(parsed_fields[key])
            
        write_shell_config(filepath, config)
        logging.info(f"AMD GPU {gpu_index} parameters updated: {config}")
        
        stdout, stderr, code = run_command("sudo /hive/sbin/amd-oc")
        if code != 0:
            logging.error(f"AMD OC script failed: {stderr}")
            return jsonify({"success": False, "message": "AMD overclock script failed to apply settings."})
                
    return jsonify({"success": True, "message": f"Overclock parameters successfully saved and applied to {brand} GPU {gpu_index}!"})

@app.route('/api/revert', methods=['POST'])
def revert_overclock():
    try:
        nv_bak = NVIDIA_OC_CONF + ".bak"
        amd_bak = AMD_OC_CONF + ".bak"

        if not os.path.exists(nv_bak) and not os.path.exists(amd_bak):
            return jsonify({"success": False, "message": "No stable backups found to restore."}), 404
            
        with config_lock:
            if os.path.exists(nv_bak):
                shutil.copy2(nv_bak, NVIDIA_OC_CONF)
            if os.path.exists(amd_bak):
                shutil.copy2(amd_bak, AMD_OC_CONF)
                
        if os.path.exists(nv_bak):
            run_command("sudo /hive/sbin/nvidia-oc")
        if os.path.exists(amd_bak):
            run_command("sudo /hive/sbin/amd-oc")
                     
        logging.info(f"Configurations successfully reverted by request from {request.remote_addr}")
        return jsonify({"success": True, "message": "Overclock settings reverted to previous configuration!"})
    except Exception as e:
        logging.error(f"Failed to revert configuration: {e}")
        return jsonify({"success": False, "message": "An internal error occurred while trying to restore configurations."}), 500

@app.route('/api/hugepages', methods=['POST'])
def toggle_hugepages():
    data = request.get_json()
    enable = data.get("enable", True) if data else True
        
    action = "enable" if enable else "disable"
    cmd = f"sudo /hive/bin/hugepages {action}"
    stdout, stderr, code = run_command(cmd)
    
    if code == 0:
        msg = f"Huge Pages successfully {action}d!"
        logging.info(msg)
        return jsonify({"success": True, "message": msg})
    else:
        logging.error(f"Failed to configure Huge Pages: {stderr}")
        return jsonify({"success": False, "message": "Failed to configure System Huge Pages."}), 500

@app.route('/api/miner/control', methods=['POST'])
def miner_control():
    data = request.get_json()
    if not data or 'action' not in data:
        return jsonify({"success": False, "message": "Missing action parameter"}), 400
        
    action = str(data['action']).strip().lower()
    if action not in ["start", "stop", "restart"]:
        return jsonify({"success": False, "message": "Invalid action parameter. Must be start, stop, or restart."}), 400
        
    logging.info(f"Miner control request: '{action}' received from IP: {request.remote_addr}")
    
    if action == "start":
        stdout, stderr, code = run_command(MINER_START_CMD)
    elif action == "stop":
        stdout, stderr, code = run_command(MINER_STOP_CMD)
    elif action == "restart":
        # Use the built-in hive restart (handles stop + start safely, even when miner is stopped)
        stdout, stderr, code = run_command(MINER_RESTART_CMD)

    output = f"{stdout}\n{stderr}".strip()

    # The hive miner script reports state via text, not exit codes:
    # - "miner start" exits 0 even when the screen is already running
    # - "miner stop" exits 1 when no screens exist (which simply means miner is stopped)
    if action == "start" and "already running" in output:
        logging.info("Miner start skipped: screen session is already running")
        return jsonify({"success": True, "message": "Miner is already running (screen session active)."})
    if action == "stop" and "No miner screens found" in output:
        logging.info("Miner stop skipped: no miner screens are running")
        return jsonify({"success": True, "message": "Miner is already stopped (no running screens)."})
    if "Maintenance mode enabled" in output:
        logging.warning("Miner control blocked: maintenance mode is enabled")
        return jsonify({"success": False, "message": "Maintenance mode is enabled. Disable it in HiveOS to control the miner."}), 409

    if code == 0:
        msg = f"Miner successfully {action}ed!"
        logging.info(msg)
        return jsonify({"success": True, "message": msg})
    else:
        logging.error(f"Miner control command failed: {output}")
        detail = output.splitlines()[-1] if output else "Unknown error"
        return jsonify({"success": False, "message": f"Miner command failed: {detail}"}), 500

# 1. System Power Routes (Reboot / Shutdown)
@app.route('/api/system/reboot', methods=['POST'])
def system_reboot():
    logging.info(f"System reboot requested by IP: {request.remote_addr}")
    cmd = 'nohup bash -c "sleep 1.5 && sudo /hive/sbin/sreboot" > /dev/null 2>&1 &'
    subprocess.Popen(cmd, shell=True)
    return jsonify({"success": True, "message": "Reboot command initiated. Rig will restart shortly."})

@app.route('/api/system/shutdown', methods=['POST'])
def system_shutdown():
    logging.info(f"System shutdown requested by IP: {request.remote_addr}")
    cmd = 'nohup bash -c "sleep 1.5 && sudo /hive/sbin/sreboot shutdown" > /dev/null 2>&1 &'
    subprocess.Popen(cmd, shell=True)
    return jsonify({"success": True, "message": "Shutdown command initiated. Rig will power down shortly."})

# 2. Miner Console Log Streamer
@app.route('/api/miner/log', methods=['GET'])
def get_miner_log():
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    miner = rig_conf.get("MINER", "").strip().lower()
    if not miner or miner == "none":
        return jsonify({"success": False, "message": "No active miner is configured on this rig."}), 404
        
    log_candidates = [
        f"/var/log/miner/{miner}/{miner}.log",
        f"/var/log/miner/{miner}/lastrun_noappend.log",
        f"/var/log/miner/{miner}/lastrun.log",
    ]
    
    log_content = ""
    found_path = None
    allowed_base = os.path.abspath("/var/log/miner")
    for p in log_candidates:
        p_abs = os.path.abspath(p)
        # Verify candidate log resides strictly inside allowed log folder path to satisfy CodeQL
        if p_abs.startswith(allowed_base + os.sep):
            if os.path.exists(p_abs):
                found_path = p_abs
                break
            
    if found_path:
        try:
            with open(found_path, 'r', errors='ignore') as f:
                lines = f.readlines()[-150:]
                log_content = "".join(lines)
        except Exception as e:
            logging.error(f"Error reading miner log {found_path}: {e}")
            return jsonify({"success": False, "message": "Failed to read miner log file."}), 500
    else:
        return jsonify({"success": False, "message": f"Log file for miner '{miner}' not found. Verify miner is running."}), 404
        
    return jsonify({"success": True, "miner": miner, "log": log_content})

# 3. Watchdog Config Management
@app.route('/api/watchdog', methods=['GET', 'POST'])
def handle_watchdog():
    if request.method == 'GET':
        rig_conf = parse_shell_config(RIG_CONF_PATH)
        return jsonify({
            "success": True,
            "wd_enabled": rig_conf.get("WD_ENABLED", "0"),
            "wd_min_hashrate": rig_conf.get("WD_MIN_HASHRATE", "0")
        })
        
    # POST
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
        
    enabled = str(data.get("wd_enabled", "0")).strip()
    min_hashrate = str(data.get("wd_min_hashrate", "0")).strip()
    
    if enabled not in ["0", "1"]:
        return jsonify({"success": False, "message": "wd_enabled must be 0 or 1."}), 400
    if not re.match(r'^[0-9\.]+$', min_hashrate):
        return jsonify({"success": False, "message": "Min hashrate must be a valid number."}), 400
        
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    rig_conf["WD_ENABLED"] = enabled
    rig_conf["WD_MIN_HASHRATE"] = min_hashrate
    
    if write_shell_config(RIG_CONF_PATH, rig_conf):
        logging.info(f"Watchdog settings updated by IP: {request.remote_addr} (Enabled={enabled}, Min={min_hashrate})")
        run_command("sudo /hive/bin/wd restart")
        return jsonify({"success": True, "message": "Watchdog settings saved and daemon restarted!"})
    else:
        return jsonify({"success": False, "message": "Failed to write watchdog settings to rig.conf."}), 500

# 4. Autofan Settings Config
@app.route('/api/autofan', methods=['GET', 'POST'])
def handle_autofan():
    if request.method == 'GET':
        config = parse_shell_config(AUTOFAN_CONF)
        return jsonify({
            "success": True,
            "enabled": config.get("ENABLED", "0"),
            "target_temp": config.get("TARGET_TEMP", "60"),
            "target_mem_temp": config.get("TARGET_MEM_TEMP", "80"),
            "min_fan": config.get("MIN_FAN", "30"),
            "max_fan": config.get("MAX_FAN", "100"),
            "critical_temp": config.get("CRITICAL_TEMP", "85")
        })
        
    # POST
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
        
    enabled = str(data.get("enabled", "0")).strip()
    target_temp = str(data.get("target_temp", "60")).strip()
    target_mem_temp = str(data.get("target_mem_temp", "80")).strip()
    min_fan = str(data.get("min_fan", "30")).strip()
    max_fan = str(data.get("max_fan", "100")).strip()
    critical_temp = str(data.get("critical_temp", "85")).strip()
    
    for val in [enabled, target_temp, target_mem_temp, min_fan, max_fan, critical_temp]:
        if not val.isdigit():
            return jsonify({"success": False, "message": "All autofan values must be integers."}), 400
            
    if enabled not in ["0", "1"]:
        return jsonify({"success": False, "message": "enabled must be 0 or 1."}), 400
    if not (30 <= int(target_temp) <= 90):
        return jsonify({"success": False, "message": "Target core temperature must be between 30 and 90 C."}), 400
    if not (40 <= int(target_mem_temp) <= 110):
        return jsonify({"success": False, "message": "Target memory temperature must be between 40 and 110 C."}), 400
    if not (0 <= int(min_fan) <= 100) or not (0 <= int(max_fan) <= 100):
        return jsonify({"success": False, "message": "Fan speed limits must be between 0 and 100%."}), 400
    if int(min_fan) > int(max_fan):
        return jsonify({"success": False, "message": "Minimum fan speed cannot be greater than maximum fan speed."}), 400
    if not (50 <= int(critical_temp) <= 95):
        return jsonify({"success": False, "message": "Critical temperature must be between 50 and 95 C."}), 400
        
    config = {
        "ENABLED": enabled,
        "TARGET_TEMP": target_temp,
        "TARGET_MEM_TEMP": target_mem_temp,
        "MIN_FAN": min_fan,
        "MAX_FAN": max_fan,
        "CRITICAL_TEMP": critical_temp,
        "CRITICAL_TEMP_ACTION": "reboot"
    }
    
    if write_shell_config(AUTOFAN_CONF, config):
        logging.info(f"Autofan configuration updated by IP: {request.remote_addr}")
        run_command("sudo /hive/bin/autofan restart")
        return jsonify({"success": True, "message": "Autofan settings saved and service restarted!"})
    else:
        return jsonify({"success": False, "message": "Failed to save autofan.conf"}), 500

# 5. Local Preset Profile Swappers
@app.route('/api/presets', methods=['GET'])
def list_presets():
    presets = []
    if os.path.exists(PRESETS_DIR):
        try:
            presets = [d for d in os.listdir(PRESETS_DIR) if os.path.isdir(os.path.join(PRESETS_DIR, d))]
        except Exception as e:
            logging.error(f"Failed to list presets directory: {e}")
    return jsonify({"success": True, "presets": sorted(presets)})

@app.route('/api/presets/save', methods=['POST'])
def save_preset():
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({"success": False, "message": "Missing preset name"}), 400
        
    name = str(data['name']).strip()
    if not re.match(r'^[A-Za-z0-9_\-\s]+$', name):
        return jsonify({"success": False, "message": "Invalid preset name. Use alphanumeric characters and spaces only."}), 400
        
    # Enforce strict path prefix containment validation to block CodeQL Path Traversal warnings
    presets_dir_abs = os.path.abspath(PRESETS_DIR)
    preset_path = os.path.abspath(os.path.join(presets_dir_abs, name))
    if not preset_path.startswith(presets_dir_abs + os.sep) and preset_path != presets_dir_abs:
        logging.warning(f"Security Alert: Blocked path containment escape in save preset '{name}' from IP {request.remote_addr}")
        return jsonify({"success": False, "message": "Invalid preset name path configuration."}), 400
    
    try:
        with config_lock:
            if not os.path.exists(preset_path):
                os.makedirs(preset_path, exist_ok=True)
                
            if os.path.exists(WALLET_CONF_PATH):
                shutil.copy2(WALLET_CONF_PATH, os.path.join(preset_path, "wallet.conf"))
            if os.path.exists(os.path.join(HIVE_CONFIG_DIR, "miner.conf")):
                shutil.copy2(os.path.join(HIVE_CONFIG_DIR, "miner.conf"), os.path.join(preset_path, "miner.conf"))
                
            rig_conf = parse_shell_config(RIG_CONF_PATH)
            write_shell_config(os.path.join(preset_path, "rig_preset.conf"), {
                "MINER": rig_conf.get("MINER", "none")
            })
            
        logging.info(f"Preset '{name}' saved successfully by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": f"Preset '{name}' successfully saved!"})
    except Exception as e:
        logging.error(f"Failed to save preset '{name}': {e}")
        return jsonify({"success": False, "message": "Failed to save preset files."}), 500

@app.route('/api/presets/apply', methods=['POST'])
def apply_preset():
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({"success": False, "message": "Missing preset name"}), 400
        
    name = str(data['name']).strip()
    if not re.match(r'^[A-Za-z0-9_\-\s]+$', name):
        return jsonify({"success": False, "message": "Invalid preset name."}), 400
        
    # Enforce strict path prefix containment validation to block CodeQL Path Traversal warnings
    presets_dir_abs = os.path.abspath(PRESETS_DIR)
    preset_path = os.path.abspath(os.path.join(presets_dir_abs, name))
    if not preset_path.startswith(presets_dir_abs + os.sep) and preset_path != presets_dir_abs:
        logging.warning(f"Security Alert: Blocked path containment escape in apply preset '{name}' from IP {request.remote_addr}")
        return jsonify({"success": False, "message": "Invalid preset name path configuration."}), 400
        
    if not os.path.exists(preset_path):
        return jsonify({"success": False, "message": f"Preset '{name}' does not exist."}), 404
        
    try:
        with config_lock:
            preset_wallet = os.path.join(preset_path, "wallet.conf")
            preset_miner = os.path.join(preset_path, "miner.conf")
            preset_rig = os.path.join(preset_path, "rig_preset.conf")
            
            if os.path.exists(preset_wallet):
                shutil.copy2(preset_wallet, WALLET_CONF_PATH)
            if os.path.exists(preset_miner):
                shutil.copy2(preset_miner, os.path.join(HIVE_CONFIG_DIR, "miner.conf"))
                
            if os.path.exists(preset_rig):
                p_rig = parse_shell_config(preset_rig)
                if "MINER" in p_rig:
                    rig_conf = parse_shell_config(RIG_CONF_PATH)
                    rig_conf["MINER"] = p_rig["MINER"]
                    write_shell_config(RIG_CONF_PATH, rig_conf)
                    
        logging.info(f"Preset '{name}' applied successfully by IP: {request.remote_addr}. Restarting miner...")
        run_command(MINER_RESTART_CMD)
        return jsonify({"success": True, "message": f"Preset '{name}' applied successfully! Miner restarting..."})
    except Exception as e:
        logging.error(f"Failed to apply preset '{name}': {e}")
        return jsonify({"success": False, "message": "Failed to restore preset configuration files."}), 500

@app.route('/api/presets/delete', methods=['POST'])
def delete_preset():
    data = request.get_json()
    if not data or 'name' not in data:
        return jsonify({"success": False, "message": "Missing preset name"}), 400
        
    name = str(data['name']).strip()
    if not re.match(r'^[A-Za-z0-9_\-\s]+$', name):
        return jsonify({"success": False, "message": "Invalid preset name."}), 400
        
    # Enforce strict path prefix containment validation to block CodeQL Path Traversal warnings
    presets_dir_abs = os.path.abspath(PRESETS_DIR)
    preset_path = os.path.abspath(os.path.join(presets_dir_abs, name))
    if not preset_path.startswith(presets_dir_abs + os.sep) and preset_path != presets_dir_abs:
        logging.warning(f"Security Alert: Blocked path containment escape in delete preset '{name}' from IP {request.remote_addr}")
        return jsonify({"success": False, "message": "Invalid preset name path configuration."}), 400
        
    if not os.path.exists(preset_path):
        return jsonify({"success": False, "message": "Preset not found."}), 404
        
    try:
        with config_lock:
            shutil.rmtree(preset_path)
            
        logging.info(f"Preset '{name}' deleted successfully by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": f"Preset '{name}' successfully deleted."})
    except Exception as e:
        logging.error(f"Failed to delete preset '{name}': {e}")
        return jsonify({"success": False, "message": "Failed to remove preset files."}), 500

def ping_host(host, count=1, timeout=2):
    """Utility helper pinging a destination IP/Host under Linux environment."""
    cmd = f"ping -c {count} -W {timeout} {host}"
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0
    except Exception:
        return False

def get_local_gateway():
    """Detects default routing gateway IP using route commands."""
    try:
        res = subprocess.run("ip route show | grep default", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout:
            parts = res.stdout.split()
            if len(parts) >= 3:
                return parts[2]
    except Exception:
        pass
    return "127.0.0.1"

@app.route('/api/diagnostics', methods=['GET'])
def get_diagnostics():
    gateway = get_local_gateway()
    
    # Run diagnostics pings
    gateway_ok = ping_host(gateway)
    internet_ok = ping_host("8.8.8.8")
    hive_api_ok = ping_host("api.hiveon.com")
    
    # DNS check
    dns_ok = False
    try:
        socket.gethostbyname("google.com")
        dns_ok = True
    except Exception:
        pass
        
    # GPU kernel driver logs check
    gpu_logs = "No driver logs detected."
    try:
        res = subprocess.run("dmesg | grep -iE 'nouveau|nvidia|amdgpu|pci|thermal|power' | tail -n 25", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0 and res.stdout:
            gpu_logs = res.stdout
    except Exception as e:
        logging.error(f"Failed to retrieve dmesg driver logs: {e}")
        gpu_logs = "Failed to retrieve driver logs from system kernel."
        
    return jsonify({
        "success": True,
        "gateway_ip": gateway,
        "gateway_ping": "Online" if gateway_ok else "Offline",
        "internet_wan": "Online" if internet_ok else "Offline",
        "dns_resolution": "Working" if dns_ok else "Failed",
        "hiveos_api": "Reachable" if hive_api_ok else "Unreachable",
        "gpu_logs": gpu_logs
    })

@app.route('/api/flightsheet', methods=['GET', 'POST'])
def handle_flightsheet():
    if request.method == 'GET':
        wallet_conf = parse_shell_config(WALLET_CONF_PATH)
        rig_conf = parse_shell_config(RIG_CONF_PATH)
        return jsonify({
            "success": True,
            "coin": wallet_conf.get("COIN", ""),
            "wallet": wallet_conf.get("WAL", ""),
            "pool": wallet_conf.get("POOL_URL", ""),
            "miner": rig_conf.get("MINER", "none")
        })
        
    # POST - Save Flight Sheet
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON payload"}), 400
        
    coin = str(data.get("coin", "")).strip()
    wallet = str(data.get("wallet", "")).strip()
    pool = str(data.get("pool", "")).strip()
    miner = str(data.get("miner", "none")).strip().lower()
    
    # Parameter inputs validation
    if not re.match(r'^[A-Za-z0-9_\-\s]+$', coin):
        return jsonify({"success": False, "message": "Invalid Coin parameter. Use alphanumeric characters only."}), 400
    if not re.match(r'^[A-Za-z0-9_\-\s\.\/\@]+$', wallet):
        return jsonify({"success": False, "message": "Invalid Wallet format."}), 400
    if not re.match(r'^[a-zA-Z0-9\.\-\:\/]+$', pool):
        return jsonify({"success": False, "message": "Invalid Pool URL format."}), 400
        
    whitelisted_miners = [
        "lolminer", "xmrig", "gminer", "rigel", "bzminer", 
        "teamredminer", "hiveon", "srbminer", "wildrig-multi",
        "bminer", "ccminer", "t-rex", "none"
    ]
    if miner not in whitelisted_miners:
        return jsonify({"success": False, "message": "Unsupported miner program choice."}), 400

    # Backup files first
    try:
        if os.path.exists(WALLET_CONF_PATH):
            shutil.copy2(WALLET_CONF_PATH, WALLET_CONF_PATH + ".bak")
        if os.path.exists(RIG_CONF_PATH):
            shutil.copy2(RIG_CONF_PATH, RIG_CONF_PATH + ".bak")
    except Exception as e:
        logging.error(f"Backup configurations failed: {e}")
        
    # Update configurations
    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    wallet_conf["COIN"] = coin
    wallet_conf["WAL"] = wallet
    wallet_conf["POOL_URL"] = pool
    # Write back
    if not write_shell_config(WALLET_CONF_PATH, wallet_conf):
        return jsonify({"success": False, "message": "Failed to write wallet.conf"}), 500
        
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    rig_conf["MINER"] = miner
    if not write_shell_config(RIG_CONF_PATH, rig_conf):
        return jsonify({"success": False, "message": "Failed to write rig.conf"}), 500
        
    logging.info(f"Emergency Local Flight Sheet updated by IP: {request.remote_addr} (Coin={coin}, Miner={miner})")
    
    # Restart miner to apply settings on the fly
    run_command(MINER_RESTART_CMD)
    return jsonify({"success": True, "message": "Flight sheet saved successfully! Miner daemon restarting..."})

@app.route('/api/overclock/reset', methods=['POST'])
def reset_overclock():
    # Create backups first
    backup_configs()
    
    nv_stock = {
        "CORE": "",
        "MEM": "",
        "PL": "",
        "FAN": ""
    }
    amd_stock = {
        "CORE": "",
        "MEM": "",
        "VDD": "",
        "VDDCI": "",
        "MVDD": "",
        "FAN": "",
        "PL": "",
        "DPM": "",
        "REF": ""
    }
    
    if write_shell_config(NVIDIA_OC_CONF, nv_stock) and write_shell_config(AMD_OC_CONF, amd_stock):
        logging.info(f"Emergency overclock reset to stock by request from {request.remote_addr}")
        # Apply clean stock settings immediately on hardware
        run_command("sudo /hive/sbin/nvidia-oc")
        run_command("sudo /hive/sbin/amd-oc")
        return jsonify({"success": True, "message": "Emergency reset completed! All overclock profiles reset to safe factory stock limits."})
    else:
        return jsonify({"success": False, "message": "Failed to overwrite overclock configuration files."}), 500

@app.route('/api/services/control', methods=['POST'])
def service_control():
    data = request.get_json()
    if not data or 'service' not in data or 'action' not in data:
        return jsonify({"success": False, "message": "Missing service or action parameter."}), 400
        
    service = str(data['service']).strip().lower()
    action = str(data['action']).strip().lower()
    
    if service not in ["wd", "autofan", "hiveos-local"]:
        return jsonify({"success": False, "message": "Invalid service target."}), 400
    if action not in ["start", "stop", "restart"]:
        return jsonify({"success": False, "message": "Invalid action choice."}), 400
        
    logging.info(f"Service control: '{action}' on '{service}' by IP: {request.remote_addr}")
    
    code = 0
    stderr = ""
    
    if service == "wd":
        if action in ["start", "restart"]:
            stdout, stderr, code = run_command("sudo /hive/bin/wd restart")
        else:
            stdout, stderr, code = run_command("sudo /hive/bin/wd stop")
    elif service == "autofan":
        if action in ["start", "restart"]:
            stdout, stderr, code = run_command("sudo /hive/bin/autofan restart")
        else:
            stdout, stderr, code = run_command("sudo /hive/bin/autofan stop")
    elif service == "hiveos-local":
        if action == "restart":
            cmd = 'nohup bash -c "sleep 1.5 && sudo systemctl restart hiveos-local.service" > /dev/null 2>&1 &'
            subprocess.Popen(cmd, shell=True)
            return jsonify({"success": True, "message": "Logger service restart scheduled."})
        elif action == "stop":
            cmd = 'nohup bash -c "sleep 1.5 && sudo systemctl stop hiveos-local.service" > /dev/null 2>&1 &'
            subprocess.Popen(cmd, shell=True)
            return jsonify({"success": True, "message": "Logger service shutdown scheduled."})
        elif action == "start":
            stdout, stderr, code = run_command("sudo systemctl start hiveos-local.service")
            
    if code == 0:
        return jsonify({"success": True, "message": f"Service '{service}' successfully {action}ed!"})
    else:
        logging.error(f"Service control execution failed on {service}: {stderr}")
        return jsonify({"success": False, "message": "Service command execution failed. Check system logs."}), 500

@app.route('/api/update/check', methods=['GET'])
def check_update():
    try:
        url = "https://raw.githubusercontent.com/h1w0rld/hiveos-local/main/version.txt"
        req = urllib.request.Request(url, headers={'User-Agent': 'HiveOS-Local-Dashboard'})
        with urllib.request.urlopen(req, timeout=5) as response:
            remote_ver = response.read().decode('utf-8').strip()
        
        update_available = remote_ver != VERSION
        
        return jsonify({
            "success": True,
            "local_version": VERSION,
            "remote_version": remote_ver,
            "update_available": update_available
        })
    except Exception as e:
        logging.warning(f"Failed to check remote version: {e}")
        return jsonify({
            "success": False,
            "local_version": VERSION,
            "remote_version": "Unknown",
            "update_available": False,
            "error": "Failed to verify version against GitHub."
        })

@app.route('/api/update/pull', methods=['POST'])
def pull_update():
    data = request.get_json()
    if not data or ('password' not in data and 'pin' not in data):
        return jsonify({"success": False, "message": "Missing password verification parameter"}), 400
        
    user_key = str(data.get('password', data.get('pin', ''))).strip()
    if not user_key or not hmac.compare_digest(user_key, str(app.config['ACCESS_PASSWORD'])):
        logging.warning(f"Failed update password verification attempt from IP: {request.remote_addr}")
        return jsonify({"success": False, "message": "Invalid password verification. Update aborted."}), 401

    logging.info(f"Dashboard update authorized with password by IP: {request.remote_addr}")
    cwd = os.getcwd()
    
    # Add safe directory flag
    run_command(f"git config --global --add safe.directory {cwd}")
    
    stdout_f, stderr_f, code_f = run_command("git fetch --all")
    stdout_r, stderr_r, code_r = run_command("git reset --hard origin/main")
    
    if code_r == 0:
        msg = "Update successfully pulled from GitHub! Restarting dashboard service..."
        logging.info(msg)
        cmd = 'nohup bash -c "sleep 1.5 && sudo systemctl restart hiveos-local.service" > /dev/null 2>&1 &'
        subprocess.Popen(cmd, shell=True)
        return jsonify({"success": True, "message": msg})
    else:
        err_msg = f"Failed to pull git update: {stderr_r or stderr_f}"
        logging.error(err_msg)
        return jsonify({"success": False, "message": "Failed to pull dashboard update from GitHub repository."}), 500

@app.route('/api/stats')
def api_stats():
    return jsonify(collect_stats_payload())

# ---------------- Cluster API (UI + peer exchange) ----------------

def _find_rig(state, rig_id):
    for r in state["rigs"]:
        if r.get("id") == rig_id:
            return r
    return None

def _rig_view(rig, state, cache):
    """Serialize a rig entry with cached stats for the UI."""
    entry = cache.get("rigs", {}).get(rig["id"], {})
    stats = entry.get("stats")
    is_self = rig.get("id") == state["self_id"]
    view = {
        "id": rig.get("id"),
        "name": rig.get("name", ""),
        "host_label": rig.get("host_label", ""),
        "is_self": is_self,
        "accesses": [clean_access_entry(a) for a in rig.get("accesses", [])],
        "updated_at": rig.get("updated_at", 0),
        "online": bool(entry.get("online")) if rig.get("id") != state["self_id"] else True,
        "last_sync": entry.get("fetched_at", 0),
        "last_error": entry.get("error", ""),
        "stats": stats
    }
    # Mask SSH passwords in UI responses
    masked = []
    for a in view["accesses"]:
        m = dict(a)
        if "password" in m:
            m["password"] = "********" if m["password"] else ""
        if "jump_password" in m:
            m["jump_password"] = "********" if m["jump_password"] else ""
        masked.append(m)
    view["accesses"] = masked
    return view

@app.route('/api/cluster/rigs', methods=['GET'])
def api_cluster_rigs():
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    cache = load_cluster_cache()
    rigs = []
    for r in state["rigs"]:
        if r.get("id") == state["self_id"]:
            # Self stats are always served fresh
            try:
                entry = {"stats": collect_stats_payload(), "fetched_at": int(time.time()),
                         "online": True, "error": ""}
            except Exception:
                entry = cache.get("rigs", {}).get(state["self_id"], {})
        else:
            entry = cache.get("rigs", {}).get(r["id"], {})
        rigs.append(_rig_view(r, state, {"rigs": {r["id"]: entry}}))
    return jsonify({
        "success": True,
        "cluster_name": state.get("cluster_name", ""),
        "self_id": state["self_id"],
        "sync_interval": state.get("sync_interval", DEFAULT_SYNC_INTERVAL),
        "last_sync": _cluster_last_sync["ts"],
        "last_sync_ok": _cluster_last_sync["ok"],
        "last_sync_message": _cluster_last_sync["message"],
        "sshpass_available": bool(shutil.which("sshpass")),
        "rigs": rigs
    })

@app.route('/api/cluster/settings', methods=['POST'])
def api_cluster_settings():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    if "cluster_name" in data:
        cname = str(data.get("cluster_name", "")).strip()
        if len(cname) > 60 or not re.match(r'^[A-Za-z0-9_\-\s]*$', cname):
            return jsonify({"success": False, "message": "Invalid cluster name."}), 400
        state["cluster_name"] = cname
    if "sync_interval" in data:
        try:
            interval = int(data.get("sync_interval"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Sync interval must be an integer."}), 400
        if not (15 <= interval <= 3600):
            return jsonify({"success": False, "message": "Sync interval must be between 15 and 3600 seconds."}), 400
        state["sync_interval"] = interval
    if save_cluster_state(state):
        return jsonify({"success": True, "message": "Cluster settings saved."})
    return jsonify({"success": False, "message": "Failed to save cluster settings."}), 500

@app.route('/api/cluster/rig', methods=['POST'])
def api_cluster_rig_save():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]

    rig_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    password = str(data.get("password", ""))
    host_label = str(data.get("host_label", "")).strip()

    if not name or len(name) > 60 or not re.match(r'^[A-Za-z0-9_\-\s\.]+$', name):
        return jsonify({"success": False, "message": "Invalid rig name (1-60 chars, letters/digits/space/-_.)."}), 400
    if password and len(password) > 128:
        return jsonify({"success": False, "message": "Dashboard password is too long."}), 400
    if host_label and len(host_label) > 80:
        return jsonify({"success": False, "message": "Host label is too long."}), 400

    now = int(time.time())
    existing = None
    if rig_id:
        existing = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
        if existing is None:
            return jsonify({"success": False, "message": "Rig not found."}), 404

    if existing is None:
        if not password:
            return jsonify({"success": False, "message": "Dashboard password of the remote rig is required."}), 400
        rig = {
            "id": uuid.uuid4().hex,
            "name": name,
            "host_label": host_label,
            "is_self": False,
            "password": password,
            "accesses": [],
            "updated_at": now,
            "added_at": now
        }
        state["rigs"].append(rig)
        action = "added"
    else:
        is_self = existing.get("id") == state["self_id"]
        if is_self:
            existing["name"] = name
            existing["host_label"] = host_label or existing.get("host_label", "")
        else:
            existing["name"] = name
            existing["host_label"] = host_label
            if password:
                existing["password"] = password
        existing["updated_at"] = now
        action = "updated"

    if save_cluster_state(state):
        logging.info(f"Cluster rig '{name}' {action} by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": f"Rig '{name}' {action} successfully!", "rig_id": existing["id"] if existing else rig["id"]})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/rig/delete', methods=['POST'])
def api_cluster_rig_delete():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig_id = str(data.get("id", "")).strip()
    if rig_id == state["self_id"]:
        return jsonify({"success": False, "message": "Cannot remove the local rig from the cluster."}), 400
    before = len(state["rigs"])
    state["rigs"] = [r for r in state["rigs"] if r.get("id") != rig_id]
    if len(state["rigs"]) == before:
        return jsonify({"success": False, "message": "Rig not found."}), 404
    if save_cluster_state(state):
        return jsonify({"success": True, "message": "Rig removed from the cluster."})
    return jsonify({"success": False, "message": "Failed to update cluster configuration."}), 500

@app.route('/api/cluster/access', methods=['POST'])
def api_cluster_access_save():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig_id = str(data.get("rig_id", "")).strip()
    rig = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found."}), 404

    # Accept masked passwords (UI resubmits unchanged secrets as '********')
    incoming = dict(data.get("access") or {})
    for secret_field in ("password", "jump_password"):
        if incoming.get(secret_field) == "********":
            existing_access = next((a for a in rig.get("accesses", [])
                                    if a.get("id") == str(incoming.get("id", "")).strip()), None)
            if existing_access and existing_access.get(secret_field):
                incoming[secret_field] = existing_access.get(secret_field)
            else:
                incoming.pop(secret_field, None)

    clean, err = validate_access_payload(incoming)
    if err:
        return jsonify({"success": False, "message": err}), 400

    accesses = [a for a in rig.get("accesses", []) if a.get("id") != clean["id"]]
    accesses.append(clean)
    rig["accesses"] = accesses
    rig["updated_at"] = int(time.time())

    if save_cluster_state(state):
        logging.info(f"SSH access '{clean['name']}' saved for rig '{rig.get('name')}' by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "SSH access saved successfully!", "access_id": clean["id"]})
    return jsonify({"success": False, "message": "Failed to save SSH access."}), 500

@app.route('/api/cluster/access/delete', methods=['POST'])
def api_cluster_access_delete():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig_id = str(data.get("rig_id", "")).strip()
    access_id = str(data.get("access_id", "")).strip()
    rig = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found."}), 404
    before = len(rig.get("accesses", []))
    rig["accesses"] = [a for a in rig.get("accesses", []) if a.get("id") != access_id]
    if len(rig["accesses"]) == before:
        return jsonify({"success": False, "message": "Access not found."}), 404
    rig["updated_at"] = int(time.time())
    if save_cluster_state(state):
        return jsonify({"success": True, "message": "SSH access removed."})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/access/test', methods=['POST'])
def api_cluster_access_test():
    """Test an SSH access (unsaved payload allowed) by running hostname over SSH."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    incoming = dict(data.get("access") or {})
    # When testing a saved access by id, load it (keeps masked secrets usable)
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    access_id = str(incoming.get("id", "")).strip()
    if incoming.get("password") == "********" or incoming.get("jump_password") == "********" or access_id:
        rig_id = str(data.get("rig_id", "")).strip()
        rig = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
        saved = next((a for a in (rig.get("accesses", []) if rig else []) if a.get("id") == access_id), None)
        if saved:
            for secret_field in ("password", "jump_password"):
                if incoming.get(secret_field) in ("********", None, ""):
                    incoming[secret_field] = saved.get(secret_field, "")
    clean, err = validate_access_payload(incoming)
    if err:
        return jsonify({"success": False, "message": err}), 400

    ok, out, ssh_err = run_ssh_command(clean, "hostname && echo __OK__", timeout=30)
    if ok and "__OK__" in out:
        hostname = out.replace("__OK__", "").strip().splitlines()
        hostname = hostname[0].strip() if hostname else "unknown"
        return jsonify({"success": True,
                        "message": "SSH connection OK. Remote host: %s" % hostname,
                        "hostname": hostname})
    return jsonify({"success": False, "message": "SSH test failed: %s" % (ssh_err or "unknown error")})

@app.route('/api/cluster/rig/test', methods=['POST'])
def api_cluster_rig_test():
    """Test all SSH accesses of a saved rig in order, returning per-access results."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig = next((r for r in state["rigs"] if r.get("id") == str(data.get("id", "")).strip()), None)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found."}), 404

    results = []
    for access in rig.get("accesses", []):
        ok, out, ssh_err = run_ssh_command(access, "hostname && echo __OK__", timeout=30)
        results.append({
            "id": access.get("id"),
            "name": access.get("name"),
            "ok": bool(ok and "__OK__" in out),
            "detail": ("hostname: %s" % out.replace("__OK__", "").strip().splitlines()[0].strip()
                       if ok and "__OK__" in out else (ssh_err or "unknown error"))
        })
    # Also verify the remote dashboard API with the stored password when SSH works
    api_ok, _, _, api_err, api_access = cluster_remote_api(rig, "GET", "api/cluster/state", timeout=40)
    return jsonify({"success": True, "results": results,
                    "api_ok": api_ok,
                    "api_detail": ("Dashboard API reachable via '%s'" % api_access) if api_ok
                                  else ("Dashboard API check failed: %s" % api_err)})

@app.route('/api/cluster/sync/now', methods=['POST'])
def api_cluster_sync_now():
    def _run():
        try:
            run_sync_cycle(triggered_by="manual")
        except Exception as e:
            logging.error(f"Manual cluster sync failed: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"success": True, "message": "Cluster sync started..."})

# Peer exchange endpoints (bearer or session authenticated)
@app.route('/api/cluster/state', methods=['GET'])
def api_cluster_state():
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    return jsonify({
        "success": True,
        "cluster_name": state.get("cluster_name", ""),
        "self_id": state["self_id"],
        "sync_interval": state.get("sync_interval", DEFAULT_SYNC_INTERVAL),
        "rigs": state["rigs"]
    })

@app.route('/api/cluster/sync', methods=['POST'])
def api_cluster_sync_push():
    data = request.get_json()
    if not data or not isinstance(data.get("rigs"), list):
        return jsonify({"success": False, "message": "Invalid sync payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    state["rigs"] = merge_rig_lists(state["rigs"], data["rigs"])
    if data.get("cluster_name") and not state.get("cluster_name"):
        state["cluster_name"] = str(data["cluster_name"])
    saved = save_cluster_state(state)
    return jsonify({"success": bool(saved), "message": "Cluster state merged." if saved else "Failed to persist merged state."})

# ---------------- Remote rig proxy (full dashboard over SSH) ----------------

@app.route('/api/remote/<rig_id>/<path:subpath>', methods=['GET', 'POST'])
def api_remote_proxy(rig_id, subpath):
    if not subpath.startswith("api/"):
        return jsonify({"success": False, "message": "Invalid remote path."}), 404
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig = next((r for r in state["rigs"]
                if r.get("id") == rig_id and r["id"] != state["self_id"]), None)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found in cluster."}), 404

    body = request.get_data(cache=True) if request.method == "POST" else None
    ok, data, http_code, err, access_name = cluster_remote_api(rig, request.method, subpath, body=body)
    if not ok:
        logging.warning(f"Remote proxy to rig '{rig.get('name')}' failed: {err}")
        return jsonify({"success": False,
                        "message": "Cannot reach rig '%s' (%s)" % (rig.get("name", rig_id), err)}), 502
    resp = Response(json.dumps(data if data is not None else {}),
                    status=http_code if http_code >= 400 else 200,
                    mimetype='application/json')
    resp.headers['X-Remote-Rig'] = rig.get("name", rig_id)
    return resp

@app.route('/')
def dashboard():
    return render_template('index.html', app_version=VERSION)

if __name__ == '__main__':
    # 2. Strict Platform Locks
    if not IS_LINUX:
        print("\n" + "="*60)
        print("[-] CRITICAL ERROR: THIS DASHBOARD MUST RUN ON LINUX HOSTS.")
        print("    HiveOS requires direct integration with sysfs and native CLI tools.")
        print("="*60 + "\n")
        os._exit(1)
        
    if not os.path.exists(HIVE_CONFIG_DIR):
        print("\n" + "="*60)
        print("[-] CRITICAL ERROR: HIVEOS CONFIG DIRECTORY NOT DETECTED (/hive-config/).")
        print("    This software runs exclusively on live standard HiveOS rig nodes.")
        print("="*60 + "\n")
        os._exit(1)

    # Initialize presets directory
    os.makedirs(PRESETS_DIR, exist_ok=True)
    
    # Start the background cluster synchronization worker
    start_cluster_worker()
    
    local_ip = get_local_ip()
    port = 1337
    
    try:
        from waitress import serve
        USE_WAITRESS = True
    except ImportError:
        USE_WAITRESS = False

    print("\n" + "="*60)
    print("      HIVEOS LOCAL GPU DASHBOARD (PORT 1337)")
    print("="*60)
    print(" -> STATUS: Running in PRODUCTION MODE (HiveOS host verified)")
    print(" -> CONFIGS: Reading/Writing from /hive-config/")
    print(f" -> ACCESS PASSWORD: {app.config['ACCESS_PASSWORD']}")
    print(f" -> DASHBOARD ADDRESS: http://{local_ip}:{port}")
    print("="*60 + "\n")
    
    if USE_WAITRESS:
        logging.info(f"Starting Waitress production WSGI server on http://{local_ip}:{port}")
        serve(app, host='0.0.0.0', port=port, threads=4)
    else:
        logging.warning("Waitress package not found. Falling back to Flask built-in development server.")
        app.run(host='0.0.0.0', port=port, debug=False)
