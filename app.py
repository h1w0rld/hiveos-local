import os
import re
import json
import glob
import hmac
import uuid
import signal
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
from flask import Flask, jsonify, request, render_template, session, Response, has_request_context

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

# Thread safety lock for files access.
# RLock (reentrant): preset save/apply handlers legitimately nest config file
# access inside an already-held config_lock - a plain Lock deadlocks there.
config_lock = threading.RLock()

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
        "rigs": [],
        "removed": [],
        "jump_hosts": [],
        "clusters": []
    }
    try:
        if os.path.exists(CLUSTER_CONF):
            with open(CLUSTER_CONF, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in ("cluster_name", "self_id", "sync_interval", "rigs", "removed", "jump_hosts", "clusters"):
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
    # Deletion tombstones: entries removed on any node, kept so deletes propagate
    state["removed"] = [t for t in state.get("removed", [])
                        if isinstance(t, dict) and t.get("id")]
    # Shared jump server library (referenced by SSH accesses via jump_id)
    state["jump_hosts"] = [j for j in state.get("jump_hosts", [])
                           if isinstance(j, dict) and j.get("id")]
    # Named clusters: groups of rig ids ({id, name, rig_ids, updated_at})
    state["clusters"] = [c for c in state.get("clusters", [])
                         if isinstance(c, dict) and c.get("id")]
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

JUMP_ENTRY_FIELDS = ["id", "name", "host", "port", "user", "auth", "password", "key_path", "updated_at"]

def clean_jump_entry(entry):
    return {k: entry[k] for k in JUMP_ENTRY_FIELDS if k in entry}

def merge_jump_hosts(base_jumps, incoming_jumps):
    """Merge the jump server library by id; the entry with the newer updated_at wins."""
    by_id = {}
    for j in base_jumps or []:
        if isinstance(j, dict) and j.get("id"):
            by_id[j["id"]] = j
    for inc in incoming_jumps or []:
        if not isinstance(inc, dict) or not inc.get("id"):
            continue
        clean = clean_jump_entry(inc)
        existing = by_id.get(clean["id"])
        if existing is None:
            clean.setdefault("updated_at", 0)
            by_id[clean["id"]] = clean
        else:
            try:
                inc_ts = int(clean.get("updated_at") or 0)
                cur_ts = int(existing.get("updated_at") or 0)
            except (TypeError, ValueError):
                inc_ts, cur_ts = 0, 0
            if inc_ts > cur_ts:
                by_id[clean["id"]] = clean
    return list(by_id.values())

CLUSTER_ENTRY_FIELDS = ["id", "name", "rig_ids", "updated_at"]

def clean_cluster_entry(entry):
    clean = {k: entry[k] for k in CLUSTER_ENTRY_FIELDS if k in entry}
    clean["rig_ids"] = [str(x) for x in (clean.get("rig_ids") or []) if isinstance(x, (str, int))]
    return clean

def merge_clusters(base_clusters, incoming_clusters, removed):
    """Merge named clusters by id; the entry with the newer updated_at wins.
    Cluster tombstones (removed entries with type 'cluster') suppress deletions."""
    removed_ids = {t.get("id") for t in (removed or []) if t.get("type") == "cluster"}
    by_id = {}
    for c in base_clusters or []:
        if isinstance(c, dict) and c.get("id"):
            by_id[c["id"]] = c
    for inc in incoming_clusters or []:
        if not isinstance(inc, dict) or not inc.get("id"):
            continue
        clean = clean_cluster_entry(inc)
        clean.setdefault("updated_at", 0)
        existing = by_id.get(clean["id"])
        if existing is None:
            if clean["id"] in removed_ids:
                continue
            by_id[clean["id"]] = clean
        else:
            try:
                inc_ts = int(clean.get("updated_at") or 0)
                cur_ts = int(existing.get("updated_at") or 0)
            except (TypeError, ValueError):
                inc_ts = cur_ts = 0
            if inc_ts > cur_ts:
                by_id[clean["id"]] = clean
    return [c for cid, c in by_id.items() if cid not in removed_ids]

def resolve_jump_host(access):
    """Resolve an access' jump reference (jump_id) against the shared jump server
    library; falls back to inline jump_* fields stored on the access itself."""
    jump_id = str(access.get("jump_id") or "").strip()
    if jump_id:
        try:
            for j in load_cluster_state().get("jump_hosts", []):
                if j.get("id") == jump_id:
                    resolved = dict(access)
                    resolved["jump_host"] = j.get("host")
                    resolved["jump_port"] = j.get("port", 22)
                    resolved["jump_user"] = j.get("user")
                    resolved["jump_auth"] = j.get("auth", "password")
                    resolved["jump_password"] = j.get("password", "")
                    resolved["jump_key_path"] = j.get("key_path", "")
                    return resolved
        except Exception as e:
            logging.error(f"Failed to resolve jump host '{jump_id}': {e}")
    return access

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

# Deletion tombstones older than this are dropped (a peer offline longer than
# TTL may resurrect a deleted rig and must be cleaned up manually)
TOMBSTONE_TTL = 30 * 24 * 3600

def _norm_tombstone(t):
    """Normalize a tombstone entry; returns None for invalid payloads."""
    if not isinstance(t, dict) or not t.get("id"):
        return None
    try:
        ts = int(t.get("updated_at") or 0)
    except (TypeError, ValueError):
        ts = 0
    norm = {"id": str(t["id"]), "updated_at": ts}
    # Optional entity type: 'rig' (default) or 'cluster'
    norm["type"] = str(t.get("type") or "rig")
    return norm

def _merge_tombstones(base_removed, incoming_removed):
    by_id = {}
    for t in list(base_removed or []) + list(incoming_removed or []):
        norm = _norm_tombstone(t)
        if norm is None:
            continue
        cur = by_id.get(norm["id"])
        if cur is None or norm["updated_at"] >= cur["updated_at"]:
            by_id[norm["id"]] = norm
    return list(by_id.values())

def _prune_tombstones(removed, now=None):
    now = int(now or time.time())
    kept = []
    for t in removed:
        if now - int(t.get("updated_at") or 0) < TOMBSTONE_TTL:
            kept.append(t)
        else:
            logging.info(f"Cluster merge: expired deletion tombstone for rig {t.get('id')}")
    return kept

def merge_rig_lists(base_rigs, incoming_rigs, base_removed=None, incoming_removed=None):
    """Merge rig entries by id; the entry with the newer updated_at wins.

    Deletion tombstones (from `removed` lists exchanged by peers) suppress rig
    entries deleted on any node, so removals propagate instead of resurrecting.
    Returns (rigs, removed)."""
    removed = _merge_tombstones(base_removed, incoming_removed)
    tomb = {t["id"]: t["updated_at"] for t in removed if t.get("type", "rig") == "rig"}

    def _ts(entry):
        try:
            return int(entry.get("updated_at") or 0)
        except (TypeError, ValueError):
            return 0

    by_id = {}
    for r in base_rigs:
        if isinstance(r, dict) and r.get("id"):
            by_id[r["id"]] = r
    for inc in incoming_rigs or []:
        if not isinstance(inc, dict) or not inc.get("id"):
            continue
        clean = clean_rig_entry(inc)
        rid = clean["id"]
        if rid in tomb and _ts(clean) <= tomb[rid]:
            # This entry was deleted on a peer after its last edit
            if rid in by_id:
                logging.info(f"Cluster merge: rig '{by_id[rid].get('name', rid)}' removed by cluster deletion")
                del by_id[rid]
            continue
        existing = by_id.get(rid)
        if existing is None:
            clean.setdefault("updated_at", 0)
            clean.setdefault("added_at", int(time.time()))
            by_id[rid] = clean
            logging.info(f"Cluster merge: discovered new rig '{clean.get('name', rid)}'")
        else:
            if _ts(clean) > _ts(existing):
                # Preserve the locally-resolved is_self flag; adopt newer remote edits
                clean["is_self"] = existing.get("is_self", False)
                by_id[rid] = clean
    # Locally known rigs deleted on a peer disappear as well
    for rid in list(by_id):
        if rid in tomb and _ts(by_id[rid]) <= tomb[rid]:
            logging.info(f"Cluster merge: rig '{by_id[rid].get('name', rid)}' removed by cluster deletion")
            del by_id[rid]
        elif rid in tomb:
            # Rig was edited/re-added after the deletion - the tombstone loses
            removed = [t for t in removed if not (t["id"] == rid and t.get("type", "rig") == "rig")]
    return list(by_id.values()), _prune_tombstones(removed)

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
        jid = str(access.get("jump_id", "")).strip()
        if jid:
            clean["jump_id"] = jid
        jhost = str(access.get("jump_host", "")).strip()
        if jhost:
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
        elif not jid:
            return None, "Jump access requires a jump server (pick one from the library)."
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

def run_ssh_command(access, remote_cmd, timeout=35, stdin_data=None):
    """Execute a command on a remote rig over SSH. Returns (ok, output, error).

    Uses a dedicated process group so a timed-out ssh can be killed together with
    its children (ProxyCommand jump processes) - otherwise they keep the pipes
    open and a naive communicate() drain would block the calling thread forever.
    """
    tmp_files = []
    proc = None
    try:
        args, err = build_ssh_command(resolve_jump_host(access), remote_cmd, tmp_files)
        if err:
            return False, "", err
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE, start_new_session=True)
        try:
            out_bytes, err_bytes = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                out_bytes, err_bytes = proc.communicate(timeout=5)
            except Exception:
                out_bytes, err_bytes = b"", b""
            return False, "", "SSH connection timed out"
        out = (out_bytes or b"").decode(errors="ignore")
        errout = (err_bytes or b"").decode(errors="ignore")
        if proc.returncode != 0:
            msg = (errout or out).strip()
            detail = msg.splitlines()[-1] if msg else "exit code %d" % proc.returncode
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
    if body is None:
        raw = None
    elif isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
    else:
        raw = json.dumps(body).encode("utf-8")
    curl_cmd = build_curl_command(method.upper(), path, raw is not None, password)
    last_error = "No SSH accesses configured for this rig"
    for access in rig.get("accesses", []):
        access_name = access.get("name") or access.get("id", "?")
        ok, out, ssh_err = run_ssh_command(access, curl_cmd, timeout=timeout, stdin_data=raw)
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
    total_mh, per_gpu, algo = get_miner_hashrate()
    for g in hw["gpus"]:
        g["hashrate"] = round(per_gpu.get(g["index"], 0.0), 2)
    return {
        "system": get_system_stats(),
        "gpus": hw["gpus"],
        "igpus": hw["igpus"],
        "total_hashrate_mh": round(total_mh, 2),
        "miner_algo": algo,
        "overclocks": get_overclocks_formatted(),
        # csrf_token only makes sense inside a request context (worker threads have none)
        "csrf_token": session.get('csrf_token', '') if has_request_context() else ''
    }

_cluster_sync_lock = threading.Lock()
_cluster_last_sync = {"ts": 0, "ok": True, "message": "Not synced yet"}

def run_sync_cycle(triggered_by="auto"):
    """One cluster sync pass: exchange rig lists with every peer and refresh stats cache."""
    if not _cluster_sync_lock.acquire(blocking=False):
        return False, "Another sync cycle is already running"
    try:
        state = load_cluster_state()
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

            # 1. Pull the peer's cluster state and merge (learns about new rigs and deletions)
            ok, data, _, err, _ = cluster_remote_api(rig, "GET", "api/cluster/state", timeout=45)
            if ok and isinstance(data, dict) and isinstance(data.get("rigs"), list):
                state["rigs"], state["removed"] = merge_rig_lists(
                    state["rigs"], data["rigs"],
                    base_removed=state.get("removed"), incoming_removed=data.get("removed"))
                state["jump_hosts"] = merge_jump_hosts(
                    state.get("jump_hosts", []), data.get("jump_hosts"))
                state["clusters"] = merge_clusters(
                    state.get("clusters", []), data.get("clusters", []), state.get("removed", []))
                if data.get("cluster_name") and not state.get("cluster_name"):
                    state["cluster_name"] = data["cluster_name"]
                entry["online"] = True
                entry["error"] = ""
            else:
                entry["online"] = False
                entry["error"] = err

            # 2. Push our merged state back to the peer (propagates new rigs/passwords/deletions)
            push_ok, _, _, push_err, _ = cluster_remote_api(
                rig, "POST", "api/cluster/sync",
                body={"cluster_name": state.get("cluster_name", ""),
                      "rigs": state["rigs"], "removed": state.get("removed", []),
                      "jump_hosts": state.get("jump_hosts", []),
                      "clusters": state.get("clusters", []),
                      "from_id": state["self_id"]},
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
        # Stay silent when everything is fine; surface only problems
        _cluster_last_sync["message"] = ("Offline: " + ", ".join(str(x) for x in offline)) if offline else ""
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
                interval = max(5, int(state.get("sync_interval", DEFAULT_SYNC_INTERVAL)))
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
                       "jump_id", "jump_host", "jump_port", "jump_user", "jump_auth", "jump_password", "jump_key_path"]

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
                    # HiveOS writes META-like JSON values in single quotes; nested
                    # double quotes would corrupt the shell-style file for hive's parser
                    if isinstance(v, str) and '"' in v:
                        f.write(f"{k}='{v}'\n")
                    else:
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
        stdout, stderr, code = run_command("nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,utilization.gpu,clocks.current.graphics,clocks.current.memory,power.limit,pci.bus_id,memory.total --format=csv,noheader,nounits")
        if code == 0 and stdout:
            lines = stdout.strip().split('\n')
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 9:
                    idx = safe_int(parts[0], 0)
                    bus_id = parts[9] if len(parts) > 9 else ""
                    vram_mb = safe_int(parts[10]) if len(parts) > 10 else 0
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
                        "hashrate": 0.0,
                        "bus_id": _short_pci_bus(bus_id),
                        "vram_mb": vram_mb,
                        "subvendor": _pci_subvendor(bus_id)
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
_HASHRATE_UNITS = {"H": 1e-6, "KH": 1e-3, "MH": 1.0, "GH": 1e3, "TH": 1e6, "PH": 1e9}

_PCI_SUBVENDOR_NAMES = {
    "0x1462": "MSI", "0x1043": "ASUS", "0x1458": "GIGABYTE", "0x3842": "EVGA",
    "0x19da": "ZOTAC", "0x196e": "PNY", "0x10de": "NVIDIA", "0x1566": "Palit",
    "0x14f7": "Gainward", "0x18c4": "KFA2", "0x1616": "Inno3D",
    "0x7377": "Colorful", "0x148c": "PC Partner",
}

def _short_pci_bus(bus_id):
    """00000000:01:00.0 -> 01:00.0 (HiveOS-style bus label)."""
    m = re.match(r'^(?:[0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9])$', str(bus_id or "").strip())
    return m.group(1) if m else ""

def _pci_subvendor(bus_id):
    """Read the board vendor name from PCI sysfs (falls back to raw id)."""
    short = _short_pci_bus(bus_id)
    if not short:
        return ""
    path = f"/sys/bus/pci/devices/0000:{short}/subsystem_vendor"
    try:
        with open(path) as f:
            vid = f.read().strip().lower()
        return _PCI_SUBVENDOR_NAMES.get(vid, vid)
    except OSError:
        return ""

def _parse_hashrate_str(text):
    match = re.search(r'([0-9.]+)\s*(PH|TH|GH|MH|KH|H)/s', str(text))
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
    """Supports srbminer / rigel / t-rex / gminer local JSON stats formats.

    Returns (total_mh, per_gpu dict, algo_name)."""
    total = 0.0
    per_gpu = {}
    algo = ""
    if not isinstance(data, dict):
        return total, per_gpu, algo

    # SRBMiner-Multi: {"algorithms":[{"name":"pearlhash","hashrate":{"1min":H/s,
    #   "gpu":{"gpu0":H/s,...,"total":H/s}}}], "gpu_devices":[{"id":0,...}]}
    algorithms = data.get("algorithms")
    if isinstance(algorithms, list) and algorithms:
        totals, gpu_sums = [], {}
        for a in algorithms:
            if not isinstance(a, dict):
                continue
            hr = a.get("hashrate")
            if not isinstance(hr, dict):
                continue
            gpu_block = hr.get("gpu") if isinstance(hr.get("gpu"), dict) else {}
            algo_total = _to_mh(gpu_block.get("total")) or _to_mh(hr.get("1min"))
            totals.append(algo_total)
            if not algo:
                algo = str(a.get("name") or "")
            for key, val in gpu_block.items():
                m = re.match(r'gpu(\d+)$', str(key))
                if m:
                    idx = int(m.group(1))
                    gpu_sums[idx] = gpu_sums.get(idx, 0.0) + _to_mh(val)
        total = sum(totals)
        per_gpu = gpu_sums
        return total, per_gpu, algo

    # rigel: {"name":"Rigel","hashrate":{"algo":H/s},"devices":[{"id":0,"hashrate":{"algo":H/s}}]}
    devices = data.get("devices")
    if isinstance(devices, list) and devices and isinstance(data.get("hashrate"), dict):
        total = max((_to_mh(v) for v in data["hashrate"].values()), default=0.0)
        algo = next(iter(data["hashrate"]), "")
        for g in devices:
            if isinstance(g, dict):
                idx = safe_int(g.get("id", -1), -1)
                hr = g.get("hashrate")
                if idx >= 0 and isinstance(hr, dict) and hr:
                    per_gpu[idx] = max((_to_mh(v) for v in hr.values()), default=0.0)
        return total, per_gpu, algo

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
        return total, per_gpu, algo

    # t-rex: {"hashrate": H/s, "gpus":[{"gpu_id":0,"hashrate":H/s}]}
    if "hashrate" in data and "gpus" in data:
        total = _to_mh(data.get("hashrate"))
        for g in data.get("gpus") or []:
            if isinstance(g, dict):
                idx = safe_int(g.get("gpu_id", g.get("id", -1)), -1)
                if idx >= 0:
                    per_gpu[idx] = _to_mh(g.get("hashrate"))
        return total, per_gpu, algo

    # gminer: {"miner":{"total_speed":["44.5 MH"]},"per_device":["11.1 MH",...]}
    miner_block = data.get("miner")
    if isinstance(miner_block, dict):
        ts = miner_block.get("total_speed") or []
        if isinstance(ts, list) and ts:
            total = _parse_hashrate_str(ts[0])
        for idx, val in enumerate(data.get("per_device") or []):
            per_gpu[idx] = _parse_hashrate_str(val)
    return total, per_gpu, algo

def is_miner_screen_running():
    """True when a HiveOS miner screen session (N.miner) is alive."""
    _, _, code = run_command("screen -ls 2>/dev/null | grep -qE '[0-9]+\\.miner'")
    return code == 0

def get_miner_hashrate():
    """Returns (total_mh, per_gpu dict, algo) from local miner stats API, log fallback."""
    total_mh = 0.0
    per_gpu = {}
    algo = ""

    # 1. Miner HTTP stats APIs (rigel 5000, t-rex 4067, gminer/xmrig 4068,
    #    lolminer 4028, srbminer 21373/21473)
    for port in (5000, 4067, 4068, 4028, 21373, 21473):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "hiveos-local"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode(errors="ignore"))
            total_mh, per_gpu, algo = _extract_api_hashrate(data)
            if total_mh > 0 or per_gpu:
                return total_mh, per_gpu, algo
        except Exception:
            continue

    # 2. Fallback: parse rigel-style miner log. Only valid while the miner screen is alive,
    #    otherwise stale log entries keep reporting hashrate after the miner stops.
    if not is_miner_screen_running():
        return 0.0, {}, algo

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
                    if not algo:
                        a = re.search(r'\[([a-z0-9_\-]+)\]', line)
                        if a:
                            algo = a.group(1)
                    # rigel: "|  Total: 245.8 MH/s|..." or legacy "Total speed: 245.8 MH/s"
                    m = re.search(r'Total(?: speed)?:\s*([0-9.]+)\s*(PH|TH|GH|MH|KH|H)/s', line)
                    if m and total_mh <= 0:
                        total_mh = float(m.group(1)) * _HASHRATE_UNITS[m.group(2)]
                    # rigel table row: "|6|RTX 3070 Laptop GPU|30.43 MH/s|22.50 MH/s|..."
                    g = re.search(r'\|\s*(\d+)\s*\|[^|]*\|\s*([0-9.]+)\s*(PH|TH|GH|MH|KH|H)/s', line)
                    if g:
                        idx = int(g.group(1))
                        if idx not in per_gpu:
                            per_gpu[idx] = float(g.group(2)) * _HASHRATE_UNITS[g.group(3)]
                    # legacy plain: "GPU0: 55.00 MH/s"
                    g2 = re.search(r'GPU(\d+):\s*([0-9.]+)\s*(PH|TH|GH|MH|KH|H)/s', line)
                    if g2:
                        idx = int(g2.group(1))
                        if idx not in per_gpu:
                            per_gpu[idx] = float(g2.group(2)) * _HASHRATE_UNITS[g2.group(3)]
                if total_mh > 0 or per_gpu:
                    break
            except Exception:
                continue

    return total_mh, per_gpu, algo

# Read configs formatted
def get_overclocks_formatted():
    nv_data = parse_shell_config(NVIDIA_OC_CONF)
    amd_data = parse_shell_config(AMD_OC_CONF)
    
    return {
        "nvidia": {
            # HiveOS nvidia-oc.conf keys: CLOCK (offset) / LCLOCK (locked),
            # MEM (offset) / LMEM (locked), PLIMIT, FAN
            "core": nv_data.get("CLOCK", nv_data.get("CORE", "")).split(),
            "mem": nv_data.get("MEM", "").split(),
            "pl": nv_data.get("PLIMIT", nv_data.get("PL", "")).split(),
            "fan": nv_data.get("FAN", "").split(),
            "lcore": nv_data.get("LCLOCK", "").split(),
            "lmem": nv_data.get("LMEM", "").split()
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

    # Optionally apply the same password to every remote rig in the cluster,
    # so the whole farm keeps one shared dashboard password
    results = []
    if data.get("apply_to_cluster"):
        state = load_cluster_state()
        _CURRENT_SELF_ID["value"] = state["self_id"]
        state_changed = False
        ok_count = 0
        for rig in state["rigs"]:
            if rig.get("id") == state["self_id"]:
                continue
            ok, _, _, err, _ = cluster_remote_api(
                rig, "POST", "api/auth/password",
                body={"current_password": rig.get("password", ""), "new_password": new_password},
                timeout=45)
            results.append({"rig": rig.get("name", rig.get("id", "?")), "ok": bool(ok),
                            "error": "" if ok else err})
            if ok:
                # Store the new password and bump updated_at so peers sync it
                rig["password"] = new_password
                rig["updated_at"] = int(time.time())
                state_changed = True
                ok_count += 1
        if state_changed:
            save_cluster_state(state)
        failed = [r["rig"] for r in results if not r["ok"]]
        if failed:
            logging.warning(f"Cluster password change failed on rigs: {', '.join(failed)}")
        else:
            logging.info(f"Cluster password change applied to {ok_count} remote rig(s)")

    logging.info(f"Dashboard access password changed by IP: {request.remote_addr}")
    message = "Access password updated successfully!"
    if data.get("apply_to_cluster") and results:
        ok_n = sum(1 for r in results if r["ok"])
        failed = [r["rig"] for r in results if not r["ok"]]
        if failed:
            message = f"Password updated locally and on {ok_n}/{len(results)} remote rig(s). Failed: {', '.join(failed)}"
        else:
            message = f"Password updated on the local rig and all {ok_n} remote rig(s)!"
    return jsonify({"success": True, "message": message, "results": results})

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
        
        # Real HiveOS nvidia-oc.conf keys (the nvidia-oc script reads CLOCK/PLIMIT,
        # not the legacy CORE/PL our older versions used to write)
        clock = (config.get("CLOCK") if "CLOCK" in config else config.get("CORE", "")).split()
        mem = config.get("MEM", "").split()
        plimit = (config.get("PLIMIT") if "PLIMIT" in config else config.get("PL", "")).split()
        fan = config.get("FAN", "").split()
        lclock = config.get("LCLOCK", "").split()
        lmem = config.get("LMEM", "").split()
        
        max_idx = max(3, gpu_index)
        clock += ["0"] * (max_idx + 1 - len(clock))
        mem += ["0"] * (max_idx + 1 - len(mem))
        plimit += ["0"] * (max_idx + 1 - len(plimit))
        fan += ["0"] * (max_idx + 1 - len(fan))
        lclock += [""] * (max_idx + 1 - len(lclock))
        lmem += [""] * (max_idx + 1 - len(lmem))
        
        if "core" in data:
            clock[gpu_index] = str(data["core"])
        if "mem" in data:
            mem[gpu_index] = str(data["mem"])
        if "pl" in data:
            plimit[gpu_index] = str(data["pl"])
        if "fan" in data:
            fan[gpu_index] = str(data["fan"])
        # Optional locked clocks (absolute values, HiveOS-style LCLOCK/LMEM)
        if "lcore" in data:
            lclock[gpu_index] = str(data["lcore"]).strip()
        if "lmem" in data:
            lmem[gpu_index] = str(data["lmem"]).strip()
            
        config.pop("CORE", None)  # legacy key written by older versions
        config.pop("PL", None)
        config["CLOCK"] = " ".join(clock)
        config["MEM"] = " ".join(mem)
        config["PLIMIT"] = " ".join(plimit)
        config["FAN"] = " ".join(fan)
        config["LCLOCK"] = " ".join(lclock)
        config["LMEM"] = " ".join(lmem)
        
        write_shell_config(filepath, config)
        logging.info(f"NVIDIA GPU {gpu_index} parameters updated: Clock={data.get('core')}, Mem={data.get('mem')}, PL={data.get('pl')}, Fan={data.get('fan')}, LCLOCK={data.get('lcore')}, LMEM={data.get('lmem')}")
        
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

# 4. AutoFan settings (HiveOS-parity: global settings + per-GPU overrides)
#
# Hive autofan daemon (/hive/sbin/autofan) re-reads autofan.conf every cycle:
#   plain keys            = global defaults (TARGET_TEMP, MIN_FAN, ...)
#   CUSTOM_* keys         = per-GPU lists; a value of 0/empty = use the global default
#   CUSTOM_MODE per GPU   = 0 auto, 1 static (speed from nvidia-oc.conf FAN), 2 hardware
#   CRITICAL_TEMP_ACTION  = "" (stop miner) | "reboot" | "shutdown"
def _af_int_list(conf, key):
    return [v for v in (conf.get(key, "") or "").split() if re.match(r'^-?[0-9]+$', v)]

def _af_pad(vals, n):
    if len(vals) < n:
        vals = vals + [vals[-1] if vals else "0"] * (n - len(vals))
    return vals[:n]

def _af_per_gpu(conf, key, n, zero_is_default=True):
    """Expand a CUSTOM_* scalar/list to a per-GPU list of ints (None = global default)."""
    vals = _af_pad(_af_int_list(conf, key), n)
    out = []
    for v in vals:
        if not re.match(r'^[0-9]+$', v):
            out.append(None)
        elif zero_is_default and int(v) == 0:
            out.append(None)
        else:
            out.append(int(v))
    return out

def _af_set_per_gpu(conf, key, n, gpu_index, value):
    """Set one value (or all when gpu_index is None) in a CUSTOM_* per-GPU list."""
    vals = _af_pad(_af_int_list(conf, key), n)
    if gpu_index is None:
        vals = [str(value)] * n
    else:
        vals[gpu_index] = str(value)
    conf[key] = " ".join(vals)

@app.route('/api/autofan', methods=['GET'])
def handle_autofan():
    conf = parse_shell_config(AUTOFAN_CONF)
    oc = parse_shell_config(NVIDIA_OC_CONF)
    gpus = get_gpu_stats().get("gpus", [])
    n = len(gpus)
    fan_vals = _af_pad(_af_int_list(oc, "FAN"), n or 1)
    mode_vals = _af_pad(_af_int_list(conf, "CUSTOM_MODE"), n or 1)
    pergpu = []
    for i in range(n):
        pergpu.append({
            "index": gpus[i].get("index"),
            "mode": int(mode_vals[i]) if mode_vals[i].isdigit() else 0,
            "static": int(fan_vals[i]) if fan_vals[i].isdigit() else 0,
            "min": _af_per_gpu(conf, "CUSTOM_MIN_FAN", n)[i],
            "max": _af_per_gpu(conf, "CUSTOM_MAX_FAN", n)[i],
            "target_core": _af_per_gpu(conf, "CUSTOM_TARGET_TEMP", n)[i],
            "target_mem": _af_per_gpu(conf, "CUSTOM_TARGET_MEM_TEMP", n)[i],
            "critical": _af_per_gpu(conf, "CUSTOM_CRITICAL_TEMP", n)[i],
        })
    return jsonify({
        "success": True,
        "enabled": conf.get("ENABLED", "0"),
        "target_temp": conf.get("TARGET_TEMP", "60"),
        "target_mem_temp": conf.get("TARGET_MEM_TEMP", "80"),
        "min_fan": conf.get("MIN_FAN", "30"),
        "max_fan": conf.get("MAX_FAN", "100"),
        "critical_temp": conf.get("CRITICAL_TEMP", "85"),
        "critical_action": conf.get("CRITICAL_TEMP_ACTION", ""),
        "smart_mode": conf.get("SMART_MODE", "0"),
        "reboot_on_errors": conf.get("REBOOT_ON_ERROR", "0"),
        "no_amd": conf.get("NO_AMD", "0"),
        "gpus": pergpu
    })

@app.route('/api/autofan/save-all', methods=['POST'])
def autofan_save_all():
    """HiveOS-style single save: global switches + full per-GPU fan table.

    Writes global switches to autofan.conf and the per-GPU columns as CUSTOM_*
    lists (0/None = use the global default). Static speeds go to the FAN list
    of nvidia-oc.conf (the daemon reads them for CUSTOM_MODE=1 rows)."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400

    enabled = str(data.get("enabled", "0")).strip()
    critical_action = str(data.get("critical_action", "")).strip().lower()
    reboot_on_errors = str(data.get("reboot_on_errors", "0")).strip()
    smart_mode = str(data.get("smart_mode", "0")).strip()
    # The worker autofan page has no "Without AMD" switch (like the HiveOS cloud
    # UI); preserve the stored value unless a payload explicitly provides it.
    preserve_no_amd = "no_amd" not in data
    no_amd = str(data.get("no_amd", "0")).strip() if not preserve_no_amd else ""
    if enabled not in ("0", "1") or reboot_on_errors not in ("0", "1") \
            or smart_mode not in ("0", "1"):
        return jsonify({"success": False, "message": "Switch values must be 0 or 1."}), 400
    if not preserve_no_amd and no_amd not in ("0", "1"):
        return jsonify({"success": False, "message": "Switch values must be 0 or 1."}), 400
    if critical_action not in ("", "reboot", "shutdown"):
        return jsonify({"success": False, "message": "Critical action must be empty, 'reboot' or 'shutdown'."}), 400

    gpus_in = data.get("gpus")
    if not isinstance(gpus_in, list) or not gpus_in:
        return jsonify({"success": False, "message": "Per-GPU fan settings are required."}), 400
    if len(gpus_in) > 64:
        return jsonify({"success": False, "message": "Too many GPUs."}), 400

    ranges = {"min": (0, 99), "max": (1, 100), "target_core": (5, 120),
              "target_mem": (10, 120), "critical": (30, 120)}
    parsed = []
    seen = set()
    for g in gpus_in:
        if not isinstance(g, dict):
            return jsonify({"success": False, "message": "Invalid GPU entry."}), 400
        try:
            idx = int(g.get("index"))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "GPU index must be an integer."}), 400
        if not (0 <= idx < 64) or idx in seen:
            return jsonify({"success": False, "message": f"Invalid or duplicate GPU index {idx}."}), 400
        seen.add(idx)
        mode = str(g.get("mode", "auto")).strip().lower()
        if mode not in ("auto", "static"):
            return jsonify({"success": False, "message": f"GPU {idx}: fan mode must be 'auto' or 'static'."}), 400
        static = None
        if mode == "static":
            try:
                static = int(g.get("static"))
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"GPU {idx}: static fan speed is required."}), 400
            if not (1 <= static <= 100):
                return jsonify({"success": False, "message": f"GPU {idx}: static fan speed must be 1-100%."}), 400
        overrides = {}
        for key in ("min", "max", "target_core", "target_mem", "critical"):
            v = g.get(key)
            if v in (None, "", 0, "0"):
                overrides[key] = 0  # 0 = global default in hive semantics
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"GPU {idx}: invalid {key} value."}), 400
            lo, hi = ranges[key]
            if not (lo <= v <= hi):
                return jsonify({"success": False, "message": f"GPU {idx}: {key} must be between {lo} and {hi}."}), 400
            overrides[key] = v
        if mode == "auto" and overrides.get("min") and overrides.get("max") \
                and overrides["min"] > overrides["max"]:
            return jsonify({"success": False, "message": f"GPU {idx}: min fan speed cannot be greater than max."}), 400
        parsed.append({"index": idx, "mode": mode, "static": static, **overrides})

    gpus_live = get_gpu_stats().get("gpus", [])
    live_idx = {g.get("index") for g in gpus_live}
    unknown = [p["index"] for p in parsed if p["index"] not in live_idx]
    if unknown:
        return jsonify({"success": False, "message": f"GPU index(es) not present on this rig: {unknown}"}), 400
    amd_idx = {g.get("index") for g in gpus_live if g.get("brand") != "NVIDIA"}
    bad = [p["index"] for p in parsed if p["index"] in amd_idx]
    if bad:
        return jsonify({"success": False, "message": f"Fan control is only supported for NVIDIA GPUs: {bad}"}), 400

    n = len(gpus_live) or max([p["index"] for p in parsed]) + 1
    conf = parse_shell_config(AUTOFAN_CONF)
    conf["ENABLED"] = enabled
    conf["CRITICAL_TEMP_ACTION"] = critical_action
    conf["REBOOT_ON_ERROR"] = reboot_on_errors
    conf["SMART_MODE"] = smart_mode
    if preserve_no_amd:
        logging.info("AutoFan save: NO_AMD not in payload, preserving existing value")
    else:
        conf["NO_AMD"] = no_amd

    order = {p["index"]: p for p in parsed}
    def gpu_values(key, default):
        return [str((order.get(i, {}).get(key) if order.get(i, {}).get(key) is not None else default)) for i in range(n)]

    conf["CUSTOM_MODE"] = " ".join("1" if order.get(i, {}).get("mode") == "static" else "0" for i in range(n))
    conf["CUSTOM_MIN_FAN"] = " ".join(gpu_values("min", 0))
    conf["CUSTOM_MAX_FAN"] = " ".join(gpu_values("max", 0))
    conf["CUSTOM_TARGET_TEMP"] = " ".join(gpu_values("target_core", 0))
    conf["CUSTOM_TARGET_MEM_TEMP"] = " ".join(gpu_values("target_mem", 0))
    conf["CUSTOM_CRITICAL_TEMP"] = " ".join(gpu_values("critical", 0))
    if not write_shell_config(AUTOFAN_CONF, conf):
        return jsonify({"success": False, "message": "Failed to write autofan.conf"}), 500

    oc = parse_shell_config(NVIDIA_OC_CONF)
    oc["FAN"] = " ".join(str(p["static"] if p["mode"] == "static" else 0) for p in
                         (order.get(i) or {"mode": "auto", "static": 0} for i in range(n)))
    if not write_shell_config(NVIDIA_OC_CONF, oc):
        return jsonify({"success": False, "message": "Failed to write nvidia-oc.conf"}), 500
    run_command("sudo /hive/sbin/nvidia-oc")

    static_cnt = sum(1 for p in parsed if p["mode"] == "static")
    logging.info(f"AutoFan saved: enabled={enabled}, action={critical_action or 'stop'}, "
                 f"smart={smart_mode}, reboot_on_error={reboot_on_errors}, no_amd={no_amd}, "
                 f"static GPUs={static_cnt} by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": "AutoFan settings saved."})

@app.route('/api/autofan/gpu', methods=['POST'])
def autofan_gpu_set():
    """Apply per-GPU fan settings (mode/static/min/max/targets) for one or all GPUs."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    gpu = data.get("gpu", "all")
    mode = str(data.get("mode", "auto")).strip().lower()
    if mode not in ("auto", "static"):
        return jsonify({"success": False, "message": "Fan mode must be 'auto' or 'static'."}), 400
    gpu_index = None
    if gpu != "all":
        try:
            gpu_index = int(gpu)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "GPU index must be an integer or 'all'."}), 400
        if not (0 <= gpu_index < 64):
            return jsonify({"success": False, "message": "GPU index out of acceptable bounds (0-63)."}), 400

    # Optional per-GPU overrides: None/0/empty = use the global default
    fields = {}
    for key in ("min", "max", "target_core", "target_mem", "critical"):
        if key in data and data[key] not in (None, "", 0, "0"):
            try:
                fields[key] = int(data[key])
            except (TypeError, ValueError):
                return jsonify({"success": False, "message": f"Invalid {key} value."}), 400
    ranges = {"min": (0, 99), "max": (1, 100), "target_core": (5, 120),
              "target_mem": (10, 120), "critical": (30, 120)}
    for key, val in fields.items():
        lo, hi = ranges[key]
        if not (lo <= val <= hi):
            return jsonify({"success": False, "message": f"{key} must be between {lo} and {hi}."}), 400
    if "min" in fields and "max" in fields and fields["min"] > fields["max"]:
        return jsonify({"success": False, "message": "Min fan speed cannot be greater than max fan speed."}), 400

    gpus = get_gpu_stats().get("gpus", [])
    n = len(gpus) or 1
    if gpu_index is not None:
        tgt = next((g for g in gpus if g.get("index") == gpu_index), None)
        if tgt and tgt.get("brand") != "NVIDIA":
            return jsonify({"success": False, "message": "Fan control is only supported for NVIDIA GPUs."}), 400
    elif any(g.get("brand") != "NVIDIA" for g in gpus):
        return jsonify({"success": False, "message": "Fan control is only supported for NVIDIA GPUs (this rig has AMD GPUs)."}), 400

    static_speed = None
    if mode == "static":
        try:
            static_speed = int(data.get("static", 0))
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": "Static fan speed must be an integer."}), 400
        if not (1 <= static_speed <= 100):
            return jsonify({"success": False, "message": "Static fan speed must be 1-100%."}), 400

    # 1) autofan.conf: mode + optional per-GPU overrides (daemon re-sources each cycle)
    conf = parse_shell_config(AUTOFAN_CONF)
    _af_set_per_gpu(conf, "CUSTOM_MODE", n, gpu_index, 1 if mode == "static" else 0)
    for key, conf_key in (("min", "CUSTOM_MIN_FAN"), ("max", "CUSTOM_MAX_FAN"),
                          ("target_core", "CUSTOM_TARGET_TEMP"), ("target_mem", "CUSTOM_TARGET_MEM_TEMP"),
                          ("critical", "CUSTOM_CRITICAL_TEMP")):
        if key in fields:
            _af_set_per_gpu(conf, conf_key, n, gpu_index, fields[key])
    if not write_shell_config(AUTOFAN_CONF, conf):
        return jsonify({"success": False, "message": "Failed to write autofan.conf"}), 500

    # 2) nvidia-oc.conf: static speed (FAN list), applied right away.
    #    In static mode the daemon takes the speed from FAN; in auto mode FAN=0
    #    releases the fan to autofan/driver control.
    oc = parse_shell_config(NVIDIA_OC_CONF)
    fan = _af_pad(_af_int_list(oc, "FAN"), n)
    fan = [str(static_speed if mode == "static" else 0)] * n if gpu_index is None else \
          fan[:gpu_index] + [str(static_speed if mode == "static" else 0)] + fan[gpu_index + 1:]
    oc["FAN"] = " ".join(fan)
    if not write_shell_config(NVIDIA_OC_CONF, oc):
        return jsonify({"success": False, "message": "Failed to write nvidia-oc.conf"}), 500
    if static_speed is not None or mode == "auto":
        run_command("sudo /hive/sbin/nvidia-oc")

    label = "all GPUs" if gpu_index is None else f"GPU #{gpu_index}"
    logging.info(f"AutoFan {label} -> {mode}" +
                 (f" {static_speed}%" if static_speed is not None else "") +
                 (f", {fields}" if fields else "") + f" by IP: {request.remote_addr}")
    msg = (f"Fan set to auto on {label}." if mode == "auto" else f"Fan speed set to {static_speed}% on {label}.")
    return jsonify({"success": True, "message": msg})

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

def _apply_flight_sheet(coin, wallet, pool, miner, extra=None):
    """Write COIN/WAL/POOL_URL into wallet.conf and MINER into rig.conf, restart miner.
    For custom miners (miner == 'custom') the HiveOS-style CUSTOM_* block is written
    exactly like HiveOS flight sheets do it."""
    coin = str(coin or "").strip()
    wallet = str(wallet or "").strip()
    pool = str(pool or "").strip()
    miner = str(miner or "none").strip().lower()
    extra = extra or {}

    if not re.match(r'^[A-Za-z0-9_\-\s]+$', coin):
        return False, "Invalid Coin parameter. Use alphanumeric characters only."
    if not re.match(r'^[A-Za-z0-9_\-\s\.\/\@\:]+$', wallet):
        return False, "Invalid Wallet format."
    if not re.match(r'^[a-zA-Z0-9\.\-\:\/]+$', pool):
        return False, "Invalid Pool URL format."

    # Miner ids follow the HiveOS package naming (catalog provides the list);
    # the strict format check blocks injection into rig.conf
    if miner != "none" and not re.match(r'^[a-z0-9_\-]{1,32}$', miner):
        return False, "Unsupported miner program choice."

    # Backup files first
    try:
        if os.path.exists(WALLET_CONF_PATH):
            shutil.copy2(WALLET_CONF_PATH, WALLET_CONF_PATH + ".bak")
        if os.path.exists(RIG_CONF_PATH):
            shutil.copy2(RIG_CONF_PATH, RIG_CONF_PATH + ".bak")
    except Exception as e:
        logging.error(f"Backup configurations failed: {e}")

    if miner == "custom":
        miner_alt = str(extra.get("miner_alt", "")).strip().lower() or "custom_miner"
        install_url = str(extra.get("install_url", "")).strip()
        algo = str(extra.get("algo", "")).strip().lower()
        user_config = str(extra.get("user_config", "")).strip()
        fs_name = str(extra.get("name", "")).strip()
        if miner_alt and not re.match(r'^[a-z0-9_\-]+$', miner_alt):
            return False, "Invalid custom miner package name."
        if install_url and not re.match(r'^https://[A-Za-z0-9\.\-/_]+$', install_url):
            return False, "Invalid miner install URL."
        if algo and not re.match(r'^[a-z0-9_\-]+$', algo):
            return False, "Invalid hash algorithm name."
        if user_config and not re.match(r'^[A-Za-z0-9_\-\.\:\%\s]+$', user_config):
            return False, "Invalid miner configuration arguments."
        if not wallet:
            return False, "Wallet address is required for a custom miner flight sheet."

        worker_name = socket.gethostname().strip().upper().replace(" ", "_") or "WORKER"
        # HiveOS resolves %WAL%/%WORKER_NAME% server-side; our local apply must do it
        # itself or the miner would literally mine to "%WAL%.%WORKER_NAME%"
        user_config = user_config.replace("%WAL%", wallet).replace("%worker_name%", worker_name).replace("%WORKER_NAME%", worker_name)
        wallet_conf = parse_shell_config(WALLET_CONF_PATH)
        wallet_conf.clear()
        if fs_name:
            wallet_conf["FS_NAME"] = fs_name
        wallet_conf["CUSTOM_MINER"] = miner_alt
        if install_url:
            wallet_conf["CUSTOM_INSTALL_URL"] = install_url
        if algo:
            wallet_conf["CUSTOM_ALGO"] = algo
        wallet_conf["CUSTOM_TEMPLATE"] = wallet + "." + worker_name
        wallet_conf["CUSTOM_URL"] = pool
        wallet_conf["CUSTOM_PASS"] = "x"
        if user_config:
            wallet_conf["CUSTOM_USER_CONFIG"] = user_config
        if coin:
            wallet_conf["META"] = json.dumps({"custom": {"coin": coin}})
        if not write_shell_config(WALLET_CONF_PATH, wallet_conf):
            return False, "Failed to write wallet.conf"

        rig_conf = parse_shell_config(RIG_CONF_PATH)
        rig_conf["MINER"] = "custom"
        if not write_shell_config(RIG_CONF_PATH, rig_conf):
            return False, "Failed to write rig.conf"

        logging.info(f"Custom flight sheet applied by IP: {request.remote_addr} (Coin={coin}, Package={miner_alt})")
        run_command(MINER_RESTART_CMD)
        return True, "Flight sheet applied successfully! Miner daemon restarting..."

    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    wallet_conf["COIN"] = coin
    wallet_conf["WAL"] = wallet
    wallet_conf["POOL_URL"] = pool
    if not write_shell_config(WALLET_CONF_PATH, wallet_conf):
        return False, "Failed to write wallet.conf"

    rig_conf = parse_shell_config(RIG_CONF_PATH)
    rig_conf["MINER"] = miner
    if not write_shell_config(RIG_CONF_PATH, rig_conf):
        return False, "Failed to write rig.conf"

    logging.info(f"Flight sheet applied by IP: {request.remote_addr} (Coin={coin}, Miner={miner})")
    run_command(MINER_RESTART_CMD)
    return True, "Flight sheet applied successfully! Miner daemon restarting..."

def _read_active_mining_config():
    """Summarize the mining setup currently applied on this rig (wallet.conf / rig.conf).

    Rigs usually mine via a HiveOS-style config (custom miner, rigel, ...) rather
    than the local wallet library, so this synthesizes the wallet/flight sheet
    view of the live configuration for the UI."""
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    miner = (rig_conf.get("MINER") or "none").strip().lower()

    name, coin, wallet, pool = "", "", "", ""
    # Flight sheet name from the header comment of wallet.conf (or FS_NAME key we write)
    try:
        with open(WALLET_CONF_PATH, 'r') as f:
            for line in f:
                m = re.match(r'^#\s*#\s*#\s*FLIGHT SHEET\s+"([^"]+)"', line.strip())
                if m:
                    name = m.group(1)
                    break
    except Exception:
        pass
    if not name:
        name = (wallet_conf.get("FS_NAME") or "").strip()

    coin = (wallet_conf.get("COIN") or "").strip()
    if not coin:
        try:
            meta = json.loads(wallet_conf.get("META", "") or "{}")
            if isinstance(meta, dict):
                for section in (miner, "custom", "rigel", "gminer", "lolminer", "srbminer",
                                "bzminer", "trex", "wildrig", "teamredminer", "xmrig"):
                    sec = meta.get(section)
                    if isinstance(sec, dict) and sec.get("coin"):
                        coin = str(sec["coin"]).upper()
                        break
        except Exception:
            pass

    wallet = (wallet_conf.get("WAL") or "").strip()
    pool = (wallet_conf.get("POOL_URL") or "").strip()
    if not wallet or not pool:
        for k, v in wallet_conf.items():
            ku = k.upper()
            if ku.endswith("_TEMPLATE") and not wallet:
                wallet = str(v).strip()
            elif ku.endswith("_URL") and "INSTALL" not in ku and not pool:
                pool = str(v).strip()
    return {
        "name": name or "Current mining config",
        "coin": coin,
        "wallet": wallet,
        "pool": pool,
        "miner": miner,
    }

def _load_json_store(path, default):
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
    except Exception as e:
        logging.error(f"Failed to read {path}: {e}")
    return list(default)

def _save_json_store(path, data):
    try:
        with config_lock:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            os.chmod(path, 0o600)
        return True
    except Exception as e:
        logging.error(f"Failed to write {path}: {e}")
        return False

WALLETS_PATH = os.path.join(HIVE_CONFIG_DIR, "wallets.json")
FSHEETS_PATH = os.path.join(HIVE_CONFIG_DIR, "flightsheets.json")

def _validate_wallet_entry(entry):
    name = str(entry.get("name", "")).strip()
    address = str(entry.get("address", "")).strip()
    coin = str(entry.get("coin", "")).strip().upper()
    if not name or len(name) > 60:
        return None, "Wallet name must be 1-60 characters."
    if not address or len(address) > 200:
        return None, "Wallet address must be 1-200 characters."
    if not re.match(r'^[A-Za-z0-9_\-\.\:\@\/]+$', address):
        return None, "Invalid wallet address format."
    if coin and not re.match(r'^[A-Za-z0-9_+\-]{1,24}$', coin):
        return None, "Invalid coin symbol."
    clean = {
        "id": str(entry.get("id", "")).strip() or uuid.uuid4().hex[:12],
        "coin": coin,
        "name": name, "address": address,
    }
    return clean, ""

def _validate_fsheet_item(item):
    """Validate one miner item of a flight sheet (HiveOS-style items[])."""
    coin = str(item.get("coin", "")).strip()
    wallet = str(item.get("wallet", "")).strip()
    pool = str(item.get("pool", "")).strip()
    miner = str(item.get("miner", "none")).strip().lower()
    if not coin:
        return None, "Coin is required in every miner item."
    if not pool:
        return None, "Pool URL is required in every miner item."
    if not re.match(r'^[A-Za-z0-9_+\-\s]*$', coin):
        return None, "Invalid coin symbol."
    if not re.match(r'^[A-Za-z0-9_\-\s\.\/\@\:\%]*$', wallet):
        return None, "Invalid wallet address."
    if not re.match(r'^[a-zA-Z0-9\.\-\:\/]*$', pool):
        return None, "Invalid pool URL format."
    # Any HiveOS-style miner id is accepted (the catalog provides the list);
    # the strict format check blocks injection into rig.conf
    if miner != "none" and not re.match(r'^[a-z0-9_\-]{1,32}$', miner):
        return None, "Invalid miner program choice."
    # Optional HiveOS-style fields for custom miners
    miner_alt = str(item.get("miner_alt", "")).strip().lower()
    install_url = str(item.get("install_url", "")).strip()
    algo = str(item.get("algo", "")).strip().lower()
    user_config = str(item.get("user_config", "")).strip()
    if miner_alt and not re.match(r'^[a-z0-9_\-]+$', miner_alt):
        return None, "Invalid custom miner package name."
    if install_url and not re.match(r'^https://[A-Za-z0-9\.\-/_]+$', install_url):
        return None, "Invalid miner install URL."
    if algo and not re.match(r'^[a-z0-9_\-]+$', algo):
        return None, "Invalid hash algorithm name."
    if user_config and not re.match(r'^[A-Za-z0-9_\-\.\:\%\s]+$', user_config):
        return None, "Invalid miner configuration arguments."
    return {
        "coin": coin, "wallet": wallet, "pool": pool, "miner": miner,
        "miner_alt": miner_alt, "install_url": install_url, "algo": algo, "user_config": user_config,
    }, ""

FSHEET_ITEM_FIELDS = ["coin", "wallet", "pool", "miner", "miner_alt", "install_url", "algo", "user_config"]

def _validate_fsheet_entry(entry):
    """Validate a flight sheet: {id, name, coin, fav, items[]}.

    Accepts legacy flat payloads ({coin, wallet, pool, miner, ...}) by wrapping
    them into a single item, keeping the old UI/import paths working."""
    name = str(entry.get("name", "")).strip()
    if not name or len(name) > 60:
        return None, "Flight sheet name must be 1-60 characters."
    items_raw = entry.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        items_raw = [{k: entry.get(k, "") for k in FSHEET_ITEM_FIELDS}]
    if len(items_raw) > 5:
        return None, "A flight sheet supports up to 5 miner items."
    items = []
    for raw in items_raw:
        if not isinstance(raw, dict):
            return None, "Invalid miner item."
        clean, err = _validate_fsheet_item(raw)
        if err:
            return None, err
        items.append(clean)
    coin = str(entry.get("coin", "")).strip() or items[0]["coin"]
    if coin and not re.match(r'^[A-Za-z0-9_+\-\s]{1,32}$', coin):
        return None, "Invalid coin symbol."
    return {
        "id": str(entry.get("id", "")).strip() or uuid.uuid4().hex[:12],
        "name": name, "coin": coin,
        "fav": bool(entry.get("fav", False)),
        "items": items,
    }, ""

def _load_wallets_store():
    wallets = _load_json_store(WALLETS_PATH, [])
    changed = False
    for w in wallets:
        if "coin" not in w:
            w["coin"] = ""
            changed = True
    if changed:
        _save_json_store(WALLETS_PATH, wallets)
    return wallets

def _load_fsheets_store():
    """Load flight sheets and migrate legacy flat entries to items[] form."""
    fsheets = _load_json_store(FSHEETS_PATH, [])
    changed = False
    for f in fsheets:
        if not isinstance(f.get("items"), list) or not f["items"]:
            f["items"] = [{k: f.get(k, "") for k in FSHEET_ITEM_FIELDS}]
            changed = True
        if "fav" not in f:
            f["fav"] = False
            changed = True
        # Drop legacy flat copies (items[] is the single source of truth now)
        for k in FSHEET_ITEM_FIELDS:
            if k in f and k != "coin":
                f.pop(k, None)
                changed = True
    if changed:
        _save_json_store(FSHEETS_PATH, fsheets)
    return fsheets

def _miner_catalog():
    """Bundled HiveOS miner catalog (static/data/hive-miners.json), cached."""
    global _MINER_CATALOG_CACHE
    if _MINER_CATALOG_CACHE is None:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "data", "hive-miners.json")
            with open(path, 'r') as f:
                data = json.load(f)
            _MINER_CATALOG_CACHE = {m["id"]: m for m in data.get("miners", []) if m.get("id")}
        except Exception as e:
            logging.error(f"Failed to load miner catalog: {e}")
            _MINER_CATALOG_CACHE = {}
    return _MINER_CATALOG_CACHE

_MINER_CATALOG_CACHE = None

def _rig_gpu_platforms():
    """GPU vendor platforms physically present on this rig ('nvidia'/'amd')."""
    brands = set()
    try:
        for g in get_gpu_stats().get("gpus", []):
            b = str(g.get("brand") or "").strip().lower()
            if b in ("nvidia", "amd"):
                brands.add(b)
    except Exception:
        pass
    return brands

def _pick_apply_item(fsheet):
    """Choose which miner item applies on this rig: prefer an item whose miner
    supports a GPU vendor present on the rig (mixed NVIDIA/AMD rigs), else the
    first item."""
    items = [i for i in fsheet.get("items", []) if isinstance(i, dict)]
    if len(items) <= 1:
        return items[0] if items else {}
    plats = _rig_gpu_platforms()
    if plats:
        catalog = _miner_catalog()
        for it in items:
            miner_id = it.get("miner", "")
            info = catalog.get(miner_id)
            # srbminer_custom-style ids inherit the base miner's platforms
            if info is None and miner_id.endswith("_custom"):
                info = catalog.get(miner_id[:-len("_custom")])
            supported = {v for v in ("nvidia", "amd") if (info or {}).get(v)}
            if supported & plats:
                return it
    return items[0]

@app.route('/api/wallets', methods=['GET'])
def list_wallets():
    return jsonify({"success": True, "wallets": _load_wallets_store(),
                    "rig_config": _read_active_mining_config()})

@app.route('/api/wallets/save', methods=['POST'])
def save_wallet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    clean, err = _validate_wallet_entry(data.get("wallet") or {})
    if err:
        return jsonify({"success": False, "message": err}), 400
    wallets = _load_wallets_store()
    wallets = [w for w in wallets if w.get("id") != clean["id"]]
    wallets.append(clean)
    if _save_json_store(WALLETS_PATH, wallets):
        return jsonify({"success": True, "message": "Wallet saved."})
    return jsonify({"success": False, "message": "Failed to save wallet."}), 500

@app.route('/api/wallets/delete', methods=['POST'])
def delete_wallet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    wid = str(data.get("id", "")).strip()
    wallets = _load_wallets_store()
    before = len(wallets)
    wallets = [w for w in wallets if w.get("id") != wid]
    if len(wallets) == before:
        return jsonify({"success": False, "message": "Wallet not found."}), 404
    if _save_json_store(WALLETS_PATH, wallets):
        return jsonify({"success": True, "message": "Wallet removed."})
    return jsonify({"success": False, "message": "Failed to remove wallet."}), 500

@app.route('/api/fsheets', methods=['GET'])
def list_fsheets():
    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    return jsonify({
        "success": True,
        "fsheets": _load_fsheets_store(),
        "wallets": _load_wallets_store(),
        "active": {
            "coin": wallet_conf.get("COIN", ""),
            "wallet": wallet_conf.get("WAL", ""),
            "pool": wallet_conf.get("POOL_URL", ""),
            "miner": rig_conf.get("MINER", "none")
        },
        # Live mining setup parsed from the rig's own configs (works even when
        # the wallet/flight sheet libraries are empty)
        "rig_config": _read_active_mining_config()
    })

@app.route('/api/fsheets/save', methods=['POST'])
def save_fsheet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    clean, err = _validate_fsheet_entry(data.get("fsheet") or {})
    if err:
        return jsonify({"success": False, "message": err}), 400
    fsheets = _load_fsheets_store()
    fsheets = [f for f in fsheets if f.get("id") != clean["id"]]
    fsheets.append(clean)
    if _save_json_store(FSHEETS_PATH, fsheets):
        return jsonify({"success": True, "message": "Flight sheet saved."})
    return jsonify({"success": False, "message": "Failed to save flight sheet."}), 500

@app.route('/api/fsheets/delete', methods=['POST'])
def delete_fsheet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    fid = str(data.get("id", "")).strip()
    fsheets = _load_fsheets_store()
    before = len(fsheets)
    fsheets = [f for f in fsheets if f.get("id") != fid]
    if len(fsheets) == before:
        return jsonify({"success": False, "message": "Flight sheet not found."}), 404
    if _save_json_store(FSHEETS_PATH, fsheets):
        return jsonify({"success": True, "message": "Flight sheet removed."})
    return jsonify({"success": False, "message": "Failed to remove flight sheet."}), 500

@app.route('/api/fsheets/apply', methods=['POST'])
def apply_fsheet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    fid = str(data.get("id", "")).strip()
    fsheet = next((f for f in _load_fsheets_store() if f.get("id") == fid), None)
    if fsheet is None:
        return jsonify({"success": False, "message": "Flight sheet not found."}), 404
    item = _pick_apply_item(fsheet)
    # Resolve wallet reference (either stored address or wallet library id)
    wallet = str(item.get("wallet", "")).strip()
    wallets = _load_wallets_store()
    w = next((x for x in wallets if x.get("id") == wallet), None)
    if w:
        wallet = w.get("address", "")
    ok, msg = _apply_flight_sheet(item.get("coin"), wallet, item.get("pool"), item.get("miner"),
                                  extra={"name": fsheet.get("name", ""), "miner_alt": item.get("miner_alt", ""),
                                         "install_url": item.get("install_url", ""), "algo": item.get("algo", ""),
                                         "user_config": item.get("user_config", "")})
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

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

    ok, msg = _apply_flight_sheet(data.get("coin"), data.get("wallet"),
                                  data.get("pool"), data.get("miner"))
    return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

@app.route('/api/overclock/reset', methods=['POST'])
def reset_overclock():
    # Create backups first
    backup_configs()
    
    nv_stock = {
        "CLOCK": "",
        "LCLOCK": "",
        "MEM": "",
        "LMEM": "",
        "PLIMIT": "",
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

# ---------------- Extra (non-GPU) fan control via hwmon sysfs ----------------

def _iter_hwmon_pwm():
    """Yield (hwmon_dir, name, pwm_index) for every controllable PWM output."""
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            chip = open(os.path.join(hw, "name"), 'r').read().strip()
        except Exception:
            continue
        for p in sorted(glob.glob(os.path.join(hw, "pwm[0-9]"))):
            idx = p[-1]
            yield hw, chip, idx

def _read_fan_entry(hw, chip, idx):
    base = os.path.join(hw, "")
    def _rd(fname, default=None):
        try:
            with open(base + fname, 'r') as f:
                return f.read().strip()
        except Exception:
            return default
    try:
        duty = int(_rd("pwm" + idx, "0"))
    except (TypeError, ValueError):
        duty = 0
    rpm = _rd("fan" + idx + "_input")
    enable = _rd("pwm" + idx + "_enable")
    label = _rd("pwm" + idx + "_label") or _rd("fan" + idx + "_label") or ("fan" + idx)
    return {
        "hwmon": os.path.basename(hw), "chip": chip, "pwm": idx,
        "label": label,
        "duty": round(duty * 100 / 255.0),
        "rpm": int(rpm) if rpm and rpm.isdigit() else None,
        "mode": ("auto" if enable == "2" else ("full" if enable == "0" else "manual")),
        "writable": os.access(base + "pwm" + idx, os.W_OK) and os.access(base + "pwm" + idx + "_enable", os.W_OK),
    }

@app.route('/api/fans', methods=['GET'])
def list_fans():
    fans = [_read_fan_entry(hw, chip, idx) for hw, chip, idx in _iter_hwmon_pwm()]
    return jsonify({"success": True, "fans": fans})

@app.route('/api/fans', methods=['POST'])
def set_fan():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    hwmon = str(data.get("hwmon", "")).strip()
    idx = str(data.get("pwm", "")).strip()
    mode = str(data.get("mode", "")).strip()
    if not re.match(r'^hwmon[0-9]+$', hwmon) or not re.match(r'^[0-9]$', idx):
        return jsonify({"success": False, "message": "Invalid fan identifier."}), 400
    if mode not in ("manual", "auto"):
        return jsonify({"success": False, "message": "Fan mode must be 'manual' or 'auto'."}), 400
    base = os.path.join("/sys/class/hwmon", hwmon, "")
    pwm_path, enable_path = base + "pwm" + idx, base + "pwm" + idx + "_enable"
    if not os.path.exists(pwm_path):
        return jsonify({"success": False, "message": "Fan not found."}), 404
    try:
        if mode == "auto":
            with open(enable_path, 'w') as f:
                f.write("2")
        else:
            duty = int(data.get("duty", 0))
            if not (0 <= duty <= 100):
                return jsonify({"success": False, "message": "Duty cycle must be 0-100%."}), 400
            with open(enable_path, 'w') as f:
                f.write("1")
            with open(pwm_path, 'w') as f:
                f.write(str(round(duty * 255 / 100.0)))
        logging.info(f"Fan {hwmon}/pwm{idx} set to {mode}" +
                     (f" at {duty}%" if mode == "manual" else "") +
                     f" by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Fan updated."})
    except Exception as e:
        logging.error(f"Fan control failed on {hwmon}/pwm{idx}: {e}")
        return jsonify({"success": False, "message": "Failed to write fan control (root perms required)."}), 500

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
    masked_jumps = []
    for j in state.get("jump_hosts", []):
        m = dict(j)
        if "password" in m:
            m["password"] = "********" if m["password"] else ""
        masked_jumps.append(m)
    return jsonify({
        "success": True,
        "cluster_name": state.get("cluster_name", ""),
        "self_id": state["self_id"],
        "sync_interval": state.get("sync_interval", DEFAULT_SYNC_INTERVAL),
        "last_sync": _cluster_last_sync["ts"],
        "last_sync_ok": _cluster_last_sync["ok"],
        "last_sync_message": _cluster_last_sync["message"],
        "sshpass_available": bool(shutil.which("sshpass")),
        "rigs": rigs,
        "jump_hosts": masked_jumps,
        "clusters": state.get("clusters", [])
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
        if not (5 <= interval <= 3600):
            return jsonify({"success": False, "message": "Sync interval must be between 5 and 3600 seconds."}), 400
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
    rig = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found."}), 404
    state["rigs"] = [r for r in state["rigs"] if r.get("id") != rig_id]
    # Tombstone the deletion so peers drop the rig on the next sync instead of pushing it back
    removed = [t for t in state.get("removed", []) if t.get("id") != rig_id]
    try:
        rig_ts = int(rig.get("updated_at") or 0)
    except (TypeError, ValueError):
        rig_ts = 0
    removed.append({"id": rig_id, "name": rig.get("name", rig_id),
                    "updated_at": max(rig_ts, int(time.time()))})
    state["removed"] = removed
    if save_cluster_state(state):
        logging.info(f"Cluster rig '{rig.get('name', rig_id)}' removed by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Rig removed from the cluster."})
    return jsonify({"success": False, "message": "Failed to update cluster configuration."}), 500

def _validate_cluster_name(name):
    if not name or len(name) > 60 or not re.match(r'^[A-Za-z0-9_\-\s]+$', name):
        return "Invalid cluster name (1-60 chars, letters/digits/space/-_)."
    return None

@app.route('/api/cluster/create', methods=['POST'])
def api_cluster_create():
    """Create a named cluster (a group of rig ids)."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    name = str(data.get("name", "")).strip()
    err = _validate_cluster_name(name)
    if err:
        return jsonify({"success": False, "message": err}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    if any(c.get("name", "").lower() == name.lower() for c in state.get("clusters", [])):
        return jsonify({"success": False, "message": "A cluster with this name already exists."}), 400
    now = int(time.time())
    cluster = {"id": uuid.uuid4().hex, "name": name, "rig_ids": [], "updated_at": now}
    state.setdefault("clusters", []).append(cluster)
    if save_cluster_state(state):
        logging.info(f"Cluster '{name}' created by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": f"Cluster '{name}' created.", "cluster_id": cluster["id"]})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/update', methods=['POST'])
def api_cluster_update():
    """Rename a cluster."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    cluster_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    err = _validate_cluster_name(name)
    if err:
        return jsonify({"success": False, "message": err}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    cluster = next((c for c in state.get("clusters", []) if c.get("id") == cluster_id), None)
    if cluster is None:
        return jsonify({"success": False, "message": "Cluster not found."}), 404
    cluster["name"] = name
    cluster["updated_at"] = int(time.time())
    if save_cluster_state(state):
        logging.info(f"Cluster renamed to '{name}' by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Cluster renamed."})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/delete', methods=['POST'])
def api_cluster_delete():
    """Delete a cluster (rigs themselves stay untouched)."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    cluster_id = str(data.get("id", "")).strip()
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    cluster = next((c for c in state.get("clusters", []) if c.get("id") == cluster_id), None)
    if cluster is None:
        return jsonify({"success": False, "message": "Cluster not found."}), 404
    state["clusters"] = [c for c in state.get("clusters", []) if c.get("id") != cluster_id]
    # Tombstone the deletion so peers drop the cluster on the next sync
    removed = [t for t in state.get("removed", []) if t.get("id") != cluster_id]
    removed.append({"id": cluster_id, "type": "cluster", "name": cluster.get("name", cluster_id),
                    "updated_at": int(time.time())})
    state["removed"] = removed
    if save_cluster_state(state):
        logging.info(f"Cluster '{cluster.get('name', cluster_id)}' deleted by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Cluster deleted."})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/members', methods=['POST'])
def api_cluster_members():
    """Set the rig membership of a cluster (full rig_ids list from the checkbox UI)."""
    data = request.get_json()
    if not data or not isinstance(data.get("rig_ids"), list):
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    cluster_id = str(data.get("id", "")).strip()
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    cluster = next((c for c in state.get("clusters", []) if c.get("id") == cluster_id), None)
    if cluster is None:
        return jsonify({"success": False, "message": "Cluster not found."}), 404
    rig_ids = {r.get("id") for r in state["rigs"]}
    clean_ids = []
    for rid in data["rig_ids"]:
        rid = str(rid).strip()
        if rid in rig_ids and rid not in clean_ids:
            clean_ids.append(rid)
    cluster["rig_ids"] = clean_ids
    cluster["updated_at"] = int(time.time())
    if save_cluster_state(state):
        logging.info(f"Cluster '{cluster.get('name')}' membership set to {len(clean_ids)} rig(s) by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Cluster membership updated."})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

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

@app.route('/api/cluster/jump', methods=['POST'])
def api_cluster_jump_save():
    """Save (add or update) a jump server in the shared library."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    incoming = dict(data.get("jump") or {})
    for secret_field in ("password",):
        if incoming.get(secret_field) == "********":
            existing = next((j for j in state.get("jump_hosts", [])
                             if j.get("id") == str(incoming.get("id", "")).strip()), None)
            if existing and existing.get(secret_field):
                incoming[secret_field] = existing.get(secret_field)
            else:
                incoming.pop(secret_field, None)

    name = str(incoming.get("name", "")).strip()
    host = str(incoming.get("host", "")).strip()
    user = str(incoming.get("user", "")).strip()
    if not name or len(name) > 60:
        return jsonify({"success": False, "message": "Jump server name must be 1-60 characters."}), 400
    if not VALID_HOST_RE.match(host):
        return jsonify({"success": False, "message": "Invalid jump server host."}), 400
    if not VALID_USER_RE.match(user):
        return jsonify({"success": False, "message": "Invalid jump server SSH user name."}), 400
    try:
        port = int(incoming.get("port", 22))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Jump server SSH port must be an integer."}), 400
    if not (1 <= port <= 65535):
        return jsonify({"success": False, "message": "Jump server SSH port must be between 1 and 65535."}), 400
    auth = str(incoming.get("auth", "password")).strip().lower()
    if auth not in ("password", "key"):
        return jsonify({"success": False, "message": "Jump server auth must be 'password' or 'key'."}), 400

    clean = {
        "id": str(incoming.get("id", "")).strip() or uuid.uuid4().hex[:12],
        "name": name, "host": host, "port": port, "user": user, "auth": auth,
        "updated_at": int(time.time()),
    }
    if auth == "password":
        password = str(incoming.get("password", ""))
        if len(password) > 128:
            return jsonify({"success": False, "message": "Jump server password is too long."}), 400
        clean["password"] = password
    else:
        key_path = str(incoming.get("key_path", "")).strip()
        if key_path:
            if not VALID_KEYPATH_RE.match(key_path):
                return jsonify({"success": False, "message": "Invalid jump server SSH key path."}), 400
            clean["key_path"] = key_path

    state["jump_hosts"] = [j for j in state.get("jump_hosts", []) if j.get("id") != clean["id"]]
    state["jump_hosts"].append(clean)
    if save_cluster_state(state):
        logging.info(f"Jump server '{name}' saved by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Jump server saved.", "jump_id": clean["id"]})
    return jsonify({"success": False, "message": "Failed to save jump server."}), 500

@app.route('/api/cluster/jump/delete', methods=['POST'])
def api_cluster_jump_delete():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    jump_id = str(data.get("jump_id", "")).strip()
    before = len(state.get("jump_hosts", []))
    state["jump_hosts"] = [j for j in state.get("jump_hosts", []) if j.get("id") != jump_id]
    if len(state.get("jump_hosts", [])) == before:
        return jsonify({"success": False, "message": "Jump server not found."}), 404
    if save_cluster_state(state):
        logging.info(f"Jump server {jump_id} deleted by IP: {request.remote_addr}")
        return jsonify({"success": True, "message": "Jump server removed."})
    return jsonify({"success": False, "message": "Failed to save cluster configuration."}), 500

@app.route('/api/cluster/jump/test', methods=['POST'])
def api_cluster_jump_test():
    """Test connectivity to a jump server (saved by id or an unsaved modal payload)."""
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    incoming = dict(data.get("jump") or {})
    jid = str(incoming.get("id", "")).strip()
    if jid or incoming.get("password") == "********":
        saved = next((j for j in state.get("jump_hosts", []) if j.get("id") == jid), None)
        if saved:
            if incoming.get("password") in ("********", None, ""):
                incoming["password"] = saved.get("password", "")
            incoming.setdefault("host", saved.get("host"))
            incoming.setdefault("port", saved.get("port", 22))
            incoming.setdefault("user", saved.get("user"))
            incoming.setdefault("auth", saved.get("auth", "password"))
            incoming.setdefault("key_path", saved.get("key_path", ""))
    host = str(incoming.get("host", "")).strip()
    user = str(incoming.get("user", "")).strip()
    if not VALID_HOST_RE.match(host):
        return jsonify({"success": False, "message": "Invalid jump server host."}), 400
    if not VALID_USER_RE.match(user):
        return jsonify({"success": False, "message": "Invalid jump server SSH user name."}), 400
    try:
        port = int(incoming.get("port", 22))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid jump server SSH port."}), 400
    access = {
        "id": "jumptest", "name": "jump-test", "type": "direct",
        "host": host, "port": port,
        "user": user, "auth": str(incoming.get("auth", "password")),
        "password": str(incoming.get("password", "") or ""),
        "key_path": str(incoming.get("key_path", "") or ""),
    }
    ok, out, ssh_err = run_ssh_command(access, "hostname && echo __OK__", timeout=30)
    if ok and "__OK__" in out:
        hostname = out.replace("__OK__", "").strip().splitlines()
        hostname = hostname[0].strip() if hostname else "unknown"
        return jsonify({"success": True,
                        "message": "Jump server connection OK. Remote host: %s" % hostname,
                        "hostname": hostname})
    return jsonify({"success": False, "message": "Jump server test failed: %s" % (ssh_err or "unknown error")})

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
    # Resolve a jump_id reference against the shared jump server library
    jid = str(incoming.get("jump_id") or "").strip()
    if jid:
        j = next((x for x in state.get("jump_hosts", []) if x.get("id") == jid), None)
        if j:
            incoming.setdefault("jump_host", j.get("host"))
            incoming.setdefault("jump_port", j.get("port", 22))
            incoming.setdefault("jump_user", j.get("user"))
            incoming.setdefault("jump_auth", j.get("auth", "password"))
            if incoming.get("jump_password") in ("********", None, ""):
                incoming["jump_password"] = j.get("password", "")
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
        "rigs": state["rigs"],
        "removed": state.get("removed", []),
        "jump_hosts": state.get("jump_hosts", []),
        "clusters": state.get("clusters", [])
    })

@app.route('/api/cluster/sync', methods=['POST'])
def api_cluster_sync_push():
    data = request.get_json()
    if not data or not isinstance(data.get("rigs"), list):
        return jsonify({"success": False, "message": "Invalid sync payload"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    state["rigs"], state["removed"] = merge_rig_lists(
        state["rigs"], data["rigs"],
        base_removed=state.get("removed"),
        incoming_removed=data.get("removed"))
    state["jump_hosts"] = merge_jump_hosts(state.get("jump_hosts", []), data.get("jump_hosts"))
    state["clusters"] = merge_clusters(state.get("clusters", []), data.get("clusters", []), state.get("removed", []))
    if data.get("cluster_name") and not state.get("cluster_name"):
        state["cluster_name"] = str(data["cluster_name"])
    saved = save_cluster_state(state)
    return jsonify({"success": bool(saved), "message": "Cluster state merged." if saved else "Failed to persist merged state."})

# ---------------- Remote rig proxy (full dashboard over SSH) ----------------

def _serve_local_api(subpath, method, body):
    """Dispatch an API call to the local Flask routes (used when proxying self)."""
    from werkzeug.test import EnvironBuilder
    headers = {}
    cookie = request.headers.get("Cookie")
    if cookie:
        headers["Cookie"] = cookie
    csrf = request.headers.get("X-CSRF-Token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if request.headers.get("Authorization"):
        headers["Authorization"] = request.headers.get("Authorization")
    ctype = request.headers.get("Content-Type")
    if ctype:
        headers["Content-Type"] = ctype
    builder = EnvironBuilder(path="/" + subpath, method=method, headers=headers,
                             data=body if body is not None else b"")
    env = builder.get_environ()
    with app.request_context(env):
        app.preprocess_request()
        rv = app.dispatch_request()
        if not isinstance(rv, Response):
            rv = app.make_response(rv)
    return rv

@app.route('/api/remote/<rig_id>/<path:subpath>', methods=['GET', 'POST'])
def api_remote_proxy(rig_id, subpath):
    if not subpath.startswith("api/"):
        return jsonify({"success": False, "message": "Invalid remote path."}), 404
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    if rig_id == state["self_id"]:
        # Managing the local rig - serve the request directly instead of going through SSH
        try:
            body = request.get_data(cache=True) if request.method == "POST" else None
            return _serve_local_api(subpath, request.method, body)
        except Exception as e:
            logging.error(f"Local dispatch of proxied API '{subpath}' failed: {e}")
            return jsonify({"success": False, "message": "Failed to serve local API call."}), 500
    rig = next((r for r in state["rigs"] if r.get("id") == rig_id), None)
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
        serve(app, host='0.0.0.0', port=port, threads=8)
    else:
        logging.warning("Waitress package not found. Falling back to Flask built-in development server.")
        app.run(host='0.0.0.0', port=port, debug=False)
