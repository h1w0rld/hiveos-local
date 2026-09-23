import os
import re
import csv
import io
import json
import glob
import base64
import hashlib
import hmac
import tarfile
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
from concurrent.futures import ThreadPoolExecutor
from collections import deque
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
OC_PRESETS_PATH = os.path.join(HIVE_CONFIG_DIR, "oc_presets.json")
OC_STATE_PATH = os.path.join(HIVE_CONFIG_DIR, "oc_preset_state.json")
CLUSTER_CONF = os.path.join(HIVE_CONFIG_DIR, "cluster.json")
CLUSTER_SELF_ID_PATH = os.path.join(HIVE_CONFIG_DIR, "cluster-self-id")
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
# A miner spawned directly by the panel inherits the panel's systemd cgroup, and
# KillMode=control-group makes `systemctl restart/stop hiveos-local` SIGKILL the
# miner with it (observed: fsheet apply from the UI, then a deploy restart, killed
# the running miner silently). Wrapping start/restart in `systemd-run --scope`
# puts the screen+miner into their own transient scope - the same place a
# `miner start` from an SSH session lands - so panel lifecycle can not touch them.
_MINER_SCOPE = "systemd-run --scope --quiet " if shutil.which("systemd-run") else ""
MINER_START_CMD = f"sudo {_MINER_SCOPE}env {HIVE_MINER_ENV} /hive/bin/miner start"
MINER_STOP_CMD = f"sudo env {HIVE_MINER_ENV} /hive/bin/miner stop"
MINER_RESTART_CMD = f"sudo {_MINER_SCOPE}env {HIVE_MINER_ENV} /hive/bin/miner restart"

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

# Last self_id this process has ever resolved (second safety net after the
# sidecar file: even if /hive-config glitches for both files, the running
# process must not forget who it is)
_KNOWN_SELF_ID = {"value": ""}

def _read_self_id_sidecar():
    """Reads the persistent rig identity (cluster-self-id sidecar)."""
    try:
        with open(CLUSTER_SELF_ID_PATH, 'r') as f:
            sid = f.read().strip()
            return sid or None
    except Exception:
        return None

def _write_self_id_sidecar(self_id):
    """Persists the rig identity next to cluster.json (atomic, like cluster.json)."""
    try:
        tmp_path = CLUSTER_SELF_ID_PATH + ".tmp"
        with open(tmp_path, 'w') as f:
            f.write(self_id)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CLUSTER_SELF_ID_PATH)
        return True
    except Exception as e:
        logging.error(f"Failed to save cluster identity sidecar: {e}")
        return False

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
            # Read under the writer's lock: without it a load hitting the save
            # window (file truncated before rewritten) saw an empty file and
            # generated a fresh self_id - the rig was reborn as a new ghost rig
            with config_lock:
                with open(CLUSTER_CONF, 'r') as f:
                    data = json.load(f)
            if isinstance(data, dict):
                for k in ("cluster_name", "self_id", "sync_interval", "rigs", "removed", "jump_hosts", "clusters"):
                    if k in data:
                        state[k] = data[k]
            else:
                # Valid JSON but not a rig state (ntfs glitch can truncate to
                # "null" etc.) - treat exactly like an unreadable file
                logging.error(f"Cluster config has unexpected type {type(data).__name__}, ignoring")
    except Exception as e:
        logging.error(f"Failed to read cluster config: {e}")

    if not state.get("self_id"):
        # The config is missing or damaged (a damaged file can also look like
        # valid JSON without a self_id key). NEVER generate a new identity
        # here: a reborn rig appears as a new rig on every peer within one
        # sync cycle (the 'extra rig' ghosts). Restore the identity from the
        # sidecar written at first creation, then from process memory.
        restored = _read_self_id_sidecar() or _KNOWN_SELF_ID.get("value")
        if restored:
            state["self_id"] = restored
            logging.error("Cluster config unreadable or without self_id - identity kept "
                          f"({restored[:8]}...), no rebirth")
        else:
            state["self_id"] = uuid.uuid4().hex
            _write_self_id_sidecar(state["self_id"])
    if _KNOWN_SELF_ID.get("value") != state["self_id"]:
        _KNOWN_SELF_ID["value"] = state["self_id"]
    if _read_self_id_sidecar() != state["self_id"]:
        # Legacy install (config predates the sidecar) or sidecar drift
        _write_self_id_sidecar(state["self_id"])
    rigs = [r for r in state.get("rigs", []) if isinstance(r, dict) and r.get("id")]
    # Do not re-add this rig while an unexpired deletion tombstone for it exists:
    # a fresh self entry (updated_at=now) would always beat the tombstone on peers
    # and resurrect the deleted rig on every sync cycle
    tomb_ids = {str(t.get("id")) for t in state.get("removed", [])
                if isinstance(t, dict) and t.get("type", "rig") == "rig"}
    if not any(r.get("id") == state["self_id"] for r in rigs) and state["self_id"] not in tomb_ids:
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
            # Atomic write (tmp + rename): readers only ever see the old or the
            # new complete file, never a truncated one - ntfs-3g on /hive-config
            # makes the in-place truncate-then-write window wide enough to hit
            tmp_path = CLUSTER_CONF + ".tmp"
            with open(tmp_path, 'w') as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, CLUSTER_CONF)
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
    # Tombstones always win until they expire: a deleted rig keeps re-adding
    # itself on every sync cycle (fresh updated_at), so comparing timestamps
    # would resurrect it. Explicit re-add via /api/cluster/rig clears the
    # tombstone (see api_cluster_rig_save).
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
        if rid in tomb:
            # This entry was deleted on a peer; tombstones win until they expire
            # or the rig is explicitly re-added
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
        if rid in tomb:
            logging.info(f"Cluster merge: rig '{by_id[rid].get('name', rid)}' removed by cluster deletion")
            del by_id[rid]
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

# ---------------- CSV cluster import: parse & validate ----------------

IMPORT_APP_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORT_REMOTE_DIR = "/root/hiveos-local"
IMPORT_INSTALL_TIMEOUT = 300

def _det_id(*parts):
    """Deterministic short id: same input -> same id on every node (idempotent import)."""
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]

def _split_import_sections(line):
    """Split a CSV line into ';'-separated sections. Quote characters are kept
    verbatim (the csv module decodes them later)."""
    parts, buf, in_quotes = [], [], False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_quotes:
            buf.append(ch)
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    buf.append('"')  # doubled quote stays doubled for csv module
                    i += 1
                else:
                    in_quotes = False
        elif ch == '"':
            in_quotes = True
            buf.append(ch)
        elif ch == ';':
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts

def _detect_import_delimiter(lines):
    """Field delimiter inside a section: ',' if any line has one, else tab, else whitespace runs."""
    joined = "\n".join(lines)
    if "," in joined:
        return ","
    if "\t" in joined:
        return "\t"
    return None  # whitespace

def _split_fields(section, delimiter):
    """Split one CSV section into fields. Returns list of (field, quoted) or None on parse error."""
    if delimiter:
        try:
            rows = list(csv.reader([section], delimiter=delimiter, skipinitialspace=True))
        except Exception:
            return None
        if len(rows) != 1:
            return None
        raw_fields = rows[0]
    else:
        raw_fields = re.split(r'\s+', section.strip())
    fields = []
    for f in raw_fields:
        fields.append(f)
    return fields

def parse_cluster_csv(text):
    """Parse and validate the cluster import CSV.

    Format per line:  name;access[;jump]
      access = host,port,user,password
      jump   = host,port,user,password (full definition) | host (reference) | '' (direct)
    Same name on several lines = one node, each line adds a route.

    Returns dict with:
      ok           - True when no validation errors
      delimiter    - detected field delimiter ('\\n' means whitespace)
      nodes        - ordered list of {name, accesses: [{line, host, port, user, password, jump_key|None}], dup_lines}
      jumps        - {key: {host, port, user, password, lines}}
      errors       - [{line, message}]
    """
    errors = []

    def err(line_no, msg):
        errors.append({"line": line_no, "message": msg})

    raw_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    code_lines = []  # (line_no, content) of meaningful lines
    for idx, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code_lines.append((idx, line))

    delimiter = _detect_import_delimiter([c for _, c in code_lines])

    node_order = []
    nodes = {}
    jumps = {}      # key: host|port|user -> def
    jump_order = []

    for line_no, line in code_lines:
        sections = _split_import_sections(line)
        # trailing empty sections are harmless ("...;" means direct)
        while len(sections) > 1 and not sections[-1].strip():
            sections.pop()
        # Tab/whitespace input usually comes without ';' separators: a single
        # section then holds name+access (5 fields) or name+access+jump (9 fields)
        if len(sections) == 1 and delimiter in ("\t", None):
            flat = _split_fields(sections[0], delimiter)
            if flat and len(flat) in (5, 9):
                d = delimiter or " "
                sections = [flat[0], d.join(flat[1:5])]
                if len(flat) == 9:
                    sections.append(d.join(flat[5:9]))
        name = sections[0].strip()
        if not name:
            err(line_no, "Node name is empty (first section before ';').")
            continue
        if delimiter == "," and "," in name:
            err(line_no, "Missing node name (start the line with the name: "
                         "name;ip,port,login,password[;jump]).")
            continue
        if len(name) > 60 or not re.match(r'^[A-Za-z0-9_\-\s\.]+$', name):
            err(line_no, "Invalid node name (1-60 chars: letters, digits, space, -_.).")
            continue
        if len(sections) < 2:
            err(line_no, "Missing access section (expected: name;ip,port,login,password[;jump]).")
            continue
        if len(sections) > 3:
            err(line_no, "Too many ';'-sections (max 3: name;access;jump).")
            continue

        access_fields = _split_fields(sections[1], delimiter)
        if access_fields is None:
            err(line_no, "Malformed access section (check quotes).")
            continue
        if not any(f.strip() for f in access_fields):
            err(line_no, "Access section is empty.")
            continue

        a_host, a_port, a_user, a_password = None, 22, None, ""
        if len(access_fields) != 4:
            err(line_no, "Access must have 4 fields: ip,port,login,password (got %d)." % len(access_fields))
            continue
        a_host = access_fields[0].strip()
        a_user = access_fields[2].strip()
        a_password = access_fields[3]
        if not VALID_HOST_RE.match(a_host):
            err(line_no, "Invalid access host/IP: '%s'." % a_host[:40])
            continue
        port_txt = access_fields[1].strip()
        if port_txt:
            try:
                a_port = int(port_txt)
            except ValueError:
                err(line_no, "Access port must be an integer.")
                continue
            if not (1 <= a_port <= 65535):
                err(line_no, "Access port must be between 1 and 65535.")
                continue
        if not VALID_USER_RE.match(a_user):
            err(line_no, "Invalid SSH login: '%s'." % a_user[:40])
            continue
        if len(a_password) > 128:
            err(line_no, "SSH password is too long (max 128).")
            continue

        jump_key = None
        if len(sections) == 3 and sections[2].strip():
            j_fields = _split_fields(sections[2], delimiter)
            if j_fields is None:
                err(line_no, "Malformed jump section (check quotes).")
                continue
            j_nz = [f for f in j_fields if f.strip()]
            if len(j_nz) == 1:
                # Reference to a jump host defined elsewhere (full def in this text or in the library)
                j_ref = next(f for f in j_fields if f.strip()).strip()
                j_ref = j_ref.strip()
                if not VALID_HOST_RE.match(j_ref):
                    err(line_no, "Invalid jump reference host/IP: '%s'." % j_ref[:40])
                    continue
                jump_key = "ref:%s" % j_ref.lower()
            elif len(j_fields) == 4:
                j_host = j_fields[0].strip()
                j_user = j_fields[2].strip()
                j_password = j_fields[3]
                j_port = 22
                port_txt = j_fields[1].strip()
                if not VALID_HOST_RE.match(j_host):
                    err(line_no, "Invalid jump host/IP: '%s'." % j_host[:40])
                    continue
                if port_txt:
                    try:
                        j_port = int(port_txt)
                    except ValueError:
                        err(line_no, "Jump port must be an integer.")
                        continue
                    if not (1 <= j_port <= 65535):
                        err(line_no, "Jump port must be between 1 and 65535.")
                        continue
                if not VALID_USER_RE.match(j_user):
                    err(line_no, "Invalid jump login: '%s'." % j_user[:40])
                    continue
                if len(j_password) > 128:
                    err(line_no, "Jump password is too long (max 128).")
                    continue
                key = "%s|%d|%s" % (j_host.lower(), j_port, j_user)
                if key not in jumps:
                    jumps[key] = {"host": j_host, "port": j_port, "user": j_user,
                                  "password": j_password, "lines": []}
                    jump_order.append(key)
                else:
                    if jumps[key]["password"] != j_password:
                        err(line_no, "Jump %s:%d (%s) is defined with a different password "
                                     "on line(s) %s - passwords must match." %
                            (j_host, j_port, j_user, ", ".join(str(x) for x in jumps[key]["lines"])))
                        continue
                jumps[key]["lines"].append(line_no)
                jump_key = key
            else:
                err(line_no, "Jump section must be a full definition (ip,port,login,password) "
                             "or a single host reference (got %d fields)." % len(j_fields))
                continue

        key = name.lower()
        if key not in nodes:
            nodes[key] = {"name": name, "accesses": [], "lines": []}
            node_order.append(key)
        node = nodes[key]
        akey = "%s|%d|%s|%s" % (a_host.lower(), a_port, a_user, jump_key or "")
        if any(a["key"] == akey for a in node["accesses"]):
            # exact duplicate route for the same node - idempotent, just remember the line
            node["lines"].append(line_no)
            continue
        node["accesses"].append({"key": akey, "line": line_no, "host": a_host,
                                 "port": a_port, "user": a_user, "password": a_password,
                                 "jump_key": jump_key})
        node["lines"].append(line_no)

    # Resolve jump references (ref:host) against full defs in this text, then the library
    lib_hosts = {}
    try:
        for j in load_cluster_state().get("jump_hosts", []):
            lib_hosts.setdefault(str(j.get("host", "")).lower(), []).append(j)
    except Exception:
        pass
    for key in node_order:
        for acc in nodes[key]["accesses"]:
            jk = acc.get("jump_key")
            if not (jk and jk.startswith("ref:")):
                continue
            ref_host = jk[4:]
            full_keys = [k for k in jump_order if jumps[k]["host"].lower() == ref_host]
            if len(full_keys) == 1:
                acc["jump_key"] = full_keys[0]
                continue
            if full_keys:
                # several local defs on the same host - ambiguous unless the library has a matching one
                err(acc["line"],
                    "Ambiguous jump reference '%s' (defined several times with different port/login)." % ref_host)
                continue
            # fall back to a direct access of a node in this import (the gateway is
            # usually listed as a node itself; its direct credentials become the jump)
            node_matches = {}
            for nk in node_order:
                for a2 in nodes[nk]["accesses"]:
                    if a2["host"].lower() == ref_host and a2["jump_key"] is None:
                        node_matches.setdefault((a2["port"], a2["user"]), set()).add(a2["password"])
            if len(node_matches) == 1:
                (j_port, j_user), passwords = next(iter(node_matches.items()))
                if len(passwords) > 1:
                    err(acc["line"], "Ambiguous jump reference '%s' (nodes disagree on its password)." % ref_host)
                    continue
                j_host = next(a2["host"] for nk in node_order for a2 in nodes[nk]["accesses"]
                              if a2["host"].lower() == ref_host and a2["jump_key"] is None)
                key2 = "%s|%d|%s" % (ref_host, j_port, j_user)
                if key2 not in jumps:
                    jumps[key2] = {"host": j_host, "port": j_port, "user": j_user,
                                   "password": next(iter(passwords)), "lines": []}
                    jump_order.append(key2)
                acc["jump_key"] = key2
                continue
            if len(node_matches) > 1:
                err(acc["line"], "Ambiguous jump reference '%s' (several nodes use this host "
                                 "with different port/login - define the jump explicitly)." % ref_host)
                continue
            lib_matches = lib_hosts.get(ref_host, [])
            if len(lib_matches) == 1:
                j = lib_matches[0]
                key2 = "%s|%d|%s" % (str(j.get("host", "")).lower(), int(j.get("port", 22) or 22),
                                     str(j.get("user", "")))
                if key2 not in jumps:
                    jumps[key2] = {"host": j.get("host", ""), "port": int(j.get("port", 22) or 22),
                                   "user": j.get("user", ""), "password": j.get("password", ""),
                                   "lines": []}
                    jump_order.append(key2)
                acc["jump_key"] = key2
            elif len(lib_matches) > 1:
                err(acc["line"], "Ambiguous jump reference '%s' (library has several jumps on this host - "
                                 "use the full definition ip,port,login,password)." % ref_host)
            else:
                err(acc["line"], "Jump host '%s' is not defined (add a full definition "
                                 "ip,port,login,password on any line)." % ref_host)

    return {"ok": not errors, "delimiter": delimiter or "whitespace",
            "nodes": [nodes[k] for k in node_order],
            "jumps": [dict(jumps[k], key=k) for k in jump_order],
            "errors": errors}

# ---------------- CSV cluster import: background job ----------------

_import_jobs = {}
_import_jobs_lock = threading.Lock()
_IMPORT_MAX_PARALLEL = 6
_IMPORT_MAX_NODES = 200
_IMPORT_MAX_TEXT = 256 * 1024

def _import_access_dict(acc, jump_entries):
    """Build an access dict (inline jump fields) for run_ssh_command."""
    access = {
        "id": _det_id("acc", acc["host"].lower(), acc["port"], acc["user"]),
        "name": "%s@%s" % (acc["user"], acc["host"]),
        "type": "direct",
        "host": acc["host"], "port": acc["port"], "user": acc["user"],
        "auth": "password", "password": acc["password"],
    }
    jk = acc.get("jump_key")
    if jk and jk in jump_entries:
        jd = jump_entries[jk]
        access["type"] = "jump"
        access["jump_host"] = jd["host"]
        access["jump_port"] = jd["port"]
        access["jump_user"] = jd["user"]
        access["jump_auth"] = "password"
        access["jump_password"] = jd["password"]
    return access

def _import_build_tar():
    """Tar.gz of the application files for uploading to nodes (in memory)."""
    buf = io.BytesIO()
    try:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name in ("app.py", "install.sh", "requirements.txt", "version.txt"):
                path = os.path.join(IMPORT_APP_DIR, name)
                if os.path.isfile(path):
                    tar.add(path, arcname=name)
            for dirname in ("templates", "static"):
                path = os.path.join(IMPORT_APP_DIR, dirname)
                if os.path.isdir(path):
                    tar.add(path, arcname=dirname)
    except Exception as e:
        logging.error(f"Cluster import: failed to pack app files: {e}")
        return None
    return buf.getvalue()

def _import_ssh_ok(res, marker=None):
    ok, out, err = res
    if not ok:
        return False, (err or "connection failed")
    if marker and marker not in out:
        return False, (err or "unexpected output")
    return True, ""

def _import_escalate(access):
    """Detect how to run root commands on a node: direct root SSH, passwordless
    sudo, or sudo with the SSH password. Returns (mode, pw64, error)."""
    ok, out, err = run_ssh_command(access, "id -u", timeout=30)
    if ok and out.strip() == "0":
        return "root", "", ""
    ok, out, err = run_ssh_command(access, "sudo -n id -u 2>/dev/null", timeout=30)
    if ok and out.strip() == "0":
        return "sudo-nopass", "", ""
    ssh_pw = str(access.get("password", "") or "")
    if ssh_pw:
        pw64 = base64.b64encode(ssh_pw.encode("utf-8")).decode("ascii")
        cmd = "printf '%s\\n' '" + pw64 + "' | base64 -d | sudo -S -p '' id -u"
        ok, out, err = run_ssh_command(access, cmd, timeout=30)
        if ok and out.strip() == "0":
            return "sudo", pw64, ""
    return None, "", "no root access (SSH user must be root, or have sudo rights with the same password)"

def _import_root_cmd(mode, pw64, script):
    """Wrap a shell script so it runs as root on the node (base64 transport).
    The script must not read stdin: under a passworded sudo the password line
    is piped in (and passes through untouched when the sudo timestamp is cached)."""
    s64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    runner = 'bash -c "$(printf %s \'' + s64 + '\' | base64 -d)"'
    if mode == "root":
        return runner
    if mode == "sudo-nopass":
        return "sudo -n " + runner
    return "printf '%s\\n' '" + pw64 + "' | base64 -d | sudo -S -p '' " + runner

def _import_install_node(access, mode, pw64):
    """Upload the app code to a node and install/refresh the service.
    Returns (ok, action, message) where action is 'installed'|'updated'|''."""
    ok, out, err = run_ssh_command(
        access, "systemctl is-active hiveos-local.service 2>/dev/null || true", timeout=30)
    was_active = ok and out.strip() == "active"
    # existing installs may live outside /root (e.g. /home/user) - follow the unit
    ok, out, err = run_ssh_command(
        access, "systemctl show hiveos-local.service -p WorkingDirectory --value 2>/dev/null", timeout=30)
    workdir = out.strip() if (ok and out.strip().startswith("/")) else ""
    target_dir = workdir or IMPORT_REMOTE_DIR
    tar_bytes = _import_build_tar()
    if not tar_bytes:
        return False, "", "failed to pack local application files"
    tmp_tar = "/tmp/hvl-%d.tar.gz" % random.randint(100000, 999999)
    ok, msg = _import_ssh_ok(
        run_ssh_command(access, "cat > " + tmp_tar, timeout=180, stdin_data=tar_bytes))
    if not ok:
        return False, "", "code upload failed: %s" % msg
    script = ("mkdir -p '%s' && tar -xzf '%s' -C '%s' && rm -f '%s' && echo __TAR_OK__"
              % (target_dir, tmp_tar, target_dir, tmp_tar))
    ok, msg = _import_ssh_ok(
        run_ssh_command(access, _import_root_cmd(mode, pw64, script), timeout=180), "__TAR_OK__")
    if not ok:
        return False, "", "code upload failed: %s" % msg
    if was_active:
        ok, msg = _import_ssh_ok(
            run_ssh_command(access, _import_root_cmd(mode, pw64,
                            "systemctl restart hiveos-local.service && echo __SVC_OK__"),
                            timeout=90), "__SVC_OK__")
        if not ok:
            return False, "", "service restart failed: %s" % msg
        return True, "updated", ""
    script = ("cd '%s' && chmod +x install.sh && ./install.sh > /tmp/hiveos-local-install.log 2>&1 "
              "&& echo __INSTALLED__ || tail -n 5 /tmp/hiveos-local-install.log" % target_dir)
    ok, out, err = run_ssh_command(
        access, _import_root_cmd(mode, pw64, script), timeout=IMPORT_INSTALL_TIMEOUT)
    if ok and "__INSTALLED__" in out:
        return True, "installed", ""
    detail = (err or out or "unknown error").strip().splitlines()
    return False, "", "install failed: %s" % (detail[-1][:200] if detail else "unknown error")

def _import_ensure_key(access, mode, pw64, force_set, self_password):
    """Read the node's dashboard key; on a fresh install overwrite it with the
    farm password so the whole cluster shares one dashboard password.
    Returns (key, kept|set) or ('', error_message)."""
    if not force_set:
        ok, out, err = run_ssh_command(
            access, _import_root_cmd(mode, pw64,
                                     "cat /hive-config/dashboard.key 2>/dev/null || true"),
            timeout=30)
        key = out.strip() if ok else ""
        if key and len(key) <= 128 and "[ERROR" not in key:
            return key, "kept"
    b64 = base64.b64encode(str(self_password).encode("utf-8")).decode("ascii")
    script = ("mkdir -p /hive-config && printf '%s' '" + b64 + "' | base64 -d > /hive-config/dashboard.key "
              "&& chmod 600 /hive-config/dashboard.key "
              "&& systemctl restart hiveos-local.service && echo __KEY_OK__")
    ok, msg = _import_ssh_ok(
        run_ssh_command(access, _import_root_cmd(mode, pw64, script), timeout=90), "__KEY_OK__")
    if not ok:
        return "", "dashboard key setup failed: %s" % msg
    return str(self_password), "set"

def _access_natural_key(a):
    """Route identity (host|port|user|jump): re-import upserts the same route
    instead of piling a second copy with a different id onto the entry."""
    return "%s|%s|%s|%s" % (str(a.get("host", "")).lower(), int(a.get("port", 22) or 22),
                            str(a.get("user", "")),
                            str(a.get("jump_id", "")) or str(a.get("type", "direct")))

def _import_upsert_jump_entries(state, jump_entries):
    """Merge the import's jump definitions into the local library by natural key
    (host+port+user); keeps existing ids, refreshes passwords (newer wins on sync)."""
    now = int(time.time())
    for jk, jd in jump_entries.items():
        existing = None
        for j in state.get("jump_hosts", []):
            if (str(j.get("host", "")).lower() == jd["host"].lower()
                    and int(j.get("port", 22) or 22) == jd["port"]
                    and str(j.get("user", "")) == jd["user"]):
                existing = j
                break
        if existing is None:
            state.setdefault("jump_hosts", []).append(dict(jd))
        else:
            existing["password"] = jd["password"]
            existing["auth"] = "password"
            existing["updated_at"] = now
            jd["id"] = existing["id"]  # accesses must reference the kept id

def _import_make_access_list(accs, jump_entries):
    """Access dicts for rig entries (jump referenced via jump_id), validated.
    Deterministic ids: re-import upserts the same route instead of duplicating it."""
    result = []
    for acc in accs:
        payload = {
            "id": _det_id("acc", acc["host"].lower(), acc["port"], acc["user"]),
            "name": "%s@%s" % (acc["user"], acc["host"]),
            "type": "jump" if (acc.get("jump_key") and acc["jump_key"] in jump_entries) else "direct",
            "host": acc["host"], "port": acc["port"], "user": acc["user"],
            "auth": "password", "password": acc["password"],
        }
        if payload["type"] == "jump":
            payload["jump_id"] = jump_entries[acc["jump_key"]]["id"]
        clean, err = validate_access_payload(payload)
        if clean:
            result.append(clean)
    return result

def _import_cluster_payload(state, entries, self_entry, jump_entries):
    """Full cluster snapshot to push to imported nodes."""
    rigs = []
    if isinstance(self_entry, dict) and self_entry.get("id"):
        rigs.append(self_entry)
    for e in entries:
        if e.get("id") != (self_entry or {}).get("id"):
            rigs.append(e)
    return {
        "cluster_name": state.get("cluster_name", ""),
        "rigs": rigs,
        "removed": [],
        "jump_hosts": [dict(j) for j in jump_entries.values()],
        "clusters": state.get("clusters", []),
        "from_id": state.get("self_id", ""),
    }

def _import_bootstrap_node(access, mode, pw64, st, node_entry, payload, sync_interval):
    """Teach the imported node about the whole cluster.

    Virgin node (no peers/jumps in its cluster.json): write a complete
    cluster.json + self-id sidecar directly (deterministic self_id -> idempotent).
    Established node: merge via its local API, renaming its entry to the real
    self_id so no duplicate of itself appears."""
    st["status"] = "bootstrapping"
    ok, out, err = run_ssh_command(
        access, _import_root_cmd(mode, pw64, "cat /hive-config/cluster.json 2>/dev/null || true"),
        timeout=30)
    remote = None
    if ok and out.strip():
        try:
            remote = json.loads(out)
        except Exception:
            remote = None
    virgin = not (isinstance(remote, dict)
                  and (len([r for r in remote.get("rigs", []) if isinstance(r, dict)]) > 1
                       or remote.get("jump_hosts")))
    if virgin:
        body = dict(payload)
        body["self_id"] = node_entry["id"]
        body["sync_interval"] = sync_interval
        body["rigs"] = [dict(node_entry, is_self=True)] + \
                       [r for r in payload.get("rigs", []) if r.get("id") != node_entry["id"]]
        body64 = base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")
        sid64 = base64.b64encode(str(node_entry["id"]).encode("utf-8")).decode("ascii")
        script = ("mkdir -p /hive-config && printf '%s' '" + body64 + "' | base64 -d > /hive-config/cluster.json.tmp "
                  "&& chmod 600 /hive-config/cluster.json.tmp "
                  "&& mv /hive-config/cluster.json.tmp /hive-config/cluster.json "
                  "&& printf '%s' '" + sid64 + "' | base64 -d > /hive-config/cluster-self-id "
                  "&& chmod 600 /hive-config/cluster-self-id && echo __W_OK__")
        ok, msg = _import_ssh_ok(
            run_ssh_command(access, _import_root_cmd(mode, pw64, script), timeout=60), "__W_OK__")
        if not ok:
            st["status"] = "failed"
            st["message"] = "cluster config write failed: %s" % msg
            return False
        return True
    # Established node: merge through its own API; adopt its real self_id
    remote_self_id = str(remote.get("self_id", ""))
    entry = json.loads(json.dumps(node_entry))
    entry["id"] = remote_self_id or entry["id"]
    body = dict(payload)
    body["rigs"] = [r for r in payload.get("rigs", []) if r.get("id") != node_entry["id"]] + [entry]
    # upload the payload to a temp file first: a passworded sudo may either consume
    # its password line from stdin (uncached) or pass the whole stream through
    # (cached timestamp) - only a file-based body is deterministic
    tmp_body = "/tmp/hvl-%d.json" % random.randint(100000, 999999)
    ok, msg = _import_ssh_ok(
        run_ssh_command(access, "cat > " + tmp_body, timeout=60,
                        stdin_data=json.dumps(body).encode("utf-8")))
    if not ok:
        st["status"] = "failed"
        st["message"] = "cluster merge failed: payload upload: %s" % msg
        return False
    curl_cmd = ("export PATH=\"$PATH:/hive/sbin:/usr/local/bin\"; "
                "curl -s -m 25 -X POST "
                "-H " + shlex.quote("Authorization: Bearer " + str(st.get("key", ""))) + " "
                "-H " + shlex.quote("Content-Type: application/json") + " "
                "--data-binary @" + tmp_body + " "
                "-w " + shlex.quote("\n__HC:%{http_code}") + " "
                + shlex.quote("http://127.0.0.1:%d/api/cluster/sync" % DASHBOARD_PORT) +
                "; rc=$?; rm -f " + tmp_body + "; exit $rc")
    ok, out, err = run_ssh_command(
        access, _import_root_cmd(mode, pw64, curl_cmd), timeout=60)
    if not ok:
        st["status"] = "failed"
        st["message"] = "cluster merge failed: %s" % (err or "connection failed")
        return False
    body_text, code = _parse_curl_output(out)
    if code >= 400:
        st["status"] = "failed"
        st["message"] = "cluster merge failed: HTTP %d" % code
        return False
    return True

def _import_job_snapshot(job):
    """Password-free view of a job for the UI."""
    with _import_jobs_lock:
        nodes = []
        for key in job["_order"]:
            st = job["nodes"][key]
            nodes.append({
                "name": st["name"],
                "status": st["status"],
                "message": st.get("message", ""),
                "accesses": [{k: a[k] for k in
                              ("host", "port", "user", "jump_label", "result", "error") if k in a}
                             for a in st["accesses"]],
                "action": st.get("action", ""),
                "key_state": st.get("key_state", ""),
                "rig_id": st.get("rig_id", ""),
            })
        return {"job_id": job["id"], "done": job["done"], "canceled": bool(job["cancel"]),
                "summary": job.get("summary", ""), "nodes": nodes}

def _import_process_node(job, st, jump_entries, self_password):
    """Per-node pipeline: verify every route, install/refresh the app, set up the dashboard key."""
    if job["cancel"]:
        st["status"] = "skipped"
        return
    # 1. Route test: every access must be verified from here
    st["status"] = "testing"
    good = []
    target_hostname = ""
    for acc in st["accesses"]:
        if job["cancel"]:
            break
        access = _import_access_dict(acc, jump_entries)
        ok, out, err = run_ssh_command(access, "hostname && echo __OK__", timeout=45)
        if ok and "__OK__" in out:
            acc["result"] = "ok"
            good.append(access)
            if not target_hostname:
                first_line = (out or "").replace("__OK__", "").strip().splitlines()
                target_hostname = first_line[0].strip() if first_line else ""
        else:
            acc["result"] = "fail"
            acc["error"] = (err or "connection failed")[:200]
    if not good:
        st["status"] = "failed"
        st["message"] = "No working SSH route"
        return
    if job["cancel"]:
        st["status"] = "skipped"
        return
    # 1a. The target is this very rig: never install/restart ourselves (that would
    # kill the running import job) - only merge routes and skip root steps
    if target_hostname and target_hostname == socket.gethostname().strip():
        st["is_self"] = True
        st["action"] = "self"
        st["key"] = self_password
        st["key_state"] = "self"
        st["status"] = "ready"
        st["message"] = "this rig - routes merged, no install needed"
        return
    # 1b. Root access detection: root SSH, passwordless sudo or sudo+SSH password
    mode, pw64, esc_err = _import_escalate(good[0])
    if mode is None:
        st["status"] = "failed"
        st["message"] = esc_err
        return
    st["root_mode"] = mode
    st["pw64"] = pw64
    # 2. Install / refresh the app on the node
    st["status"] = "installing"
    ok, action, msg = _import_install_node(good[0], mode, pw64)
    st["action"] = action
    if not ok:
        st["status"] = "failed"
        st["message"] = msg
        return
    if job["cancel"]:
        st["status"] = "skipped"
        return
    # 3. Dashboard key: fresh installs get the farm password, existing keep theirs
    st["status"] = "key"
    key, key_state = _import_ensure_key(good[0], mode, pw64, force_set=(action == "installed"),
                                        self_password=self_password)
    if not key:
        st["status"] = "failed"
        st["message"] = key_state
        return
    st["key"] = key
    st["key_state"] = key_state
    st["status"] = "ready"

def _cluster_import_worker(job, parsed):
    self_password = str(app.config.get('ACCESS_PASSWORD', ''))
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    now = int(time.time())

    # Jump library: merge by natural key, reuse existing ids
    jump_entries = {}
    for jd in parsed["jumps"]:
        entry = {
            "id": _det_id("jump", jd["host"].lower(), jd["port"], jd["user"]),
            "name": "jump-%s" % jd["host"],
            "host": jd["host"], "port": jd["port"], "user": jd["user"],
            "auth": "password", "password": jd["password"], "updated_at": now,
        }
        for j in state.get("jump_hosts", []):
            if (str(j.get("host", "")).lower() == jd["host"].lower()
                    and int(j.get("port", 22) or 22) == jd["port"]
                    and str(j.get("user", "")) == jd["user"]):
                entry["id"] = j.get("id") or entry["id"]
                break
        jump_entries[jd["key"]] = entry

    for node in parsed["nodes"]:
        key = node["name"].lower()
        accesses = []
        for acc in node["accesses"]:
            jk = acc.get("jump_key")
            label = "direct"
            if jk and jk in jump_entries:
                jj = jump_entries[jk]
                label = "%s:%d (%s)" % (jj["host"], jj["port"], jj["user"])
            accesses.append({"host": acc["host"], "port": acc["port"], "user": acc["user"],
                             "password": acc["password"], "jump_key": jk, "jump_label": label,
                             "result": "", "error": ""})
        job["nodes"][key] = {"name": node["name"], "status": "queued",
                             "message": "", "accesses": accesses,
                             "action": "", "key_state": "", "key": "", "rig_id": ""}
    with _import_jobs_lock:
        job["_order"] = [node["name"].lower() for node in parsed["nodes"]]

    def process_node(key):
        st = job["nodes"][key]
        try:
            _import_process_node(job, st, jump_entries, self_password)
        except Exception as e:
            logging.error(f"Cluster import: node '{st['name']}' processing error: {e}")
            st["status"] = "failed"
            st["message"] = "internal error: %s" % str(e)[:150]

    keys = list(job["nodes"].keys())
    with ThreadPoolExecutor(max_workers=max(1, min(_IMPORT_MAX_PARALLEL, len(keys)))) as pool:
        list(pool.map(process_node, keys))

    ready_keys = [k for k in keys if job["nodes"][k]["status"] == "ready"]
    if job["cancel"]:
        for k in keys:
            if job["nodes"][k]["status"] in ("queued", "testing", "installing", "key", "ready"):
                job["nodes"][k]["status"] = "skipped"
        ready_keys = [k for k in ready_keys if job["nodes"][k]["status"] == "ready"]

    # 4. Build the entries this node will own locally, then push the full state to nodes.
    # A node whose name matches an existing rig (including this rig itself) merges into
    # that entry - keeping its real id so the payload cannot duplicate it after sync.
    entries = {}
    for k in ready_keys:
        st = job["nodes"][k]
        node = next(n for n in parsed["nodes"] if n["name"].lower() == k)
        # only verified routes are stored in the entry (dead routes slow every SSH call)
        verified = [a for a in node["accesses"]
                    if any(x["host"] == a["host"] and x["port"] == a["port"]
                           and x["user"] == a["user"] and x["result"] == "ok" for x in st["accesses"])]
        new_accesses = _import_make_access_list(verified, jump_entries)
        existing = next((r for r in state["rigs"]
                         if str(r.get("name", "")).strip().lower() == st["name"].lower()), None)
        if existing is not None:
            entry = json.loads(json.dumps(existing))
            by_key = {}
            for a in entry.get("accesses", []):
                if isinstance(a, dict):
                    by_key[_access_natural_key(a)] = a
            for a in new_accesses:
                by_key[_access_natural_key(a)] = a
            entry["accesses"] = list(by_key.values())
            if entry.get("id") != state["self_id"]:
                entry["password"] = st["key"]
            entry["host_label"] = existing.get("host_label") or st["accesses"][0]["host"]
            entry.setdefault("added_at", now)
            entry["updated_at"] = now
        else:
            entry = {
                "id": _det_id("rig", k),
                "name": st["name"],
                "host_label": st["accesses"][0]["host"],
                "is_self": False,
                "password": st["key"],
                "accesses": new_accesses,
                "updated_at": now,
                "added_at": now,
            }
        entries[k] = entry
    if ready_keys:
        # the payload's self entry must be the merged one (with the new CSV routes)
        self_entry = next((r for r in state["rigs"] if r.get("id") == state["self_id"]), None)
        merged_self = next((entries[k] for k in ready_keys if entries[k].get("id") == state["self_id"]), None)
        payload = _import_cluster_payload(state, [entries[k] for k in ready_keys],
                                          merged_self or self_entry, jump_entries)

        def bootstrap_node(key):
            st = job["nodes"][key]
            try:
                if job["cancel"]:
                    st["status"] = "skipped"
                    return
                if entries[key].get("id") == state["self_id"]:
                    st["status"] = "done"
                    st["message"] = (st.get("message", "") + "; this rig (nothing to bootstrap)").strip("; ")
                    return
                # reuse the first verified access of this node
                access = None
                for acc in st["accesses"]:
                    if acc["result"] == "ok":
                        access = _import_access_dict(acc, jump_entries)
                        break
                if access is None:
                    st["status"] = "failed"
                    st["message"] = "no verified route left for bootstrap"
                    return
                if not _import_bootstrap_node(access, st.get("root_mode", "root"),
                                              st.get("pw64", ""), st, entries[key], payload,
                                              state.get("sync_interval", DEFAULT_SYNC_INTERVAL)):
                    return
                st["status"] = "done"
                st["message"] = "cluster config %s" % ("merged" if st.get("key_state") == "kept" else "written")
            except Exception as e:
                logging.error(f"Cluster import: node '{st['name']}' bootstrap error: {e}")
                st["status"] = "failed"
                st["message"] = "internal error: %s" % str(e)[:150]

        with ThreadPoolExecutor(max_workers=max(1, min(_IMPORT_MAX_PARALLEL, len(ready_keys)))) as pool:
            list(pool.map(bootstrap_node, ready_keys))

    # 5. Local upsert on this node + one sync cycle to propagate everywhere
    done_keys = [k for k in ready_keys if job["nodes"][k]["status"] == "done"]
    for k in ready_keys:
        st = job["nodes"][k]
        if st["status"] == "bootstrapping":
            st["status"] = "failed"
            st["message"] = "bootstrap interrupted"
    if done_keys or job["cancel"]:
        state = load_cluster_state()
        _CURRENT_SELF_ID["value"] = state["self_id"]
        now = int(time.time())
        _import_upsert_jump_entries(state, jump_entries)
        for k in ready_keys:
            st = job["nodes"][k]
            existing = next((r for r in state["rigs"]
                             if str(r.get("name", "")).strip().lower() == st["name"].lower()), None)
            node = next(n for n in parsed["nodes"] if n["name"].lower() == k)
            verified = [a for a in node["accesses"]
                        if any(x["host"] == a["host"] and x["port"] == a["port"]
                               and x["user"] == a["user"] and x["result"] == "ok" for x in st["accesses"])]
            new_accesses = _import_make_access_list(verified, jump_entries)
            if existing is not None:
                by_key = {}
                for a in existing.get("accesses", []):
                    if isinstance(a, dict):
                        by_key[_access_natural_key(a)] = a
                for a in new_accesses:
                    by_key[_access_natural_key(a)] = a
                existing["accesses"] = list(by_key.values())
                if existing.get("id") != state["self_id"]:
                    existing["password"] = st["key"]
                existing.setdefault("added_at", now)
                existing["updated_at"] = now
                if not existing.get("host_label"):
                    existing["host_label"] = st["accesses"][0]["host"]
                st["rig_id"] = existing["id"]
                st["message"] = (st["message"] + "; merged into existing rig entry").strip("; ")
            else:
                state["rigs"].append(entries[k])
                st["rig_id"] = entries[k]["id"]
        save_cluster_state(state)
        if done_keys and not job["cancel"]:
            try:
                run_sync_cycle(triggered_by="import")
            except Exception as e:
                logging.error(f"Cluster import: post-import sync failed: {e}")

    added = len([k for k in ready_keys if job["nodes"][k]["status"] == "done"])
    failed = len([k for k in keys if job["nodes"][k]["status"] == "failed"])
    skipped = len([k for k in keys if job["nodes"][k]["status"] == "skipped"])
    job["summary"] = ("%d added/merged, %d failed, %d skipped" % (added, failed, skipped)
                      if job["cancel"] and skipped else
                      "%d added/merged, %d failed" % (added, failed))
    job["done"] = True
    job["finished_at"] = int(time.time())
    logging.info(f"Cluster import job {job['id']}: {job['summary']}")

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

# ---------------- Metrics history (worker Stats tab) ----------------
# Samples the live stats once a minute into /hive-config/metrics_history.json
# (per-day samples + Activity events + electricity rate) so the dashboard can
# draw the HiveOS-style Statistics charts (1d/3d).

METRICS_PATH = os.path.join(HIVE_CONFIG_DIR, "metrics_history.json")
METRICS_SAMPLE_INTERVAL = 60      # seconds between samples
METRICS_MAX_DAYS = 4              # keep the 3d view + one buffer day
METRICS_MAX_EVENTS = 600
_METRICS_EVENT_LEVELS = ("info", "file", "danger", "warning", "success")
_metrics_last_miner_running = {"value": None}

def _metrics_day_key(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))

def _load_metrics_store():
    try:
        if os.path.exists(METRICS_PATH):
            with open(METRICS_PATH, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("days", {})
                data.setdefault("events", [])
                data.setdefault("rate", 0.0)
                data.setdefault("meta", {})
                return data
    except Exception as e:
        logging.error(f"Failed to read metrics history: {e}")
    return {"days": {}, "events": [], "rate": 0.0, "meta": {}}

def _save_metrics_store(store):
    try:
        tmp_path = METRICS_PATH + ".tmp"
        with config_lock:
            with open(tmp_path, 'w') as f:
                json.dump(store, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, METRICS_PATH)
        return True
    except Exception as e:
        logging.error(f"Failed to save metrics history: {e}")
        return False

def record_metrics_event(level, message, ts=None):
    """Append an event to the Activity feed of the worker Statistics tab."""
    try:
        if level not in _METRICS_EVENT_LEVELS:
            level = "info"
        store = _load_metrics_store()
        events = store.get("events", [])
        events.append({"ts": int(ts if ts is not None else time.time()),
                       "level": level, "message": str(message)[:300]})
        cutoff = time.time() - METRICS_MAX_DAYS * 86400
        store["events"] = [e for e in events if isinstance(e, dict) and e.get("ts", 0) >= cutoff][-METRICS_MAX_EVENTS:]
        _save_metrics_store(store)
    except Exception as e:
        logging.error(f"Failed to record metrics event: {e}")

def _metrics_take_sample():
    """Collect one stats sample into the history store; miner start/stop
    transitions emit Activity events. Returns True when a sample was stored."""
    try:
        payload = collect_stats_payload()
    except Exception as e:
        logging.error(f"Metrics sampler: failed to collect stats: {e}")
        return False
    gpus = payload.get("gpus") or []
    if not gpus:
        return False
    ts = int(time.time())
    temps = [int(g.get("temp", 0) or 0) for g in gpus]
    fans = [int(g.get("fan", 0) or 0) for g in gpus]
    powers = [round(float(g.get("power", 0) or 0), 1) for g in gpus]
    hashrates = [round(float(g.get("hashrate", 0) or 0), 2) for g in gpus]
    total_w = round(sum(powers), 1)
    total_mh = round(float(payload.get("total_hashrate_mh", 0) or 0), 2)
    sample = [ts, temps, fans, powers, hashrates, total_w, total_mh]

    store = _load_metrics_store()
    days = store.setdefault("days", {})
    day = days.setdefault(_metrics_day_key(ts), {"samples": []})
    samples = day.setdefault("samples", [])
    if not samples or samples[-1][0] < ts - METRICS_SAMPLE_INTERVAL - 5:
        samples.append(sample)
    store["meta"] = {"algo": str(payload.get("miner_algo") or ""), "gpu_count": len(gpus)}
    cutoff_key = _metrics_day_key(ts - METRICS_MAX_DAYS * 86400)
    for k in [k for k in list(days.keys()) if k < cutoff_key]:
        del days[k]
    cutoff = ts - METRICS_MAX_DAYS * 86400
    store["events"] = [e for e in store.get("events", []) if isinstance(e, dict) and e.get("ts", 0) >= cutoff]
    _save_metrics_store(store)

    # Miner state transitions -> Activity events (skip the very first check)
    running = bool((payload.get("system") or {}).get("miner_running"))
    prev = _metrics_last_miner_running["value"]
    if prev is not None and prev != running:
        record_metrics_event("success" if running else "warning",
                             "Miner started" if running else "Miner stopped", ts=ts)
    _metrics_last_miner_running["value"] = running
    return True

def _metrics_sampler_worker():
    time.sleep(10)
    while True:
        try:
            _metrics_take_sample()
        except Exception as e:
            logging.error(f"Metrics sampler error: {e}")
        time.sleep(METRICS_SAMPLE_INTERVAL)

def start_metrics_sampler():
    t = threading.Thread(target=_metrics_sampler_worker, daemon=True, name="metrics-sampler")
    t.start()
    logging.info("Metrics sampler started")

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
    # Per-GPU list values (space-separated, e.g. "280 280 100"): validate each
    # token as a scalar of the same field ("0" token = "leave that GPU unchanged")
    list_lens = [len(str(v).split()) for v in data.values()
                 if isinstance(v, str) and len(str(v).split()) > 1]
    if list_lens:
        for tok_idx in range(max(list_lens)):
            sub = {}
            for k, v in data.items():
                if isinstance(v, str) and len(v.split()) > 1:
                    toks = v.split()
                    sub[k] = toks[tok_idx] if tok_idx < len(toks) else "0"
                else:
                    sub[k] = v
            is_valid, err = validate_overclock_ranges(brand, sub)
            if not is_valid:
                return False, err
        return True, ""
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

            if "lcore" in data and str(data["lcore"]).strip() not in ("", "0"):
                val = int(data["lcore"])
                if not (500 <= val <= 3000):
                    return False, "NVIDIA fixed core clock must be between 500 and 3000 MHz."

            if "lmem" in data and str(data["lmem"]).strip() not in ("", "0"):
                val = int(data["lmem"])
                if not (0 <= val <= 20000):
                    return False, "NVIDIA lock memory clock must be between 0 and 20000 MHz."

            if "delay" in data and str(data["delay"]).strip() not in ("", "0"):
                val = int(data["delay"])
                if not (0 <= val <= 3600):
                    return False, "NVIDIA OC apply delay must be between 0 and 3600 seconds."

            for flag in ("led", "p0", "idle", "pill"):
                if flag in data and str(data[flag]).strip() not in ("", "0", "1"):
                    return False, f"NVIDIA {flag} flag must be 0 or 1."
                    
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

# Serialize nvidia-oc invocations: the hive script kills any concurrently running
# instance, so overlapping applies (rapid Apply clicks, autofan + OC in the same
# cycle) interrupt each other mid-run and locked clocks silently never take effect
nvidia_oc_lock = threading.Lock()

def run_nvidia_oc():
    """Run /hive/sbin/nvidia-oc exclusively; returns (stdout, stderr, returncode)."""
    with nvidia_oc_lock:
        return run_command("sudo /hive/sbin/nvidia-oc")

def _nvtool_lock_confirmed(gpu_index, expected):
    """True when the driver's locked-clock registration equals `expected`.

    nvtool --setclocks is idempotent: re-setting the value that is already
    locked reports 'was already set'. First call says 'already set' ->
    registered. First call claims to apply (lock was missing) -> one
    re-assert must confirm it stuck; a silently failed change prints
    'SET ... MHz' twice and never 'already set'. Live clocks.sm cannot
    serve as the only confirmation: at idle the mobile driver floats clocks
    below (or above) the locked range when no load is present, which read
    as false 'not confirmed' after a fresh boot (RIG9: applied 1350 while
    the miner was stopped, all 8 GPUs reported 1110-1275; under load the
    same lock reads exactly 1350)."""
    for _ in range(2):
        stdout, _, code = run_command(
            f"sudo timeout 10 nvtool -q --nodev -i {gpu_index} --setclocks {int(expected)}")
        if code != 0:
            return False
        if "was already set" in stdout:
            return True
        # claimed to apply (was missing) — re-assert once to prove it stuck;
        # a silently failed change prints 'SET ... MHz' again, never 'already set'
    return False

def verify_locked_clocks(expected):
    """Check actual SM clocks against {gpu_index: locked_mhz}. Returns a dict of
    {gpu_index: (expected, actual)} mismatches. Empty dict on verification failure
    (no nvidia-smi output) so callers can skip gracefully. Ampere snaps locked
    clocks to a ~15 MHz grid (lock 1300 reads back 1305), so a small tolerance
    counts as confirmed; anything outside the tolerance is double-checked
    against the driver's lock registration (nvtool idempotence) before it
    counts as a real mismatch — idle GPUs legitimately float off the lock.
    Tolerance is 16 MHz: the ~15 MHz grid snap must pass, but a silently
    failed lock CHANGE (old lock still active, e.g. 1350 vs expected 1320 =
    30 MHz) must NOT pass — with the wider 32 MHz tolerance such drift was
    swallowed as 'confirmed' and the apply reported success while the old
    lock stayed active (observed on RIG9: 1350->1320 round trip left the old
    lock in place; the nvtool re-assert below both detects and self-heals
    that case)."""
    stdout, _, _ = run_command("nvidia-smi --query-gpu=index,clocks.sm --format=csv,noheader,nounits")
    actual = {}
    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            actual[int(parts[0])] = safe_int(parts[1])
    if not actual:
        return {}
    mismatch = {}
    for i, v in expected.items():
        act = actual.get(i)
        if act is not None and abs(act - v) <= 16:
            continue
        if _nvtool_lock_confirmed(i, v):
            continue
        mismatch[i] = (v, act)
    return mismatch

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
            # Locked clocks: explicit "0" is stored in the conf (cloud parity —
            # keeps per-GPU positions in the space-separated list); the UI shows
            # unlocked GPUs as blank
            "lcore": [v if v != "0" else "" for v in nv_data.get("LCLOCK", "").split()],
            "lmem": [v if v != "0" else "" for v in nv_data.get("LMEM", "").split()],
            # Rig-wide HiveOS flags (cloud OC modal parity)
            "delay": nv_data.get("RUNNING_DELAY", ""),
            "led": "1" if nv_data.get("LOGO_BRIGHTNESS", "") == "0" else "0",
            "p0": "1" if nv_data.get("FORCESTATE", "") == "1" else "0",
            "idle": "1" if nv_data.get("POWERMIZER", "") == "2" else "0",
            "pill": "1" if nv_data.get("OHGODAPILL_ENABLED", "") == "1" else "0"
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

def _apply_nvidia_oc(data, apply_all, gpu_index):
    """NVIDIA overclock apply shared by /api/overclock and OC presets (all-GPU
    form values). Writes nvidia-oc.conf, runs /hive/sbin/nvidia-oc and verifies
    locked clocks (retry + nvidia-smi -lgc fallback). Returns (ok, message);
    an empty message means a clean apply."""
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

    if apply_all:
        # Target every NVIDIA GPU on this rig (fallback: current conf length)
        try:
            gpus = get_gpu_stats().get("gpus", [])
            n = sum(1 for g in gpus if g.get("brand") == "NVIDIA")
        except Exception:
            n = 0
        if n == 0:
            n = max(len(clock), len(mem), len(plimit), len(fan), 1)
        max_idx = max(3, n - 1)
    else:
        max_idx = max(3, gpu_index)
    clock += ["0"] * (max_idx + 1 - len(clock))
    mem += ["0"] * (max_idx + 1 - len(mem))
    plimit += ["0"] * (max_idx + 1 - len(plimit))
    fan += ["0"] * (max_idx + 1 - len(fan))
    # Locked clocks keep explicit "0" for unlocked GPUs (cloud parity):
    # empty entries would vanish in hive's word-splitting of the conf list
    # and shift every following per-GPU value one position left
    lclock += ["0"] * (max_idx + 1 - len(lclock))
    lmem += ["0"] * (max_idx + 1 - len(lmem))

    def _apply_values(field_key, lst, transform=None):
        """Single-GPU mode: set one index (empty offset = no change; locked
        clocks store explicit "0"). All-GPUs mode: an empty value is skipped
        (per-GPU differences are preserved), a filled value goes to every GPU,
        and a space-separated list sets tokens per GPU index ("0"/empty token
        = leave that GPU's current value untouched)."""
        if field_key not in data:
            return
        raw = str(data[field_key]).strip()
        if apply_all:
            tokens = raw.split()
            if len(tokens) > 1:
                for i, t in enumerate(tokens):
                    if i >= len(lst) or t in ("", "0"):
                        continue
                    lst[i] = transform(t) if transform else t
                return
            if raw == "":
                return
            val = transform(raw) if transform else raw
            for i in range(len(lst)):
                lst[i] = val
        else:
            # Empty offset on a single GPU = "no change" (same as all-GPU mode);
            # writing "" would leave a hole in the conf list that hive's
            # word-splitting drops, shifting every following per-GPU value.
            # Locked clocks keep their explicit ""->"0" transform (clear lock).
            if raw == "" and transform is None:
                return
            lst[gpu_index] = transform(raw) if transform else raw

    _apply_values("core", clock)
    _apply_values("mem", mem)
    _apply_values("pl", plimit)
    _apply_values("fan", fan)
    # Optional locked clocks (absolute values, HiveOS-style LCLOCK/LMEM);
    # cleared locks are stored as explicit "0" to preserve list positions
    _apply_values("lcore", lclock, lambda v: "0" if str(v).strip() in ("", "0") else v)
    _apply_values("lmem", lmem, lambda v: "0" if str(v).strip() in ("", "0") else v)

    # Rig-wide flags, HiveOS nvidia-oc.conf semantics (cloud OC modal parity):
    # RUNNING_DELAY = apply delay, LOGO_BRIGHTNESS 0 = LEDs off,
    # FORCESTATE 1 = force P0, POWERMIZER 2 = idle power reduction,
    # OHGODAPILL_* = "tablet" for GDDR5X cards
    if "delay" in data:
        config["RUNNING_DELAY"] = "" if str(data["delay"]).strip() in ("", "0") else str(data["delay"]).strip()
    if "led" in data:
        config["LOGO_BRIGHTNESS"] = "0" if str(data["led"]).strip() == "1" else ""
    if "p0" in data:
        config["FORCESTATE"] = "1" if str(data["p0"]).strip() == "1" else ""
    if "idle" in data:
        config["POWERMIZER"] = "2" if str(data["idle"]).strip() == "1" else "1"
    if "pill" in data:
        config["OHGODAPILL_ENABLED"] = "1" if str(data["pill"]).strip() == "1" else ""
        config["OHGODAPILL_START_TIMEOUT"] = ""
        config["OHGODAPILL_ARGS"] = ""

    config.pop("CORE", None)  # legacy key written by older versions
    config.pop("PL", None)
    config["CLOCK"] = " ".join(clock)
    config["MEM"] = " ".join(mem)
    config["PLIMIT"] = " ".join(plimit)
    config["FAN"] = " ".join(fan)
    config["LCLOCK"] = " ".join(lclock)
    config["LMEM"] = " ".join(lmem)

    if not write_shell_config(filepath, config):
        return False, "Failed to write nvidia-oc.conf"
    target_label = "all GPUs" if apply_all else f"GPU {gpu_index}"
    logging.info(f"NVIDIA {target_label} parameters updated: Clock={data.get('core')}, Mem={data.get('mem')}, PL={data.get('pl')}, Fan={data.get('fan')}, LCLOCK={data.get('lcore')}, LMEM={data.get('lmem')}, Delay={data.get('delay')}, LED={data.get('led')}, P0={data.get('p0')}, Idle={data.get('idle')}, Pill={data.get('pill')}")

    stdout, stderr, code = run_nvidia_oc()
    if code != 0:
        logging.error(f"NVIDIA OC script failed: {stderr}")
        return False, "NVIDIA overclock script failed to apply settings."

    # Route/manual overclock edit: no preset can claim these values anymore
    # (the OC preset appliers re-set their own marker right after this call)
    _set_oc_applied("")

    # Verify locked clocks actually took effect: under full mining load nvtool
    # calls can silently fail, so retry the whole apply up to 2 more times
    expected_locks = {}
    for i, v in enumerate(lclock):
        try:
            if int(str(v).strip()) > 0:
                expected_locks[i] = int(str(v).strip())
        except ValueError:
            continue
    if expected_locks:
        mismatch = verify_locked_clocks(expected_locks)
        for _ in range(2):
            if not mismatch:
                break
            logging.warning(f"Locked clocks not confirmed {mismatch}, retrying nvidia-oc")
            time.sleep(3)
            run_nvidia_oc()
            mismatch = verify_locked_clocks(expected_locks)
        if mismatch:
            # nvtool (used inside hive's nvidia-oc) silently fails to move locked
            # clocks while the miner runs at full load; plain nvidia-smi -lgc still
            # works there (observed on laptop rigs) — last-resort fallback
            def _smi_lock_fallback(bad):
                for i, (exp, _act) in sorted(bad.items()):
                    run_command(f"sudo nvidia-smi -i {i} -lgc {exp},{exp}")

            logging.warning(f"Locked clocks still unconfirmed {mismatch}, falling back to nvidia-smi -lgc")
            _smi_lock_fallback(mismatch)
            time.sleep(2)
            mismatch = verify_locked_clocks(expected_locks)
            for _ in range(2):
                if not mismatch:
                    break
                time.sleep(3)
                _smi_lock_fallback(mismatch)
                mismatch = verify_locked_clocks(expected_locks)
        if mismatch:
            bad = ", ".join(f"GPU {i} ({exp} vs {act})" for i, (exp, act) in sorted(mismatch.items()))
            logging.warning(f"NVIDIA locked clock verification failed: {bad}")
            return True, f"Saved, but locked clock not confirmed on: {bad}. Try applying again or check nvidia-smi."

    return True, ""

@app.route('/api/overclock', methods=['POST'])
def save_overclock():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid JSON payload"}), 400
        
    brand = data.get("brand", "").upper()
    # gpu:"all" = apply the same values to every NVIDIA GPU (like the fan "all" row)
    apply_all = str(data.get("gpu", "")).strip().lower() == "all"
    gpu_index = None
    if not apply_all:
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
        if key not in ["brand", "index", "gpu"]:
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
        ok, message = _apply_nvidia_oc(data, apply_all, gpu_index)
        if not ok:
            return jsonify({"success": False, "message": message})
        if not message:
            message = f"Overclock parameters successfully saved and applied to NVIDIA {'all GPUs' if apply_all else f'GPU {gpu_index}'}!"
        record_metrics_event("info", f"Overclock applied: NVIDIA {'all GPUs' if apply_all else f'GPU {gpu_index}'}")
        return jsonify({"success": True, "message": message})

    # brand == "AMD" (validated above)
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

    record_metrics_event("info", f"Overclock applied: AMD {'all GPUs' if apply_all else f'GPU {gpu_index}'}")
    return jsonify({"success": True, "message": f"Overclock parameters successfully saved and applied to AMD {'all GPUs' if apply_all else f'GPU {gpu_index}'}!"})

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
            run_nvidia_oc()
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
        record_metrics_event("success", {"start": "Miner started", "stop": "Miner stopped",
                                         "restart": "Miner restarted"}.get(action, f"Miner {action}ed"))
        return jsonify({"success": True, "message": msg})
    else:
        logging.error(f"Miner control command failed: {output}")
        detail = output.splitlines()[-1] if output else "Unknown error"
        return jsonify({"success": False, "message": f"Miner command failed: {detail}"}), 500

# 1. System Power Routes (Reboot / Shutdown)
@app.route('/api/system/reboot', methods=['POST'])
def system_reboot():
    logging.info(f"System reboot requested by IP: {request.remote_addr}")
    record_metrics_event("warning", "Reboot initiated from panel")
    cmd = 'nohup bash -c "sleep 1.5 && sudo /hive/sbin/sreboot" > /dev/null 2>&1 &'
    subprocess.Popen(cmd, shell=True)
    return jsonify({"success": True, "message": "Reboot command initiated. Rig will restart shortly."})

@app.route('/api/system/shutdown', methods=['POST'])
def system_shutdown():
    logging.info(f"System shutdown requested by IP: {request.remote_addr}")
    record_metrics_event("danger", "Shutdown initiated from panel")
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

    # Custom-miner flight sheets run as MINER=custom with the real package name
    # in wallet.conf (CUSTOM_MINER); hive's wrapper logs to
    # /var/log/miner/custom/<package>.log (CUSTOM_LOG_BASENAME.log)
    if miner == "custom":
        wallet_conf = parse_shell_config(WALLET_CONF_PATH)
        custom = (wallet_conf.get("CUSTOM_MINER") or "").strip().lower()
        if custom and re.match(r'^[a-z0-9_\-]{1,64}$', custom):
            miner = custom  # show the real package name in the UI log title
            log_candidates = [
                f"/var/log/miner/custom/{custom}.log",
                f"/var/log/miner/custom/{custom}/lastrun_noappend.log",
                f"/var/log/miner/custom/{custom}/lastrun.log",
            ] + log_candidates

    log_content = ""
    found_path = None
    allowed_base = os.path.abspath("/var/log/miner")
    for p in log_candidates:
        p_abs = os.path.abspath(p)
        # Verify candidate log resides strictly inside allowed log folder path to satisfy CodeQL
        if p_abs.startswith(allowed_base + os.sep):
            if os.path.isfile(p_abs):
                found_path = p_abs
                break

    if not found_path and os.path.isdir(allowed_base):
        # Fallback: newest .log file anywhere under /var/log/miner (custom
        # wrappers may log to their own basename; rotated archives are .gz)
        try:
            newest = None
            for root, _dirs, files in os.walk(allowed_base):
                for fn in files:
                    if not fn.endswith(".log"):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        mt = os.path.getmtime(fp)
                    except OSError:
                        continue
                    if newest is None or mt > newest[0]:
                        newest = (mt, fp)
            if newest:
                found_path = newest[1]
        except Exception as e:
            logging.error(f"Miner log fallback scan failed: {e}")

    if not found_path:
        return jsonify({"success": False, "message": f"Log file for miner '{miner}' not found. Verify miner is running."}), 404

    try:
        tail_lines = deque(maxlen=150)
        with open(found_path, 'r', errors='ignore') as f:
            for line in f:
                tail_lines.append(line)
        log_content = "".join(tail_lines)
    except Exception as e:
        logging.error(f"Error reading miner log {found_path}: {e}")
        return jsonify({"success": False, "message": "Failed to read miner log file."}), 500

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
    # CUSTOM_STATIC_FAN keeps the static speeds typed in the advanced editor for
    # GPUs that are currently in auto mode (FAN only holds speeds for static GPUs)
    static_saved = _af_pad(_af_int_list(conf, "CUSTOM_STATIC_FAN"), n or 1)
    mode_vals = _af_pad(_af_int_list(conf, "CUSTOM_MODE"), n or 1)
    pergpu = []
    for i in range(n):
        is_static = mode_vals[i] == "1"
        static_val = fan_vals[i] if (is_static and fan_vals[i].isdigit()) else \
            (static_saved[i] if static_saved[i].isdigit() else "0")
        pergpu.append({
            "index": gpus[i].get("index"),
            "mode": int(mode_vals[i]) if mode_vals[i].isdigit() else 0,
            "static": int(static_val) if static_val.isdigit() else 0,
            "min": _af_per_gpu(conf, "CUSTOM_MIN_FAN", n)[i],
            "max": _af_per_gpu(conf, "CUSTOM_MAX_FAN", n)[i],
            "target_core": _af_per_gpu(conf, "CUSTOM_TARGET_TEMP", n)[i],
            "target_mem": _af_per_gpu(conf, "CUSTOM_TARGET_MEM_TEMP", n)[i],
            "critical": _af_per_gpu(conf, "CUSTOM_CRITICAL_TEMP", n)[i],
        })
    # Empty scalars fall back to defaults: the key may exist with "" in a conf
    # that was never pushed from the Hive cloud (e.g. RIG9) — the read-only
    # Critical temp field would otherwise show blank. When every GPU carries
    # the same CUSTOM_* override it wins over the stored scalar (the global is
    # only a fallback for GPUs without an override, e.g. RIG1: global 65,
    # CUSTOM all 67 -> the effective 67 must be shown)
    def _effective_global(key, current):
        vals = [v for v in (_af_per_gpu(conf, key, n) if n else []) if v is not None]
        if n and len(vals) == n and len(set(vals)) == 1:
            return str(vals[0])
        return current

    return jsonify({
        "success": True,
        "enabled": conf.get("ENABLED", "0"),
        "target_temp": _effective_global("CUSTOM_TARGET_TEMP", conf.get("TARGET_TEMP") or "60"),
        "target_mem_temp": _effective_global("CUSTOM_TARGET_MEM_TEMP", conf.get("TARGET_MEM_TEMP") or "90"),
        "min_fan": _effective_global("CUSTOM_MIN_FAN", conf.get("MIN_FAN") or "30"),
        "max_fan": _effective_global("CUSTOM_MAX_FAN", conf.get("MAX_FAN") or "100"),
        "critical_temp": _effective_global("CUSTOM_CRITICAL_TEMP", conf.get("CRITICAL_TEMP") or "70"),
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
        static = 0
        try:
            static = int(g.get("static", 0) or 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "message": f"GPU {idx}: static fan speed is invalid."}), 400
        if mode == "static":
            if not (1 <= static <= 100):
                return jsonify({"success": False, "message": f"GPU {idx}: static fan speed must be 1-100%."}), 400
        elif not (0 <= static <= 100):
            return jsonify({"success": False, "message": f"GPU {idx}: static fan speed must be 0-100%."}), 400
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
    # CUSTOM_STATIC_FAN (ignored by the autofan daemon) keeps the static speeds
    # typed in the advanced editor for ALL GPUs so the values stick even for
    # GPUs currently in auto mode and are re-used when a GPU switches to static
    conf["CUSTOM_STATIC_FAN"] = " ".join(str((order.get(i) or {"static": 0}).get("static", 0) or 0) for i in range(n))
    # Sync the global scalars with the per-GPU lists: when every GPU uses the
    # same value it becomes the global (a stale one would keep showing in the
    # read-only UI and mislead, e.g. crit 65 while all GPUs run 67). With mixed
    # values the stored global is kept as-is (it is only a fallback for GPUs
    # whose CUSTOM_* entry is 0/empty). Empty conf globals also get backfilled
    # here (a rig whose autofan.conf was never pushed from the Hive cloud)
    for key, conf_key in (("min", "MIN_FAN"), ("max", "MAX_FAN"),
                          ("target_core", "TARGET_TEMP"), ("target_mem", "TARGET_MEM_TEMP"),
                          ("critical", "CRITICAL_TEMP")):
        vals = [order.get(i, {}).get(key, 0) for i in range(n)]
        if vals and all(v == vals[0] and v != 0 for v in vals):
            conf[conf_key] = str(vals[0])
    if not write_shell_config(AUTOFAN_CONF, conf):
        return jsonify({"success": False, "message": "Failed to write autofan.conf"}), 500

    # FAN keeps only the speeds of GPUs currently in static mode (the daemon
    # reads it for CUSTOM_MODE=1 rows; 0 releases the fan to driver/daemon control)
    oc = parse_shell_config(NVIDIA_OC_CONF)
    oc["FAN"] = " ".join(str(p["static"] if p["mode"] == "static" else 0) for p in
                         (order.get(i) or {"mode": "auto", "static": 0} for i in range(n)))
    if not write_shell_config(NVIDIA_OC_CONF, oc):
        return jsonify({"success": False, "message": "Failed to write nvidia-oc.conf"}), 500
    run_nvidia_oc()

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
        run_nvidia_oc()

    label = "all GPUs" if gpu_index is None else f"GPU #{gpu_index}"
    logging.info(f"AutoFan {label} -> {mode}" +
                 (f" {static_speed}%" if static_speed is not None else "") +
                 (f", {fields}" if fields else "") + f" by IP: {request.remote_addr}")
    msg = (f"Fan set to auto on {label}." if mode == "auto" else f"Fan speed set to {static_speed}% on {label}.")
    return jsonify({"success": True, "message": msg})

# 5b. Algo-bound GPU overclock presets (HiveOS "OC per algorithm" parity).
# An OC preset holds the all-GPU overclock form values (the same fields as the
# rig's "Set settings for all GPUs" card) and can be bound to a mining algorithm
# or marked as the default. When the rig switches to an algo (flight sheet
# apply), the preset bound to that algo applies automatically
# (_auto_switch_oc_for_algo); when no binding matches, the default preset is used.
OC_FORM_FIELDS = ("core", "lcore", "mem", "lmem", "pl", "fan", "delay")
OC_FLAGS = ("led", "p0", "idle", "pill")


def _conf_to_form_values(nv_conf):
    """Collapse per-GPU nvidia-oc.conf lists into all-GPU form fields: a field
    is filled only when every GPU carries the same non-zero value."""
    def uniform(raw):
        vals = [v for v in str(raw or "").split() if v and v != "0"]
        return vals[0] if vals and len(set(vals)) == 1 else ""
    return {
        "core": uniform(nv_conf.get("CLOCK", "")),
        "lcore": uniform(nv_conf.get("LCLOCK", "")),
        "mem": uniform(nv_conf.get("MEM", "")),
        "lmem": uniform(nv_conf.get("LMEM", "")),
        "pl": uniform(nv_conf.get("PLIMIT", "")),
        "fan": uniform(nv_conf.get("FAN", "")),
        "delay": str(nv_conf.get("RUNNING_DELAY", "") or ""),
        "led": "1" if nv_conf.get("LOGO_BRIGHTNESS", "") == "0" else "0",
        "p0": "1" if nv_conf.get("FORCESTATE", "") == "1" else "0",
        "idle": "1" if nv_conf.get("POWERMIZER", "") == "2" else "0",
        "pill": "1" if nv_conf.get("OHGODAPILL_ENABLED", "") == "1" else "0",
    }


def _load_oc_presets():
    """Store entries normalized to the form-values model (migrates the
    short-lived full-conf snapshots of 1.10.26 in place)."""
    raw = [p for p in _load_json_store(OC_PRESETS_PATH, []) if p.get("id")]
    changed = False
    for p in raw:
        if "values" not in p:
            p["values"] = _conf_to_form_values(p.get("nvidia", {}))
            p.pop("nvidia", None)
            p.pop("amd", None)
            p.setdefault("algo", "")
            p.setdefault("is_default", False)
            changed = True
    if changed:
        _save_json_store(OC_PRESETS_PATH, raw)
    return raw


def _live_oc_form_values():
    """Current rig overclock collapsed to the all-GPU form fields (form prefill)."""
    return _conf_to_form_values(parse_shell_config(NVIDIA_OC_CONF))


def _load_oc_state():
    try:
        with config_lock:
            if os.path.exists(OC_STATE_PATH):
                with open(OC_STATE_PATH, 'r') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logging.error(f"Failed to read OC preset state: {e}")
    return {}


def _set_oc_applied(pid):
    """Remember which OC preset is currently applied on the rig — single ACTIVE
    semantics: only one preset can be the live one, and any manual overclock
    edit (route-driven _apply_nvidia_oc) clears the marker."""
    state = _load_oc_state()
    if state.get("applied_id", "") == pid:
        return
    state["applied_id"] = pid
    try:
        with config_lock:
            with open(OC_STATE_PATH, 'w') as f:
                json.dump(state, f)
            os.chmod(OC_STATE_PATH, 0o600)
    except Exception as e:
        logging.error(f"Failed to write OC preset state: {e}")


def _oc_matches_live(values):
    """True when the live nvidia-oc.conf matches the preset's form values.
    Empty preset clock fields are wildcards (apply leaves them unchanged);
    flags and delay compare strictly."""
    nv = parse_shell_config(NVIDIA_OC_CONF)

    def uniform(key):
        vals = [v for v in str(nv.get(key, "") or "").split() if v and v != "0"]
        return vals[0] if vals and len(set(vals)) == 1 else ""

    for field, key in (("core", "CLOCK"), ("lcore", "LCLOCK"), ("mem", "MEM"),
                       ("lmem", "LMEM"), ("pl", "PLIMIT"), ("fan", "FAN")):
        want = str(values.get(field, "") or "").strip()
        if not want or want == "0":
            continue
        tokens = want.split()
        if len(tokens) > 1:
            # per-GPU list: every non-zero token must match the live conf at
            # the same index; zero tokens are wildcards (untouched on apply)
            live = str(nv.get(key, "") or "").split()
            for i, t in enumerate(tokens):
                if t in ("", "0"):
                    continue
                if i >= len(live) or live[i] != t:
                    return False
        elif uniform(key) != want:
            return False
    want_delay = str(values.get("delay", "") or "").strip()
    if want_delay and want_delay != "0":
        if str(nv.get("RUNNING_DELAY", "") or "").strip() != want_delay:
            return False
    flag_checks = (("led", "LOGO_BRIGHTNESS", "0"), ("p0", "FORCESTATE", "1"),
                   ("idle", "POWERMIZER", "2"), ("pill", "OHGODAPILL_ENABLED", "1"))
    for flag, key, on_val in flag_checks:
        if (str(values.get(flag, "0")) == "1") != (nv.get(key, "") == on_val):
            return False
    return True


def _apply_oc_preset_values(values):
    """Push an OC preset to hardware: values may be uniform scalars (expanded to
    every GPU) or space-separated per-GPU lists ("0" token = leave that GPU's
    current value). Flags and delay are rig-wide. Empty clock fields are left
    unchanged. Conf write, nvidia-oc run and locked-clock verification included."""
    payload = {}
    for field in OC_FORM_FIELDS:
        v = str(values.get(field, "") or "").strip()
        if field == "delay":
            payload[field] = v
        elif v and v != "0":
            payload[field] = " ".join(v.split())
    for flag in OC_FLAGS:
        payload[flag] = "1" if str(values.get(flag, "0")) == "1" else "0"
    is_valid, err = validate_overclock_ranges("NVIDIA", payload)
    if not is_valid:
        return False, err
    return _apply_nvidia_oc(payload, True, None)


def _auto_switch_oc_for_algo(algo):
    """Apply the OC preset bound to `algo`, falling back to the default preset
    when no binding matches. Returns a message suffix for the caller's toast,
    or None when nothing was applied / the chosen preset is already live."""
    presets = _load_oc_presets()
    if not presets:
        return None
    algo = str(algo or "").strip().lower()
    preset = None
    reason = ""
    if algo:
        preset = next((p for p in presets
                       if str(p.get("algo", "")).strip().lower() == algo), None)
        reason = f"algo {algo}"
    if preset is None:
        preset = next((p for p in presets if p.get("is_default")), None)
        reason = "default"
    if preset is None:
        logging.info(f"OC auto-switch: no bound or default OC preset for algo '{algo or '?'}'")
        return None
    if _oc_matches_live(preset.get("values", {})):
        # Values already live — the chosen preset is still the active one
        _set_oc_applied(preset.get("id", ""))
        logging.info(f"OC auto-switch: preset '{preset.get('name')}' already matches live OC")
        return None
    ok, msg = _apply_oc_preset_values(preset.get("values", {}))
    if ok:
        _set_oc_applied(preset.get("id", ""))
        logging.info(f"OC auto-switch: preset '{preset.get('name')}' applied ({reason})")
        return f" OC preset '{preset.get('name')}' applied ({reason})."
    logging.error(f"OC auto-switch failed ({reason}): {msg}")
    return f" OC preset '{preset.get('name')}' failed: {msg}"


def _nvidia_gpu_list():
    """[{index, name, bus}] of NVIDIA GPUs for the per-GPU preset table; falls
    back to the live conf list length when stats are unavailable."""
    gpus = []
    try:
        for g in get_gpu_stats().get("gpus", []):
            if g.get("brand") == "NVIDIA":
                gpus.append({"index": g.get("index", len(gpus)),
                             "name": g.get("name", ""), "bus": g.get("bus_id", "")})
    except Exception:
        pass
    if not gpus:
        try:
            nv = parse_shell_config(NVIDIA_OC_CONF)
            n = max([len(str(nv.get(k, "") or "").split())
                     for k in ("CLOCK", "LCLOCK", "MEM", "LMEM", "PLIMIT", "FAN")] or [0])
            gpus = [{"index": i, "name": "", "bus": ""} for i in range(n)]
        except Exception:
            gpus = []
    return gpus


@app.route('/api/oc-presets', methods=['GET'])
def list_oc_presets():
    applied_id = _load_oc_state().get("applied_id", "")
    presets = []
    for p in _load_oc_presets():
        presets.append({
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "algo": p.get("algo", ""),
            "is_default": bool(p.get("is_default")),
            "values": p.get("values", {}),
            "created_at": p.get("created_at", 0),
            # Single ACTIVE: the preset last applied by the rig AND still matching
            # the live conf (manual OC edits clear the applied marker)
            "active": p.get("id", "") == applied_id and _oc_matches_live(p.get("values", {}))
        })
    return jsonify({"success": True, "presets": presets,
                    "live": _live_oc_form_values(), "gpus": _nvidia_gpu_list()})


@app.route('/api/oc-presets/save', methods=['POST'])
def save_oc_preset():
    data = request.get_json() or {}
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 60 or not re.match(r'^[A-Za-z0-9_\-\s]+$', name):
        return jsonify({"success": False, "message": "Invalid OC preset name. Use alphanumeric characters and spaces only."}), 400
    algo = str(data.get("algo", "")).strip().lower()
    if algo and not re.match(r'^[a-z0-9_\-]{1,32}$', algo):
        return jsonify({"success": False, "message": "Invalid algorithm name."}), 400
    pid = str(data.get("id", "")).strip()

    values = data.get("values")
    clean = None
    if values is not None:
        if not isinstance(values, dict):
            return jsonify({"success": False, "message": "Invalid overclock values."}), 400
        clean = {}
        for field in OC_FORM_FIELDS:
            v = " ".join(str(values.get(field, "") or "").split())
            if v and not is_safe_parameter_value(v):
                return jsonify({"success": False, "message": f"Invalid value for '{field}'."}), 400
            clean[field] = v
        for flag in OC_FLAGS:
            clean[flag] = "1" if str(values.get(flag, "0")) == "1" else "0"
        is_valid, err = validate_overclock_ranges("NVIDIA", clean)
        if not is_valid:
            return jsonify({"success": False, "message": err}), 400

    with config_lock:
        presets = _load_oc_presets()
        now = int(time.time())
        make_default = bool(data.get("is_default"))
        if pid:
            preset = next((p for p in presets if p.get("id") == pid), None)
            if preset is None:
                return jsonify({"success": False, "message": "OC preset not found."}), 404
            preset["name"] = name
            # algo is optional on updates — the UI edit flow sends it, but a
            # metadata-only save must not wipe the binding
            if "algo" in data:
                preset["algo"] = algo
            preset["updated_at"] = now
            if clean is not None:
                preset["values"] = clean
            if make_default:
                preset["is_default"] = True
            msg = f"OC preset '{name}' updated."
        else:
            if clean is None:
                return jsonify({"success": False, "message": "Missing overclock values."}), 400
            # Saving under an existing name refreshes that preset's values/binding
            preset = next((p for p in presets
                           if str(p.get("name", "")).lower() == name.lower()), None)
            if preset is not None:
                preset["algo"] = algo
                preset["values"] = clean
                preset["updated_at"] = now
                if make_default:
                    preset["is_default"] = True
                msg = f"OC preset '{name}' updated."
            else:
                preset = {
                    "id": uuid.uuid4().hex[:12], "name": name, "algo": algo,
                    "is_default": False, "values": clean,
                    "created_at": now, "updated_at": now
                }
                # A share/import payload may carry is_default on a fresh preset
                if make_default:
                    preset["is_default"] = True
                presets.append(preset)
                msg = f"OC preset '{name}' saved."
        if make_default:
            for other in presets:
                if other is not preset:
                    other["is_default"] = False
        if not _save_json_store(OC_PRESETS_PATH, presets):
            return jsonify({"success": False, "message": "Failed to save OC presets store."}), 500

    logging.info(f"OC preset '{name}' saved by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": msg})


@app.route('/api/oc-presets/bind', methods=['POST'])
def bind_oc_preset():
    """Inline algorithm binding from the presets list dropdown ('' = unbound).
    An algo can be bound to a single preset — rebinding steals it from others."""
    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    algo = str(data.get("algo", "")).strip().lower()
    if algo and not re.match(r'^[a-z0-9_\-]{1,32}$', algo):
        return jsonify({"success": False, "message": "Invalid algorithm name."}), 400
    with config_lock:
        presets = _load_oc_presets()
        preset = next((p for p in presets if p.get("id") == pid), None)
        if preset is None:
            return jsonify({"success": False, "message": "OC preset not found."}), 404
        if algo:
            for other in presets:
                if other is not preset and str(other.get("algo", "")).lower() == algo:
                    other["algo"] = ""
        preset["algo"] = algo
        preset["updated_at"] = int(time.time())
        if not _save_json_store(OC_PRESETS_PATH, presets):
            return jsonify({"success": False, "message": "Failed to save OC presets store."}), 500
    name = preset.get("name", "")
    logging.info(f"OC preset '{name}' bound to algo '{algo or 'none'}' by IP: {request.remote_addr}")
    return jsonify({"success": True,
                    "message": f"OC preset '{name}' bound to {algo}." if algo else f"OC preset '{name}' unbound."})


@app.route('/api/oc-presets/default', methods=['POST'])
def default_oc_preset():
    """Mark one preset as the default fallback (empty id clears the default)."""
    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    with config_lock:
        presets = _load_oc_presets()
        if pid:
            preset = next((p for p in presets if p.get("id") == pid), None)
            if preset is None:
                return jsonify({"success": False, "message": "OC preset not found."}), 404
            for p in presets:
                p["is_default"] = (p is preset)
            msg = f"OC preset '{preset.get('name')}' set as default."
        else:
            for p in presets:
                p["is_default"] = False
            msg = "Default OC preset cleared."
        if not _save_json_store(OC_PRESETS_PATH, presets):
            return jsonify({"success": False, "message": "Failed to save OC presets store."}), 500
    logging.info(f"OC default preset changed by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": msg})


@app.route('/api/oc-presets/apply', methods=['POST'])
def apply_oc_preset():
    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    with config_lock:
        preset = next((p for p in _load_oc_presets() if p.get("id") == pid), None)
    if preset is None:
        return jsonify({"success": False, "message": "OC preset not found."}), 404
    try:
        ok, msg = _apply_oc_preset_values(preset.get("values", {}))
    except Exception as e:
        logging.error(f"Failed to apply OC preset '{preset.get('name')}': {e}")
        return jsonify({"success": False, "message": "Failed to apply OC preset values."}), 500
    if not ok:
        return jsonify({"success": False, "message": msg}), 500
    name = preset.get("name", "")
    _set_oc_applied(preset.get("id", ""))
    logging.info(f"OC preset '{name}' applied by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": msg or f"OC preset '{name}' applied."})


@app.route('/api/oc-presets/delete', methods=['POST'])
def delete_oc_preset():
    data = request.get_json() or {}
    pid = str(data.get("id", "")).strip()
    with config_lock:
        presets = _load_oc_presets()
        remaining = [p for p in presets if p.get("id") != pid]
        if len(remaining) == len(presets):
            return jsonify({"success": False, "message": "OC preset not found."}), 404
        if not _save_json_store(OC_PRESETS_PATH, remaining):
            return jsonify({"success": False, "message": "Failed to save OC presets store."}), 500
    if _load_oc_state().get("applied_id", "") == pid:
        _set_oc_applied("")
    logging.info(f"OC preset '{pid}' deleted by IP: {request.remote_addr}")
    return jsonify({"success": True, "message": "OC preset deleted."})


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
        template = str(extra.get("template", "")).strip()
        pool_pass = str(extra.get("pass", "")).strip()
        fs_name = str(extra.get("name", "")).strip()
        if miner_alt and not re.match(r'^[a-z0-9_\-]+$', miner_alt):
            return False, "Invalid custom miner package name."
        if install_url and not re.match(r'^https://[A-Za-z0-9\.\-/_]+$', install_url):
            return False, "Invalid miner install URL."
        if algo and not re.match(r'^[a-z0-9_\-]+$', algo):
            return False, "Invalid hash algorithm name."
        if user_config and not re.match(r'^[A-Za-z0-9_\-\.\:\%\s]+$', user_config):
            return False, "Invalid miner configuration arguments."
        if template and not re.match(r'^[A-Za-z0-9_\-\.\@\:%]*$', template):
            return False, "Invalid wallet and worker template."
        if pool_pass and not re.match(r'^[A-Za-z0-9_\-\.\:\%\@\#\+\/\s\=\,]*$', pool_pass):
            return False, "Invalid pool password."
        if not wallet:
            return False, "Wallet address is required for a custom miner flight sheet."

        worker_name = socket.gethostname().strip().upper().replace(" ", "_") or "WORKER"
        # HiveOS resolves %WAL%/%WORKER_NAME% server-side; our local apply must do it
        # itself or the miner would literally mine to "%WAL%.%WORKER_NAME%"
        custom_template = template or (wallet + "." + worker_name)
        custom_template = custom_template.replace("%WAL%", wallet).replace("%worker_name%", worker_name).replace("%WORKER_NAME%", worker_name)
        user_config = user_config.replace("%WAL%", wallet).replace("%worker_name%", worker_name).replace("%WORKER_NAME%", worker_name)
        wallet_conf = parse_shell_config(WALLET_CONF_PATH)
        wallet_conf.clear()
        if fs_name:
            wallet_conf["FS_NAME"] = fs_name
        if str(extra.get("fs_id", "")).strip():
            wallet_conf["FS_ID"] = str(extra["fs_id"]).strip()
        wallet_conf["CUSTOM_MINER"] = miner_alt
        if install_url:
            wallet_conf["CUSTOM_INSTALL_URL"] = install_url
        if algo:
            wallet_conf["CUSTOM_ALGO"] = algo
        wallet_conf["CUSTOM_TEMPLATE"] = custom_template
        wallet_conf["CUSTOM_URL"] = pool
        wallet_conf["CUSTOM_PASS"] = pool_pass or "x"
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
    if extra:
        if str(extra.get("name", "")).strip():
            wallet_conf["FS_NAME"] = str(extra["name"]).strip()
        if str(extra.get("fs_id", "")).strip():
            wallet_conf["FS_ID"] = str(extra["fs_id"]).strip()
    if not write_shell_config(WALLET_CONF_PATH, wallet_conf):
        return False, "Failed to write wallet.conf"

    rig_conf = parse_shell_config(RIG_CONF_PATH)
    rig_conf["MINER"] = miner
    if not write_shell_config(RIG_CONF_PATH, rig_conf):
        return False, "Failed to write rig.conf"

    logging.info(f"Flight sheet applied by IP: {request.remote_addr} (Coin={coin}, Miner={miner})")
    run_command(MINER_RESTART_CMD)
    return True, "Flight sheet applied successfully! Miner daemon restarting..."

def _fsheet_is_active(fsheet):
    """True when this flight sheet is the one currently applied on the rig.
    Primary check: the FS_ID/FS_NAME marker written into wallet.conf on apply;
    fallback: compare the sheet's first item against the live mining config."""
    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    fs_id = (wallet_conf.get("FS_ID") or "").strip()
    fs_name = (wallet_conf.get("FS_NAME") or "").strip()
    if fs_id:
        return str(fsheet.get("id", "")) == fs_id
    if fs_name:
        return str(fsheet.get("name", "")) == fs_name
    active = _read_active_mining_config()
    live_coin = str(active.get("coin") or "").lower()
    if not live_coin:
        return False
    item = _pick_apply_item(fsheet)
    wallet = str(item.get("wallet", "")).strip()
    w = next((x for x in _load_wallets_store() if x.get("id") == wallet), None)
    if w:
        wallet = w.get("address", "")
    return (str(item.get("coin", "")).lower() == live_coin and
            str(item.get("pool", "")) == active.get("pool") and
            str(item.get("miner", "")).lower() == str(active.get("miner") or "").lower() and
            wallet == active.get("wallet"))

@app.route('/api/fsheets/unset', methods=['POST'])
def unset_fsheet():
    """Hive 'Unset': leave the rig without a flight sheet — clear the mining
    config and stop the miner."""
    try:
        if os.path.exists(WALLET_CONF_PATH):
            shutil.copy2(WALLET_CONF_PATH, WALLET_CONF_PATH + ".bak")
        if os.path.exists(RIG_CONF_PATH):
            shutil.copy2(RIG_CONF_PATH, RIG_CONF_PATH + ".bak")
    except Exception as e:
        logging.error(f"Backup configurations failed: {e}")
    if not write_shell_config(WALLET_CONF_PATH, {}):
        return jsonify({"success": False, "message": "Failed to clear wallet.conf"}), 500
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    rig_conf["MINER"] = "none"
    if not write_shell_config(RIG_CONF_PATH, rig_conf):
        return jsonify({"success": False, "message": "Failed to update rig.conf"}), 500
    logging.info(f"Flight sheet unset by IP: {request.remote_addr} (miner stopped)")
    run_command(MINER_STOP_CMD)
    return jsonify({"success": True, "message": "Flight sheet unset. The rig has no active flight sheet; miner stopped."})

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
        "fs_id": (wallet_conf.get("FS_ID") or "").strip(),
    }

def _wallet_matches_live(addr, live):
    """Robust match of a library wallet address against the live mining wallet.
    Live configs may carry a worker suffix ('addr.WORKER' templates) or similar
    decorations, so plain equality alone misses wallets that are really in use."""
    addr = str(addr or "").strip()
    live = str(live or "").strip()
    if not addr or not live:
        return False
    if addr == live:
        return True
    if live.startswith(addr + "."):
        return True
    base = live.split(".")[0]
    return len(base) >= 8 and addr.startswith(base)

def _fsheet_wallet_refs(fsheet):
    """Wallet references used by a flight sheet (library ids or raw addresses)."""
    refs = set()
    for it in (fsheet.get("items") or []):
        ref = str(it.get("wallet", "") or "").strip()
        if ref:
            refs.add(ref)
    return refs

def _wallet_usage_stats():
    """Wallet library enriched with usage info for the Wallets tab:
    used_in — number of users of the wallet: flight sheets referencing it
    (by id or raw address, a sheet counts once even with several items) plus
    the live mining config when it mines with the same wallet value (last OR
    condition: pure wallet-value match, worker-suffix tolerant; skipped when
    the applied sheet already accounts for the wallet — no double counting);
    active  — wallet belongs to the currently applied flight sheet (any item)
    or matches the live mining config. Several wallets can be active at once
    when the active sheet (or live config) uses different wallets."""
    wallets = _load_wallets_store()
    fsheets = _load_fsheets_store()
    by_id = {str(w.get("id", "")): w for w in wallets}

    def sheet_wallet_ids(refs):
        ids = set()
        for wid, w in by_id.items():
            if wid in refs:
                ids.add(wid)
            elif w.get("address") and any(
                    _wallet_matches_live(str(w["address"]), str(r)) for r in refs):
                ids.add(wid)
        return ids

    used_in = {}
    for f in fsheets:
        refs = _fsheet_wallet_refs(f)
        if not refs:
            continue
        for wid in sheet_wallet_ids(refs):
            used_in[wid] = used_in.get(wid, 0) + 1
    active_ids = set()
    applied_ids = set()
    active_sheet = next((f for f in fsheets if _fsheet_is_active(f)), None)
    if active_sheet:
        applied_ids = sheet_wallet_ids(_fsheet_wallet_refs(active_sheet))
        active_ids |= applied_ids
    live_wallet = str(_read_active_mining_config().get("wallet") or "")
    if live_wallet:
        for wid, w in by_id.items():
            if _wallet_matches_live(w.get("address"), live_wallet):
                active_ids.add(wid)
                if wid not in applied_ids:
                    used_in[wid] = used_in.get(wid, 0) + 1
    enriched = []
    for w in wallets:
        row = dict(w)
        wid = str(w.get("id", ""))
        row["used_in"] = used_in.get(wid, 0)
        row["active"] = wid in active_ids
        enriched.append(row)
    return enriched

def _rig_wallet_used_in():
    """Usage count for the live-config wallet shown as the '(rig)' pseudo-row
    when no library wallet matches it: the live config itself (the rig is
    mining with this wallet right now) + saved sheets referencing the same
    wallet value (worker-suffix tolerant)."""
    live_wallet = str(_read_active_mining_config().get("wallet") or "")
    if not live_wallet:
        return 0
    used = 1
    for f in _load_fsheets_store():
        if any(_wallet_matches_live(str(r), live_wallet)
               for r in _fsheet_wallet_refs(f)):
            used += 1
    return used

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
    template = str(item.get("template", "")).strip()
    pool_pass = str(item.get("pass", "")).strip()
    if miner_alt and not re.match(r'^[a-z0-9_\-]+$', miner_alt):
        return None, "Invalid custom miner package name."
    if install_url and not re.match(r'^https://[A-Za-z0-9\.\-/_]+$', install_url):
        return None, "Invalid miner install URL."
    if algo and not re.match(r'^[a-z0-9_\-]+$', algo):
        return None, "Invalid hash algorithm name."
    if user_config and not re.match(r'^[A-Za-z0-9_\-\.\:\%\s]+$', user_config):
        return None, "Invalid miner configuration arguments."
    if template and not re.match(r'^[A-Za-z0-9_\-\.\@\:%]*$', template):
        return None, "Invalid wallet and worker template."
    if pool_pass and not re.match(r'^[A-Za-z0-9_\-\.\:\%\@\#\+\/\s\=\,]*$', pool_pass):
        return None, "Invalid pool password."
    return {
        "coin": coin, "wallet": wallet, "pool": pool, "miner": miner,
        "miner_alt": miner_alt, "install_url": install_url, "algo": algo, "user_config": user_config,
        "template": template, "pass": pool_pass,
    }, ""

FSHEET_ITEM_FIELDS = ["coin", "wallet", "pool", "miner", "miner_alt", "install_url", "algo", "user_config", "template", "pass"]

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
    return jsonify({"success": True, "wallets": _wallet_usage_stats(),
                    "rig_used_in": _rig_wallet_used_in(),
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
            "miner": rig_conf.get("MINER", "none"),
            "fs_id": (wallet_conf.get("FS_ID") or "").strip(),
            "fs_name": (wallet_conf.get("FS_NAME") or "").strip()
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
    prev = next((f for f in fsheets if f.get("id") == clean["id"]), None)
    was_active = prev is not None and _fsheet_is_active(prev)
    fsheets = [f for f in fsheets if f.get("id") != clean["id"]]
    fsheets.append(clean)
    if not _save_json_store(FSHEETS_PATH, fsheets):
        return jsonify({"success": False, "message": "Failed to save flight sheet."}), 500
    if was_active:
        # The edited sheet is the one currently running — re-apply it whenever
        # the rig's live config no longer matches the sheet content (e.g. a new
        # miner version in the install URL). Covers both fresh edits and drift
        # left by applies made before auto-re-apply existed; a save that already
        # matches the live config (fav toggle, rename) never restarts the miner.
        item = _pick_apply_item(clean)
        wallet = str(item.get("wallet", "")).strip()
        w = next((x for x in _load_wallets_store() if x.get("id") == wallet), None)
        if w:
            wallet = w.get("address", "")
        if _live_config_matches_item(item, wallet):
            return jsonify({"success": True, "message": "Flight sheet saved."})
        ok, msg = _apply_flight_sheet(item.get("coin"), wallet, item.get("pool"), item.get("miner"),
                                      extra={"name": clean.get("name", ""), "fs_id": clean.get("id", ""),
                                             "miner_alt": item.get("miner_alt", ""),
                                             "install_url": item.get("install_url", ""), "algo": item.get("algo", ""),
                                             "user_config": item.get("user_config", ""), "template": item.get("template", ""),
                                             "pass": item.get("pass", "")})
        if ok:
            return jsonify({"success": True, "message": "Flight sheet saved and re-applied to the rig. Miner restarting..."})
        return jsonify({"success": True, "message": f"Flight sheet saved, but re-apply failed: {msg}", "apply_error": msg})
    return jsonify({"success": True, "message": "Flight sheet saved."})

def _live_config_matches_item(item, wallet_address):
    """True when wallet.conf/rig.conf already reflect this flight sheet item's
    apply-relevant content (miner, package, install URL/version, algo, pool,
    user config, pass, coin, wallet)."""
    wallet_conf = parse_shell_config(WALLET_CONF_PATH)
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    miner = str(item.get("miner", "")).strip().lower() or "none"
    if (rig_conf.get("MINER") or "none").strip().lower() != miner:
        return False
    wc = lambda k: (wallet_conf.get(k) or "").strip()
    if miner == "custom":
        hostname = socket.gethostname().strip().upper().replace(" ", "_") or "WORKER"
        user_config = str(item.get("user_config", "")).strip()
        user_config = user_config.replace("%WAL%", wallet_address).replace("%worker_name%", hostname).replace("%WORKER_NAME%", hostname)
        try:
            meta = json.loads(wallet_conf.get("META", "") or "{}")
            live_coin = str(meta.get("custom", {}).get("coin", "")).strip().lower()
        except Exception:
            live_coin = ""
        if not live_coin:
            live_coin = wc("COIN").lower()
        return (wc("CUSTOM_MINER") == str(item.get("miner_alt", "")).strip().lower() and
                wc("CUSTOM_INSTALL_URL") == str(item.get("install_url", "")).strip() and
                wc("CUSTOM_ALGO") == str(item.get("algo", "")).strip().lower() and
                wc("CUSTOM_URL") == str(item.get("pool", "")).strip() and
                wc("CUSTOM_USER_CONFIG") == user_config and
                wc("CUSTOM_PASS") == (str(item.get("pass", "")).strip() or "x") and
                (wc("CUSTOM_TEMPLATE") or "").startswith(wallet_address) and
                live_coin == str(item.get("coin", "")).strip().lower())
    return (wc("COIN").lower() == str(item.get("coin", "")).strip().lower() and
            wc("WAL") == wallet_address and
            wc("POOL_URL") == str(item.get("pool", "")).strip())

@app.route('/api/fsheets/delete', methods=['POST'])
def delete_fsheet():
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    fid = str(data.get("id", "")).strip()
    fsheets = _load_fsheets_store()
    fsheet = next((f for f in fsheets if f.get("id") == fid), None)
    if fsheet is None:
        return jsonify({"success": False, "message": "Flight sheet not found."}), 404
    if _fsheet_is_active(fsheet):
        return jsonify({"success": False, "message": "This flight sheet is active on the rig. Unset it first."}), 400
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
                                  extra={"name": fsheet.get("name", ""), "fs_id": fsheet.get("id", ""),
                                         "miner_alt": item.get("miner_alt", ""),
                                         "install_url": item.get("install_url", ""), "algo": item.get("algo", ""),
                                         "user_config": item.get("user_config", ""), "template": item.get("template", ""),
                                         "pass": item.get("pass", "")})
    if ok:
        # Algo-bound OC preset (if any) follows the rig onto the new algorithm
        oc_msg = _auto_switch_oc_for_algo(item.get("algo", ""))
        if oc_msg:
            msg = msg + oc_msg
        record_metrics_event("info", f"Flight sheet applied: {fsheet.get('name', '') or fid}")
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
        "FAN": "",
        "RUNNING_DELAY": "",
        "LOGO_BRIGHTNESS": "",
        "FORCESTATE": "",
        "POWERMIZER": "",
        "OHGODAPILL_ENABLED": "",
        "OHGODAPILL_START_TIMEOUT": "",
        "OHGODAPILL_ARGS": ""
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
        run_nvidia_oc()
        run_command("sudo /hive/sbin/amd-oc")
        _set_oc_applied("")
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
    return jsonify({"success": True, "fans": fans, "mknet": _mknet_status()})

# ---------------- 8MK_NET USB fan controller (CP210x serial, hive 8mknet_autofan) ----------------
# The controller firmware implements the regulation itself: fan_mode 2 = auto
# (keeps the minimal speed within min..max to reach target_temp), 1 = static
# (manual_fan_speed). Parameters are passed via 8mknet_autofan.conf and applied
# by invoking the hive script (sources /etc/environment itself).

MKNET_SCRIPT = "/hive/opt/8mknet/8mknet_autofan"
MKNET_CONF = os.path.join(HIVE_CONFIG_DIR, "8mknet_autofan.conf")
MKNET_SERIAL = "/dev/serial/by-id/usb-Silicon_Labs_Device_for_hiveos_Autofan8MK_NET-if00-port0"
MKNET_STATS_OK = "/run/hive/8mknet_latest_ok"
MKNET_OUTPUT = "/run/hive/8mknet_autofan"

def _mknet_present():
    return os.path.exists(MKNET_SCRIPT) and os.path.exists(MKNET_SERIAL)

def _mknet_stats():
    """Latest controller report: {"casefan":[...8...], "thermosensors":[...]}."""
    for path in (MKNET_STATS_OK, MKNET_OUTPUT):
        try:
            with open(path, 'r') as f:
                lines = [l.strip() for l in f.read().splitlines() if l.strip().startswith('{')]
        except Exception:
            continue
        for line in reversed(lines):
            try:
                j = json.loads(line)
            except Exception:
                continue
            if isinstance(j, dict) and "casefan" in j:
                return j
    return None

def _mknet_status():
    present = _mknet_present()
    if not present:
        return {"present": False}
    conf = parse_shell_config(MKNET_CONF)
    def _num(key, default):
        try:
            return int(str(conf.get(key, "")).strip())
        except (TypeError, ValueError):
            return default
    return {
        "present": True,
        "stats": _mknet_stats(),
        "config": {
            "auto": str(conf.get("AUTO_ENABLED", "1")).strip() == "1",
            "target_temp": _num("TARGET_TEMP", 60),
            "target_mem_temp": _num("TARGET_MEM_TEMP", 90),
            "min_fan": _num("MIN_FAN", 5),
            "max_fan": _num("MAX_FAN", 100),
            "static_speed": _num("MANUAL_FAN", 70),
        },
    }

@app.route('/api/fans/mknet', methods=['POST'])
def set_mknet_fans():
    if not _mknet_present():
        return jsonify({"success": False, "message": "8MK_NET controller not found on this rig."}), 404
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in ("auto", "static"):
        return jsonify({"success": False, "message": "Fan mode must be 'auto' or 'static'."}), 400
    def _int(key, lo, hi, default=None):
        v = data.get(key)
        if v in (None, ""):
            if default is not None:
                return default
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if lo <= v <= hi else None

    target_temp = _int("target_temp", 30, 95)
    target_mem = _int("target_mem_temp", 40, 110)
    min_fan = _int("min_fan", 0, 99)
    max_fan = _int("max_fan", 1, 100)
    static_speed = _int("static_speed", 0, 100)
    if target_temp is None or target_mem is None:
        return jsonify({"success": False, "message": "Target temperatures must be 30-95 / 40-110 °C."}), 400
    if min_fan is None or max_fan is None:
        return jsonify({"success": False, "message": "Fan speeds must be 0-99 / 1-100%."}), 400
    if min_fan > max_fan:
        return jsonify({"success": False, "message": "Min fan speed cannot be greater than max."}), 400
    if mode == "static" and static_speed is None:
        return jsonify({"success": False, "message": "Static fan speed must be 0-100%."}), 400

    conf = parse_shell_config(MKNET_CONF)
    conf["AUTO_ENABLED"] = "1" if mode == "auto" else "0"
    conf["TARGET_TEMP"] = str(target_temp)
    conf["TARGET_MEM_TEMP"] = str(target_mem)
    conf["MIN_FAN"] = str(min_fan)
    conf["MAX_FAN"] = str(max_fan)
    if mode == "static" and static_speed is not None:
        conf["MANUAL_FAN"] = str(static_speed)
    if not write_shell_config(MKNET_CONF, conf):
        return jsonify({"success": False, "message": "Failed to write 8mknet_autofan.conf"}), 500
    out, err, code = run_command(f"sudo {MKNET_SCRIPT} --get_json")
    applied = '"casefan"' in (out or "")
    logging.info(f"8MK_NET fans set to {mode}"
                 + (f", static {static_speed}%" if mode == "static" else
                    f", target {target_temp}°C, min {min_fan}%, max {max_fan}%")
                 + f" by IP: {request.remote_addr}")
    if not applied:
        return jsonify({"success": True, "message": "Settings saved, but the controller did not respond - check the USB connection."})
    return jsonify({"success": True, "message": "8MK_NET fan settings applied."})


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

# ---------------- Metrics history API (worker Statistics tab) ----------------

def _metrics_parse_range(date_str, days_str):
    """Local-time [start, end) epoch bounds for the selected day(s); days is 1 or 3."""
    days = 3 if str(days_str) == "3" else 1
    try:
        st = time.strptime(str(date_str), "%Y-%m-%d")
    except (ValueError, TypeError):
        st = time.localtime()
    start = time.mktime((st.tm_year, st.tm_mon, st.tm_mday, 0, 0, 0, 0, 0, -1))
    return start, start + days * 86400, days

@app.route('/api/metrics/history')
def api_metrics_history():
    start, end, days = _metrics_parse_range(request.args.get("date"), request.args.get("days", 1))
    store = _load_metrics_store()
    day_keys = sorted(store.get("days", {}).keys())
    first_key = time.strftime("%Y-%m-%d", time.localtime(start))
    last_key = time.strftime("%Y-%m-%d", time.localtime(end - 1))
    samples = []
    for key in day_keys:
        if key < first_key or key > last_key:
            continue
        for s in store["days"][key].get("samples", []):
            if start <= s[0] < end:
                samples.append(s)
    events = [e for e in store.get("events", []) if start <= e.get("ts", 0) < end]
    meta = store.get("meta", {}) if isinstance(store.get("meta", {}), dict) else {}
    return jsonify({
        "success": True,
        "start": first_key,
        "days": days,
        "rate": float(store.get("rate", 0.0) or 0.0),
        "algo": str(meta.get("algo", "") or ""),
        "gpu_count": int(meta.get("gpu_count", 0) or 0),
        "samples": samples,
        "events": events
    })

@app.route('/api/metrics/rate', methods=['POST'])
def api_metrics_rate():
    data = request.get_json(silent=True) or {}
    try:
        rate = round(float(data.get("rate", 0)), 2)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Rate must be a number."}), 400
    if not (0 <= rate <= 10000):
        return jsonify({"success": False, "message": "Rate out of range (0-10000)."}), 400
    store = _load_metrics_store()
    old = float(store.get("rate", 0.0) or 0.0)
    store["rate"] = rate
    _save_metrics_store(store)
    if old != rate:
        record_metrics_event("info", f"Electricity rate set to {rate:g} RUB/kWh")
    return jsonify({"success": True, "rate": rate, "message": f"Rate saved: {rate:g} RUB/kWh"})

@app.route('/api/metrics/export')
def api_metrics_export():
    start, end, days = _metrics_parse_range(request.args.get("date"), request.args.get("days", 1))
    date_str = time.strftime("%Y-%m-%d", time.localtime(start))
    store = _load_metrics_store()
    day_keys = sorted(store.get("days", {}).keys())
    first_key = time.strftime("%Y-%m-%d", time.localtime(start))
    last_key = time.strftime("%Y-%m-%d", time.localtime(end - 1))
    rows = []
    gpu_count = 0
    for key in day_keys:
        if key < first_key or key > last_key:
            continue
        for s in store["days"][key].get("samples", []):
            if not (start <= s[0] < end):
                continue
            ts, temps, fans, powers, hashrates, total_w, total_mh = (list(s) + [0] * 7)[:7]
            gpu_count = max(gpu_count, len(temps))
            rows.append([ts, temps, fans, powers, hashrates, total_w, total_mh])

    lines = []
    header = ["time"]
    for label, count in (("temp", gpu_count), ("fan", gpu_count), ("power_w", gpu_count), ("hashrate_mh", gpu_count)):
        header.extend([f"{label}_{i}" for i in range(count)])
    header.extend(["total_power_w", "total_hashrate_mh"])
    lines.append(",".join(header))
    for ts, temps, fans, powers, hashrates, total_w, total_mh in rows:
        vals = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))]
        for series in (temps, fans, powers, hashrates):
            vals.extend(str(v) for v in series)
        vals.extend([str(total_w), str(total_mh)])
        lines.append(",".join(vals))
    csv_data = "\n".join(lines) + "\n"
    return Response(csv_data, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=metrics_{date_str}_{days}d.csv"})

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
    # The jump may be intentionally reachable only from clients (one-directional
    # policy) - report that distinctly instead of a bare SSH timeout failure
    if not _probe_tcp(host, port):
        return jsonify({
            "success": False,
            "jump_unreachable": True,
            "message": ("Jump host %s:%s is unreachable from this rig - it serves clients that "
                        "can reach it directly (e.g. your Mac). Rigs use their direct routes." % (host, port))
        })
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
def _probe_tcp(host, port, timeout=4):
    """Quick TCP reachability probe (no SSH). Returns True if connect succeeded."""
    try:
        with socket.create_connection((str(host), int(port or 22)), timeout=timeout):
            return True
    except Exception:
        return False

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

    # A jump host may be intentionally unreachable from this rig (one-directional
    # policy: the jump serves clients like the user's Mac, not the cluster nodes).
    # Probe the jump first: unreachable -> 'client-side route' (not a failure),
    # reachable -> verify the full SSH path through it as usual.
    if clean.get("type") == "jump":
        if not _probe_tcp(clean.get("jump_host"), clean.get("jump_port", 22)):
            return jsonify({
                "success": False,
                "jump_unreachable": True,
                "message": ("Jump host %s:%s is unreachable from this rig - this route is client-side: "
                            "it works from devices that can reach the jump (e.g. your Mac). "
                            "The rig itself uses its direct routes." %
                            (clean.get("jump_host"), clean.get("jump_port", 22)))
            })

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
        # jump routes: probe the jump first - unreachable means 'client-side route'
        if str(access.get("type", "direct")) == "jump":
            resolved = resolve_jump_host(dict(access))
            if not _probe_tcp(resolved.get("jump_host"), resolved.get("jump_port", 22)):
                results.append({
                    "id": access.get("id"),
                    "name": access.get("name"),
                    "ok": False,
                    "jump_unreachable": True,
                    "detail": ("jump %s:%s is unreachable from this rig - "
                               "client-side route (e.g. used from the Mac)" %
                               (resolved.get("jump_host"), resolved.get("jump_port", 22)))
                })
                continue
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

# ---------------- CSV cluster import API ----------------

@app.route('/api/cluster/import/parse', methods=['POST'])
def api_cluster_import_parse():
    """Live CSV validation for the import dialog (no state changes, no passwords echoed)."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", ""))
    if len(text) > _IMPORT_MAX_TEXT:
        return jsonify({"success": False, "errors": [{"line": 0, "message": "Input is too large (max 256 KB)."}]}), 400
    parsed = parse_cluster_csv(text)
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    existing = {str(r.get("name", "")).strip().lower(): r for r in state["rigs"]}
    nodes = []
    for node in parsed["nodes"]:
        row = {
            "name": node["name"],
            "lines": node["lines"],
            "accesses": [{"host": a["host"], "port": a["port"], "user": a["user"],
                          "jump": (a["jump_key"] or "").replace("|", ":")} for a in node["accesses"]],
        }
        ex = existing.get(node["name"].lower())
        if ex is not None:
            row["match"] = "self" if ex.get("id") == state["self_id"] else "merge"
        else:
            row["match"] = "new"
        nodes.append(row)
    return jsonify({
        "success": parsed["ok"],
        "delimiter": parsed["delimiter"],
        "nodes": nodes,
        "jumps": [{"host": j["host"], "port": j["port"], "user": j["user"],
                   "lines": j["lines"]} for j in parsed["jumps"]],
        "errors": parsed["errors"],
    })

@app.route('/api/cluster/import', methods=['POST'])
def api_cluster_import_start():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", ""))
    if len(text) > _IMPORT_MAX_TEXT:
        return jsonify({"success": False, "message": "Input is too large (max 256 KB)."}), 400
    parsed = parse_cluster_csv(text)
    if not parsed["ok"]:
        return jsonify({"success": False, "errors": parsed["errors"]}), 400
    if not parsed["nodes"]:
        return jsonify({"success": False, "errors": [{"line": 0, "message": "Nothing to import."}]}), 400
    if len(parsed["nodes"]) > _IMPORT_MAX_NODES:
        return jsonify({"success": False, "errors": [{"line": 0, "message": "Too many nodes (max %d per import)." % _IMPORT_MAX_NODES}]}), 400
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "cancel": False, "done": False, "started_at": int(time.time()),
           "finished_at": 0, "summary": "", "nodes": {}, "_order": []}
    with _import_jobs_lock:
        _import_jobs[jid] = job
        # keep only the recent jobs around
        finished = [k for k, v in _import_jobs.items() if v["done"]]
        if len(finished) > 4:
            for k in finished[:-4]:
                _import_jobs.pop(k, None)
    t = threading.Thread(target=_cluster_import_worker, args=(job, parsed),
                         daemon=True, name="cluster-import-%s" % jid)
    t.start()
    logging.info(f"Cluster import job {jid} started: {len(parsed['nodes'])} nodes "
                 f"by IP: {request.remote_addr}")
    return jsonify({"success": True, "job_id": jid})

@app.route('/api/cluster/import/status/<jid>', methods=['GET'])
def api_cluster_import_status(jid):
    job = _import_jobs.get(jid)
    if job is None:
        return jsonify({"success": False, "message": "Job not found."}), 404
    return jsonify({"success": True, "job": _import_job_snapshot(job)})

@app.route('/api/cluster/import/cancel/<jid>', methods=['POST'])
def api_cluster_import_cancel(jid):
    job = _import_jobs.get(jid)
    if job is None:
        return jsonify({"success": False, "message": "Job not found."}), 404
    job["cancel"] = True
    return jsonify({"success": True, "message": "Cancel requested."})

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
    # Render the auth decision server-side: the overlay is hidden in the HTML
    # for an authenticated session and shown from the first paint otherwise —
    # no flash of the app before the login form (and no flash of the login
    # form for users with a valid session)
    return render_template('index.html', app_version=VERSION,
                           authenticated=bool(session.get('authenticated')))

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

    # Start the background cluster synchronization worker
    start_cluster_worker()

    # Start the metrics history sampler (worker Statistics tab)
    start_metrics_sampler()

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
