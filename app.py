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
import ipaddress
import shutil
import tempfile
import subprocess
import platform
import logging
import threading
import random
import time
from datetime import datetime, timezone, timezone
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import urllib.error
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
CLUSTER_SELF_ENTRY_PATH = os.path.join(HIVE_CONFIG_DIR, "cluster-self-entry.json")
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
    # A missing self entry is almost always data loss (unreadable cluster.json,
    # import bootstrap racing the running panel), not a first boot: restore the
    # rig's own name and SSH routes from the snapshot sidecar. Falling back to
    # RIG_ID with empty accesses rebirths the rig under its worker id and
    # strips its routes - the numeric-name rigs with no connections.
    snap = _read_self_entry_snapshot()
    if snap is not None:
        logging.error("Self entry missing from cluster config - restoring name/routes "
                      f"from snapshot ('{snap.get('name', '')}')")
        return {
            "id": state["self_id"],
            "name": snap.get("name") or rig_conf.get("RIG_ID", "") or socket.gethostname(),
            "host_label": snap.get("host_label") or get_local_ip(),
            "location": snap.get("location") or "",
            "ha_plug": snap.get("ha_plug") or "",
            "ha_enabled": bool(snap.get("ha_enabled", True)),
            "is_self": True,
            "password": str(app.config.get('ACCESS_PASSWORD', '')),
            "accesses": [clean_access_entry(a) for a in snap.get("accesses", [])
                         if isinstance(a, dict)],
            "updated_at": now,
            "added_at": now,
        }
    return {
        "id": state["self_id"],
        "name": rig_conf.get("RIG_ID", "") or socket.gethostname(),
        "host_label": get_local_ip(),
        "location": "",
        "ha_plug": "",
        "ha_enabled": True,
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

_SELF_ENTRY_SNAPSHOT_FINGERPRINT = {"value": None}

def _read_self_entry_snapshot():
    """Reads the persisted self entry (name + SSH routes) or None."""
    try:
        with open(CLUSTER_SELF_ENTRY_PATH, 'r') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("name"), str) \
                and isinstance(data.get("accesses", []), list):
            return data
    except Exception:
        pass
    return None

def _self_entry_snapshot_payload(entry):
    return {
        "name": str(entry.get("name", "")),
        "host_label": str(entry.get("host_label", "")),
        "location": str(entry.get("location", "") or ""),
        "ha_plug": str(entry.get("ha_plug", "") or ""),
        "ha_enabled": bool(entry.get("ha_enabled", True)),
        "accesses": [clean_access_entry(a) for a in entry.get("accesses", [])
                     if isinstance(a, dict)],
    }

def _write_self_entry_snapshot(entry):
    """Persists the self entry data next to cluster.json (atomic, 0600)."""
    payload = _self_entry_snapshot_payload(entry)
    fingerprint = json.dumps(payload, sort_keys=True)
    if fingerprint == _SELF_ENTRY_SNAPSHOT_FINGERPRINT.get("value"):
        return True
    try:
        tmp_path = CLUSTER_SELF_ENTRY_PATH + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CLUSTER_SELF_ENTRY_PATH)
        _SELF_ENTRY_SNAPSHOT_FINGERPRINT["value"] = fingerprint
        return True
    except Exception as e:
        logging.error(f"Failed to save cluster self entry snapshot: {e}")
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
        # Keep the self-entry snapshot (name + SSH routes) in sync so a rebirth
        # after data loss restores the rig instead of stripping it
        self_entry = next((r for r in state.get("rigs", [])
                           if isinstance(r, dict) and r.get("id") == state.get("self_id")), None)
        if self_entry is not None:
            _write_self_entry_snapshot(self_entry)
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
                  "-o", "ConnectTimeout=6",
                  "-o", "ServerAliveInterval=5",
                  "-o", "ServerAliveCountMax=3",
                  "-o", "LogLevel=ERROR"]

def _ssh_control_path(access):
    """Deterministic per-route multiplex socket: distinct for LAN / netbird /
    jump variants of the same rig so a master opened over one route is never
    reused for another."""
    sig = "%s@%s:%s|j=%s:%s|%s" % (access.get("user", ""), access.get("host", ""), access.get("port", 22),
                                   access.get("jump_host", ""), access.get("jump_port", 22),
                                   access.get("key_path", ""))
    return "/tmp/hive-cm-" + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16]

def build_ssh_command(access, remote_cmd, tmp_files):
    """Build a safe argv list for ssh (direct connection or via jump server)."""
    args = []
    proxy_arg = None
    # Jump server hop via ProxyCommand
    if access.get("type") == "jump":
        jopts = " ".join(["-o StrictHostKeyChecking=no",
                          "-o UserKnownHostsFile=/dev/null",
                          "-o ConnectTimeout=6",
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
    # Connection multiplexing: one SSH handshake per route per ~2 minutes instead
    # of one per API call (sync does 3 calls per rig per cycle) - keeps sync fast
    # and reconnect churn low.
    args += ["-o", "ControlMaster=auto",
             "-o", "ControlPath=%s" % _ssh_control_path(access),
             "-o", "ControlPersist=120s"]
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
    """Call a remote rig's dashboard API over its SSH routes.

    Routes are tried in strict priority: lan (direct RFC1918) -> netbird
    (direct overlay) -> jump (via gateway). Routes known to be down are
    skipped while they cool down and cheaply re-probed afterwards, so the
    cluster fails over to a backup route fast and falls back to the
    preferred LAN route automatically once it is reachable again.
    Requires curl on the remote rig and sshpass locally for password auth.
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
    _route_failback_probes(rig)
    now = time.time()
    last_error = "No SSH accesses configured for this rig"
    for access in _ordered_rig_accesses(rig):
        access_name = access.get("name") or access.get("id", "?")
        key = _route_health_key(rig.get("id"), access)
        health = _get_route_health(key)
        if health.get("state") == "down" and now < float(health.get("probe_after") or 0):
            if last_error == "No SSH accesses configured for this rig":
                last_error = "All routes are down (auto-retry in progress)"
            continue  # route known down, cooling down - skip without waiting
        t0 = time.monotonic()
        ok, out, ssh_err = run_ssh_command(access, curl_cmd, timeout=timeout, stdin_data=raw)
        latency_ms = (time.monotonic() - t0) * 1000
        if not ok:
            _mark_route_down(key, ssh_err)
            last_error = "%s: %s" % (access_name, ssh_err)
            continue
        body_text, http_code = _parse_curl_output(out)
        try:
            data = json.loads(body_text)
        except Exception:
            _mark_route_up(key, latency_ms)  # SSH transport works, payload is odd
            last_error = "%s: invalid response from remote dashboard (HTTP %d)" % (access_name, http_code)
            continue
        if http_code >= 400:
            _mark_route_up(key, latency_ms)
            msg = "HTTP %d" % http_code
            if isinstance(data, dict) and data.get("message"):
                msg += " - %s" % data.get("message")
            last_error = "%s: %s" % (access_name, msg)
            continue
        _mark_route_up(key, latency_ms)
        return True, data, http_code, "", access_name
    return False, None, 0, last_error, ""

# ---------------- Cluster route health & smart failover ----------------
# Rigs talk to each other over SSH "routes" with a strict priority:
#   lan (direct RFC1918) -> netbird (direct 100.64/10 overlay) -> jump (gateway).
# Per-route health is tracked in memory and persisted inside the cluster cache;
# failing routes are skipped while they cool down (exponential backoff, capped)
# and re-probed with a cheap TCP connect so the cluster returns to the
# preferred LAN route automatically once it is reachable again.

_NETBIRD_NET = ipaddress.ip_network("100.64.0.0/10")

ROUTE_COOLDOWN_BASE = 5.0       # seconds before a failed route is re-probed
ROUTE_COOLDOWN_MAX = 20.0       # cap: worst-case failback latency after failures
ROUTE_PROBE_TCP_TIMEOUT = 1.5   # cheap reachability check, no SSH handshake
ROUTE_BACKGROUND_PROBE_EVERY = 15  # background prober period (feeds UI dots)

_access_health_lock = threading.Lock()
_ACCESS_HEALTH = {}  # "rig_id|access_id" -> {state, fails, probe_after, last_ok,
                     #    latency_ms, err, probe, probe_at, probe_ms}

def _access_route_class(access):
    """(priority, class) for an SSH access: 0/lan, 1/netbird, 2/jump."""
    if str(access.get("type", "direct")) == "jump":
        return 2, "jump"
    try:
        ip = ipaddress.ip_address(str(access.get("host", "")).strip())
        if ip.version == 4 and ip in _NETBIRD_NET:
            return 1, "netbird"
    except ValueError:
        pass
    return 0, "lan"

def _route_health_key(rig_id, access):
    return "%s|%s" % (rig_id, access.get("id"))

def _get_route_health(key):
    with _access_health_lock:
        h = _ACCESS_HEALTH.get(key)
        return dict(h) if h else {}

def _update_route_health(key, **fields):
    with _access_health_lock:
        h = _ACCESS_HEALTH.setdefault(key, {"state": "unknown", "fails": 0, "probe_after": 0.0,
                                            "last_ok": 0, "latency_ms": 0, "err": "",
                                            "probe": "unknown", "probe_at": 0, "probe_ms": 0})
        h.update(fields)
        return dict(h)

def _route_cooldown(fails):
    return min(ROUTE_COOLDOWN_BASE * (2 ** min(int(fails), 3)), ROUTE_COOLDOWN_MAX)

def _mark_route_down(key, err=""):
    h = _get_route_health(key)
    fails = int(h.get("fails", 0)) + 1
    _update_route_health(key, state="down", fails=fails, err=str(err)[:200],
                         probe_after=time.time() + _route_cooldown(fails))

def _mark_route_up(key, latency_ms=None):
    fields = {"state": "up", "fails": 0, "err": "", "last_ok": int(time.time())}
    if latency_ms is not None:
        fields["latency_ms"] = int(latency_ms)
    _update_route_health(key, **fields)

def _route_probe_target(access):
    """Host whose TCP reachability gates this route (jump: the gateway itself)."""
    if str(access.get("type", "")) == "jump":
        acc = access
        if str(access.get("jump_id") or "").strip() and not access.get("jump_host"):
            acc = resolve_jump_host(dict(access))  # jump_id-only access: resolve gateway
        return acc.get("jump_host"), acc.get("jump_port", 22)
    return access.get("host"), access.get("port", 22)

def _route_failback_probes(rig):
    """Re-probe down routes whose cooldown expired (TCP only, no SSH):
    recovers the preferred LAN route without a full SSH attempt."""
    rig_id = rig.get("id")
    now = time.time()
    for access in rig.get("accesses", []):
        key = _route_health_key(rig_id, access)
        h = _get_route_health(key)
        if h.get("state") != "down" or now < float(h.get("probe_after") or 0):
            continue
        host, port = _route_probe_target(access)
        if _probe_tcp(host, port, timeout=ROUTE_PROBE_TCP_TIMEOUT):
            _update_route_health(key, state="up", fails=0, err="",
                                 last_ok=int(now), probe="ok", probe_ms=0)
        else:
            fails = int(h.get("fails", 0)) + 1
            _update_route_health(key, fails=fails, probe="fail",
                                 probe_after=now + _route_cooldown(fails))

def _ordered_rig_accesses(rig):
    """Accesses sorted by route priority (lan -> netbird -> jump)."""
    return sorted(rig.get("accesses", []), key=_access_route_class)

def _route_background_prober():
    """Periodically TCP-probe every remote rig route (cheap connect, pooled):
    keeps the per-route dots on the Cluster tab truthful for standby routes
    that the sync itself does not use right now."""
    while True:
        time.sleep(ROUTE_BACKGROUND_PROBE_EVERY)
        try:
            state = load_cluster_state()
            targets = []
            for rig in state.get("rigs", []):
                if rig.get("id") == state.get("self_id"):
                    continue
                for access in rig.get("accesses", []):
                    targets.append((_route_health_key(rig["id"], access), access))
            if not targets:
                continue
            def _probe_one(item):
                key, access = item
                host, port = _route_probe_target(access)
                t0 = time.monotonic()
                ok = _probe_tcp(host, port, timeout=ROUTE_PROBE_TCP_TIMEOUT)
                ms = int((time.monotonic() - t0) * 1000)
                _update_route_health(key, probe="ok" if ok else "fail",
                                     probe_at=int(time.time()), probe_ms=ms)
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(_probe_one, targets))
        except Exception as e:
            logging.error(f"Route background prober failed: {e}")

def start_route_prober():
    t = threading.Thread(target=_route_background_prober, name="route-prober", daemon=True)
    t.start()
    return t

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
        # A running panel would clobber this file with its in-memory state at the
        # end of the current sync cycle (a rebirth under the RIG_ID name with no
        # SSH routes) - stop it around the write and let it boot from the new file
        script = ("systemctl stop hiveos-local.service 2>/dev/null; "
                  "mkdir -p /hive-config && printf '%s' '" + body64 + "' | base64 -d > /hive-config/cluster.json.tmp "
                  "&& chmod 600 /hive-config/cluster.json.tmp "
                  "&& mv /hive-config/cluster.json.tmp /hive-config/cluster.json "
                  "&& printf '%s' '" + sid64 + "' | base64 -d > /hive-config/cluster-self-id "
                  "&& chmod 600 /hive-config/cluster-self-id; "
                  "wrc=$?; systemctl start hiveos-local.service 2>/dev/null; "
                  "[ $wrc -eq 0 ] && echo __W_OK__")
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

def _seed_route_health(cache):
    """Restore route health from the persisted cluster cache (once per boot)."""
    stored = cache.get("access_health")
    if not isinstance(stored, dict) or not stored:
        return
    with _access_health_lock:
        if _ACCESS_HEALTH:
            return
        for k, v in stored.items():
            if isinstance(v, dict):
                _ACCESS_HEALTH[str(k)] = dict(v)

def load_cluster_cache():
    try:
        if os.path.exists(CLUSTER_CACHE):
            with open(CLUSTER_CACHE, 'r') as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("rigs", {})
                _seed_route_health(data)
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
        "miner_uptime_s": get_miner_uptime_seconds(),
        "shares": get_miner_shares(),
        "extra_fans": get_extra_fans_summary(),
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
# Serializes load->modify->save flows on the metrics store (sampler thread vs
# request threads used to lose events/samples written concurrently)
_metrics_lock = threading.Lock()

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
        with _metrics_lock:
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

    with _metrics_lock:
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

# =============================================================
# Local config guard ("Local mode" per rig)
# =============================================================
# When enabled, THIS panel is the source of truth for the rig's mining
# configuration (flight sheet, fans, overclock, autofan): a snapshot of the
# config files is captured on enable (and refreshed after every local apply)
# and enforced - at panel/rig start and on a periodic pass the files are
# compared against the snapshot, and any foreign change (typically a config
# pulled from the HiveOS cloud agent after a reboot, sometimes minutes or
# hours later) is reverted and re-applied to the hardware. Cloud mode (the
# default) changes nothing: no checks, no automatic applies - manual only.
GUARD_CONF = os.path.join(HIVE_CONFIG_DIR, "local_guard.json")
GUARD_FILES = ["rig.conf", "wallet.conf", "nvidia-oc.conf", "amd-oc.conf", "autofan.conf"]
GUARD_CHECK_INTERVAL = 600    # periodic enforcement pass (10 min)
# Early passes right after panel start (boot/reboot): hive's cloud agent may
# pull its config a few minutes into the boot, so check twice early, then
# settle into the periodic 10-min loop.
GUARD_BOOT_DELAYS = (90, 300)
# The cloud agent may pull its config at ANY point in the first minutes (RIG2
# 27 Oct: 3 min after the panel's first check) - poll every minute while the
# rig is young so a foreign config never runs for long.
GUARD_EARLY_INTERVAL = 60
GUARD_EARLY_WINDOW = 1500

_guard_state_lock = threading.Lock()
_GUARD_STATE = {"loaded": False, "data": None}

def _guard_default_state():
    return {"enabled": False, "since": 0, "snapshot": {},
            "last_check": 0, "last_drift": 0, "last_fix": 0, "last_action": ""}

def _guard_load():
    """In-memory cached guard state, seeded from /hive-config/local_guard.json."""
    with _guard_state_lock:
        if _GUARD_STATE["loaded"]:
            return _GUARD_STATE["data"]
        st = _guard_default_state()
        try:
            with open(GUARD_CONF, 'r') as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                st["enabled"] = bool(disk.get("enabled"))
                st["since"] = int(disk.get("since") or 0)
                snap = disk.get("snapshot")
                if isinstance(snap, dict):
                    st["snapshot"] = {str(k): str(v) for k, v in snap.items()}
                st["last_check"] = int(disk.get("last_check") or 0)
                st["last_drift"] = int(disk.get("last_drift") or 0)
                st["last_fix"] = int(disk.get("last_fix") or 0)
                st["last_action"] = str(disk.get("last_action") or "")
        except Exception:
            pass
        _GUARD_STATE["data"] = st
        _GUARD_STATE["loaded"] = True
        return st

def _guard_save(st):
    """Persist guard state atomically (local_guard.json, 0600) and refresh the cache."""
    with _guard_state_lock:
        _GUARD_STATE["data"] = st
        _GUARD_STATE["loaded"] = True
        try:
            with config_lock:
                tmp_path = GUARD_CONF + ".tmp"
                with open(tmp_path, 'w') as f:
                    json.dump(st, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, GUARD_CONF)
            return True
        except Exception as e:
            logging.error(f"Failed to save local guard state: {e}")
            return False

def _guard_reset_cache():
    """Drop the in-memory cache (tests / manual state reload)."""
    with _guard_state_lock:
        _GUARD_STATE["loaded"] = False
        _GUARD_STATE["data"] = None

def _guard_read_file(name):
    try:
        with open(os.path.join(HIVE_CONFIG_DIR, name), 'r') as f:
            return f.read()
    except Exception:
        return None

def _guard_write_file(name, content):
    """Restore one config file atomically (tmp + fsync + rename, 0600)."""
    path = os.path.join(HIVE_CONFIG_DIR, name)
    try:
        with config_lock:
            tmp_path = path + ".guard-tmp"
            with open(tmp_path, 'w') as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        return True
    except Exception as e:
        logging.error(f"Local guard: failed to restore {name}: {e}")
        return False

def guard_capture_snapshot(names=None):
    """Capture the CURRENT config files as the protected state. names=None:
    every guarded file (enable / explicit refresh). names=[...]: ONLY those
    files - a local write to one file must never silently adopt foreign
    (cloud-pulled) changes sitting in the others."""
    st = _guard_load()
    snap = dict(st.get("snapshot") or {}) if names else {}
    for name in (names or GUARD_FILES):
        if name not in GUARD_FILES:
            continue
        content = _guard_read_file(name)
        if content is not None:
            snap[name] = content
    st["snapshot"] = snap
    if not names:
        st["since"] = int(time.time())
    _guard_save(st)
    return st

def _guard_file_drifted(name):
    """True when Local mode is on and the live file differs (semantically)
    from the protected snapshot, i.e. the guard is about to revert it."""
    try:
        st = _guard_load()
        if not st.get("enabled") or st.get("suspended"):
            return False
        snap = (st.get("snapshot") or {}).get(name)
        if snap is None:
            return False
        cur = _guard_read_file(name)
        return cur != snap and not _conf_semantically_equal(cur, snap)
    except Exception:
        return False

def _guard_merge_save(st, fields):
    """Save only the given pass-owned fields over the FRESH on-disk/in-memory
    state, so a snapshot refreshed concurrently (guard_note_config_change)
    is never rolled back by a stale copy from the start of a check pass."""
    fresh = _guard_load()
    for k in fields:
        fresh[k] = st.get(k)
    _guard_save(fresh)

def guard_note_config_change(names=None):
    """Hook fired after every config write made through this panel: refresh the
    protected snapshot for the written file(s) so the guard enforces the rig's
    newest settings, not a stale copy. Cheap no-op while guard is disabled."""
    try:
        st = _guard_load()
        if not st.get("enabled"):
            return
        guard_capture_snapshot(names)
        logging.info("Local guard: snapshot refreshed after local config change")
    except Exception as e:
        logging.error(f"Local guard: snapshot refresh failed: {e}")

def _shell_conf_semantic(content):
    """Parse KEY="value" shell-conf lines into a normalized dict (quotes and
    inner whitespace differences ignored). None = unparseable content."""
    vals = {}
    try:
        for line in (content or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            vals[k.strip()] = " ".join(v.split())
    except Exception:
        return None
    return vals

def _conf_semantically_equal(a, b):
    """True when two conf-file contents carry the same settings (hive rewrites
    the files with its own quoting/formatting - byte comparison then sees a
    'drift' that is not one; restoring + re-applying on such a rewrite
    re-asserts clocks under load and can fault a marginal GPU, which is how
    the RIG6 14 Oct death loop was driven)."""
    sa, sb = _shell_conf_semantic(a), _shell_conf_semantic(b)
    if sa is None or sb is None:
        return False
    return sa == sb

def guard_run_check(apply=True):
    """One enforcement pass: compare config files against the snapshot, restore
    drifted files and re-apply them to the hardware. Returns a report dict."""
    st = _guard_load()
    now = int(time.time())
    report = {"enabled": bool(st.get("enabled")), "checked": now, "drifted": [], "fixed": False, "action": ""}
    if not report["enabled"]:
        return report
    if st.get("suspended"):
        # OC recovery owns the hardware right now - do not fight its applies
        report["action"] = "suspended (oc recovery in progress)"
        return report
    st["last_check"] = now
    snapshot = st.get("snapshot", {})
    drifted = []
    adopted = []
    for name in snapshot:
        if name not in GUARD_FILES:
            continue
        cur = _guard_read_file(name)
        if cur == snapshot[name]:
            continue
        if _conf_semantically_equal(cur, snapshot[name]):
            # Same settings, different formatting: adopt the new bytes so the
            # snapshot stops flapping, but never re-apply (no drift of values)
            if apply:
                st["snapshot"][name] = cur
                adopted.append(name)
            continue
        drifted.append(name)
    report["drifted"] = drifted
    if not drifted:
        _guard_merge_save(st, ["last_check"] + (["snapshot"] if adopted else []))
        return report

    st["last_drift"] = now
    logging.warning(f"Local guard: config drift detected ({', '.join(drifted)}) - restoring local settings")
    restart_miner = run_nv = run_amd = False
    restored = []
    for name in drifted:
        if name in ("rig.conf", "wallet.conf"):
            restart_miner = True
        elif name == "nvidia-oc.conf":
            run_nv = True
        elif name == "amd-oc.conf":
            run_amd = True
        if apply and _guard_write_file(name, snapshot[name]):
            restored.append(name)

    actions = []
    if apply:
        # autofan.conf needs no action: the daemon re-sources it every cycle
        if run_nv:
            run_nvidia_oc()
            actions.append("nvidia-oc applied")
        if run_amd:
            run_amd_oc()
            actions.append("amd-oc applied")
        if restart_miner:
            run_command(MINER_RESTART_CMD)
            actions.append("miner restarted")
    st["last_fix"] = now
    st["last_action"] = "reverted " + ", ".join(drifted) + ("; " + "; ".join(actions) if actions else "")
    report["fixed"] = bool(restored)
    report["action"] = st["last_action"]
    # Merge-only save: a panel config write may have refreshed the snapshot
    # (guard_note_config_change) while this pass was running — keep it instead
    # of overwriting with the stale copy captured at pass start.
    _guard_merge_save(st, ["last_check", "last_drift", "last_fix", "last_action"])
    if apply and restored:
        record_metrics_event("warning", "Local guard: config overwritten externally (" +
                             ", ".join(drifted) + ") - local settings re-applied")
    return report

def _guard_worker():
    for delay in GUARD_BOOT_DELAYS:
        time.sleep(delay)
        try:
            guard_run_check(apply=True)
        except Exception as e:
            logging.error(f"Local guard boot check failed: {e}")
    started = time.time()
    while True:
        young = time.time() - started < GUARD_EARLY_WINDOW + sum(GUARD_BOOT_DELAYS)
        time.sleep(GUARD_EARLY_INTERVAL if young else GUARD_CHECK_INTERVAL)
        try:
            guard_run_check(apply=True)
        except Exception as e:
            logging.error(f"Local guard check failed: {e}")

# Fast reaction: hive's `hello` rewrites rig.conf / wallet.conf / nvidia-oc.conf
# from the cloud a few minutes into every boot and starts the miner right after.
# Waiting for the next periodic pass left the rig on the CLOUD flight sheet for
# up to a minute (RIG2 27 Oct). In Local mode a cheap 2 s mtime watch notices
# any change to the guarded files, waits for the writes to finish (hello writes
# three files, with a driver check between them) and runs the guard at once.
GUARD_FAST_POLL = 2
GUARD_FAST_QUIET = 6      # files must stay unchanged this long before the check
GUARD_FAST_MAX_WAIT = 90  # ...but never defer a pending check longer than this

def _guard_files_sig():
    sig = []
    for name in GUARD_FILES:
        try:
            stt = os.stat(os.path.join(HIVE_CONFIG_DIR, name))
            sig.append((name, stt.st_mtime_ns, stt.st_size))
        except OSError:
            sig.append((name, 0, 0))
    return tuple(sig)

def _guard_fast_tick(ctx, now=None):
    """One watch step. Returns the guard report when a check was run."""
    now = time.time() if now is None else now
    st = _guard_load()
    if not st.get("enabled") or st.get("suspended"):
        ctx["sig"] = None
        ctx["dirty"] = ctx["first_dirty"] = 0
        return None
    sig = _guard_files_sig()
    if ctx.get("sig") is None:
        ctx["sig"] = sig
        return None
    if sig != ctx["sig"]:
        ctx["sig"] = sig
        ctx["dirty"] = now
        ctx["first_dirty"] = ctx.get("first_dirty") or now
    dirty = ctx.get("dirty") or 0
    if dirty and (now - dirty >= GUARD_FAST_QUIET or now - ctx.get("first_dirty", now) >= GUARD_FAST_MAX_WAIT):
        ctx["dirty"] = ctx["first_dirty"] = 0
        rep = guard_run_check(apply=True)
        ctx["sig"] = _guard_files_sig()   # the guard's own restores are not a new change
        return rep
    return None

def _guard_fast_watch():
    ctx = {}
    while True:
        time.sleep(GUARD_FAST_POLL)
        try:
            rep = _guard_fast_tick(ctx)
            if rep and rep.get("drifted"):
                logging.warning("Local guard (fast watch): reverted %s", ", ".join(rep["drifted"]))
        except Exception as e:
            logging.error(f"Local guard fast watch failed: {e}")

def start_guard_worker():
    t = threading.Thread(target=_guard_worker, daemon=True, name="local-guard")
    t.start()
    threading.Thread(target=_guard_fast_watch, daemon=True, name="local-guard-fast").start()
    logging.info("Local guard worker started")

def _guard_public(st):
    return {"enabled": bool(st.get("enabled")),
            "suspended": bool(st.get("suspended")),
            "since": int(st.get("since") or 0),
            "files": sorted(st.get("snapshot", {}).keys()),
            "last_check": int(st.get("last_check") or 0),
            "last_drift": int(st.get("last_drift") or 0),
            "last_fix": int(st.get("last_fix") or 0),
            "last_action": str(st.get("last_action") or "")}

@app.route('/api/guard', methods=['GET', 'POST'])
def api_guard():
    """Config source mode per rig: Local (enforced snapshot) vs Cloud (default)."""
    if request.method == 'GET':
        return jsonify({"success": True, "guard": _guard_public(_guard_load())})
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    if enabled is not None:
        want = bool(enabled)
        st = _guard_load()
        if want and not st.get("enabled"):
            # Enabling: protect the rig's CURRENT settings from now on
            st = guard_capture_snapshot()
            st["enabled"] = True
            _guard_save(st)
            logging.info(f"Local guard ENABLED by request from {request.remote_addr} "
                         f"({len(st.get('snapshot', {}))} files protected)")
            record_metrics_event("info", "Local mode enabled - rig settings are enforced locally")
        elif not want and st.get("enabled"):
            st["enabled"] = False
            st["last_action"] = "disabled"
            _guard_save(st)
            logging.info(f"Local guard disabled by request from {request.remote_addr}")
            record_metrics_event("info", "Local mode disabled - back to cloud behavior (no enforcement)")
    if data.get("refresh_snapshot"):
        guard_capture_snapshot()
    return jsonify({"success": True, "guard": _guard_public(_guard_load())})

# ---------------- Self-heal worker ----------------
# Auto-repair for the failure modes observed on the farm (11 Oct 2026, RIG6):
#   (a) custom miner package missing after a fsheet version bump: hive's
#       custom-get wipes the old install when the install URL changes and
#       re-downloads from GitHub, which can crawl or stall forever - the miner
#       never starts. Heal: pull the tarball from a peer that already has it
#       (1 MB base64 chunks over the cluster's SSH routes), then run custom-get
#       and start the miner.
#   (b) broken HIVE_HOST_URL (e.g. plain http://): the hive agent logs
#       "No connection with API server. Stop the miner" and kills every new
#       miner start a few seconds in. Heal: adopt the https host used by the
#       peer majority and restart the hive agent.
#   (c) dead miner screen left by a failed start ("screen session is already
#       running" blocks every retry). Heal: miner stop + start.
#   (d) string RIG_ID in rig.conf (e.g. 'RIG_1' instead of the numeric cloud
#       worker id): the hive agent authenticates its stats pushes with
#       id_rig=RIG_ID, the cloud answers -32602 'invalid request' to every
#       push and the agent's built-in policy stops the miner and refuses to
#       start it again. Heal: restore the numeric id (heal record -> agent
#       log pushes -> guard snapshot -> newest backup), refresh the guard
#       snapshot and escalate to a hive agent restart if the pushes stay
#       invalid.
# Every action is rate-limited and reported to the Activity feed (Stats tab).

HEAL_STATE_PATH = os.path.join(HIVE_CONFIG_DIR, "heal_state.json")
HEAL_CHECK_INTERVAL = 600      # periodic heal pass (10 min)
HEAL_BOOT_DELAY = 300          # first pass: give the hive agent time to boot the miner
HEAL_MINER_GAP = 1800          # min seconds between miner start/restart heal actions
HEAL_ZOMBIE_ZERO_SECONDS = 900 # miner alive but zero hashrate for this long = zombie -> restart
HEAL_ZOMBIE_GAP = 3600         # min seconds between zombie restarts
HEAL_HOST_GAP = 86400          # min seconds between rig.conf API-host fixes
HEAL_WD_GAP = 3600             # min seconds between watchdog-intent re-asserts
HEAL_ZOMBIE_ESCALATE_STREAK = 3  # zombie restarts without recovery -> try a reboot
HEAL_ZOMBIE_ESCALATE_ZERO = 10800  # ...and the miner must have been dead at least this long
HEAL_RIGID_ESCALATE = 180      # wait this long after an id fix before restarting the agent
HEAL_RIGID_RESTART_GAP = 3600  # min seconds between hive agent restarts after an id fix
HEAL_CHUNK = 1024 * 1024       # peer transfer chunk size (bytes)
HEAL_CHUNK_TIMEOUT = 240       # ssh timeout for one chunk
HEAL_CHUNK_RETRIES = 2
AGENT_LOG_PATH = "/var/log/hive-agent.log"
MINER_STOP_FLAG = "/run/hive/MINER_STOP"
MINER_RUN_FLAG = "/run/hive/MINER_RUN"
MINER_LOG_DIR = "/var/log/miner"
HEAL_DOWNLOADS_DIR = "/hive/miners/custom/downloads"
HEAL_CUSTOM_DIR = "/hive/miners/custom"

_heal_state_lock = threading.Lock()
_heal_state_cache = {"loaded": False, "data": None}

def _heal_state_load():
    with _heal_state_lock:
        if _heal_state_cache["loaded"]:
            return _heal_state_cache["data"]
        st = {"last_check": 0, "last_action": "", "last_host_fix": 0, "last_miner_heal": 0,
              "last_transfer": "", "last_rigid_fix": 0, "last_rigid_restart": 0,
              "last_wd_fix": 0, "zombie_streak": 0, "zombie_series_since": 0,
              "rig_id_ok": None}
        try:
            with open(HEAL_STATE_PATH, 'r') as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in st:
                    if k in disk:
                        st[k] = disk[k]
        except Exception:
            pass
        _heal_state_cache["data"] = st
        _heal_state_cache["loaded"] = True
        return st

def _heal_state_save(st):
    with _heal_state_lock:
        _heal_state_cache["data"] = st
        _heal_state_cache["loaded"] = True
        try:
            _atomic_write_file(HEAL_STATE_PATH, json.dumps(st, indent=2))
        except Exception as e:
            logging.error(f"Failed to save heal state: {e}")

def _conf_value(path, key):
    """Read a KEY="value" entry from a hive shell config file."""
    try:
        with open(path, 'r') as f:
            for line in f:
                m = re.match(r'^%s="(.*)"\s*$' % re.escape(key), line)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None

def _agent_log_no_connection():
    """True when the hive agent recently logged 'No connection with API server.
    Stop the miner' (its built-in policy that kills miner starts)."""
    try:
        with open(AGENT_LOG_PATH, 'r', errors='ignore') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            return "No connection with API server. Stop the miner" in f.read()
    except Exception:
        return False

_HIVE_DATE_FORMATS = ("%a %d %b %Y %I:%M:%S %p %z", "%a %d %b %Y %H:%M:%S %z")

def _agent_log_last_noconn_ts():
    """Epoch of the most recent 'No connection with API server. Stop the miner'
    line in the hive agent log (0 when absent or unparsable)."""
    try:
        with open(AGENT_LOG_PATH, 'r', errors='ignore') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            text = f.read()
        idx = text.rfind("No connection with API server. Stop the miner")
        if idx < 0:
            return 0
        line_start = text.rfind("\n", 0, idx) + 1
        m = re.match(r'^\[([^\]]+)\]', text[line_start:])
        if not m:
            return 0
        stamp = m.group(1).strip()
        for fmt in _HIVE_DATE_FORMATS:
            try:
                return int(datetime.strptime(stamp, fmt).timestamp())
            except ValueError:
                continue
        return 0
    except Exception:
        return 0

def _heal_parse_url_basename(url):
    """'https://host/path/srbminer_custom-3.7.4.tar.gz' -> (name, version, basename)."""
    base = os.path.basename(str(url or "").strip())
    m = re.match(r'^([A-Za-z0-9._-]+?)-([0-9][A-Za-z0-9._-]*)\.tar\.gz$', base)
    if not m:
        return None, None, base
    return m.group(1), m.group(2), base

def _majority_peers_value(values):
    """Most common non-empty value."""
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]

def _heal_check_api_host(st, report):
    """rig.conf API host sanity: if the agent suffered 'No connection ... Stop the
    miner' and our host is plain http or differs from the peer majority, adopt the
    peers' host and restart the hive agent."""
    our_host = _conf_value(RIG_CONF_PATH, "HIVE_HOST_URL")
    if not our_host:
        return
    agent_hurt = _agent_log_no_connection()
    if not agent_hurt and not our_host.startswith("http://"):
        return
    now = int(time.time())
    if now - int(st.get("last_host_fix") or 0) < HEAL_HOST_GAP:
        report["notes"].append("host fix skipped (rate limit)")
        return
    peers_hosts = []
    try:
        state = load_cluster_state()
        for rig in state.get("rigs", []):
            if rig.get("is_self") or rig.get("id") == state.get("self_id"):
                continue
            ok, data, _code, _err, _acc = cluster_remote_api(rig, "GET", "api/heal/info", timeout=20)
            if ok and isinstance(data, dict):
                peers_hosts.append(str(data.get("hive_host") or ""))
    except Exception as e:
        report["notes"].append("peer host query failed: %s" % e)
    majority = _majority_peers_value(peers_hosts)
    if not majority or not majority.startswith("https://") or majority == our_host:
        return
    # backup + rewrite HIVE_HOST_URL only
    try:
        with open(RIG_CONF_PATH, 'r') as f:
            content = f.read()
        backup = RIG_CONF_PATH + ".bak-heal-%d" % now
        with open(backup, 'w') as f:
            f.write(content)
        new_content, n = re.subn(r'^HIVE_HOST_URL=".*"$', 'HIVE_HOST_URL="%s"' % majority,
                                 content, count=1, flags=re.M)
        if n != 1:
            report["notes"].append("HIVE_HOST_URL not found in rig.conf")
            return
        _atomic_write_file(RIG_CONF_PATH, new_content, mode=0o644)
        guard_note_config_change(["rig.conf"])
        run_command("sudo systemctl restart hive")
        st["last_host_fix"] = now
        st["last_action"] = "api host fixed to %s (was %s)" % (majority, our_host)
        st["last_transfer"] = ""
        _heal_state_save(st)
        report["fixed"].append(st["last_action"])
        logging.warning("Self-heal: %s", st["last_action"])
        record_metrics_event("warning", "Self-heal: rig API host was '%s' (agent kept stopping the miner) - switched to '%s'" % (our_host, majority))
    except Exception as e:
        logging.error("Self-heal: host fix failed: %s", e)
        report["notes"].append("host fix failed: %s" % e)

def _conf_content_value(content, key):
    """Read a KEY="value" entry from an already-loaded hive shell config."""
    m = re.search(r'^%s="(.*)"\s*$' % re.escape(key), content or "", flags=re.M)
    return m.group(1) if m else None

def _agent_log_numeric_rig_ids():
    """Numeric rig ids seen in hive agent stats pushes ('"rig_id":"12345"')."""
    try:
        with open(AGENT_LOG_PATH, 'r', errors='ignore') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 500000))
            vals = re.findall(r'"rig_id":"(\d+)"', f.read())
        if vals:
            return vals[-1]
    except Exception:
        pass
    return None

def _rigid_candidates(st):
    """(rig_id, worker_name) candidates to restore a poisoned rig.conf RIG_ID,
    most trustworthy first: heal record, agent log, guard snapshot, newest
    numeric backup. Non-numeric sources are skipped - a backup written while
    the config was already broken must never be restored."""
    out = []
    rec = st.get("rig_id_ok") or {}
    if str(rec.get("rig_id") or "").isdigit():
        out.append((str(rec["rig_id"]), str(rec.get("worker_name") or "")))
    log_id = _agent_log_numeric_rig_ids()
    if log_id:
        out.append((log_id, ""))
    try:
        snap = ((_guard_load().get("snapshot") or {}).get("rig.conf")) or ""
        rid = _conf_content_value(snap, "RIG_ID")
        if rid and rid.isdigit():
            out.append((rid, _conf_content_value(snap, "WORKER_NAME") or ""))
    except Exception:
        pass
    try:
        baks = sorted((p for p in glob.glob(RIG_CONF_PATH + ".bak*") if os.path.isfile(p)),
                      key=os.path.getmtime, reverse=True)
        for p in baks:
            try:
                with open(p, 'r', errors='ignore') as f:
                    c = f.read()
                rid = _conf_content_value(c, "RIG_ID")
                if rid and rid.isdigit():
                    out.append((rid, _conf_content_value(c, "WORKER_NAME") or ""))
                    break
            except Exception:
                continue
    except Exception:
        pass
    return out

def _agent_log_last_push_state(after_ts):
    """'invalid' when the newest agent push response logged after after_ts is an
    'invalid request' error, 'ok' when it carries a result, '' when unknown."""
    try:
        with open(AGENT_LOG_PATH, 'r', errors='ignore') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            text = f.read()
        best_ts, best_state = 0, ""
        for line in text.splitlines():
            if '"result"' not in line and 'invalid request' not in line:
                continue
            m = re.match(r'^\[([^\]]+)\]', line)
            if not m:
                continue
            ts = 0
            for fmt in _HIVE_DATE_FORMATS:
                try:
                    ts = int(datetime.strptime(m.group(1).strip(), fmt).timestamp())
                    break
                except ValueError:
                    continue
            if ts > best_ts:
                best_ts = ts
                best_state = "invalid" if "invalid request" in line else "ok"
        if best_ts and best_ts >= after_ts:
            return best_state
    except Exception:
        pass
    return ""

def _heal_check_rig_id(st, report):
    """rig.conf RIG_ID must stay the NUMERIC cloud worker id: the hive agent
    authenticates its stats pushes with it and its policy stops the miner once
    the cloud keeps rejecting them (-32602 'invalid request'). Restore the
    numeric id when a string one (rename leftovers, stale snapshot) shows up,
    refresh the guard snapshot so it enforces the fixed file, and escalate to a
    hive agent restart when the agent keeps logging invalid pushes."""
    rid = _conf_value(RIG_CONF_PATH, "RIG_ID")
    now = int(time.time())
    wname = _conf_value(RIG_CONF_PATH, "WORKER_NAME") or ""
    if rid and rid.isdigit():
        rec = st.get("rig_id_ok") or {}
        if rec.get("rig_id") != rid or rec.get("worker_name") != wname:
            st["rig_id_ok"] = {"rig_id": rid, "worker_name": wname, "ts": now}
        # escalation: fixed earlier but the agent still logs invalid pushes
        fix_ts = int(st.get("last_rigid_fix") or 0)
        if fix_ts and now - fix_ts >= HEAL_RIGID_ESCALATE and \
                now - int(st.get("last_rigid_restart") or 0) >= HEAL_RIGID_RESTART_GAP and \
                _agent_log_last_push_state(fix_ts) == "invalid":
            try:
                run_command("sudo systemctl restart hive")
                st["last_rigid_restart"] = now
                _heal_state_save(st)
                report["fixed"].append("hive agent restarted (rig id fixed but pushes still invalid)")
                record_metrics_event("warning", "Self-heal: RIG_ID was fixed but the agent kept logging invalid pushes - restarted the hive agent")
            except Exception as e:
                report["notes"].append("agent restart failed: %s" % e)
        return
    if not rid:
        report["notes"].append("rig id: RIG_ID missing from rig.conf")
        return
    candidate = _rigid_candidates(st)
    if not candidate:
        report["notes"].append("rig id: no numeric candidate found (record/log/snapshot/backups)")
        return
    new_id, new_worker = candidate[0]
    try:
        with open(RIG_CONF_PATH, 'r') as f:
            content = f.read()
        with open(RIG_CONF_PATH + ".bak-healrigid-%d" % now, 'w') as f:
            f.write(content)
        new_content, n = re.subn(r'^RIG_ID=".*"$', 'RIG_ID="%s"' % new_id, content, count=1, flags=re.M)
        if n != 1:
            report["notes"].append("rig id: RIG_ID line not found in rig.conf")
            return
        worker = new_worker or wname
        if worker:
            new_content, nw = re.subn(r'^WORKER_NAME=".*"$', 'WORKER_NAME="%s"' % worker,
                                      new_content, count=1, flags=re.M)
            if nw != 1:
                worker = ""  # WORKER_NAME line absent - leave the file as parsed
        _atomic_write_file(RIG_CONF_PATH, new_content, mode=0o644)
        guard_note_config_change(["rig.conf"])
        st["last_rigid_fix"] = now
        st["rig_id_ok"] = {"rig_id": new_id, "worker_name": worker, "ts": now}
        st["last_action"] = "RIG_ID restored to %s (was '%s')" % (new_id, rid)
        _heal_state_save(st)
        report["fixed"].append(st["last_action"])
        logging.warning("Self-heal: %s", st["last_action"])
        record_metrics_event("warning", "Self-heal: rig.conf RIG_ID was '%s' (the agent stops the miner when the cloud rejects its pushes) - restored numeric worker id '%s'" % (rid, new_id))
    except Exception as e:
        logging.error("Self-heal: rig id fix failed: %s", e)
        report["notes"].append("rig id fix failed: %s" % e)

def _heal_archive_path(name, ver, basename):
    if name and ver:
        return os.path.join(HEAL_DOWNLOADS_DIR, "%s-%s.tar.gz" % (name, ver))
    return os.path.join(HEAL_DOWNLOADS_DIR, basename) if basename else None

def _archive_valid(path):
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 1024:
            return False
        code = subprocess.run(["gunzip", "-t", path], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode
        return code == 0
    except Exception:
        return False

def _heal_fetch_chunk(peer_rig, remote_path, chunk_index, out_path, expected_len):
    """Fetch one 1 MB chunk of a peer file as base64 over the SSH routes."""
    cmd = ("dd if=%s bs=%d skip=%d count=1 2>/dev/null | base64 -w0"
           % (remote_path, HEAL_CHUNK, chunk_index))
    for access in _ordered_rig_accesses(peer_rig):
        ok, out, err = run_ssh_command(access, cmd, timeout=HEAL_CHUNK_TIMEOUT)
        if not ok:
            logging.info("Self-heal: chunk %d via %s failed: %s", chunk_index,
                         access.get("name") or access.get("id"), err)
            continue
        try:
            data = base64.b64decode(out.strip().encode("ascii"), validate=True)
        except Exception:
            continue
        if expected_len and len(data) != expected_len:
            continue
        return data
    return None

def _heal_restore_package(name, ver, basename, url, peer_rig, peer_name, st, report):
    """Copy the tarball from the peer chunk by chunk, then let custom-get install."""
    remote_path = _heal_archive_path(name, ver, basename)
    if not remote_path:
        return False
    ok, data, _code, err, _acc = cluster_remote_api(
        peer_rig, "GET", "api/heal/pkg?name=%s&ver=%s" % (name, ver), timeout=20)
    if not ok or not isinstance(data, dict) or not data.get("valid"):
        report["notes"].append("peer %s reports no valid archive" % peer_name)
        return False
    total = int(data.get("size") or 0)
    if total <= 0:
        return False
    nchunks = (total + HEAL_CHUNK - 1) // HEAL_CHUNK
    local_path = remote_path
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        cur = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        if cur > total:
            os.remove(local_path)
            cur = 0
        if cur % HEAL_CHUNK:
            with open(local_path, "r+b") as f:  # drop a torn tail chunk
                f.truncate((cur // HEAL_CHUNK) * HEAL_CHUNK)
            cur = (cur // HEAL_CHUNK) * HEAL_CHUNK
        start_chunk = cur // HEAL_CHUNK
        for i in range(start_chunk, nchunks):
            expected = HEAL_CHUNK if i < nchunks - 1 else total - i * HEAL_CHUNK
            got = None
            for _r in range(HEAL_CHUNK_RETRIES + 1):
                got = _heal_fetch_chunk(peer_rig, remote_path, i, local_path, expected)
                if got is not None:
                    break
            if got is None:
                st["last_transfer"] = "chunk %d/%d failed" % (i + 1, nchunks)
                _heal_state_save(st)
                report["notes"].append(st["last_transfer"])
                return False
            with open(local_path, "ab") as f:
                f.write(got)
        if not _archive_valid(local_path):
            os.remove(local_path)
            report["notes"].append("assembled archive failed gunzip -t")
            return False
    except Exception as e:
        logging.error("Self-heal: transfer from %s failed: %s", peer_name, e)
        report["notes"].append("transfer failed: %s" % e)
        return False
    # Install with hive's own tooling and start the miner if it is still down
    out, err, code = run_command("sudo %s/custom-get %s" % (HEAL_CUSTOM_DIR, shlex.quote(url)))
    installed = os.path.isdir(os.path.join(HEAL_CUSTOM_DIR, name))
    now = int(time.time())
    st["last_miner_heal"] = now
    st["last_transfer"] = "restored %s-%s from %s" % (name, ver, peer_name)
    st["last_action"] = "custom miner package restored" if installed else "custom-get failed (code %s)" % code
    _heal_state_save(st)
    if not installed:
        report["notes"].append("custom-get failed: %s" % (err or out or code))
        return False
    report["fixed"].append("miner package %s-%s restored from peer %s" % (name, ver, peer_name))
    logging.warning("Self-heal: miner package %s-%s restored from peer %s", name, ver, peer_name)
    record_metrics_event("warning", "Self-heal: miner package '%s' (%s) was missing - restored from rig '%s' and miner restarted" % (name, ver, peer_name))
    if not os.path.exists(MINER_RUN_FLAG):
        run_command(MINER_START_CMD)
    return True

def _heal_check_miner(st, report):
    """Miner down + not intentionally stopped: fix the package / clear a dead
    screen / start the miner."""
    miner = _conf_value(RIG_CONF_PATH, "MINER")
    if not miner or miner == "none":
        return
    maintenance = _conf_value(RIG_CONF_PATH, "MAINTENANCE")
    if maintenance and maintenance.strip() not in ("", "0"):
        return
    if os.path.exists(MINER_STOP_FLAG):
        return  # intentional stop (panel/cloud/autofan critical) - do not fight it
    running = os.path.exists(MINER_RUN_FLAG)
    if running:
        return
    now = int(time.time())
    if now - int(st.get("last_miner_heal") or 0) < HEAL_MINER_GAP:
        report["notes"].append("miner heal skipped (rate limit)")
        return
    out, _err, code = run_command("sudo screen -ls")
    screen_alive = code == 0 and ".miner" in (out or "")
    download_active = False
    pkg_missing = False
    if miner == "custom":
        custom_miner = _conf_value(WALLET_CONF_PATH, "CUSTOM_MINER")
        out2, _e2, _c2 = run_command("pgrep -af custom-get")
        download_active = bool((out2 or "").strip())
        pkg_missing = bool(custom_miner) and not os.path.isdir(os.path.join(HEAL_CUSTOM_DIR, custom_miner))
        if pkg_missing and download_active:
            return  # hive's own custom-get/wget is working on the download
        if pkg_missing and not download_active:
            url = _conf_value(WALLET_CONF_PATH, "CUSTOM_INSTALL_URL")
            name, ver, basename = _heal_parse_url_basename(url)
            if url and name:
                # a peer that already runs this version ships the tarball
                try:
                    state = load_cluster_state()
                except Exception as e:
                    state = None
                    report["notes"].append("cluster state unavailable: %s" % e)
                peers = []
                if state:
                    peers = [r for r in state.get("rigs", [])
                             if not r.get("is_self") and r.get("id") != state.get("self_id")]
                for peer in peers:
                    peer_name = peer.get("name") or peer.get("id")
                    if _heal_restore_package(custom_miner, ver, basename, url, peer, peer_name, st, report):
                        return
                # no peer had a usable archive: fall through - a dead screen is
                # cleared below so hive's own downloader can retry GitHub
    if not download_active and screen_alive:
        # dead screen left by a failed start blocks every "miner start" with
        # "screen session is already running" - clear it and start over
        run_command("sudo /hive/bin/miner stop")
        time.sleep(3)
        run_command(MINER_START_CMD)
        st["last_miner_heal"] = now
        st["last_action"] = "stale miner screen cleared - miner restarted"
        _heal_state_save(st)
        report["fixed"].append(st["last_action"])
        logging.warning("Self-heal: %s", st["last_action"])
        record_metrics_event("warning", "Self-heal: dead miner screen cleared, miner restarted")
        return
    if pkg_missing:
        return  # package still missing, nothing usable to transfer this cycle
    # Package present (or non-custom miner): agent may have died or crashed.
    # The agent stops miners when it loses the API dialog and only restarts them
    # on a cloud command - do not fight it while its complaint is fresh (<1h);
    # an old line in a rotated log must not block healing forever.
    noconn_ts = _agent_log_last_noconn_ts()
    if noconn_ts and time.time() - noconn_ts < 3600:
        report["notes"].append("agent reported API connection loss recently (%d min ago); miner start skipped"
                               % (int(time.time() - noconn_ts) // 60))
        return
    run_command(MINER_START_CMD)
    st["last_miner_heal"] = now
    st["last_action"] = "miner was down (no MINER_RUN, no MINER_STOP) - started"
    _heal_state_save(st)
    report["fixed"].append(st["last_action"])
    logging.warning("Self-heal: %s", st["last_action"])
    record_metrics_event("warning", "Self-heal: miner was not running - started it (flight sheet is active)")

def _heal_check_zombie(st, report):
    """Miner process alive but zero hashrate for a long time (zombie workers:
    seen after Xid faults or live OC changes - SRBMiner keeps the process and
    screen, every worker sits at 0.00 H/s): restart it. Nothing else in the
    stack reacts to this state: oc_recovery needs a restart loop or a fresh
    Xid burst, hive wd is disabled on most rigs."""
    miner = _conf_value(RIG_CONF_PATH, "MINER")
    if not miner or miner == "none":
        return
    if not os.path.exists(MINER_RUN_FLAG):
        return  # miner down: _heal_check_miner owns that case
    if os.path.exists(MINER_STOP_FLAG):
        return
    maintenance = _conf_value(RIG_CONF_PATH, "MAINTENANCE")
    if maintenance and maintenance.strip() not in ("", "0"):
        return
    try:
        if (_oc_state_load() or {}).get("active"):
            return  # oc_recovery is walking clocks - do not fight its tests
    except Exception:
        pass
    now = int(time.time())
    if _oc_read_khs() > 0:
        st["zombie_zero_since"] = 0
        st["zombie_streak"] = 0
        st["zombie_series_since"] = 0
        return
    zs = int(st.get("zombie_zero_since") or 0)
    if not zs:
        st["zombie_zero_since"] = now
        return
    if now - zs < HEAL_ZOMBIE_ZERO_SECONDS:
        return
    # Escalation: restarts that never produced hashrate mean the miner (or the
    # network under it) needs a machine-level reset, not another restart.
    # RIG1 25 Oct: the miner sat at zero for 11 hours and was "restarted" 10
    # times without any effect - a single reboot cured it in 4 minutes.
    streak = int(st.get("zombie_streak") or 0)
    series = int(st.get("zombie_series_since") or 0)
    if streak >= HEAL_ZOMBIE_ESCALATE_STREAK and series and now - series >= HEAL_ZOMBIE_ESCALATE_ZERO:
        nst = _net_heal_state_load()
        issued, desc = _net_heal_try_reboot(
            nst, "miner zero for %d min despite %d restarts" % ((now - series) // 60, streak))
        _net_heal_state_save(nst)
        if issued:
            st["last_zombie_heal"] = now
            st["zombie_zero_since"] = 0
            st["zombie_streak"] = 0
            st["zombie_series_since"] = 0
            st["last_action"] = "zombie miner unrecoverable - " + desc
            report["fixed"].append(st["last_action"])
            logging.warning("Self-heal: %s", st["last_action"])
            record_metrics_event("danger", "Self-heal: " + st["last_action"])
            return
        report["notes"].append("zombie escalation deferred (%s)" % desc)
    if now - int(st.get("last_zombie_heal") or 0) < HEAL_ZOMBIE_GAP:
        report["notes"].append("zombie restart skipped (rate limit)")
        return
    noconn_ts = _agent_log_last_noconn_ts()
    if noconn_ts and time.time() - noconn_ts < 3600:
        report["notes"].append("agent reported API connection loss recently; zombie restart skipped")
        return
    run_command(MINER_RESTART_CMD)
    st["last_zombie_heal"] = now
    st["zombie_zero_since"] = 0
    st["zombie_streak"] = streak + 1
    if not series:
        st["zombie_series_since"] = now
    st["last_action"] = "zombie miner restarted (alive but zero hashrate for %d min)" % ((now - zs) // 60)
    report["fixed"].append(st["last_action"])
    logging.warning("Self-heal: %s", st["last_action"])
    record_metrics_event("warning", "Self-heal: " + st["last_action"])


def heal_run_check():
    """One self-heal pass: rig id sanity + API host sanity + miner/package repair."""
    st = _heal_state_load()
    report = {"checked": int(time.time()), "fixed": [], "notes": []}
    try:
        _heal_check_rig_id(st, report)
    except Exception as e:
        logging.error("Self-heal: rig id check failed: %s", e)
        report["notes"].append("rig id check failed: %s" % e)
    try:
        _heal_check_api_host(st, report)
    except Exception as e:
        logging.error("Self-heal: api host check failed: %s", e)
        report["notes"].append("api host check failed: %s" % e)
    try:
        _heal_check_miner(st, report)
    except Exception as e:
        logging.error("Self-heal: miner check failed: %s", e)
        report["notes"].append("miner check failed: %s" % e)
    try:
        _heal_check_zombie(st, report)
    except Exception as e:
        logging.error("Self-heal: zombie check failed: %s", e)
        report["notes"].append("zombie check failed: %s" % e)
    try:
        _heal_check_wd_intent(st, report)
    except Exception as e:
        logging.error("Self-heal: watchdog intent check failed: %s", e)
        report["notes"].append("watchdog intent check failed: %s" % e)
    st["last_check"] = report["checked"]
    if report["fixed"]:
        st["last_action"] = "; ".join(report["fixed"])
    _heal_state_save(st)
    return report

def _heal_worker():
    time.sleep(HEAL_BOOT_DELAY)
    while True:
        try:
            heal_run_check()
        except Exception as e:
            logging.error(f"Self-heal check failed: {e}")
        time.sleep(HEAL_CHECK_INTERVAL)

def start_heal_worker():
    t = threading.Thread(target=_heal_worker, daemon=True, name="self-heal")
    t.start()
    logging.info("Self-heal worker started (rig id guard, package repair, API host fix, stale screen)")

@app.route('/api/heal/info')
def api_heal_info():
    """Peer info used by the self-heal API-host consensus check."""
    return jsonify({"success": True, "hive_host": _conf_value(RIG_CONF_PATH, "HIVE_HOST_URL") or ""})

@app.route('/api/heal/pkg')
def api_heal_pkg():
    """Peer check for a custom miner tarball (used by the self-heal transfer)."""
    name = request.args.get("name", "")
    ver = request.args.get("ver", "")
    if not re.match(r'^[A-Za-z0-9._-]+$', name or "") or not re.match(r'^[A-Za-z0-9._-]+$', ver or ""):
        return jsonify({"success": False, "message": "Bad package name"}), 400
    path = _heal_archive_path(name, ver, None)
    if not path:
        return jsonify({"success": False, "message": "Bad package name"}), 400
    exists = os.path.exists(path)
    return jsonify({"success": True, "exists": exists, "size": os.path.getsize(path) if exists else 0,
                    "valid": _archive_valid(path) if exists else False})

@app.route('/api/heal/status')
def api_heal_status():
    return jsonify({"success": True, "heal": _heal_state_load(),
                    "oc_recovery": _oc_state_load(),
                    "net_heal": _net_heal_state_load()})

# ---------------- Network self-heal: Wi-Fi/link recovery WITHOUT reboot ----------------
# A local worker that detects layered connectivity loss (interface gone, wifi
# de-associated, no DHCP route, gateway unreachable, internet unreachable) and
# recovers it with soft actions only. Reboot is the LAST resort, gated by a
# long arm time, a min gap, and quiet hours (fans are loud at night).
# Key distinction: gateway OK + internet dead USED to mean "router WAN problem,
# rig not at fault -> no action". RIG1 23 Oct proved a hole in that matrix: its
# own USB-wifi uplink died while the router WAN stayed alive (peers kept
# mining) - the rig sat uselessly at zero hashrate for 29 minutes and then died
# entirely. Now a persistent "gateway OK + internet dead" is classified with
# cluster evidence: any peer reporting fresh net-heal state 'ok' (its miner
# hashes = its internet works) proves the WAN is alive and OUR uplink is the
# dead part -> the recovery ladder engages. Fresh 'inet_down' on peers = WAN
# down for everyone -> still no rig action. No peer evidence at all -> after a
# long window run SOFT steps only (reboot disabled: never reboot on a guess).

NET_HEAL_STATE_PATH = os.path.join(HIVE_CONFIG_DIR, "net_heal.json")
NET_HEAL_WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
NET_HEAL_NETPLAN_DIR = "/etc/netplan"
NET_HEAL_CHECK_INTERVAL = 60        # probe period
NET_HEAL_BOOT_DELAY = 240           # wifi needs time to associate after boot
NET_HEAL_GRACE = 240                # external causes (router/AP reboot) self-heal in this window
NET_HEAL_STEP_GAP = 120             # give each recovery step this long to take effect
NET_HEAL_REBOOT_ARM = 600           # ladder exhausted -> still dead this long -> reboot eligible
NET_HEAL_REBOOT_GAP = 6 * 3600      # min time between net-heal reboots
NET_HEAL_REBOOT_QUIET = (23, 8)     # local hours: reboot deferred (loud fans at night)
NET_HEAL_REBOOT_RETRY = 600         # deferred reboot (quiet hours/rate limit): retry pacing
NET_HEAL_FLAP_WINDOW = 3600         # flap-storm accumulator window
NET_HEAL_FLAP_THRESHOLD = 4         # short outages within the window -> storm -> ladder
NET_HEAL_FLAP_MAX_LEN = 600         # an outage longer than this is an outage, not a flap
NET_HEAL_TCP_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 53))
NET_HEAL_INET_TIMEOUT = 4
NET_HEAL_GRACE_LAYERS = ("not-associated", "gateway-unreachable", "no-default-route")
NET_HEAL_STATIC_RETRY = 600         # re-assert static wifi ip at most this often
NET_HEAL_STATIC_BAK_KEEP = 3
NET_HEAL_SELF_INET_CONFIRM = 120    # gateway OK + internet dead this long -> ask the cluster
NET_HEAL_PEER_CHECK_GAP = 300       # min interval between peer verdict rounds
NET_HEAL_PEER_FRESH = 300           # peer net-heal last_check max age for a valid verdict
NET_HEAL_PEER_MAX = 3               # peers queried per verdict round
NET_HEAL_NOPEER_INET_WAIT = 600     # no peer evidence -> soft ladder (reboot OFF) after this long
NET_HEAL_SOFT_CYCLE_GAP = 1800      # min spacing between repeated no-reboot soft cycles

_net_heal_state_lock = threading.Lock()
_net_heal_state_cache = {"loaded": False, "data": None}

def _net_heal_state_load():
    with _net_heal_state_lock:
        if _net_heal_state_cache["loaded"]:
            return _net_heal_state_cache["data"]
        st = {"state": "ok", "last_check": 0, "last_action": "", "layer": "",
              "broken_since": 0, "step": 0, "step_ts": 0, "driver": "",
              "last_reboot": 0, "reboots": [], "inet_down_since": 0,
              "static": None, "static_try": 0,
              "no_reboot": False, "wan_down_noted": False,
              "peer_check_ts": 0, "soft_cycle_ts": 0, "peer_verdict": {},
              "flaps": [], "obs_problem": False}
        try:
            with open(NET_HEAL_STATE_PATH, 'r') as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in st:
                    if k in disk:
                        st[k] = disk[k]
        except Exception:
            pass
        _net_heal_state_cache["data"] = st
        _net_heal_state_cache["loaded"] = True
        return st

def _net_heal_state_save(st):
    with _net_heal_state_lock:
        _net_heal_state_cache["data"] = st
        _net_heal_state_cache["loaded"] = True
        try:
            _atomic_write_file(NET_HEAL_STATE_PATH, json.dumps(st, indent=2))
        except Exception as e:
            logging.error("Failed to save net heal state: %s", e)

def _net_heal_wifi_iface():
    """First wireless interface name, or None on non-wifi rigs."""
    try:
        for d in sorted(os.listdir('/sys/class/net')):
            if os.path.isdir('/sys/class/net/%s/wireless' % d):
                return d
    except Exception:
        pass
    return None

def _net_heal_carrier_state(iface):
    """'missing' (driver gone), 'down' (admin down / not associated), 'up'."""
    base = '/sys/class/net/%s' % iface
    if not os.path.exists(base):
        return "missing"
    try:
        with open(base + '/carrier', 'r') as f:
            return "up" if f.read().strip() == "1" else "down"
    except Exception:
        return "down"  # OSError EINVAL when admin-down

def _net_heal_gateway(iface):
    """(gw_ip|None, has_default_route) for the given interface."""
    out, _err, _rc = run_command("ip route show default")
    for line in (out or "").splitlines():
        if " dev %s " % iface not in line and not line.rstrip().endswith(" dev %s" % iface):
            continue
        m = re.search(r'via (\S+)', line)
        return (m.group(1) if m else None), True
    return None, False

def _net_heal_run(cmd, timeout=30):
    """run_command under coreutils timeout: a hung modprobe/netplan/systemctl
    must never wedge the worker thread (the ladder would stop advancing and
    the recovery would stall until the next panel restart)."""
    return run_command("timeout %d %s" % (int(timeout), cmd))

def _net_heal_ping(gw):
    _out, _err, rc = _net_heal_run("ping -c1 -W2 %s" % gw, 10)
    return rc == 0

def _net_heal_inet_ok():
    """True when any public TCP endpoint is reachable (pure IP, no DNS)."""
    for host, port in NET_HEAL_TCP_PROBES:
        try:
            s = socket.create_connection((host, port), timeout=NET_HEAL_INET_TIMEOUT)
            s.close()
            return True
        except Exception:
            continue
    return False

def _net_heal_detect_driver(iface):
    try:
        p = os.path.realpath('/sys/class/net/%s/device/driver' % iface)
        if p and os.path.basename(p):
            return os.path.basename(p)
    except Exception:
        pass
    return ""

def _net_heal_wpa_conf_fix():
    """netpre writes key_mgmt=PSK (invalid, must be WPA-PSK) into
    wpa_supplicant.conf on some boots - wpa_supplicant then fails to parse and
    the service dies. Sanitize with an ATOMIC, validated write: this file is
    parsed at every boot, a truncated/corrupted write here would kill wifi
    persistently (the exact disaster class this worker must never cause)."""
    path = NET_HEAL_WPA_CONF
    tmp = path + ".tmp-netheal"
    try:
        with open(path, 'r') as f:
            content = f.read()
        if not re.search(r'key_mgmt\s*=\s*PSK\b', content):
            return False
        fixed = re.sub(r'key_mgmt\s*=\s*PSK\b', 'key_mgmt=WPA-PSK', content)
        # Never make it worse: a usable conf must keep ssid and psk.
        if not re.search(r'ssid\s*=', fixed) or not re.search(r'psk\s*=', fixed):
            return False
        try:
            mode = os.stat(path).st_mode & 0o777
            uid = os.stat(path).st_uid
            gid = os.stat(path).st_gid
        except Exception:
            mode, uid, gid = 0o600, 0, 0
        with open(tmp, 'w') as f:
            f.write(fixed)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, uid, gid)
        except Exception:
            pass
        bak = path + ".bak-netheal"
        if not os.path.exists(bak):
            try:
                with open(bak, 'w') as f:
                    f.write(content)
                os.chmod(bak, 0o600)
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False

def _net_heal_current_ip(iface):
    out, _err, _rc = _net_heal_run("ip -4 -o addr show dev %s" % iface, 15)
    m = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out or "")
    return m.group(1) if m else ""

def _net_heal_static_check(st, iface, now):
    """Re-assert the intended static wifi address after hive rewrites the
    netplan yaml to dhcp on boot (proven RIG8/RIG7 behavior: every reboot the
    rig DHCPs a foreign/other rig's IP and LAN conflicts return). Opt-in per
    rig via the 'static' key in net_heal.json (POST /api/net_heal/static).
    Safety: SSID/password are extracted from the CURRENT yaml (never stored or
    regenerated by us), the write is atomic with a rotating backup, and
    netplan apply on any parse error leaves the running config untouched -
    association itself never depends on the IP fields, so the worst case is
    'no change', not a dead adapter."""
    si = st.get("static")
    if not (isinstance(si, dict) and si.get("ip") and si.get("gw")):
        return
    want_ip = str(si["ip"]).split("/")[0]
    cur = _net_heal_current_ip(iface)
    if cur == want_ip:
        return
    if now - int(st.get("static_try") or 0) < NET_HEAL_STATIC_RETRY:
        return
    st["static_try"] = now
    target = None
    for p in sorted(glob.glob(os.path.join(NET_HEAL_NETPLAN_DIR, "*.yaml"))):
        try:
            with open(p, 'r') as f:
                text = f.read()
        except Exception:
            continue
        if "wifis:" in text and re.search(r'^\s+%s:\s*$' % re.escape(iface), text, re.M):
            target = (p, text)
            break
    if not target:
        _net_heal_state_save(st)
        return
    path, text = target
    m_ssid = re.search(r'access-points:\s*\n\s+([^\s#][^:\n]*):\s*$', text, re.M)
    m_pass = re.search(r'password:\s*"([^"]*)"', text)
    if not m_ssid or m_pass is None:
        _net_heal_state_save(st)
        return
    ssid, password = m_ssid.group(1).strip(), m_pass.group(1)
    gw = str(si["gw"])
    dns = si.get("dns") or [gw]
    dns_lines = "\n".join("       - %s" % d for d in dns)
    new_text = (
        "network:\n"
        " version: 2\n"
        " wifis:\n"
        "  %s:\n"
        "   dhcp4: no\n"
        "   addresses: [%s]\n"
        "   routes:\n"
        "     - to: default\n"
        "       via: %s\n"
        "   nameservers:\n"
        "     addresses:\n"
        "%s\n"
        "   access-points:\n"
        "    %s:\n"
        "     password: \"%s\"\n" % (iface, si["ip"], gw, dns_lines, ssid, password)
    )
    tmp = path + ".tmp-netheal"
    try:
        baks = sorted(glob.glob(path + ".bak-netheal-static-*"))
        for old in baks[:-max(1, NET_HEAL_STATIC_BAK_KEEP - 1)]:
            try:
                os.remove(old)
            except Exception:
                pass
        with open(path + ".bak-netheal-static-%d" % now, 'w') as f:
            f.write(text)
        with open(tmp, 'w') as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o644)
        except Exception:
            pass
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        _net_heal_state_save(st)
        return
    _net_heal_run("netplan apply", 90)
    st["last_action"] = "static wifi ip re-asserted (%s -> %s)" % (cur or "none", want_ip)
    record_metrics_event("warning", "Net-heal: " + st["last_action"])
    _net_heal_state_save(st)

_NET_HEAL_STEP_LABELS = {1: "dhcp renew", 2: "wpa restart", 3: "netplan apply",
                         4: "wifi driver reload", 5: "reboot (last resort)"}

def _net_heal_try_reboot(st, reason):
    """Quiet-hours + rate-limit gated sreboot, shared by the recovery ladder
    (step 5) and the zombie-miner escalation. Returns (issued, description)."""
    now = int(time.time())
    hour = time.localtime().tm_hour
    qs, qe = NET_HEAL_REBOOT_QUIET
    in_quiet = (hour >= qs or hour < qe) if qs > qe else (qs <= hour < qe)
    if in_quiet:
        return False, "reboot deferred (quiet hours %02d:00-%02d:00)" % (qs, qe)
    if now - int(st.get("last_reboot") or 0) < NET_HEAL_REBOOT_GAP:
        return False, "reboot deferred (rate limit)"
    run_command('nohup bash -c "sleep 1.5 && sudo /hive/sbin/sreboot" > /dev/null 2>&1 &')
    st["last_reboot"] = now
    reboots = list(st.get("reboots") or [])
    reboots.append(now)
    st["reboots"] = reboots[-20:]
    # The reboot ends the episode: a stale flap accumulator must not
    # re-trigger a storm right after the fresh boot.
    st["flaps"] = []
    st["obs_problem"] = False
    return True, "reboot issued (%s)" % reason


def _net_heal_apply_step(step, st, iface):
    """Run recovery step N. Returns (ok, description)."""
    if step == 1:
        cmds = []
        if _net_heal_carrier_state(iface) != "up":
            _net_heal_run("ip link set %s up" % iface, 15)
            cmds.append("ip link set %s up" % iface)
        _net_heal_run("networkctl renew %s" % iface, 30)
        cmds.append("networkctl renew %s" % iface)
        return True, "; ".join(cmds)
    if step == 2:
        conf_fixed = _net_heal_wpa_conf_fix()
        for unit in ("wpa_supplicant", "wpa_supplicant@%s" % iface):
            _net_heal_run("systemctl restart %s 2>/dev/null" % unit, 30)
        _net_heal_run("wpa_cli -i %s reassociate 2>/dev/null" % iface, 15)
        return True, "wpa restart%s" % (" + wpa conf sanitized" if conf_fixed else "")
    if step == 3:
        _net_heal_run("netplan apply", 90)
        return True, "netplan apply"
    if step == 4:
        drv = st.get("driver") or ""
        if not drv:
            return False, "driver unknown - reload skipped"
        _net_heal_run("sh -c 'modprobe -r %s && sleep 2 && modprobe %s'" % (drv, drv), 120)
        _net_heal_run("netplan apply", 90)
        return True, "driver reload %s" % drv
    if step == 5:
        if st.get("no_reboot"):
            return False, "reboot skipped (no peer evidence - soft mode)"
        issued, desc = _net_heal_try_reboot(st, "wifi dead > %d min" %
                                            ((int(time.time()) - int(st.get("broken_since") or time.time())) // 60))
        return issued, desc
    return False, "unknown step"

def _net_heal_reset_broken(st):
    st["state"] = "ok"
    st["layer"] = ""
    st["broken_since"] = 0
    st["step"] = 0
    st["step_ts"] = 0
    st["no_reboot"] = False
    st["wan_down_noted"] = False
    st["peer_check_ts"] = 0
    st["soft_cycle_ts"] = 0

def _net_heal_peer_inet_verdict():
    """Classify a persistent 'gateway OK + internet dead' with cluster evidence.

    Returns (verdict, detail): 'our_uplink' when at least one peer reports a
    FRESH net-heal state 'ok' (the peer's miner is hashing = its internet
    works = the router WAN is alive, so OUR uplink is the dead part),
    'wan_down' when a fresh 'inet_down' peer (and no contradicting 'ok') shows
    the internet is down for everyone, or None when there is no usable
    evidence (peers offline, no panels, wired rigs with 'n/a', stale states).
    Never raises."""
    detail = {"checked": {}}
    try:
        state = load_cluster_state()
    except Exception as e:
        detail["error"] = str(e)
        return None, detail
    self_id = state.get("self_id")
    ok_peer = wan_peer = None
    tries = 0
    now = int(time.time())
    for rig in state.get("rigs", []):
        if tries >= NET_HEAL_PEER_MAX:
            break
        if rig.get("is_self") or rig.get("id") == self_id:
            continue
        tries += 1
        try:
            ok, data, _code, _err, _acc = cluster_remote_api(rig, "GET", "api/heal/status", timeout=15)
        except Exception:
            continue
        if not ok or not isinstance(data, dict):
            continue
        nh = data.get("net_heal") or {}
        name = str(rig.get("name") or rig.get("id") or "?")
        st_name = str(nh.get("state") or "")
        try:
            lc = int(nh.get("last_check") or 0)
        except Exception:
            lc = 0
        fresh = bool(lc and now - lc <= NET_HEAL_PEER_FRESH)
        detail["checked"][name] = {"state": st_name, "fresh": fresh}
        if not fresh:
            continue
        if st_name == "ok" and ok_peer is None:
            ok_peer = name
        elif st_name == "inet_down" and wan_peer is None:
            wan_peer = name
    if ok_peer:
        return "our_uplink", {"peer": ok_peer, "checked": detail["checked"]}
    if wan_peer:
        return "wan_down", {"peer": wan_peer, "checked": detail["checked"]}
    return None, detail

def _net_heal_advance_ladder(st, iface, now):
    """Shared ladder driver: grace, step pacing, the no-reboot soft-cycle
    pacing, and step application. Used both by the classic broken-layer path
    and by the peer-verified self-uplink path."""
    # Before the first action: external causes (AP/router reboot) get a long
    # grace window, local causes get one probe interval of observation.
    # After that: give each recovery step NET_HEAL_STEP_GAP to take effect.
    if st["step"] == 0:
        wait = NET_HEAL_GRACE if st.get("layer") in NET_HEAL_GRACE_LAYERS else NET_HEAL_STEP_GAP
        if now - st["broken_since"] < wait:
            _net_heal_state_save(st)
            return
    elif now - st["step_ts"] < NET_HEAL_STEP_GAP:
        _net_heal_state_save(st)
        return
    # Advance the ladder. Steps 1-4 = soft actions; step 5 (reboot) arms only
    # after a long observation window past the last soft step, and retries
    # (paced) while deferred (quiet hours / rate limit). Without peer evidence
    # the reboot is disabled: paced soft cycles repeat instead.
    if st["step"] < 4:
        st["step"] += 1
        st["step_ts"] = now
    elif st["step"] == 4:
        if now - st["step_ts"] < NET_HEAL_REBOOT_ARM:
            _net_heal_state_save(st)
            return
        if st.get("no_reboot"):
            if now - int(st.get("soft_cycle_ts") or 0) < NET_HEAL_SOFT_CYCLE_GAP:
                st["last_action"] = "soft ladder exhausted - waiting before next soft cycle (reboot disabled: no peer evidence)"
                _net_heal_state_save(st)
                return
            st["soft_cycle_ts"] = now
            st["step"] = 1
            st["step_ts"] = now
        else:
            st["step"] = 5
            # Backdate so the first reboot attempt is immediate; deferred
            # retries (quiet hours / rate limit) then pace at RETRY, not at
            # the per-minute poll (the old code re-emitted the deferral
            # event every interval all night).
            st["step_ts"] = now - NET_HEAL_REBOOT_RETRY
    elif st["step"] == 5:
        if now - st["step_ts"] < NET_HEAL_REBOOT_RETRY:
            _net_heal_state_save(st)
            return
    ok, desc = _net_heal_apply_step(st["step"], st, iface)
    if st["step"] == 5 and not ok:
        # Deferred (quiet hours / rate limit): pace the next attempt at RETRY
        # instead of re-emitting the deferral every poll interval.
        st["step_ts"] = now
    st["last_action"] = "step %d (%s): %s" % (st["step"], _NET_HEAL_STEP_LABELS.get(st["step"], "?"), desc)
    record_metrics_event("warning" if ok or st["step"] < 5 else "info", "Net-heal: " + st["last_action"])
    _net_heal_state_save(st)

def _net_heal_record_flap(st, now):
    """Append a short-outage recovery to the flap-storm accumulator.
    Returns the number of episodes within the window."""
    flaps = [int(t) for t in (st.get("flaps") or [])
             if now - int(t) <= NET_HEAL_FLAP_WINDOW]
    flaps.append(now)
    st["flaps"] = flaps[-16:]
    return len(flaps)


def _net_heal_flap_ended(st, now, had_problem):
    """True when a SHORT outage just ended: the previous check observed a
    problem, this one is healthy, and the broken spell is flap-length (a long
    outage ending in recovery is an outage, not a flap)."""
    if not had_problem or not st.get("broken_since"):
        return False
    return (now - int(st["broken_since"])) < NET_HEAL_FLAP_MAX_LEN


def _net_heal_storm_engage(st, iface, now, count):
    st["layer"] = "flap-storm"
    st["state"] = "broken"
    st["broken_since"] = now
    st["step"] = 0
    st["step_ts"] = 0
    st["no_reboot"] = False
    st["driver"] = st.get("driver") or _net_heal_detect_driver(iface)
    st["last_action"] = ("flap storm detected (%d short outages in %d min) - recovery ladder engaged"
                         % (count, NET_HEAL_FLAP_WINDOW // 60))
    record_metrics_event("warning", "Net-heal: " + st["last_action"])
    _net_heal_state_save(st)


def _net_heal_storm_healthy(st, iface, now, had_problem):
    """Healthy observation while a flap storm is active: count the flap if one
    just ended, keep the ladder armed (it advances on the next problem
    observation - individual recoveries must not abort it, that is exactly how
    the RIG1 25 Oct storm survived for 11 h), and end the storm only after a
    full quiet window without episodes."""
    st["obs_problem"] = False  # this check observed a healthy network
    if _net_heal_flap_ended(st, now, had_problem):
        _net_heal_record_flap(st, now)
    flaps = [int(t) for t in (st.get("flaps") or []) if now - int(t) <= NET_HEAL_FLAP_WINDOW]
    st["flaps"] = flaps
    if len(flaps) < NET_HEAL_FLAP_THRESHOLD and (not flaps or now - max(flaps) >= NET_HEAL_FLAP_WINDOW):
        st["obs_problem"] = False
        record_metrics_event("info", "Net-heal: flap storm over - network stable for %d min"
                             % (NET_HEAL_FLAP_WINDOW // 60))
        _net_heal_reset_broken(st)
        st["inet_down_since"] = 0
        st["driver"] = _net_heal_detect_driver(iface)
        _net_heal_state_save(st)
        return
    _net_heal_state_save(st)


def _net_heal_recovery_gate(st, iface, now, had_problem):
    """Healthy check outside a storm: count a just-ended short outage; when
    the accumulator crosses the threshold, engage the flap-storm ladder
    instead of resetting. Returns True when the storm was engaged."""
    if not _net_heal_flap_ended(st, now, had_problem):
        return False
    count = _net_heal_record_flap(st, now)
    if count < NET_HEAL_FLAP_THRESHOLD:
        return False
    _net_heal_storm_engage(st, iface, now, count)
    return True


def net_heal_run_check():
    """One connectivity probe + (maybe) one recovery action."""
    st = _net_heal_state_load()
    now = int(time.time())
    st["last_check"] = now
    iface = _net_heal_wifi_iface()
    if not iface:
        st["obs_problem"] = False
        if st.get("state") != "n/a":
            st["state"] = "n/a"
            st["last_action"] = "no wireless interface - worker idle"
            _net_heal_state_save(st)
        return
    # Static ip drift is checked even when everything else is healthy: an ip
    # conflict (dhcp squatting another rig's address) does NOT break
    # connectivity, so the miner keeps hashing and the khs fast-path below
    # would otherwise hide it forever (RIG8 drill 19 Oct: .105 conflict with
    # RIG5 while hashing at full speed).
    if st.get("static"):
        _net_heal_static_check(st, iface, now)
        st = _net_heal_state_load()
    had_problem = bool(st.get("obs_problem"))
    storm_active = st.get("state") == "broken" and st.get("layer") == "flap-storm"
    # Miner hashing = pool reachable = network fine (fast path, also resets).
    if _oc_read_khs() > 0:
        if storm_active:
            _net_heal_storm_healthy(st, iface, now, had_problem)
            return
        if _net_heal_recovery_gate(st, iface, now, had_problem):
            st["obs_problem"] = False
            return
        if st.get("broken_since") or st.get("inet_down_since"):
            record_metrics_event("info", "Net-heal: connectivity recovered (miner hashing)")
        st["obs_problem"] = False
        _net_heal_reset_broken(st)
        st["inet_down_since"] = 0
        st["driver"] = _net_heal_detect_driver(iface)
        _net_heal_state_save(st)
        return
    layer = None
    cstate = _net_heal_carrier_state(iface)
    if cstate == "missing":
        layer = "iface-missing"
    elif cstate == "down":
        layer = "iface-down"
    else:
        gw, has_route = _net_heal_gateway(iface)
        if not has_route or not gw:
            layer = "no-default-route"
        elif not _net_heal_ping(gw):
            layer = "gateway-unreachable"
    st["obs_problem"] = layer is not None
    if storm_active and layer is None:
        # L2/L3 fine during a storm: the storm ends only when the internet is
        # healthy too (an inet-dead observation still counts as a problem).
        if _net_heal_inet_ok():
            _net_heal_storm_healthy(st, iface, now, had_problem)
        else:
            _net_heal_advance_ladder(st, iface, now)
        return
    if layer is None:
        # L2/L3 fine: is the internet actually reachable?
        if _net_heal_inet_ok():
            if _net_heal_recovery_gate(st, iface, now, had_problem):
                st["obs_problem"] = False
                return
            if st.get("inet_down_since"):
                record_metrics_event("info", "Net-heal: internet connectivity recovered")
            st["inet_down_since"] = 0
            if st.get("broken_since"):
                record_metrics_event("info", "Net-heal: connectivity recovered after %d min" % ((now - st["broken_since"]) // 60))
            st["obs_problem"] = False
            _net_heal_reset_broken(st)
            st["driver"] = _net_heal_detect_driver(iface)
            _net_heal_state_save(st)
            return
        # Internet dead while wifi/gateway fine.
        if not st.get("inet_down_since"):
            st["inet_down_since"] = now
            st["wan_down_noted"] = False
            st["peer_check_ts"] = 0
            st["last_action"] = "internet unreachable while wifi/gateway OK (router WAN?) - no rig action"
            record_metrics_event("info", "Net-heal: " + st["last_action"])
            st["state"] = "inet_down"
            _net_heal_state_save(st)
            return
        if st.get("state") == "broken" and st.get("layer") == "self-uplink":
            pass  # ladder already engaged - fall through to the shared driver
        else:
            if now - st["inet_down_since"] < NET_HEAL_SELF_INET_CONFIRM:
                st["state"] = "inet_down"
                _net_heal_state_save(st)
                return
            # Persistent. Ask the cluster who still sees the internet (a peer
            # that hashes has a working internet path): a fresh peer 'ok'
            # proves the router WAN is alive and OUR uplink is dead - engage
            # the ladder; fresh peer 'inet_down' = WAN down for everyone -
            # keep hands off the rig.
            if now - int(st.get("peer_check_ts") or 0) < NET_HEAL_PEER_CHECK_GAP:
                st["state"] = "inet_down"
                _net_heal_state_save(st)
                return
            st["peer_check_ts"] = now
            verdict, detail = _net_heal_peer_inet_verdict()
            st["peer_verdict"] = {"ts": now, "result": verdict or "none", "detail": detail}
            if verdict == "our_uplink":
                st["layer"] = "self-uplink"
                st["state"] = "broken"
                st["broken_since"] = now
                st["step"] = 0
                st["step_ts"] = 0
                st["no_reboot"] = False
                st["soft_cycle_ts"] = 0
                st["driver"] = _net_heal_detect_driver(iface)
                st["last_action"] = "own uplink dead (peer %s sees internet OK) - recovery ladder engaged" % (detail.get("peer") or "?")
                record_metrics_event("warning", "Net-heal: " + st["last_action"])
            elif verdict == "wan_down":
                st["state"] = "inet_down"
                if not st.get("wan_down_noted"):
                    st["wan_down_noted"] = True
                    st["last_action"] = "internet down for peers too (%s) - router WAN, no rig action" % (detail.get("peer") or "?")
                    record_metrics_event("info", "Net-heal: " + st["last_action"])
                _net_heal_state_save(st)
                return
            else:
                # No peer evidence (peers offline / panels down). After a long
                # window suspect our own link, but NEVER reboot on a guess:
                # soft steps only, paced soft cycles.
                if now - st["inet_down_since"] < NET_HEAL_NOPEER_INET_WAIT:
                    st["state"] = "inet_down"
                    _net_heal_state_save(st)
                    return
                st["layer"] = "self-uplink"
                st["state"] = "broken"
                if not st.get("broken_since"):
                    st["broken_since"] = now
                st["no_reboot"] = True
                st["soft_cycle_ts"] = now
                st["driver"] = st.get("driver") or _net_heal_detect_driver(iface)
                st["last_action"] = "own uplink suspected (no peer evidence for %d min) - soft ladder, reboot disabled" % (NET_HEAL_NOPEER_INET_WAIT // 60)
                record_metrics_event("warning", "Net-heal: " + st["last_action"])
        _net_heal_advance_ladder(st, iface, now)
        return
    # Broken at some layer.
    if st.get("inet_down_since"):
        st["inet_down_since"] = 0
        record_metrics_event("info", "Net-heal: internet connectivity recovered")
    if not storm_active and (st.get("layer") != layer or not st.get("broken_since")):
        # During a flap storm the layer flaps between problem states every few
        # checks - re-arming the ladder on each transition would restart it
        # forever without ever reaching the reboot. Keep the storm ladder.
        st["layer"] = layer
        st["broken_since"] = now
        st["step"] = 0
        st["step_ts"] = 0
        st["no_reboot"] = False
        st["state"] = "broken"
        st["last_action"] = "network problem detected: %s" % layer
        record_metrics_event("info", "Net-heal: " + st["last_action"])
    _net_heal_advance_ladder(st, iface, now)

def _net_heal_worker():
    time.sleep(NET_HEAL_BOOT_DELAY)
    while True:
        try:
            net_heal_run_check()
        except Exception as e:
            logging.error("Net-heal check failed: %s", e)
        time.sleep(NET_HEAL_CHECK_INTERVAL)

def start_net_heal_worker():
    t = threading.Thread(target=_net_heal_worker, daemon=True, name="net-heal")
    t.start()
    logging.info("Network self-heal worker started (wifi/link recovery, reboot last resort)")

@app.route('/api/net_heal/static', methods=['POST'])
def api_net_heal_static():
    """Set/clear the per-rig static wifi intent the worker re-asserts whenever
    hive flips the netplan yaml back to dhcp (every boot). Empty ip = clear."""
    data = request.get_json(silent=True) or {}
    ip = str(data.get("ip") or "").strip()
    gw = str(data.get("gw") or "").strip()
    iface = str(data.get("iface") or "wlan0").strip()
    dns = data.get("dns") if isinstance(data.get("dns"), list) else []
    host_re = r'^(\d{1,3}\.){3}\d{1,3}$'
    def _valid_ip(s):
        return bool(re.match(host_re, s)) and all(int(o) <= 255 for o in s.split("."))
    if not re.match(r'^[a-z0-9]{2,15}$', iface):
        return jsonify({"success": False, "message": "Bad interface"}), 400
    if not ip:
        st = _net_heal_state_load()
        st.pop("static", None)
        st["static_try"] = 0
        _net_heal_state_save(st)
        return jsonify({"success": True, "static": None})
    if not _valid_ip(ip.split("/")[0]) or (("/" in ip) and not re.match(r'^/\d{1,2}$', ip[ip.index("/"):])):
        return jsonify({"success": False, "message": "Bad ip"}), 400
    if not _valid_ip(gw):
        return jsonify({"success": False, "message": "Bad gateway"}), 400
    dns = [str(d).strip() for d in dns][:3]
    for d in dns:
        if not _valid_ip(d):
            return jsonify({"success": False, "message": "Bad dns"}), 400
    st = _net_heal_state_load()
    st["static"] = {"iface": iface, "ip": ip if "/" in ip else ip + "/24",
                    "gw": gw, "dns": dns or [gw]}
    st["static_try"] = 0
    _net_heal_state_save(st)
    return jsonify({"success": True, "static": st["static"]})

# ---------------- Blackbox: persistent flight recorder ----------------
# /var/log and journald are tmpfs on these rigs: every reboot erased the
# evidence of why a rig froze / lost power / lost its network (RIG9 21 Oct,
# RIG1 23+25 Oct, RIG5 26 Oct). blackbox/hive_blackbox.py runs as a systemd
# service and streams kernel log, journal, hive/miner/panel logs and a 5 s
# telemetry heartbeat into /var/lib/hive-blackbox/boots/<n>_<time>/ on the real
# disk (fsync every second). The panel installs/updates it on every start and
# exposes status / post-mortem report / purge. Everything lives under ONE
# directory - purge removes it all.

BLACKBOX_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blackbox", "hive_blackbox.py")
BLACKBOX_BIN_DIR = "/usr/local/lib/hive-blackbox"
BLACKBOX_BIN = BLACKBOX_BIN_DIR + "/hive_blackbox.py"
BLACKBOX_UNIT = "/etc/systemd/system/hive-blackbox.service"
BLACKBOX_DATA = "/var/lib/hive-blackbox"
BLACKBOX_UNIT_TEXT = """[Unit]
Description=Hive blackbox flight recorder (persistent logs for post-mortems)
DefaultDependencies=no
After=local-fs.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %s run
Restart=always
RestartSec=5
Nice=10
TimeoutStopSec=15
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
""" % BLACKBOX_BIN

def _file_sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return ""

def blackbox_ensure_installed():
    """Install / update the blackbox service. Returns a short status string."""
    if not os.path.exists(BLACKBOX_SRC):
        return "source missing"
    changed = False
    if _file_sha(BLACKBOX_SRC) != _file_sha(BLACKBOX_BIN):
        os.makedirs(BLACKBOX_BIN_DIR, exist_ok=True)
        with open(BLACKBOX_SRC, "rb") as f:
            data = f.read()
        tmp = BLACKBOX_BIN + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o755)
        os.replace(tmp, BLACKBOX_BIN)
        changed = True
    try:
        with open(BLACKBOX_UNIT, "r") as f:
            unit_ok = f.read() == BLACKBOX_UNIT_TEXT
    except Exception:
        unit_ok = False
    if not unit_ok:
        _atomic_write_file(BLACKBOX_UNIT, BLACKBOX_UNIT_TEXT, 0o644)
        run_command("systemctl daemon-reload")
        run_command("systemctl enable hive-blackbox")
        changed = True
    active, _e, _c = run_command("systemctl is-active hive-blackbox")
    if changed or (active or "").strip() != "active":
        run_command("systemctl restart hive-blackbox")
        return "installed/updated"
    return "ok"

def _blackbox_worker():
    time.sleep(20)
    while True:
        try:
            res = blackbox_ensure_installed()
            if res != "ok":
                logging.info("Blackbox recorder: %s", res)
        except Exception as e:
            logging.error("Blackbox install check failed: %s", e)
        time.sleep(3600)

def start_blackbox_worker():
    threading.Thread(target=_blackbox_worker, daemon=True, name="blackbox-install").start()
    logging.info("Blackbox installer started (persistent flight recorder)")

def _blackbox_cli(args):
    return run_command("timeout 90 python3 %s %s" % (shlex.quote(BLACKBOX_BIN), args))

@app.route('/api/blackbox/status')
def api_blackbox_status():
    out, _err, rc = _blackbox_cli("status")
    try:
        st = json.loads(out)
    except Exception:
        st = {}
    active, _e, _c = run_command("systemctl is-active hive-blackbox")
    st["service"] = (active or "").strip() or "unknown"
    return jsonify({"success": True, "blackbox": st})

@app.route('/api/blackbox/report')
def api_blackbox_report():
    boot = str(request.args.get("boot", "prev"))
    if not re.match(r'^(prev|previous|latest|\d{1,5})$', boot):
        return jsonify({"success": False, "message": "Bad boot reference"}), 400
    try:
        minutes = max(1, min(120, int(request.args.get("minutes", 15))))
    except ValueError:
        minutes = 15
    out, err, rc = _blackbox_cli("report --boot %s --minutes %d --json" % (boot, minutes))
    try:
        rep = json.loads(out)
    except Exception:
        return jsonify({"success": False, "message": (err or out or "report failed")[:300]}), 500
    return jsonify({"success": True, "report": rep})

@app.route('/api/blackbox/purge', methods=['POST'])
def api_blackbox_purge():
    data = request.get_json(silent=True) or {}
    out, err, rc = _blackbox_cli("purge" + (" --all" if data.get("all") else ""))
    logging.warning("Blackbox purge (%s) by IP %s: %s", "all" if data.get("all") else "old boots",
                    request.remote_addr, (out or "").strip())
    return jsonify({"success": rc == 0, "message": (out or err or "").strip()})

# ---------------- OC recovery: Xid death-loop self-healing ----------------
# A GPU that starts throwing fatal driver faults (Xid 13/31) under mining load
# kills the whole miner run: SRBMiner stops every worker ("Pearl backend error
# E1027"), the miner exits and hive restarts it every few minutes with a
# permanently zero hashrate (RIG6 14 Oct: GPU7 faulted on every core-lock
# re-assert after a live OC apply; a reboot did not help). This worker walks
# the faulting GPU's locked clocks DOWN step by step until the rig mines
# cleanly, then walks back UP toward the original frequency to find the
# failure point - never exceeding the frequency that was set before the
# trouble - and settles one step below it. Core first, then memory; if the
# memory floor still fails, the GPU is disabled from the miner config so the
# remaining cards keep mining.

OC_RECOVERY_STATE_PATH = os.path.join(HIVE_CONFIG_DIR, "oc_recovery.json")
OC_REC_CHECK_INTERVAL = 90          # idle: detection poll
OC_REC_CHECK_INTERVAL_ACTIVE = 15   # recovery engaged: state machine tick
OC_REC_BOOT_DELAY = 420             # let boot/guard/miner settle first
OC_REC_RESTARTS_REQUIRED = 3        # miner restarts before recovery engages
OC_REC_RESTART_WINDOW = 3600        # ...counted within this window
OC_REC_STEP_CORE = 15               # core lock step for the upward refine (MHz)
OC_REC_STEP_DOWN_CORE = 45          # coarse first-descent step (3 fine steps per test)
OC_REC_STEP_MEM = 15                # memory lock step (MHz)
OC_REC_FLOOR_CORE = 1000            # never lower the core lock below this
OC_REC_FLOOR_MEM = 810              # never lower the memory lock below this
OC_REC_WARMUP = 240                 # miner init + pool reconnect before judging
OC_REC_OBSERVE = 120                # clean window that confirms a step works
OC_REC_COOLDOWN = 900               # min seconds between finished recoveries
OC_REC_XID_CODES = ("13", "31")     # fatal compute / MMU faults
OC_REC_ZOMBIE_SECONDS = 180         # khs==0 this long with a fresh Xid burst engages even without a restart loop
OC_REC_BURST_TTL = 900              # a fresh Xid burst stays actionable this long
OC_REC_BURST_SEED = 3               # fatal Xids in one burst that prove the CURRENT clock fails
OC_REC_KHS_PATH = "/run/hive/khs"
OC_REC_TEST_SECONDS = OC_REC_WARMUP + OC_REC_OBSERVE
OC_REC_HOT_TEST_SECONDS = 150       # hot probe window: the miner never stopped, no warmup needed
OC_REC_FAILFAST_GRACE = 25          # min seconds into a test before early-fail checks
OC_REC_HOT_RECENT_XID = 300         # target card must be fault-free this long before a hot probe

_oc_state_lock = threading.Lock()
_oc_state_cache = {"loaded": False, "data": None}

def _oc_state_default():
    return {"active": False, "phase": "idle", "updated_at": 0, "last_run": 0,
            "last_result": "", "gpu": None, "pci": "", "kind": "core",
            "ceiling": 0, "current": 0, "working": 0, "floor": 0,
            "step": OC_REC_STEP_CORE, "down_step": 0, "test": None, "history": [],
            "orig_core": {}, "orig_mem": {}, "known_core": {}, "known_mem": {},
            "failed_up": {}, "force_cold": False, "last_fail_ts": 0,
            "restart_times": [], "last_run_mtime": 0, "last_log_ino": 0,
            "xid_counts": {}, "xid_burst": {}, "zero_since": 0, "zombie": False}

def _oc_state_load():
    with _oc_state_lock:
        if _oc_state_cache["loaded"]:
            return _oc_state_cache["data"]
        st = _oc_state_default()
        try:
            with open(OC_RECOVERY_STATE_PATH, 'r') as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in st:
                    if k in disk:
                        st[k] = disk[k]
        except Exception:
            pass
        _oc_state_cache["data"] = st
        _oc_state_cache["loaded"] = True
        return st

def _oc_state_save(st):
    st["updated_at"] = int(time.time())
    with _oc_state_lock:
        _oc_state_cache["data"] = st
        _oc_state_cache["loaded"] = True
        try:
            _atomic_write_file(OC_RECOVERY_STATE_PATH, json.dumps(st, indent=2))
        except Exception as e:
            logging.error("Failed to save oc recovery state: %s", e)

# Any hashrate below this counts as ZERO for every healer (zombie check,
# net-heal fast path, oc-recovery pass criteria). The agent writes the miner's
# 1-minute AVERAGE into /run/hive/khs: a zombie miner with every worker stuck
# at 0.00 H/s still reports a phantom 0.00026 kH/s (RIG6 25 Oct: 0.26 H/s
# average) which defeated every "khs > 0 = healthy" test for hours. Real rigs
# mine at tens of TH/s (=1e8+ kH/s) - a 1 GH/s floor is safe by orders of
# magnitude.
MINING_KHS_FLOOR = 1000000.0

def _oc_read_khs():
    """Current rig hashrate (kH/s) as reported by the hive agent. A stale
    file (>120s without an update, hive-watchdog semantics) reads as 0 -
    a dead miner leaves the last value behind otherwise. Sub-floor values
    (phantom averages of zombie workers) read as 0 as well."""
    try:
        age = time.time() - os.path.getmtime(OC_REC_KHS_PATH)
        if age > 120:
            return 0.0
        with open(OC_REC_KHS_PATH, 'r') as f:
            val = float((f.read() or "0").strip() or 0)
        return val if val >= MINING_KHS_FLOOR else 0.0
    except Exception:
        return 0.0

def _oc_xid_counts():
    """{pci_norm: count} of fatal Xid faults (13/31) per GPU from dmesg."""
    counts = {}
    out, _err, _code = run_command("dmesg")
    for m in re.finditer(r"Xid \(PCI:([0-9A-Fa-f:]+)\): (\d+)", out or ""):
        if m.group(2) not in OC_REC_XID_CODES:
            continue
        pci = _oc_norm_pci(m.group(1))
        if pci:
            counts[pci] = counts.get(pci, 0) + 1
    return counts

def _oc_norm_pci(pci):
    """0000:09:00 (dmesg) / 00000000:0000:09:00.0 (nvidia-smi) -> '09:00'.
    Lowercased: dmesg prints hex bus numbers in lower case ('0c:00') while
    nvidia-smi uses upper case ('0C:00.0') - the case mismatch made the
    pci->gpu mapping fail on every rig with a letter in the bus id (RIG1
    GPUs 6/7 on 0A/0C: 'Xid loop on PCI 0c:00 but no matching GPU index',
    silently killing every engage since 15 Oct)."""
    parts = [p for p in str(pci).split(":") if p]
    if len(parts) < 2:
        return ""
    tail = parts[-1].split(".")[0]
    return (f"{parts[-2]}:{tail}" if tail else "").lower()

def _oc_gpu_for_pci(pci):
    """Map a dmesg PCI address to the nvidia GPU index. None = unknown."""
    out, _err, code = run_command(
        "nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader")
    if code != 0:
        return None
    want = _oc_norm_pci(pci)
    for line in (out or "").splitlines():
        bits = [b.strip() for b in line.split(",")]
        if len(bits) >= 2 and _oc_norm_pci(bits[1]) == want:
            try:
                return int(bits[0])
            except ValueError:
                return None
    return None

def _oc_conf_token(kind, gpu):
    """Current locked-clock token of one GPU from nvidia-oc.conf (hive
    semantics: a short/scalar list applies its last token to all GPUs)."""
    try:
        cfg = parse_shell_config(NVIDIA_OC_CONF)
    except Exception:
        cfg = {}
    tokens = (cfg.get("LCLOCK" if kind == "core" else "LMEM") or "").split()
    if not tokens:
        return 0
    return tokens[gpu] if gpu < len(tokens) else tokens[-1]

def _oc_apply_single(kind, gpu, value):
    """Write one locked-clock value for one GPU into nvidia-oc.conf and apply
    it to the driver (same code path as the panel's per-GPU OC form)."""
    key = "lcore" if kind == "core" else "lmem"
    ok, msg = _apply_nvidia_oc({key: str(value)}, False, gpu)
    if not ok:
        logging.error("OC recovery: apply failed (GPU %s %s=%s): %s", gpu, kind, value, msg)
    return ok

def _oc_miner_log_path():
    """Path of the current miner log ('' = unknown); the hive wrapper
    recreates/rotates this file on EVERY miner (re)start, so its inode is a
    reliable restart signal even for the in-screen miner-run restart loop
    (which never touches MINER_RUN)."""
    miner = _conf_value(RIG_CONF_PATH, "MINER") or ""
    if not miner:
        return ""
    if miner == "custom":
        pkg = _conf_value(WALLET_CONF_PATH, "CUSTOM_MINER") or ""
        if not pkg:
            return ""
        return os.path.join(MINER_LOG_DIR, "custom", f"{pkg}.log")
    return os.path.join(MINER_LOG_DIR, f"{miner}.log")

def _oc_log_bad_lines():
    """Number of worker-death markers in the current miner log (0 = clean).
    The hive wrapper recreates the log on every miner start, so within one
    run any marker means a worker died - possibly only one of several."""
    path = _oc_miner_log_path()
    if not path:
        return 0
    try:
        with open(path, 'r', errors='ignore') as f:
            data = f.read()
    except Exception:
        return 0
    return data.count("stopped after repeated solve errors") + data.count(" DEAD [")

def _oc_miner_log_clean():
    """True when the current miner log has no worker-death lines (a fresh run
    per start: the log is rotated by the hive wrapper on every start)."""
    return _oc_log_bad_lines() == 0

def _oc_hot_ok(st):
    """Hot probes need a provably healthy miner: hashrate flowing, no stop
    flag, run flag present, clean miner log and no fault on the target card
    for the last OC_REC_HOT_RECENT_XID seconds (khs stays readable for up to
    120s after a miner death, so the recent-fault guard is what really
    protects against hot-applying onto a rig that just crashed)."""
    if os.path.exists(MINER_STOP_FLAG) or not os.path.exists(MINER_RUN_FLAG):
        return False
    if _oc_read_khs() <= 0 or not _oc_miner_log_clean():
        return False
    return int(time.time()) - int(st.get("last_fail_ts") or 0) >= OC_REC_HOT_RECENT_XID

def _oc_suspended_guard(suspend):
    try:
        g = _guard_load()
        g["suspended"] = bool(suspend)
        _guard_save(g)
    except Exception as e:
        logging.error("OC recovery: guard suspend failed: %s", e)

def _oc_begin_test(st, kind, value):
    """Apply the candidate value to the target GPU and record the test.
    A provably healthy miner is probed HOT: only the target card's clock
    changes while the rig keeps mining (a miner restart costs every card a
    warmup and drops the rig to zero for minutes). A dead/zombie miner gets
    the classic stop -> apply -> restart. A later pass judges the outcome."""
    st["kind"] = kind
    st["current"] = int(value)
    counts = _oc_xid_counts()
    base = counts.get(st["pci"], 0)
    force_cold = bool(st.get("force_cold"))
    st["force_cold"] = False
    gpu = st["gpu"]
    hot = kind == "core" and not force_cold and _oc_hot_ok(st)
    if hot:
        ok = _oc_apply_single(kind, gpu, value)
        st["test"] = {"kind": kind, "value": int(value), "started_at": int(time.time()),
                      "xid_base": base, "applied": bool(ok),
                      "hot": True, "log_base": _oc_log_bad_lines()}
        logging.warning("OC recovery: GPU %s hot-probing %s=%s (baseline Xid %s, miner keeps running)",
                        gpu, kind, value, base)
        record_metrics_event("info", f"OC recovery: GPU {gpu} hot-probing {kind} lock {value} MHz (miner keeps running)")
    else:
        run_command("sudo /hive/bin/miner stop")
        time.sleep(5)
        ok = _oc_apply_single(kind, gpu, value)
        st["test"] = {"kind": kind, "value": int(value), "started_at": int(time.time()),
                      "xid_base": base, "applied": bool(ok),
                      "hot": False, "log_base": 0}
        run_command(MINER_RESTART_CMD)
        logging.warning("OC recovery: GPU %s testing %s=%s (baseline Xid %s, miner restarted)",
                        gpu, kind, value, base)
        record_metrics_event("info", f"OC recovery: GPU {gpu} testing {kind} lock {value} MHz")
    _oc_state_save(st)

def _oc_evaluate_test(st):
    """Judge the in-flight test: pass = hashrate back, no new fatal Xid on the
    target PCI, no NEW worker-death lines in the miner log (the baseline is
    snapshotted at test start - a hot probe shares the log with the ongoing
    run)."""
    test = st.get("test") or {}
    khs = _oc_read_khs()
    counts = _oc_xid_counts()
    new_xid = counts.get(st["pci"], 0) - int(test.get("xid_base") or 0)
    log_bad = _oc_log_bad_lines() > int(test.get("log_base") or 0)
    passed = khs > 0 and new_xid <= 0 and not log_bad
    return passed, {"khs": khs, "new_xid": max(0, new_xid), "log_bad": log_bad}

def _oc_history(st, line):
    st["history"] = (st.get("history") or [])[-29:] + [f"{time.strftime('%m-%d %H:%M')} {line}"]

def _oc_finish(st, level, result):
    st["active"] = False
    st["phase"] = "done"
    st["test"] = None
    st["zombie"] = False
    st["zero_since"] = 0
    st["xid_burst"] = {}
    st["last_run"] = int(time.time())
    st["last_result"] = result
    gpu = str(st.get("gpu"))
    if st.get("kind") == "core" and st.get("working"):
        st.setdefault("known_core", {})[gpu] = int(st["working"])
    elif st.get("kind") == "mem" and st.get("working"):
        st.setdefault("known_mem", {})[gpu] = int(st["working"])
    _oc_suspended_guard(False)
    _oc_state_save(st)
    logging.warning("OC recovery finished: %s", result)
    record_metrics_event(level, f"OC recovery: {result}")

def _oc_next_medium(st):
    """Core floor reached without a working point: switch to memory, or (no
    mem lock to lower) straight to disabling the GPU."""
    gpu = str(st.get("gpu"))
    mem_ceiling = max(int(_oc_conf_token("mem", st["gpu"]) or 0),
                      int((st.get("orig_mem") or {}).get(gpu) or 0))
    if mem_ceiling <= OC_REC_FLOOR_MEM:
        return "disable"
    st["kind"] = "mem"
    st["ceiling"] = mem_ceiling
    st["floor"] = OC_REC_FLOOR_MEM
    st["step"] = OC_REC_STEP_MEM
    st["down_step"] = OC_REC_STEP_MEM
    st.setdefault("orig_mem", {})[gpu] = mem_ceiling
    known_m = int((st.get("known_mem") or {}).get(gpu) or 0)
    if known_m and known_m <= mem_ceiling:
        st["current"] = known_m
    else:
        st["current"] = max(mem_ceiling - OC_REC_STEP_MEM, OC_REC_FLOOR_MEM)
    st["working"] = 0
    return "down"

def _oc_settle_or_descend(st):
    """Re-apply the last working value (up-phase failure = step back down);
    if even that stops working, keep descending from it."""
    st["phase"] = "down_" + st["kind"]
    st["current"] = int(st.get("working") or st.get("ceiling"))
    st["working"] = 0

def _oc_step_down_value(st):
    """First descent uses the coarse step (fewer tests to reach the failure
    band); the fine step is for the upward refine."""
    step = int(st.get("down_step") or st.get("step") or OC_REC_STEP_CORE)
    val = int(st.get("current") or 0) - step
    return max(val, int(st.get("floor") or 0))

def _oc_recovery_pass():
    """One state-machine transition (at most one clock test) per call."""
    st = _oc_state_load()
    now = int(time.time())

    # Bookkeeping in every pass: miner starts (MINER_RUN mtime changes at a
    # hive-level start; the active miner log inode changes at EVERY restart,
    # including the in-screen miner-run supervision loop) and fatal Xid
    # counts power the idle-phase detection.
    try:
        mtime = int(os.path.getmtime(MINER_RUN_FLAG)) if os.path.exists(MINER_RUN_FLAG) else 0
        prev = int(st.get("last_run_mtime") or 0)
        if mtime and prev and mtime != prev:
            st["restart_times"] = [t for t in (st.get("restart_times") or [])
                                   if now - t < OC_REC_RESTART_WINDOW]
            st["restart_times"] = st["restart_times"] + [now]
        if mtime:
            st["last_run_mtime"] = mtime
    except Exception as e:
        logging.error("OC recovery: restart tracking failed: %s", e)
    try:
        log_path = _oc_miner_log_path()
        ino = os.stat(log_path).st_ino if log_path and os.path.exists(log_path) else 0
        prev_ino = int(st.get("last_log_ino") or 0)
        if ino and prev_ino and ino != prev_ino:
            st["restart_times"] = [t for t in (st.get("restart_times") or [])
                                   if now - t < OC_REC_RESTART_WINDOW]
            st["restart_times"] = st["restart_times"] + [now]
        if ino:
            st["last_log_ino"] = ino
    except Exception as e:
        logging.error("OC recovery: log-inode tracking failed: %s", e)
    try:
        st["xid_counts"] = _oc_xid_counts()
        # accumulate fresh faults into a burst that survives a few idle polls:
        # a dying card often bursts once and then sits idle (no load = no new
        # faults), so "growth since the previous poll" is gone by the next one
        counts = st.get("xid_counts") or {}
        prev = st.get("prev_xid_counts") or {}
        fresh = {p: counts[p] - int(prev.get(p) or 0)
                 for p in counts if counts[p] > int(prev.get(p) or 0)}
        burst = st.get("xid_burst") or {}
        if fresh:
            if now - int(burst.get("ts") or 0) < OC_REC_BURST_TTL:
                merged = {p: int(burst.get(p) or 0) for p in burst if p != "ts"}
                for p, c in fresh.items():
                    merged[p] = merged.get(p, 0) + c
            else:
                merged = dict(fresh)
            merged["ts"] = now
            burst = merged
        st["xid_burst"] = burst
        st["prev_xid_counts"] = counts
    except Exception as e:
        logging.error("OC recovery: xid sampling failed: %s", e)
        st["xid_counts"] = {}

    if not st.get("active"):
        _oc_detect(st, now)
        _oc_state_save(st)
        return

    gpu = st.get("gpu")
    test = st.get("test")
    if test:
        started = int(test.get("started_at") or 0)
        total = OC_REC_HOT_TEST_SECONDS if test.get("hot") else OC_REC_TEST_SECONDS
        age = now - started
        if age < total:
            if age < OC_REC_FAILFAST_GRACE:
                return  # let the apply/miner settle before judging anything
            # fail fast: a fault on the target card (or fresh worker-death
            # lines) has already decided this test - waiting out the full
            # window would only keep the rig down longer (observed 17 Oct:
            # failing steps stayed in their window for minutes with the miner
            # already dead, khs=0)
            bad_now = _oc_log_bad_lines()
            log_bad_now = bad_now > int(test.get("log_base") or 0)
            new_xid_now = _oc_xid_counts().get(st["pci"], 0) - int(test.get("xid_base") or 0)
            if new_xid_now <= 0 and not log_bad_now:
                return  # nothing decisive yet - keep waiting out the window
            passed = False
            detail = {"khs": _oc_read_khs(), "new_xid": max(0, new_xid_now),
                      "log_bad": log_bad_now, "early": True}
        elif not test.get("applied"):
            # the apply itself failed (conf write): treat as a failed step
            passed = False
            detail = {"khs": _oc_read_khs(), "new_xid": 0, "log_bad": False, "apply": False}
        else:
            passed, detail = _oc_evaluate_test(st)
        st["test"] = None
        kind = st.get("kind")
        value = int(test.get("value") or 0)
        verdict = "OK" if passed else f"FAIL ({detail})"
        _oc_history(st, f"GPU {gpu} {test.get('kind') or kind}={value}: {verdict}")
        if test.get("kind") != "disable":
            logging.warning("OC recovery: GPU %s %s=%s -> %s", gpu, kind, value, verdict)
        if not passed:
            st["last_fail_ts"] = now

        if st.get("phase", "").startswith("down"):
            if passed:
                st["working"] = value
                st.setdefault("known_core" if kind == "core" else "known_mem", {})[str(gpu)] = value
                st["phase"] = "up_" + kind
                record_metrics_event("warning",
                                     f"OC recovery: GPU {gpu} mines at {kind} lock {value} MHz - probing upward")
            else:
                if (test.get("hot") and not detail.get("new_xid")
                        and not detail.get("log_bad") and not (detail.get("khs") or 0) > 0):
                    # a hot probe that ends with zero hashrate but NO fault
                    # evidence smells like the apply hung the miner (a known
                    # srbminer quirk under load) rather than the clock being
                    # bad - retry the same value with a proper restart before
                    # stepping down
                    st["current"] = value
                    st["force_cold"] = True
                    _oc_history(st, f"GPU {gpu} {kind}={value}: hot probe inconclusive (khs=0, no Xid) - cold retry")
                    _oc_state_save(st)
                    return
                nxt = _oc_step_down_value(st)
                if value <= int(st.get("floor") or 0) or nxt >= value:
                    # floor reached without success
                    medium = "disable" if st.get("kind") == "mem" else _oc_next_medium(st)
                    if medium == "disable":
                        st["phase"] = "disable"
                        st["working"] = 0
                        record_metrics_event("warning",
                                             f"OC recovery: GPU {gpu} unstable at minimum clocks - disabling it from the miner")
                    else:
                        st["phase"] = "down_" + st["kind"]
                    _oc_state_save(st)
                    return
                st["current"] = nxt
        elif st.get("phase", "").startswith("up"):
            if passed:
                st["working"] = value
                st.setdefault("known_core" if kind == "core" else "known_mem", {})[str(gpu)] = value
                fu = (st.get("failed_up") or {}).get(f"{gpu}_{kind}")
                if value + int(st.get("step") or 0) > int(st.get("ceiling") or 0):
                    _oc_finish(st, "success",
                               f"GPU {gpu} stable at {kind} lock {value} MHz (original ceiling)")
                    return
                if fu and value + int(st.get("step") or 0) >= int(fu):
                    # a previous recovery already saw this step fail - settle here
                    _oc_finish(st, "success",
                               f"GPU {gpu} settled at {kind} lock {value} MHz (failure point {fu} MHz known)")
                    return
                st["current"] = value + int(st.get("step") or 0)
                st["phase"] = "up_" + kind
            else:
                # failure point found: remember it and settle on the last working value
                st.setdefault("failed_up", {})[f"{gpu}_{kind}"] = value
                if st.get("working"):
                    _oc_settle_or_descend(st)
                else:
                    _oc_finish(st, "warning",
                               f"GPU {gpu} failed the settle-back test at {kind} {value} MHz; will re-descend")
                    return
        elif st.get("phase") == "disable":
            _oc_finish(st, "danger" if detail.get("khs", 0) and detail.get("khs", 0) > 0 else "warning",
                       f"GPU {gpu} disabled from the miner ({kind} recovery exhausted); khs={int(detail.get('khs') or 0)}")
            return
        _oc_state_save(st)
        return

    phase = st.get("phase")
    if phase in ("down_core", "down_mem", "up_core", "up_mem"):
        kind = st["kind"]
        if phase.startswith("up"):
            candidate = int(st.get("working") or 0) + int(st.get("step") or 0)
            fu = (st.get("failed_up") or {}).get(f"{gpu}_{kind}")
            if candidate > int(st.get("ceiling") or 0):
                _oc_finish(st, "success",
                           f"GPU {gpu} settled at {kind} lock {st.get('working')} MHz (original ceiling)")
                return
            if fu and candidate >= int(fu):
                # a previous recovery already saw this step fail - settle here
                _oc_finish(st, "success",
                           f"GPU {gpu} settled at {kind} lock {st.get('working')} MHz (failure point {fu} MHz known)")
                return
        else:
            candidate = int(st.get("current") or 0)
            if candidate <= int(st.get("floor") or 0) and candidate < int(st.get("ceiling") or 0):
                medium = "disable" if st.get("kind") == "mem" else _oc_next_medium(st)
                if medium == "disable":
                    st["phase"] = "disable"
                    _oc_state_save(st)
                    return
                st["phase"] = "down_" + st["kind"]
                _oc_state_save(st)
                return
        _oc_begin_test(st, kind, candidate)
        return
    if phase == "disable":
        _oc_disable_gpu(st)
        return
    # unknown phase: reset
    st["active"] = False
    st["phase"] = "idle"
    _oc_state_save(st)

def _oc_disable_gpu(st):
    """Remove a hopelessly unstable GPU from the miner config (SRBMiner:
    --disable-gpu <ids> in CUSTOM_USER_CONFIG) and restart the miner."""
    gpu = int(st.get("gpu") or 0)
    # the whole walk ran on zero hashrate and the Xid activity is long gone:
    # this smells like a pool/network outage, not a bad card - disabling a
    # healthy GPU would be wrong, restore the original clocks instead
    burst_ts = int((st.get("xid_burst") or {}).get("ts") or 0)
    if burst_ts and int(time.time()) - burst_ts > OC_REC_BURST_TTL:
        orig_c = int((st.get("orig_core") or {}).get(str(gpu)) or 0)
        orig_m = int((st.get("orig_mem") or {}).get(str(gpu)) or 0)
        if orig_c or orig_m:
            run_command("sudo /hive/bin/miner stop")
            time.sleep(3)
            if orig_c:
                _oc_apply_single("core", gpu, orig_c)
            if orig_m:
                _oc_apply_single("mem", gpu, orig_m)
            run_command(MINER_RESTART_CMD)
        _oc_finish(st, "warning",
                   f"GPU {gpu} hit the clock floor with zero hashrate but no recent Xid faults - "
                   f"pool/network outage suspected, original clocks restored, GPU NOT disabled")
        return
    miner = _conf_value(RIG_CONF_PATH, "MINER") or ""
    if miner != "custom":
        _oc_finish(st, "warning", f"GPU {gpu} unstable at minimum clocks; miner '{miner}' has no per-GPU disable support - manual action needed")
        return
    cur = _conf_value(RIG_CONF_PATH, "CUSTOM_USER_CONFIG") or ""
    flags = cur.split()
    ids = []
    out_flags = []
    skip_next = False
    for fl in flags:
        if skip_next:
            ids += [x for x in fl.split(",") if x]
            skip_next = False
            continue
        if fl == "--disable-gpu":
            skip_next = True
            continue
        out_flags.append(fl)
    if str(gpu) not in ids:
        ids.append(str(gpu))
    new_cfg = " ".join(out_flags + ["--disable-gpu", ",".join(ids)]).strip()
    _set_conf_value(RIG_CONF_PATH, "CUSTOM_USER_CONFIG", new_cfg)
    _oc_history(st, f"GPU {gpu} disabled (CUSTOM_USER_CONFIG: --disable-gpu {','.join(ids)})")
    run_command(MINER_RESTART_CMD)
    record_metrics_event("danger", f"OC recovery: GPU {gpu} disabled from the miner (clock recovery exhausted)")
    # leave the phase as disable: the next pass evaluates and finishes
    st["test"] = {"kind": "disable", "value": gpu, "started_at": int(time.time()), "xid_base": 0, "applied": True}

def _oc_detect(st, now):
    """Idle-phase detection. Three ways in, all requiring a fresh fatal-Xid
    burst on ONE pci (that is what distinguishes a bad clock from a pool or
    network outage):
    - restart loop: OC_REC_RESTARTS_REQUIRED+ miner restarts within the hour
      AND a fresh burst - the fast crash-restart cycle (miner dies on Xid,
      the hive supervision loop brings it back in seconds, hashrate returns
      for minutes until the next fault) engages IMMEDIATELY, a sustained
      khs==0 is NOT required (observed 17 Oct on RIG1: GPU7 faulted every
      ~10 min for hours while the old khs==0 gate kept recovery idle);
    - zombie miner: khs==0 for OC_REC_ZOMBIE_SECONDS with NO restart loop;
    - classic death loop: the restart path with a persistent zero hashrate.
    """
    miner = _conf_value(RIG_CONF_PATH, "MINER") or ""
    if not miner or miner == "none":
        return
    maintenance = _conf_value(RIG_CONF_PATH, "MAINTENANCE")
    if maintenance and maintenance.strip() not in ("", "0"):
        return
    if os.path.exists(MINER_STOP_FLAG):
        return
    if now - int(st.get("last_run") or 0) < OC_REC_COOLDOWN:
        return
    khs = _oc_read_khs()
    if khs > 0:
        # hashrate alive: no zombie. The burst is deliberately KEPT - a card
        # that faults every few minutes produces hashrate between crashes,
        # clearing the burst here made engagement impossible for exactly that
        # case; the burst expires through its own TTL instead
        st["zero_since"] = 0
    burst = st.get("xid_burst") or {}
    pcis = {p: int(v) for p, v in burst.items() if p != "ts"}
    loop = len(st.get("restart_times") or []) >= OC_REC_RESTARTS_REQUIRED
    if not pcis:
        st["zero_since"] = 0
        return
    if now - int(burst.get("ts") or 0) > OC_REC_BURST_TTL:
        return
    zs = int(st.get("zero_since") or 0)
    if not zs and khs <= 0:
        zs = now
        st["zero_since"] = zs
    if not loop:
        # zombie path: a zero-hash state must be persistent (with khs>0 there
        # is no zombie - the miner is mining between faults)
        if khs > 0 or now - zs < OC_REC_ZOMBIE_SECONDS:
            return
    pci = max(pcis, key=lambda p: pcis[p])
    gpu = _oc_gpu_for_pci(pci)
    if gpu is None:
        logging.warning("OC recovery: Xid loop on PCI %s but no matching GPU index", pci)
        return
    # engage
    g = str(gpu)
    cur_core = int(_oc_conf_token("core", gpu) or 0)
    cur_mem = int(_oc_conf_token("mem", gpu) or 0)
    orig_core = max(cur_core, int((st.get("orig_core") or {}).get(g) or 0))
    orig_mem = max(cur_mem, int((st.get("orig_mem") or {}).get(g) or 0))
    # a ceiling RAISED since the last engagement makes the previously found
    # failure point obsolete - the extra headroom must be re-tested. Compare
    # against the ceiling recorded at the previous engage: comparing against
    # orig_core itself re-emptied the map on EVERY engage (the ceiling is
    # stable between engagements), so the walk re-probed the same boundary
    # again and again (observed RIG6 GPU7: 1505/1520 dance at each episode)
    fu = st.get("failed_up") or {}
    prev_core = int((st.get("orig_core") or {}).get(g) or 0)
    prev_mem = int((st.get("orig_mem") or {}).get(g) or 0)
    if prev_core and orig_core > prev_core:
        fu.pop(g + "_core", None)
    if prev_mem and orig_mem > prev_mem:
        fu.pop(g + "_mem", None)
    st["active"] = True
    st["gpu"] = gpu
    st["pci"] = pci
    st["zombie"] = not loop
    st["zero_since"] = 0
    st["orig_core"] = {**(st.get("orig_core") or {}), g: orig_core}
    st["orig_mem"] = {**(st.get("orig_mem") or {}), g: orig_mem}
    st["test"] = None
    st["working"] = 0
    st["force_cold"] = False
    st["kind"] = "core"
    burst_n = int(pcis.get(pci) or 0)
    # a restart loop proves the CURRENT clock fails: keep the walk from
    # re-testing it (seed the failure point with that level), and when the
    # previously settled clock is faulting again the boundary moved down to
    # it (a short hot-probe window can pass a level that only faults after
    # minutes - recurring engagements then converge level by level)
    if loop:
        if fu.get(g + "_core") is not None and int(fu[g + "_core"]) >= cur_core > OC_REC_FLOOR_CORE:
            fu[g + "_core"] = cur_core
            st["known_core"] = {k: v for k, v in (st.get("known_core") or {}).items() if k != g}
        elif fu.get(g + "_core") is None and burst_n >= OC_REC_BURST_SEED and cur_core > OC_REC_FLOOR_CORE:
            fu[g + "_core"] = cur_core
        if fu.get(g + "_mem") is not None and int(fu[g + "_mem"]) >= cur_mem > OC_REC_FLOOR_MEM:
            fu[g + "_mem"] = cur_mem
            st["known_mem"] = {k: v for k, v in (st.get("known_mem") or {}).items() if k != g}
        elif fu.get(g + "_mem") is None and burst_n >= OC_REC_BURST_SEED and cur_mem > OC_REC_FLOOR_MEM:
            fu[g + "_mem"] = cur_mem
    st["failed_up"] = fu
    known_c = int((st.get("known_core") or {}).get(g) or 0)
    proven_c = int(fu.get(g + "_core") or 0)
    if orig_core > OC_REC_FLOOR_CORE:
        st["ceiling"] = orig_core
        st["floor"] = OC_REC_FLOOR_CORE
        st["step"] = OC_REC_STEP_CORE
        st["down_step"] = OC_REC_STEP_DOWN_CORE
        st["phase"] = "down_core"
        if proven_c:
            st["current"] = max(proven_c - OC_REC_STEP_DOWN_CORE, OC_REC_FLOOR_CORE)
        elif known_c and known_c <= orig_core:
            st["current"] = known_c  # repeat recovery: re-test the last good value
        elif not loop:
            # zombie: detection did not prove this clock fails (the burst may
            # be a one-off) - re-test the current value first, a plain miner
            # restart alone may bring the rig back at the original clocks
            st["current"] = orig_core
        else:
            # detection already proved the current value fails - start below it
            st["current"] = max(orig_core - OC_REC_STEP_DOWN_CORE, OC_REC_FLOOR_CORE)
    elif orig_mem > OC_REC_FLOOR_MEM:
        st["kind"] = "mem"
        st["ceiling"] = orig_mem
        st["floor"] = OC_REC_FLOOR_MEM
        st["step"] = OC_REC_STEP_MEM
        st["down_step"] = OC_REC_STEP_MEM
        st["phase"] = "down_mem"
        proven_m = int(fu.get(g + "_mem") or 0)
        if proven_m:
            st["current"] = max(proven_m - OC_REC_STEP_MEM, OC_REC_FLOOR_MEM)
        else:
            known_m = int((st.get("known_mem") or {}).get(g) or 0)
            if known_m and known_m <= orig_mem:
                st["current"] = known_m
            elif not loop:
                st["current"] = orig_mem  # zombie: re-test the current value first
            else:
                st["current"] = max(orig_mem - OC_REC_STEP_MEM, OC_REC_FLOOR_MEM)
    else:
        st["phase"] = "disable"
        st["ceiling"] = 0
    st["restart_times"] = []
    _oc_suspended_guard(True)
    if loop:
        _oc_history(st, f"engaged: GPU {gpu} (PCI {pci}), restarts {OC_REC_RESTARTS_REQUIRED}+, "
                        f"Xid burst +{burst_n}"
                        + (f", failure point {proven_c} MHz seeded" if proven_c else ""))
    else:
        _oc_history(st, f"engaged: GPU {gpu} (PCI {pci}), zombie miner (khs=0 for {now - zs}s, "
                        f"Xid burst +{burst_n} on {pci})")
    logging.warning("OC recovery: engaged for GPU %s (PCI %s), phase %s, ceiling %s (zombie=%s)",
                    gpu, pci, st["phase"], st.get("ceiling"), not loop)
    record_metrics_event("warning",
                         f"OC recovery: GPU {gpu} Xid faults with zero hashrate detected - walking {st['kind']} clock from {st.get('current', st.get('ceiling'))} MHz")

_oc_recovery_boot_done = {"flag": False}

def _oc_recovery_worker():
    time.sleep(OC_REC_BOOT_DELAY)
    _oc_recovery_boot_done["flag"] = True
    while True:
        try:
            _oc_recovery_pass()
        except Exception as e:
            logging.error("OC recovery pass failed: %s", e)
        try:
            active = _oc_state_load().get("active")
        except Exception:
            active = False
        time.sleep(OC_REC_CHECK_INTERVAL_ACTIVE if active else OC_REC_CHECK_INTERVAL)

def start_oc_recovery_worker():
    t = threading.Thread(target=_oc_recovery_worker, daemon=True, name="oc-recovery")
    t.start()
    logging.info("OC recovery worker started (Xid death-loop self-healing)")

def _set_conf_value(path, key, value):
    """Set one KEY="value" line in a hive shell config (replace or append),
    preserving every other line and the file mode."""
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
        mode = os.stat(path).st_mode & 0o777
    except Exception:
        lines = []
        mode = 0o644
    pat = re.compile(r'^%s=".*"\s*$' % re.escape(key))
    newline = '%s="%s"\n' % (key, value)
    replaced = False
    out = []
    for line in lines:
        if pat.match(line.rstrip("\n")):
            out.append(newline)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(newline)
    try:
        _atomic_write_file(path, "".join(out), mode=mode)
        return True
    except Exception as e:
        logging.error("Failed to set %s in %s: %s", key, path, e)
        return False

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

        # Re-read the file and fold it in before saving: UI edits, imports and
        # API merges that landed while this cycle was talking to peers (offline
        # peers stretch a cycle to minutes via timeouts) must survive this save
        try:
            fresh = load_cluster_state()
            if fresh.get("self_id") == state.get("self_id"):
                state["rigs"], state["removed"] = merge_rig_lists(
                    fresh["rigs"], state["rigs"],
                    base_removed=fresh.get("removed"), incoming_removed=state.get("removed"))
                state["jump_hosts"] = merge_jump_hosts(
                    fresh.get("jump_hosts", []), state.get("jump_hosts", []))
                state["clusters"] = merge_clusters(
                    fresh.get("clusters", []), state.get("clusters", []), state.get("removed", []))
                if fresh.get("sync_interval"):
                    state["sync_interval"] = fresh["sync_interval"]
        except Exception as e:
            logging.error(f"Cluster sync: pre-save reconcile failed: {e}")
        save_cluster_state(state)
        with _access_health_lock:
            cache["access_health"] = {k: dict(v) for k, v in _ACCESS_HEALTH.items()}
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

# Atomic file write: tmp + fsync + os.replace — a crash mid-write can never
# truncate the previous content (same class of bug as the 1.10.22 ghost-rig fix)
def _atomic_write_file(path, content, mode=0o600):
    with config_lock:
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)

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
            _atomic_write_file(PIN_PATH, key) # Read/write by root only
            logging.info("Generated new dashboard access password (value not logged)")
        except Exception as e:
            logging.error(f"Failed to write access password: {e}")
        return key

# Dashboard access password (loaded or generated once at import time)
ACCESS_PASSWORD = load_or_generate_access_key()
app.config['ACCESS_PASSWORD'] = ACCESS_PASSWORD

# ---------------- Cluster (multi-rig) state management ----------------

RIG_ENTRY_FIELDS = ["id", "name", "host_label", "is_self", "password", "accesses", "updated_at", "added_at",
                    # Home Assistant integration: room mark (boiler/house/none) + smart
                    # plug entity mapped to this rig (synced with the cluster state)
                    "location", "ha_plug", "ha_enabled"]
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
            # Every panel-side write to a config file is a legitimate local change:
            # keep the local-guard snapshot in sync so it never fights the user
            # (only for THIS file: other files may hold cloud-pulled changes
            # that the guard has yet to revert)
            guard_note_config_change([os.path.basename(filepath)])
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
    """Run /hive/sbin/nvidia-oc exclusively; returns (stdout, stderr, returncode).

    hive's sbin scripts read NVIDIA_OC_CONF/GPU_DETECT_JSON from their
    environment (normally delivered via /etc/environment + sudoers env_keep).
    The panel's systemd service has neither, and nvidia-oc then silently
    exits 0 with '... does not exist, exiting' BEFORE applying anything -
    every panel apply logged success while no setting reached the driver
    (observed on RIG2 12 Oct: LMEM=810 and PL writes never landed). Pass
    both variables explicitly, the same way HIVE_MINER_ENV fixes PATH for
    the miner wrapper. PATH must include /hive/bin too: the script's first
    statement is `n=$(gpu-detect NVIDIA)` and without it the script exits 0
    silently before applying anything. GPU_DETECT_JSON may legally be
    missing - the script falls back to `gpu-detect listjson`. The `quiet`
    argument suppresses the script's own 'NVIDIA OC failed' message to the
    cloud on nonzero exits - mixed desktop+mobile rigs exit with the count
    of failed per-GPU ops (laptop power limits report [Not Supported]),
    which would spam farm notifications on every panel apply; the panel
    reports outcomes itself via lock verification."""
    with nvidia_oc_lock:
        return run_command(
            "sudo env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/hive/bin:/hive/sbin "
            "NVIDIA_OC_CONF=/hive-config/nvidia-oc.conf "
            "GPU_DETECT_JSON=/run/hive/gpu-detect.json /hive/sbin/nvidia-oc quiet")

def run_amd_oc():
    """Run /hive/sbin/amd-oc with the environment variables the hive script
    reads from its caller (same systemd env rationale as run_nvidia_oc)."""
    return run_command(
        "sudo env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/hive/bin:/hive/sbin "
        "AMD_OC_CONF=/hive-config/amd-oc.conf "
        "GPU_DETECT_JSON=/run/hive/gpu-detect.json /hive/sbin/amd-oc")

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
            f"sudo timeout 10 /hive/sbin/nvtool -q --nodev -i {gpu_index} --setclocks {int(expected)}")
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

def _verify_locked_mem(expected):
    """Check actual memory clocks against {gpu_index: locked_mhz} — LMEM
    counterpart of verify_locked_clocks. Same contract: {} when nvidia-smi
    gives nothing; a value within 32 MHz of the lock counts as confirmed;
    anything further is re-checked via nvtool idempotence (`nvtool --setmem`
    reports 'was already set' when the lock is registered in the driver) —
    mobile drivers legitimately float/refuse memory locks while idle, and the
    re-assert doubles as a self-heal for a silently failed lock."""
    stdout, _, _ = run_command("nvidia-smi --query-gpu=index,clocks.mem --format=csv,noheader,nounits")
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
        if act is not None and abs(act - v) <= 32:
            continue
        confirmed = False
        for _ in range(2):
            stdout2, _, code = run_command(
                f"sudo timeout 10 /hive/sbin/nvtool -q --nodev -i {i} --setmem {int(v)}")
            if code != 0:
                break
            if "was already set" in stdout2:
                confirmed = True
                break
        if not confirmed:
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
        stdout, stderr, code = run_command("nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,utilization.gpu,clocks.current.graphics,clocks.current.memory,power.limit,pci.bus_id,memory.total,vbios_version,power.min_limit,power.max_limit --format=csv,noheader,nounits")
        if code == 0 and stdout:
            lines = stdout.strip().split('\n')
            for line in lines:
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 9:
                    idx = safe_int(parts[0], 0)
                    bus_id = parts[9] if len(parts) > 9 else ""
                    vram_mb = safe_int(parts[10]) if len(parts) > 10 else 0
                    vbios_raw = parts[11] if len(parts) > 11 else ""
                    vbios = "" if (not vbios_raw or vbios_raw.upper().startswith("N/A") or vbios_raw.upper().startswith("[N/A")) else vbios_raw
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
                        "subvendor": _pci_subvendor(bus_id),
                        "vbios": vbios,
                        "power_min": safe_float(parts[12]) if len(parts) > 12 else 0.0,
                        "power_max": safe_float(parts[13]) if len(parts) > 13 else 0.0
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

def get_miner_uptime_seconds():
    """Seconds since the HiveOS miner screen session started; None when not running."""
    out, _, code = run_command("screen -ls 2>/dev/null | grep -oE '[0-9]+\\.miner' | head -n 1 | cut -d. -f1")
    pid = out.strip()
    if code != 0 or not pid.isdigit():
        return None
    out2, _, code2 = run_command(f"ps -o etimes= -p {pid} 2>/dev/null")
    if code2 != 0 or not out2.strip().isdigit():
        return None
    try:
        return int(out2.strip())
    except (TypeError, ValueError):
        return None

_SHARES_KEY_PAIRS = (
    ("total_accepted_shares", "total_rejected_shares"),   # srbminer (top-level)
    ("accepted_shares", "rejected_shares"),
    ("accepted_count", "rejected_count"),                 # t-rex
    ("accepted", "rejected"),
)

def _shares_num(v):
    """Numeric value tolerant to string-encoded numbers (SRBMiner style)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None

def _shares_sum_dict(v):
    """Sum of a per-GPU share dict like {'gpu0': 73, 'gpu1': 66}; None when not a dict."""
    if isinstance(v, dict) and v:
        return sum(_shares_num(x) or 0 for x in v.values())
    return None

def _extract_api_shares(data):
    """(accepted, rejected) from miner JSON stats, None when not reported.

    Known shapes: srbminer algorithms[].shares {accepted, rejected} (+ per-GPU
    gpu_accepted_shares/gpu_rejected_shares dicts), srbminer/t-rex/gminer
    top-level total_*_shares counters (numbers or strings), miner{} block,
    pools[] entries."""
    if not isinstance(data, dict):
        return None
    blocks = [data]
    if isinstance(data.get("miner"), dict):
        blocks.append(data["miner"])
    if isinstance(data.get("pools"), list):
        blocks.extend(p for p in data["pools"] if isinstance(p, dict))
    for block in blocks:
        for ak, rk in _SHARES_KEY_PAIRS:
            a, r = _shares_num(block.get(ak)), _shares_num(block.get(rk))
            if a is not None and r is not None:
                return int(a), int(r)
    # srbminer: algorithms[].shares aggregates + per-GPU dicts
    if isinstance(data.get("algorithms"), list):
        acc = rej = 0
        found = False
        for a in data["algorithms"]:
            if not isinstance(a, dict):
                continue
            sh = a.get("shares")
            if isinstance(sh, dict):
                av, rv = _shares_num(sh.get("accepted")), _shares_num(sh.get("rejected"))
                if av is not None and rv is not None:
                    acc += av
                    rej += rv
                    found = True
                    continue
            av = _shares_num(a.get("gpu_accepted_shares"))
            rv = _shares_num(a.get("gpu_rejected_shares"))
            if av is None:
                av = _shares_sum_dict(a.get("gpu_accepted_shares"))
            if rv is None:
                rv = _shares_sum_dict(a.get("gpu_rejected_shares"))
            if av is not None and rv is not None:
                acc += av
                rej += rv
                found = True
        if found:
            return int(acc), int(rej)
    return None

_miner_shares_cache = {"ts": 0.0, "val": None}
_miner_shares_log_ts = {"ts": 0.0}

def get_miner_shares():
    """Accepted/rejected share counters from local miner stats APIs; None when unknown."""
    now = time.time()
    if now - _miner_shares_cache["ts"] < 5:
        return _miner_shares_cache["val"]
    val = None
    if is_miner_screen_running():
        for port in (5000, 4067, 4068, 4028, 21373, 21473):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "hiveos-local"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    data = json.loads(resp.read().decode(errors="ignore"))
                shares = _extract_api_shares(data)
                if shares:
                    val = {"accepted": shares[0], "rejected": shares[1]}
                    break
                # Diagnostics: log the response shape once per 10 min so unknown
                # miner API formats can be identified from hiveos-local.log
                if now - _miner_shares_log_ts["ts"] > 600:
                    _miner_shares_log_ts["ts"] = now
                    keys = sorted(data.keys()) if isinstance(data, dict) else type(data).__name__
                    logging.warning(f"miner stats API :{port} has no known share keys; top-level keys: {keys}")
            except Exception:
                continue
    _miner_shares_cache.update({"ts": now, "val": val})
    return val

def get_extra_fans_summary():
    """{'count': N, 'avg_duty': X, 'source': 'hwmon'|'mknet'} for non-GPU fans; None when none.

    Sources: hwmon PWM channels first, then the 8MK_NET USB fan controller
    (RIG9-style rigs expose their case fans there, not via hwmon)."""
    duties = []
    try:
        for hw, chip, idx in _iter_hwmon_pwm():
            entry = _read_fan_entry(hw, chip, idx)
            duties.append(entry.get("duty", 0))
    except Exception:
        duties = []
    if duties:
        return {"count": len(duties), "avg_duty": round(sum(duties) / len(duties)), "source": "hwmon"}
    try:
        st = _mknet_stats()
        cf = st.get("casefan") if isinstance(st, dict) else None
        vals = []
        for v in cf or []:
            n = _shares_num(v)
            if n is not None:
                vals.append(n)
        if vals:
            kind = _mknet_kind()
            lbl = MKNET_CONTROLLERS[kind]["label"] if kind else "USB fan controller"
            return {"count": len(vals), "avg_duty": round(sum(vals) / len(vals)), "source": "mknet", "label": lbl}
    except Exception:
        pass
    return None

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
        if not token or not expected or not hmac.compare_digest(str(token), str(expected)):
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
    # Prune stale entries so a distributed scan cannot grow the dict unbounded
    for bad_ip in [k for k, v in failed_login_attempts.items() if v.get("blocked_until", 0.0) < now]:
        del failed_login_attempts[bad_ip]
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
        _atomic_write_file(PIN_PATH, new_password)
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
    # Pad short lists with their LAST token - hive semantics: a scalar applies
    # to every GPU (hive's own ref_arr padding extends the last element).
    # Padding with "0" instead silently turned scalar configs into GPU0-only
    # lists and wiped the OC on all other cards on the next all-GPU apply
    # (observed 12 Oct: RIG3/5/6/7/8 lost core offset/lock/PL after an
    # lmem-only apply).
    def _pad(lst):
        lst += [lst[-1]] * (max_idx + 1 - len(lst)) if lst else ["0"] * (max_idx + 1 - len(lst))

    _pad(clock)
    _pad(mem)
    _pad(plimit)
    _pad(fan)
    # Locked clocks keep explicit "0" for unlocked GPUs (cloud parity):
    # empty entries would vanish in hive's word-splitting of the conf list
    # and shift every following per-GPU value one position left
    _pad(lclock)
    _pad(lmem)

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
    # hive's nvidia-oc exits with the count of failed per-GPU operations, so
    # mixed desktop+mobile rigs are permanently nonzero (RIG2: exit 3 - the
    # laptop GPUs report power limits as [Not Supported]). The lock
    # verification below is the real judge of success; a wholesale failure
    # (nothing applied at all) is what must be caught here.
    flat = re.sub(r"\x1b\[[0-9;]*m", "", stdout or "")
    tail = " | ".join(line.strip() for line in flat.strip().splitlines()[-8:] if line.strip())
    if "does not exist, exiting" in flat or "No cards available" in flat:
        logging.error(f"NVIDIA OC script applied nothing: {tail or stderr}")
        return False, "NVIDIA overclock script failed to apply settings."
    if code != 0:
        logging.warning(f"NVIDIA OC script exit code {code}: {tail or stderr}")
    else:
        logging.info(f"NVIDIA OC script output: {tail or '(empty)'}")

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
    # Verify only real GPUs: the conf lists are padded to at least 4 tokens
    # for hive word-splitting parity, but indices beyond the actual GPU count
    # must not be expected in verification - a 2-GPU rig reported phantom
    # "GPU 2/3 locked clock not confirmed" (observed on RIG3, 2x RTX 3060)
    n_real = n if apply_all else (gpu_index + 1)
    expected_locks = {i: v for i, v in expected_locks.items() if i < n_real}
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

    # Same verification for locked memory clocks (LMEM): hive applies them with
    # `nvtool --setmem`, which fails just as silently as the core-lock path
    # (observed on RIG2: an all-GPU apply wrote LMEM=810, nvidia-oc exited 0,
    # and memory stayed at 5801 MHz — no retries, no warning, miner on unlocked
    # clocks). MEM == -3000 is hive's LOWMEM marker: the script downlocks
    # memory to a hardcoded 810 MHz for such GPUs.
    expected_mems = {}
    for i, v in enumerate(lmem):
        try:
            if int(str(v).strip()) > 0:
                expected_mems[i] = int(str(v).strip())
        except ValueError:
            continue
    for i, v in enumerate(mem):
        try:
            if int(str(v).strip()) == -3000:
                expected_mems.setdefault(i, 810)
        except ValueError:
            continue
    expected_mems = {i: v for i, v in expected_mems.items() if i < n_real}
    if expected_mems:
        mismatch = _verify_locked_mem(expected_mems)
        for _ in range(2):
            if not mismatch:
                break
            logging.warning(f"Locked memory clocks not confirmed {mismatch}, retrying nvidia-oc")
            time.sleep(3)
            run_nvidia_oc()
            mismatch = _verify_locked_mem(expected_mems)
        if mismatch:
            def _smi_mem_fallback(bad):
                for i, (exp, _act) in sorted(bad.items()):
                    run_command(f"sudo nvidia-smi -i {i} -lmc {exp}")

            logging.warning(f"Locked memory clocks still unconfirmed {mismatch}, falling back to nvidia-smi -lmc")
            _smi_mem_fallback(mismatch)
            time.sleep(2)
            mismatch = _verify_locked_mem(expected_mems)
            for _ in range(2):
                if not mismatch:
                    break
                time.sleep(3)
                _smi_mem_fallback(mismatch)
                mismatch = _verify_locked_mem(expected_mems)
        if mismatch:
            bad = ", ".join(f"GPU {i} ({exp} vs {act})" for i, (exp, act) in sorted(mismatch.items()))
            logging.warning(f"NVIDIA locked memory clock verification failed: {bad}")
            return True, f"Saved, but locked memory clock not confirmed on: {bad}. Try applying again or check nvidia-smi."

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

    if apply_all:
        # "all" previously produced gpu_index=None -> TypeError on list padding.
        # Resolve the AMD GPU count from live stats; fall back to the longest
        # existing per-GPU list so the payload still applies to known entries.
        fields_probe = ["CORE", "MEM", "VDD", "VDDCI", "MVDD", "FAN", "PL", "DPM", "REF"]
        try:
            amd_gpus = [g for g in get_gpu_stats().get("gpus", []) if g.get("brand") == "AMD"]
        except Exception:
            amd_gpus = []
        if amd_gpus:
            gpu_index = len(amd_gpus) - 1
        else:
            gpu_index = max([len(config.get(f, "").split()) for f in fields_probe] + [1]) - 1
        if gpu_index < 0:
            gpu_index = 0

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

    stdout, stderr, code = run_amd_oc()
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
            run_amd_oc()
        # Reverted state becomes the locally protected state (guard hook)
        guard_note_config_change(["nvidia-oc.conf", "amd-oc.conf"])

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
#
# HiveOS watchdog daemon (/hive/bin/wd, unit hive-watchdog) reads thresholds from
# JSON maps, NOT from a flat key:
#   WD_TYPE=miner -> WD_MINHASHES='{"<MINER>": <kH/s>}'        (per miner)
#   WD_TYPE=algo  -> WD_MINHASHES_ALGO='{"<algo>": <kH/s>}'   (per algorithm)
# If the map has no entry for the running miner/algo, the hashrate check is
# silently skipped ("minimal hashrate is not set") - the watchdog then only
# checks LA/GPU-offline. The legacy flat key WD_MIN_HASHRATE (which older
# panel versions wrote) is ignored by current wd versions entirely.
# Thresholds are in kH/s (the agent's /run/hive/khs file); the UI sends MH/s,
# converted here (x1000). Actions on low hashrate come from rig.conf keys
# WD_MINER (minutes -> miner restart) and WD_REBOOT (minutes -> rig reboot).
def _wd_threshold_maps(rig_conf):
    maps = {}
    for key in ("WD_MINHASHES", "WD_MINHASHES_ALGO"):
        try:
            m = json.loads(rig_conf.get(key, "") or "{}")
            maps[key] = m if isinstance(m, dict) else {}
        except Exception:
            maps[key] = {}
    return maps


def _wd_current_algo():
    try:
        wc = parse_shell_config(WALLET_CONF_PATH)
        algo = str(wc.get("CUSTOM_ALGO") or "").strip().lower()
        if algo:
            return algo
    except Exception:
        pass
    try:
        with open("/run/hive/last_stat.json") as f:
            st = json.load(f)
        algo = str((st.get("params") or {}).get("miner_stats", {}).get("algo") or "").strip().lower()
        if algo and algo != "null":
            return algo
    except Exception:
        pass
    return ""


def _to_float(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


@app.route('/api/watchdog', methods=['GET', 'POST'])
def handle_watchdog():
    # wd_min_hashrate is canonical kH/s in both directions; the UI keeps a unit
    # selector (kH/s, MH/s, GH/s, TH/s) and converts on its side.
    if request.method == 'GET':
        rig_conf = parse_shell_config(RIG_CONF_PATH)
        maps = _wd_threshold_maps(rig_conf)
        miner = str(rig_conf.get("MINER") or "").strip()
        algo = _wd_current_algo()
        eff_khs = _to_float(maps["WD_MINHASHES"].get(miner)) if miner else None
        if eff_khs is None and algo:
            eff_khs = _to_float(maps["WD_MINHASHES_ALGO"].get(algo))
        if eff_khs is None:
            # legacy flat value was written by old panel versions in MH/s form units
            legacy = _to_float(rig_conf.get("WD_MIN_HASHRATE", ""))
            eff_khs = legacy * 1000.0 if legacy is not None else 0.0
        return jsonify({
            "success": True,
            "wd_enabled": rig_conf.get("WD_ENABLED", "0"),
            "wd_min_hashrate": eff_khs,
            "wd_miner": miner,
            "wd_algo": algo,
            "wd_min_hashrate_khs": eff_khs,
            "wd_restart_min": (rig_conf.get("WD_MINER") or "").strip(),
            "wd_reboot_min": (rig_conf.get("WD_REBOOT") or "").strip()
        })

    # POST
    data = request.get_json()
    if not data:
        return jsonify({"success": False, "message": "Invalid payload"}), 400

    enabled = str(data.get("wd_enabled", "0")).strip()
    khs = _to_float(data.get("wd_min_hashrate", "0"))

    if enabled not in ["0", "1"]:
        return jsonify({"success": False, "message": "wd_enabled must be 0 or 1."}), 400
    if khs is None or khs < 0:
        return jsonify({"success": False, "message": "Min hashrate must be a valid number."}), 400

    rig_conf = parse_shell_config(RIG_CONF_PATH)
    miner = str(rig_conf.get("MINER") or "").strip()
    algo = _wd_current_algo()
    maps = _wd_threshold_maps(rig_conf)
    khs = int(khs)

    if miner:
        maps["WD_MINHASHES"][miner] = khs
    if algo:
        maps["WD_MINHASHES_ALGO"][algo] = khs
    if not miner and not algo:
        return jsonify({"success": False, "message": "Cannot determine current miner or algorithm on this rig."}), 400

    rig_conf["WD_ENABLED"] = enabled
    rig_conf["WD_MINHASHES"] = json.dumps(maps["WD_MINHASHES"], separators=(",", ":"))
    rig_conf["WD_MINHASHES_ALGO"] = json.dumps(maps["WD_MINHASHES_ALGO"], separators=(",", ":"))
    # legacy flat key is not read by current wd versions - drop it to avoid confusion
    rig_conf.pop("WD_MIN_HASHRATE", None)

    if write_shell_config(RIG_CONF_PATH, rig_conf):
        target = miner or algo
        logging.info(f"Watchdog settings updated by IP: {request.remote_addr} (Enabled={enabled}, {target}: {khs} kH/s)")
        run_command("sudo /hive/bin/wd restart")
        # Persist the user's intent: hive/cloud rewrites can silently reset the
        # WD maps (RIG1+RIG2 lost their armed thresholds this way between the
        # 1.12.16 arming and 11 Oct; the local-guard snapshot then defended the
        # wiped state). The self-heal worker re-asserts this intent, so the
        # hashrate watchdog can not be disarmed behind our back again.
        _wd_intent_save({
            "enabled": enabled,
            "miner": miner,
            "miner_khs": khs if miner else None,
            "algo": algo,
            "algo_khs": khs if algo else None,
            "ts": int(time.time())
        })
        return jsonify({
            "success": True,
            "message": "Watchdog settings saved!",
            "wd_min_hashrate_khs": khs
        })
    else:
        return jsonify({"success": False, "message": "Failed to write watchdog settings to rig.conf."}), 500


WD_INTENT_PATH = os.path.join(HIVE_CONFIG_DIR, "wd_intent.json")
_wd_intent_lock = threading.Lock()
_wd_intent_cache = {"loaded": False, "data": None}


def _wd_intent_load():
    with _wd_intent_lock:
        if _wd_intent_cache["loaded"]:
            return _wd_intent_cache["data"]
        data = None
        try:
            with open(WD_INTENT_PATH, 'r') as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                data = disk
        except Exception:
            pass
        _wd_intent_cache["data"] = data
        _wd_intent_cache["loaded"] = True
        return data


def _wd_intent_save(intent):
    with _wd_intent_lock:
        _wd_intent_cache["data"] = intent
        _wd_intent_cache["loaded"] = True
        try:
            _atomic_write_file(WD_INTENT_PATH, json.dumps(intent, indent=2))
        except Exception as e:
            logging.error(f"Failed to save watchdog intent: {e}")


def _wd_intent_drift(intent, rig_conf, maps):
    """Keys where the live rig.conf watchdog state differs from the stored
    intent. Only the intent's own miner/algo entries are enforced - foreign
    map entries (other miners/algos) are left untouched."""
    drift = []
    if str(rig_conf.get("WD_ENABLED", "0")) != str(intent.get("enabled", "0")):
        drift.append("WD_ENABLED")
    miner = str(intent.get("miner") or "")
    want = intent.get("miner_khs")
    if miner and want is not None:
        got = _to_float(maps["WD_MINHASHES"].get(miner))
        if got is None or abs(got - float(want)) > 0.5:
            drift.append("WD_MINHASHES[%s]" % miner)
    algo = str(intent.get("algo") or "")
    want = intent.get("algo_khs")
    if algo and want is not None:
        got = _to_float(maps["WD_MINHASHES_ALGO"].get(algo))
        if got is None or abs(got - float(want)) > 0.5:
            drift.append("WD_MINHASHES_ALGO[%s]" % algo)
    return drift


def _heal_check_wd_intent(st, report):
    """Self-heal #5: the user's watchdog arming is enforced against config
    erosion. hive/cloud rewrites of rig.conf can reset WD_ENABLED/WD_MINHASHES
    (observed: both RIG1 and RIG2 sat 'enabled' with empty threshold maps for
    weeks - the hashrate branch of hive wd was silently dead). The intent is
    written by POST /api/watchdog; here it is re-applied (write goes through
    write_shell_config, so the local-guard snapshot is refreshed too and
    starts defending the armed values instead of the wiped ones)."""
    intent = _wd_intent_load()
    if not intent or str(intent.get("enabled", "0")) != "1":
        return
    if _guard_file_drifted("rig.conf"):
        # rig.conf was just overwritten from the cloud and Local mode will
        # revert it: rewriting it now would make the guard ADOPT the cloud
        # config (RIG2 27 Oct: wrong miner after boot). Guard goes first.
        report["notes"].append("watchdog intent check deferred (guard is reverting rig.conf)")
        return
    rig_conf = parse_shell_config(RIG_CONF_PATH)
    maps = _wd_threshold_maps(rig_conf)
    drift = _wd_intent_drift(intent, rig_conf, maps)
    if not drift:
        return
    now = int(time.time())
    if now - int(st.get("last_wd_fix") or 0) < HEAL_WD_GAP:
        report["notes"].append("watchdog drift fix skipped (rate limit): " + ", ".join(drift))
        return
    st["last_wd_fix"] = now
    miner = str(intent.get("miner") or "")
    algo = str(intent.get("algo") or "")
    rig_conf["WD_ENABLED"] = "1"
    want = intent.get("miner_khs")
    if miner and want is not None:
        maps["WD_MINHASHES"][miner] = int(float(want))
        rig_conf["WD_MINHASHES"] = json.dumps(maps["WD_MINHASHES"], separators=(",", ":"))
    want = intent.get("algo_khs")
    if algo and want is not None:
        maps["WD_MINHASHES_ALGO"][algo] = int(float(want))
        rig_conf["WD_MINHASHES_ALGO"] = json.dumps(maps["WD_MINHASHES_ALGO"], separators=(",", ":"))
    if write_shell_config(RIG_CONF_PATH, rig_conf):
        run_command("sudo /hive/bin/wd restart")
        st["last_action"] = "watchdog threshold re-asserted (%s)" % ", ".join(drift)
        report["fixed"].append(st["last_action"])
        logging.warning("Self-heal: %s", st["last_action"])
        record_metrics_event("warning", "Self-heal: " + st["last_action"])
    else:
        report["notes"].append("watchdog threshold re-assert failed (write error)")

# 3b. Hardware watchdog (iTCO_wdt via systemd RuntimeWatchdogSec)
#
# A kernel freeze (RIG9 21 Oct: whole-OS hang for 5.5h, only a manual reset
# helped; same class as RIG2 30 Sep) kills every in-OS healer - the only
# automatic cure is a hardware timer that resets the board when PID1 stops
# petting it. With RuntimeWatchdogSec>0 in /etc/systemd/system.conf systemd
# opens /dev/watchdog and pings it every RuntimeWatchdogSec/2; a frozen
# kernel stops being petted and the hardware resets the rig. Applied by an
# atomic conf rewrite + `systemctl daemon-reexec` (no reboot needed; verified
# live: enable -> state active, disable -> device released, state inactive).
# The BTC B250/Kaby boards on this farm expose iTCO_wdt built into the hive
# kernel (/sys/class/watchdog/watchdog0).
SWDT_SYSTEM_CONF = "/etc/systemd/system.conf"
SWDT_SYSFS = "/sys/class/watchdog/watchdog0"
SWDT_TIMEOUT_SEC = 10

def _swdt_conf_lines():
    try:
        with open(SWDT_SYSTEM_CONF, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except Exception:
        return []

def _swdt_status():
    st = {"available": os.path.exists("/dev/watchdog"), "identity": "", "hw_state": "",
          "hw_timeout": 0, "enabled": False, "conf_value": ""}
    for key, name in (("identity", "identity"), ("hw_state", "state"), ("hw_timeout", "timeout")):
        try:
            st[key] = open(os.path.join(SWDT_SYSFS, name)).read().strip()
        except Exception:
            pass
    try:
        st["hw_timeout"] = int(st["hw_timeout"] or 0)
    except (TypeError, ValueError):
        st["hw_timeout"] = 0
    # last uncommented RuntimeWatchdogSec wins (systemd config semantics)
    val = None
    for ln in _swdt_conf_lines():
        m = re.match(r"^\s*RuntimeWatchdogSec\s*=\s*(\S+)\s*$", ln)
        if m:
            val = m.group(1)
    st["conf_value"] = val or ""
    st["enabled"] = val not in (None, "", "0")
    return st

def _swdt_set_enabled(enable):
    """Rewrite RuntimeWatchdogSec in system.conf atomically and re-exec systemd.
    Returns (ok, message, status)."""
    lines = _swdt_conf_lines()
    if not lines:
        return False, "Cannot read " + SWDT_SYSTEM_CONF, _swdt_status()
    new_val = str(SWDT_TIMEOUT_SEC) if enable else "0"
    out, replaced = [], False
    for ln in lines:
        if re.match(r"^\s*#?\s*RuntimeWatchdogSec\s*=", ln):
            if not replaced:
                out.append("RuntimeWatchdogSec=" + new_val)
                replaced = True
            # duplicate RuntimeWatchdogSec lines are dropped - the last one
            # would silently win in systemd anyway
        else:
            out.append(ln)
    if not replaced:
        # insert as the first directive of the [Manager] section, or before
        # the first other section header; append a [Manager] block if absent
        ins = None
        for i, ln in enumerate(out):
            s = ln.strip()
            if s == "[Manager]":
                ins = i + 1
                break
            if s.startswith("[") and s.endswith("]"):
                ins = i
                break
        if ins is None:
            out += ["", "[Manager]", "RuntimeWatchdogSec=" + new_val]
        else:
            out.insert(ins, "RuntimeWatchdogSec=" + new_val)
    try:
        ts = int(time.time())
        shutil.copy2(SWDT_SYSTEM_CONF, SWDT_SYSTEM_CONF + ".bak-swdt-" + str(ts))
        for old in sorted(glob.glob(SWDT_SYSTEM_CONF + ".bak-swdt-*"))[:-5]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass
    tmp = SWDT_SYSTEM_CONF + ".tmp-swdt"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(SWDT_SYSTEM_CONF, tmp)
        os.replace(tmp, SWDT_SYSTEM_CONF)
    except OSError as e:
        return False, "Failed to write " + SWDT_SYSTEM_CONF + ": " + str(e), _swdt_status()
    try:
        p = subprocess.run("systemctl daemon-reexec", shell=True,
                           capture_output=True, text=True, timeout=60)
        rc, err = p.returncode, (p.stderr or "").strip()
    except Exception as e:
        rc, err = -1, str(e)
    want = "active" if enable else "inactive"
    deadline = time.time() + 10
    while True:
        st = _swdt_status()
        if st["hw_state"] == want:
            break
        if time.time() >= deadline:
            break
        time.sleep(1)
    if rc != 0:
        return False, "systemctl daemon-reexec failed: " + (err or "rc=" + str(rc)), st
    if st["enabled"] != enable or st["hw_state"] != want:
        return False, ("Hardware watchdog did not reach the expected state (conf=%s, hw=%s)."
                       % (st["conf_value"] or "?", st["hw_state"] or "?")), st
    if enable:
        msg = ("Hardware watchdog enabled (%s pings /dev/watchdog every %d s - a frozen kernel "
               "gets hardware-reset within ~%d s)."
               % (st["identity"] or "systemd", max((st["hw_timeout"] or SWDT_TIMEOUT_SEC) // 2, 1),
                  st["hw_timeout"] or SWDT_TIMEOUT_SEC))
    else:
        msg = "Hardware watchdog disabled (device released, auto-reset off)."
    logging.info("Hardware watchdog %s by IP: %s", "enabled" if enable else "disabled", request.remote_addr)
    return True, msg, st

@app.route('/api/syswatchdog', methods=['GET', 'POST'])
def handle_syswatchdog():
    if request.method == 'GET':
        return jsonify({"success": True, **_swdt_status()})
    data = request.get_json(silent=True) or {}
    if "enabled" not in data:
        return jsonify({"success": False, "message": "Provide enabled: true/false."}), 400
    enable = bool(data.get("enabled"))
    if enable and not _swdt_status()["available"]:
        return jsonify({"success": False, "message": "No /dev/watchdog device on this rig."}), 400
    ok, msg, st = _swdt_set_enabled(enable)
    return jsonify({"success": ok, "message": msg, **st}), (200 if ok else 500)

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
        # Several presets may share an algo — the default-flagged one wins
        matching = [p for p in presets
                    if str(p.get("algo", "")).strip().lower() == algo]
        preset = next((p for p in matching if p.get("is_default")), None) or \
            (matching[0] if matching else None)
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
    Several presets may share an algorithm; the default-flagged one is the
    preset that actually auto-applies for miners on that algorithm."""
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

def _resolve_worker_name():
    """%WORKER_NAME% source: rig.conf WORKER_NAME first (it is the cloud worker
    name), then RIG_ID (legacy rigs where WORKER_NAME is absent), then hostname.
    RIG_ID itself must stay the NUMERIC cloud worker id - the hive agent
    authenticates its stats pushes with it (id_rig=RIG_ID), so it can never be
    used as the pool worker name on properly registered rigs."""
    try:
        conf = parse_shell_config(RIG_CONF_PATH)
    except Exception:
        conf = {}
    name = (conf.get("WORKER_NAME") or "").strip()
    if not name:
        name = (conf.get("RIG_ID") or "").strip()
    name = name.upper().replace(" ", "_")
    if name:
        return name
    return socket.gethostname().strip().upper().replace(" ", "_") or "WORKER"


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

        worker_name = _resolve_worker_name()
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
        _atomic_write_file(path, json.dumps(data, indent=2))
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
        hostname = _resolve_worker_name()
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
        run_amd_oc()
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

# ---------------- USB fan controllers (CP210x serial, hive 8mknet/ykeda autofan) ----------------
# The controller firmware implements the regulation itself: fan_mode 2 = auto
# (keeps the minimal speed within min..max to reach target_temp), 1 = static
# (manual_fan_speed). Parameters are passed via <kind>_autofan.conf and applied
# by invoking the hive script (sources /etc/environment itself).
# Two hive controller models share the JSON protocol: 8mknet (iSerial
# Autofan8MK_NET) and ykeda (iSerial ykeda_autofan, e.g. RIG3).

MKNET_CONTROLLERS = {
    "8mknet": {
        "label": "8MK_NET USB controller",
        "script": "/hive/opt/8mknet/8mknet_autofan",
        "conf": os.path.join(HIVE_CONFIG_DIR, "8mknet_autofan.conf"),
        "serial": "/dev/serial/by-id/usb-Silicon_Labs_Device_for_hiveos_Autofan8MK_NET-if00-port0",
        "stats_ok": "/run/hive/8mknet_latest_ok",
        "output": "/run/hive/8mknet_autofan",
        "min_fan": 5,
        "static_speed": 70,
    },
    "ykeda": {
        "label": "Ykeda USB controller",
        "script": "/hive/opt/ykeda/ykeda_autofan",
        "conf": os.path.join(HIVE_CONFIG_DIR, "ykeda_autofan.conf"),
        "serial": "/dev/serial/by-id/usb-Silicon_Labs_Donnager_ykeda_autofan-if00-port0",
        "stats_ok": "/run/hive/ykeda_latest_ok",
        "output": "/run/hive/ykeda_autofan",
        "min_fan": 30,
        "static_speed": 50,
    },
}
# hive writes this marker at boot when a ykeda controller is detected
# (YKEDA_AVAILABLE_FILE in /etc/environment); the by-id name of the ykeda
# unit (ID_MODEL=Donnager) is less stable than this marker.
MKNET_YKEDA_AVAILABLE = "/run/hive/ykeda_available"

def _mknet_kind():
    """Active USB fan controller kind: '8mknet' | 'ykeda' | None."""
    for kind in ("8mknet", "ykeda"):
        c = MKNET_CONTROLLERS[kind]
        if not os.path.exists(c["script"]):
            continue
        if os.path.exists(c["serial"]):
            return kind
        if kind == "ykeda" and os.path.exists(MKNET_YKEDA_AVAILABLE):
            return kind
    return None

def _mknet_present():
    return _mknet_kind() is not None

def _mknet_stats(kind=None):
    """Latest controller report: {"casefan":[...N...], "thermosensors":[...]}."""
    if kind is None:
        kind = _mknet_kind()
    if not kind:
        return None
    c = MKNET_CONTROLLERS[kind]
    for path in (c["stats_ok"], c["output"]):
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
    kind = _mknet_kind()
    if not kind:
        return {"present": False}
    c = MKNET_CONTROLLERS[kind]
    conf = parse_shell_config(c["conf"])
    def _num(key, default):
        try:
            return int(str(conf.get(key, "")).strip())
        except (TypeError, ValueError):
            return default
    return {
        "present": True,
        "kind": kind,
        "label": c["label"],
        "stats": _mknet_stats(kind),
        "config": {
            "auto": str(conf.get("AUTO_ENABLED", "1")).strip() == "1",
            "target_temp": _num("TARGET_TEMP", 60),
            "target_mem_temp": _num("TARGET_MEM_TEMP", 90),
            "min_fan": _num("MIN_FAN", c["min_fan"]),
            "max_fan": _num("MAX_FAN", 100),
            "static_speed": _num("MANUAL_FAN", c["static_speed"]),
        },
    }

@app.route('/api/fans/mknet', methods=['POST'])
def set_mknet_fans():
    kind = _mknet_kind()
    if not kind:
        return jsonify({"success": False, "message": "USB fan controller not found on this rig."}), 404
    paths = MKNET_CONTROLLERS[kind]
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

    conf = parse_shell_config(paths["conf"])
    conf["AUTO_ENABLED"] = "1" if mode == "auto" else "0"
    conf["TARGET_TEMP"] = str(target_temp)
    conf["TARGET_MEM_TEMP"] = str(target_mem)
    conf["MIN_FAN"] = str(min_fan)
    conf["MAX_FAN"] = str(max_fan)
    if mode == "static" and static_speed is not None:
        conf["MANUAL_FAN"] = str(static_speed)
    if not write_shell_config(paths["conf"], conf):
        return jsonify({"success": False, "message": f"Failed to write {os.path.basename(paths['conf'])}"}), 500
    out, err, code = run_command(f"sudo {paths['script']} --get_json")
    applied = '"casefan"' in (out or "")
    logging.info(f"{paths['label']} fans set to {mode}"
                 + (f", static {static_speed}%" if mode == "static" else
                    f", target {target_temp}°C, min {min_fan}%, max {max_fan}%")
                 + f" by IP: {request.remote_addr}")
    if not applied:
        return jsonify({"success": True, "message": "Settings saved, but the controller did not respond - check the USB connection."})
    return jsonify({"success": True, "message": f"{paths['label']} fan settings applied."})


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

def _version_tuple(v):
    """(1, 12, 6) for 'v1.12.6' / '1.12.6-dev' — numeric prefix compare."""
    parts = re.findall(r'\d+', str(v or ""))
    return tuple(int(x) for x in parts[:4]) if parts else None

@app.route('/api/update/check', methods=['GET'])
def check_update():
    try:
        # Cache-bust the raw URL: GitHub's CDN serves version.txt with a
        # max-age window, and a rig updated right after a push used to be
        # told about the previous release (stale 1.12.5 vs local 1.12.6).
        url = ("https://raw.githubusercontent.com/h1w0rld/hiveos-local/main/version.txt"
               "?t=" + str(int(time.time())))
        req = urllib.request.Request(url, headers={'User-Agent': 'HiveOS-Local-Dashboard'})
        with urllib.request.urlopen(req, timeout=5) as response:
            remote_ver = response.read().decode('utf-8').strip()

        local_t, remote_t = _version_tuple(VERSION), _version_tuple(remote_ver)
        if local_t is None or remote_t is None:
            return jsonify({
                "success": False,
                "local_version": VERSION,
                "remote_version": remote_ver or "Unknown",
                "update_available": False,
                "error": "Invalid version payload."
            })
        # An update exists only when the published release is NEWER than the
        # local one — a stale/fetched-older remote must not flag the banner.
        update_available = remote_t > local_t

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

_env_versions_cache = {"ts": 0.0, "driver": "", "hiveos": ""}

def _detect_env_versions(max_age=60):
    """(driver_version, hiveos_version) with a short in-process cache.

    driver: first line of `nvidia-smi --query-gpu=driver_version` (e.g. 595.91.07)
    hiveos: installed `hive` apt package version (e.g. 0.6-231, what selfupgrade moves)
    """
    now = time.time()
    if _env_versions_cache["ts"] and now - _env_versions_cache["ts"] < max_age:
        return _env_versions_cache["driver"], _env_versions_cache["hiveos"]
    driver, hiveos = "", ""
    out, _, _ = run_command("nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1")
    driver = out.strip().splitlines()[0].strip() if out.strip() else ""
    out, _, _ = run_command("dpkg-query -W -f='${Version}' hive 2>/dev/null")
    hiveos = out.strip()
    _env_versions_cache.update({"ts": now, "driver": driver, "hiveos": hiveos})
    return driver, hiveos

_latest_env_cache = {"ts": 0.0, "driver": "", "hiveos": ""}

def _detect_latest_versions(max_age=60):
    """(driver_latest, hiveos_latest) — versions an update would move to.

    driver: newest version offered by `nvidia-driver-update --list` (network
    fetch of the hive driver index, cached for 60s so a manual Refresh picks up
    new releases promptly; the rig has no NVIDIA "stable" marker — max of all
    listed branches is the freshest).
    hiveos: apt candidate of the `hive` package (from the rig's current apt
    lists; selfupgrade refreshes them during the actual upgrade).
    """
    now = time.time()
    if _latest_env_cache["ts"] and now - _latest_env_cache["ts"] < max_age:
        return _latest_env_cache["driver"], _latest_env_cache["hiveos"]
    driver_latest, hiveos_latest = "", ""

    def _vt(v):
        parts = re.findall(r"\d+", v or "")
        return tuple(int(p) for p in parts[:4]) if parts else ()

    out, _, _ = run_command("sudo /hive/sbin/nvidia-driver-update --list 2>/dev/null")
    if out:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        tokens = re.findall(r"\b\d{3,4}\.\d+(?:\.\d+)*\b", plain)
        if tokens:
            driver_latest = max(tokens, key=_vt)
    out, _, _ = run_command("apt-cache policy hive 2>/dev/null")
    m = re.search(r"Candidate:\s*(\S+)", out or "")
    if m and m.group(1) != "(none)":
        hiveos_latest = m.group(1)
    _latest_env_cache.update({"ts": now, "driver": driver_latest, "hiveos": hiveos_latest})
    return driver_latest, hiveos_latest

@app.route('/api/version', methods=['GET'])
def api_version():
    """Lightweight per-rig version report (no GitHub round-trip).

    Used by the Cluster Update card: self calls it directly, peers through
    the /api/remote/<id> SSH proxy. `version` is this dashboard's release,
    `driver`/`hiveos` (+ their `_latest` counterparts) feed the update-form
    component switch (GPU Drivers / Hive OS rows).
    """
    driver, hiveos = _detect_env_versions()
    driver_latest, hiveos_latest = _detect_latest_versions()
    return jsonify({
        "success": True,
        "version": VERSION,
        "driver": driver, "driver_latest": driver_latest,
        "hiveos": hiveos, "hiveos_latest": hiveos_latest,
    })

# ---------------- Component update jobs (GPU drivers / Hive OS) ----------------
# Long-running system updates run in the background (driver download alone can
# take 10+ minutes): POST /api/update/pull with component != 'panel' starts a
# nohup job and returns immediately; GET /api/update/status reports progress.
# Job state is derivable from the persisted status file (pid) + the job log
# (UPDATE_EXIT_CODE marker), so it survives a panel restart mid-job.

UPDATE_STATUS_PATH = os.path.join(HIVE_CONFIG_DIR, "update_status.json")
UPDATE_LOG_DIR = "/var/log"
UPDATE_COMPONENT_CMDS = {
    # sudo resets PATH to secure_path, which drops /hive/bin and /hive/sbin -
    # selfupgrade internally calls miner/hpkg/wd/autofan and dies with exit 127
    # ("command not found") when run from a non-login context (panel). Restore
    # the full Hive PATH explicitly for the child process.
    # nvidia-driver-update stops the miner itself (nvstop) and restarts it after
    "drivers": ("if ! command -v nvidia-smi >/dev/null 2>&1; then "
                "echo 'No NVIDIA GPUs detected on this rig - driver update is not applicable'; exit 3; fi; "
                "sudo env PATH=/hive/sbin:/hive/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
                "/hive/sbin/nvidia-driver-update"),
    "hiveos": ("sudo env PATH=/hive/sbin:/hive/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
               "/hive/bin/selfupgrade"),
}
UPDATE_COMPONENT_LABELS = {
    "drivers": "NVIDIA driver update",
    "hiveos": "Hive OS upgrade",
}

def _update_log_path(component):
    return os.path.join(UPDATE_LOG_DIR, f"hive-local-update-{component}.log")

def _update_status_read():
    try:
        with open(UPDATE_STATUS_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _update_status_write(data):
    tmp = UPDATE_STATUS_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, UPDATE_STATUS_PATH)
    except OSError:
        pass

def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False

def _read_log_tail(path, limit=3000):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit * 4))
            return fh.read().decode("utf-8", "replace")[-limit:]
    except OSError:
        return ""

def _update_exit_code(log_text):
    """Last UPDATE_EXIT_CODE:<n> marker written by the job wrapper."""
    for line in reversed(log_text.splitlines()):
        line = line.strip()
        if line.startswith("UPDATE_EXIT_CODE:"):
            try:
                return int(line.split(":", 1)[1])
            except ValueError:
                return None
    return None

def _update_job_state(component):
    entry = _update_status_read().get(component)
    if not isinstance(entry, dict) or not entry.get("pid"):
        return None
    state = {
        "component": component,
        "pid": entry.get("pid"),
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
    }
    if _pid_alive(entry.get("pid")):
        state.update({"running": True, "ok": None, "exit_code": None,
                      "log_tail": _read_log_tail(_update_log_path(component), 1500)})
        return state
    log_text = _read_log_tail(_update_log_path(component))
    code = _update_exit_code(log_text)
    state.update({"running": False, "exit_code": code, "ok": code == 0,
                  "log_tail": log_text})
    return state

def _update_job_start(component):
    """Start a background update job; returns its state, or None if busy."""
    current = _update_job_state(component)
    if current and current.get("running"):
        return None
    log_path = _update_log_path(component)
    try:
        open(log_path, "w").close()
    except OSError:
        pass
    inner = "{cmd} > {log} 2>&1; rc=$?; echo UPDATE_EXIT_CODE:$rc >> {log}".format(
        cmd=UPDATE_COMPONENT_CMDS[component], log=log_path)
    try:
        proc = subprocess.Popen(["bash", "-c", inner], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    status = _update_status_read()
    status[component] = {"pid": proc.pid, "started_at": int(time.time())}
    _update_status_write(status)
    return _update_job_state(component)

@app.route('/api/update/status', methods=['GET'])
def update_job_status():
    """Background update job progress (self direct, peers via SSH proxy)."""
    states = {}
    for component in UPDATE_COMPONENT_CMDS:
        state = _update_job_state(component)
        if state:
            states[component] = state
    return jsonify({"success": True, "jobs": states})

GITHUB_TARBALL_URL = "https://codeload.github.com/h1w0rld/hiveos-local/tar.gz/refs/heads/main"

def _download_update_tarball(dest_path, timeout=90):
    """Fetch the current main-branch tarball from GitHub (public repo, no auth)."""
    req = urllib.request.Request(GITHUB_TARBALL_URL, headers={'User-Agent': 'HiveOS-Local-Dashboard'})
    with urllib.request.urlopen(req, timeout=timeout) as response, open(dest_path, 'wb') as fh:
        shutil.copyfileobj(response, fh)

def _update_from_tarball(cwd):
    """Fallback update path for tar-copy installs without a .git directory
    (and for rigs whose git flow failed): overlay the GitHub release tarball
    over the install directory. Semantics mirror `git reset --hard origin/main`
    for tracked files — existing files are overwritten, untracked local files
    are left alone."""
    tmp = os.path.join(tempfile.gettempdir(), f"hiveos-local-update-{int(time.time())}.tar.gz")
    try:
        try:
            _download_update_tarball(tmp)
        except Exception as e:
            return False, f"tarball download failed: {e}"
        try:
            with open(tmp, 'rb') as fh:
                if fh.read(2) != b'\x1f\x8b':
                    return False, "downloaded update is not a gzip archive"
        except OSError as e:
            return False, f"tarball read failed: {e}"
        _, err, code = run_command(f"tar -xzf {tmp} -C {cwd} --strip-components=1")
        if code != 0:
            return False, f"tar extract failed: {err or 'exit code ' + str(code)}"
        return True, ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

@app.route('/api/update/pull', methods=['POST'])
def pull_update():
    data = request.get_json()
    if not data or ('password' not in data and 'pin' not in data):
        return jsonify({"success": False, "message": "Missing password verification parameter"}), 400

    user_key = str(data.get('password', data.get('pin', ''))).strip()
    if not user_key or not hmac.compare_digest(user_key, str(app.config['ACCESS_PASSWORD'])):
        logging.warning(f"Failed update password verification attempt from IP: {request.remote_addr}")
        return jsonify({"success": False, "message": "Invalid password verification. Update aborted."}), 401

    component = str(data.get('component') or 'panel').strip()
    if component not in ('panel',) + tuple(UPDATE_COMPONENT_CMDS):
        return jsonify({"success": False, "message": "Unknown update component."}), 400

    logging.info(f"Dashboard update authorized with password by IP: {request.remote_addr} (component={component})")

    # Drivers / Hive OS: long background jobs — start and return immediately.
    if component in UPDATE_COMPONENT_CMDS:
        state = _update_job_start(component)
        if state is None:
            label = UPDATE_COMPONENT_LABELS[component]
            return jsonify({"success": False,
                            "message": f"{label} is already running on this rig."}), 409
        if state is False:
            return jsonify({"success": False,
                            "message": "Failed to start the update job."}), 500
        label = UPDATE_COMPONENT_LABELS[component]
        logging.info(f"{label} started in background (pid {state.get('pid')})")
        return jsonify({"success": True, "component": component, "async": True,
                        "message": f"{label} started! Track progress in the Status column."})

    cwd = os.getcwd()

    updated = False
    msg = ""
    detail = ""

    if os.path.isdir(os.path.join(cwd, ".git")):
        # Add safe directory flag
        run_command(f"git config --global --add safe.directory {cwd}")

        stdout_f, stderr_f, code_f = run_command("git fetch --all")
        stdout_r, stderr_r, code_r = run_command("git reset --hard origin/main")

        if code_r == 0:
            updated = True
            msg = "Update successfully pulled from GitHub! Restarting dashboard service..."
        else:
            detail = f"git: {stderr_r or stderr_f}"

    if not updated:
        # Tar-copy installs (no .git) and rigs whose git flow failed update
        # by overlaying the GitHub tarball directly.
        ok, err = _update_from_tarball(cwd)
        if ok:
            updated = True
            msg = "Update installed from GitHub release archive! Restarting dashboard service..."
        else:
            detail = (detail + " | " if detail else "") + err

    if updated:
        logging.info(msg)
        cmd = 'nohup bash -c "sleep 1.5 && sudo systemctl restart hiveos-local.service" > /dev/null 2>&1 &'
        subprocess.Popen(cmd, shell=True)
        return jsonify({"success": True, "message": msg})

    err_msg = f"Failed to pull dashboard update: {detail}"
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
    with _metrics_lock:
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

# ---------------- Home Assistant integration --------------------------------
# Panel <-> Home Assistant bridge for two assisted operations:
#   (1) Smart-plug power cycling: a rig marked as "boiler room" (К) or "house"
#       (Д) that is COMPLETELY unreachable (no SSH route answers) for several
#       minutes gets its smart plug toggled off/on through the HA REST API.
#       The 13h POST-hang case (RIG2, 27 Oct) and the 11h "alive but dark"
#       case (RIG1, 25 Oct) are exactly the states no in-OS mechanism can
#       get out of - the plug is the external actor.
#   (2) Fan assist in the boiler room: GPU temps of the rigs marked К drive
#       one fan (a smart plug + optional channel switches that pick the
#       speed). Target band 57-62 °C: any card > 62 -> fan on; average < 57
#       -> fan off. The freeze protection ALWAYS outranks cooling: outdoor
#       below 0 or the room sensor below +5 (or a dead room sensor falling
#       back to a negative outdoor reading, or both sensors dark) means
#       "no fan, ever" while it holds - a frozen pipe costs the building,
#       a hot GPU costs nothing (rigs have their own autofan/critics).
#
# Connection data lives on EVERY panel (/hive-config/ha_config.json) and is
# spread with the Share button (SSH proxy). Per-rig marks and plug entities
# are fields of the cluster state and sync with it. Decisions are anchored
# on the plug's os.last_changed in HA (not on local memory), so any number
# of panels running the workers converge on the same truth and double
# commands are idempotent.

HA_CONF_PATH = os.path.join(HIVE_CONFIG_DIR, "ha_config.json")
HA_STATE_PATH = os.path.join(HIVE_CONFIG_DIR, "ha_state.json")
HA_ENTITY_ID_RE = re.compile(r'^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$')
HA_DOMAINS = ("switch", "fan", "sensor", "binary_sensor", "group", "input_boolean")

HA_CHECK_TIMEOUT = 8        # GET /api/ auth probe
HA_CALL_TIMEOUT = 10        # services / states calls
HA_PROBE_INTERVAL = 120     # worker re-probe of connection health
HA_BOOT_DELAY = 420         # let the rig finish booting before acting

HA_POWER_INTERVAL = 60      # powercycle worker poll
HA_POWER_DOWN = 300         # rig fully unreachable this long -> plug cycle
HA_POWER_DWELL = 30         # plug stays OFF this long (guaranteed power cycle)
HA_POWER_GAP = 3600         # min time between plug cycles per rig
HA_POWER_TCP_TIMEOUT = 3    # per-route reachability probe

HA_FAN_INTERVAL = 60
HA_FAN_ON_TEMP = 62.0       # any K-rig card above this -> fan needed
HA_FAN_OFF_TEMP = 57.0      # all-K-rig average below this -> fan off
HA_FAN_MIN_DWELL = 600      # min time in the CURRENT plug state (anti-flapping)
HA_FAN_MAX_ON = 1800        # continuous run cap -> forced off
HA_FAN_ON_FRESH = 180       # rig stats must be fresher than this for a fan-ON
HA_FAN_OFF_FRESH = 600      # stale-but-not-too-stale temps may still turn the fan off
HA_FAN_MAX_CYCLES = 12      # max auto on/off cycles per day
# Layout discovery - the farm arrangement lives in the user's Home Assistant
# (requested 28 Oct: "риги расставлены уже в Home Assistant, просто надо
# брать инфу оттуда"): room marks sit in input_select.rig_<N> (k/d/-),
# per-rig plugs are switches named 'RIG <N>', the fan master plug is the
# switch named 'Вентилятор', the room temperature watch is the sensor named
# 'Кочегарка ... Temperature'.
HA_FAN_MASTER_LABEL = "вентилятор"      # exact switch friendly_name (lowercase)
HA_ROOM_LABEL_PREFIX = "кочегарка"      # sensor friendly_name prefix (lowercase)
HA_ROOM_LABEL_SUBSTR = "temperature"    # ... and required substring
HA_FAN_ON_CHANNEL = ""      # optional channel entity to lift with the assist
HA_FAN_ON_MASTER = ""       # (set below in _ha_load_conf defaults)

_ha_state_lock = threading.RLock()

def _ha_load_conf():
    try:
        with config_lock:
            with open(HA_CONF_PATH, 'r') as f:
                cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}

def _ha_save_conf(cfg):
    with _ha_state_lock:
        cfg["updated_at"] = int(time.time())
        _atomic_write_file(HA_CONF_PATH, json.dumps(cfg, indent=2), mode=0o600)

def _ha_load_state():
    try:
        with config_lock:
            with open(HA_STATE_PATH, 'r') as f:
                st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}

def _ha_save_state(st):
    with _ha_state_lock:
        _atomic_write_file(HA_STATE_PATH, json.dumps(st, indent=2), mode=0o600)

def _ha_conf_valid(cfg):
    return bool(cfg.get("url") and cfg.get("token"))

def _ha_base_url(url):
    url = str(url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url.rstrip("/")

def _ha_api(conf, method, path, body=None, timeout=None):
    """One call to the HA REST API. Returns (ok, data_or_text, http_code, err)."""
    base = _ha_base_url(conf.get("url"))
    token = str(conf.get("token") or "")
    if not base or not token:
        return False, None, 0, "Home Assistant is not configured"
    url = base + path
    data = None
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json",
               "User-Agent": "hiveos-local"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout or HA_CALL_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, json.loads(raw), resp.status, ""
            except Exception:
                return True, raw, resp.status, ""
    except urllib.error.HTTPError as e:
        try:
            msg = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            msg = ""
        return False, None, e.code, "HA error: HTTP %d %s" % (e.code, msg)
    except Exception as e:
        return False, None, 0, "HA unreachable: %s" % (str(e) or type(e).__name__)

def _ha_check(conf):
    """Auth probe: GET /api must answer 200 with a message."""
    ok, data, code, err = _ha_api(conf, "GET", "/api/", timeout=HA_CHECK_TIMEOUT)
    if ok:
        return True, "Connected (%s)" % (data.get("message") if isinstance(data, dict) else "ok")
    if code == 401:
        return False, "Unauthorized - check the long-lived token"
    return False, err

def _ha_probe_state(conf):
    """Connection probe from a worker context; returns (ok, err)."""
    ok, _msg = _ha_check(conf)
    return ok, (_msg if not ok else "")

def _ha_state_entity(conf, entity_id):
    ok, data, code, err = _ha_api(conf, "GET", "/api/states/" + str(entity_id or ""), timeout=HA_CALL_TIMEOUT)
    if not ok:
        return None, err
    if not isinstance(data, dict) or not data.get("entity_id"):
        return None, "Entity not found: %s" % entity_id
    return data, ""

def _ha_service(conf, service, entity_id):
    """switch.turn_on / fan.turn_on / switch.turn_off ... Returns (ok, err)."""
    domain, _, _name = str(entity_id or "").partition(".")
    ok, _data, code, err = _ha_api(conf, "POST", "/api/services/%s/%s" % (domain, service),
                                   body={"entity_id": entity_id}, timeout=HA_CALL_TIMEOUT)
    if ok:
        return True, ""
    return False, err

def _ha_state_ts(entity):
    """Parse the HA state object's last_changed into epoch seconds (0 on failure)."""
    try:
        ts = datetime.fromisoformat(str(entity.get("last_changed") or "").replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    except Exception:
        return 0.0

def _ha_entity_value(state):
    """Float value of a sensor state; None when unavailable/unknown/non-numeric."""
    if not isinstance(state, dict):
        return None
    val = state.get("state")
    try:
        return float(val)
    except (TypeError, ValueError):
        attrs = state.get("attributes") or {}
        try:
            return float(attrs.get("temperature"))
        except (TypeError, ValueError):
            return None

def _ha_sensor_temp(conf, entity_id):
    """Temperature from a sensor entity; (None, 'unavailable') on dead sensor."""
    if not entity_id:
        return None, "not configured"
    state, err = _ha_state_entity(conf, entity_id)
    if state is None:
        return None, err or "unreachable"
    if state.get("state") in ("unavailable", "unknown", "None"):
        return None, "unavailable"
    return _ha_entity_value(state), ""

def _ha_pull_layout(conf, state):
    """Pull the farm layout from Home Assistant (28 Oct redesign: the rigs are
    arranged over THERE, the panel never marks them by hand):
      rooms:  input_select.rig_<N> with state k/d/-  -> rig.location boiler/house/none
      plugs:  switch named 'RIG <N>'                 -> rig.ha_plug
      fan:    switch named 'Вентилятор'              -> fan master plug (power the WHOLE fan)
      room:   sensor named 'Кочегарка ... Temperature' -> room temp watch:
              alive  -> the user's HA automations manage the fan themselves
              dark   -> the panel holds the cards in the 57-62°C band
    Changed rig fields are written through to the persisted cluster state.
    Returns {"ok", "rigs": {rid: {...}}, "master_plug", "room_sensor",
             "room_value", "room_state", "err"}."""
    try:
        ok_st, data, code, err = _ha_api(conf, "GET", "/api/states", timeout=HA_CALL_TIMEOUT)
        if not ok_st or not isinstance(data, list):
            return {"ok": False, "err": err or code or "HA /api/states failed"}
        byid = {}
        for e in data:
            if isinstance(e, dict) and e.get("entity_id"):
                byid[str(e["entity_id"])] = e
    except Exception as e:
        return {"ok": False, "err": str(e)}

    def fname(e):
        return str((e.get("attributes") or {}).get("friendly_name") or "").strip().lower()

    switch_ids = sorted(e for e in byid if e.startswith("switch."))
    master = ""
    for eid in switch_ids:
        if fname(byid[eid]) == HA_FAN_MASTER_LABEL:
            master = eid
            break
    room_sensor = ""
    room_value = None
    for eid in sorted(e for e in byid if e.startswith("sensor.")):
        fn = fname(byid[eid])
        if fn.startswith(HA_ROOM_LABEL_PREFIX) and HA_ROOM_LABEL_SUBSTR in fn:
            room_sensor = eid
            room_value = _ha_entity_value(byid[eid])
            break
    room_state = str(byid[room_sensor].get("state") or "") if room_sensor else ""

    rigmap = {}
    changed = False
    for rig in state.get("rigs", []):
        m = re.search(r'(\d+)\s*$', str(rig.get("name") or ""))
        if not m:
            continue
        n = m.group(1)
        loc = ""
        sel = byid.get("input_select.rig_%s" % n)
        if sel is not None:
            loc = {"k": "boiler", "d": "house", "-": "none"}.get(
                str(sel.get("state") or "").strip().lower(), "")
        plug = ""
        for eid in switch_ids:
            if fname(byid[eid]) == "rig %s" % n:
                plug = eid
                break
        if not loc and not plug:
            continue
        rid = rig.get("id")
        rigmap[rid] = {"location": loc, "ha_plug": plug}
        if loc and rig.get("location", "") != loc:
            rig["location"] = loc
            rig["updated_at"] = int(time.time())
            changed = True
        if plug and rig.get("ha_plug", "") != plug:
            rig["ha_plug"] = plug
            rig["updated_at"] = int(time.time())
            changed = True
    if changed:
        save_cluster_state(state)
    return {"ok": True, "rigs": rigmap, "master_plug": master,
            "room_sensor": room_sensor, "room_value": room_value,
            "room_state": room_state, "err": ""}

def _ha_reachable(rig):
    """True when ANY configured SSH route opens a TCP connection.
    This is the 'is the machine up at all' signal of the plug powercycle.
    (_probe_tcp lives further down the module - resolved at runtime.)"""
    try:
        for access in _ordered_rig_accesses(rig):
            host = access.get("host")
            port = int(access.get("port") or 22)
            if not host:
                continue
            if _probe_tcp(host, port, timeout=HA_POWER_TCP_TIMEOUT):
                return True
    except Exception:
        pass
    return False

def _ha_is_powercycle_rig(rig):
    """К (boiler) and Д (house) rigs may be power cycled; a rig in a lived-in
    house (location 'none'/empty) is NEVER touched - a night reboot there
    would wake people up. The per-rig HA switch (ha_enabled False, the
    вкл/выкл toggle in the HA menu) additionally suppresses the automation;
    manual powercycle does not check this (an explicit user action)."""
    if rig.get("ha_enabled", True) is False:
        return False
    return (rig.get("location") in ("boiler", "house")) and bool(rig.get("ha_plug"))

def _ha_cluster_rigs():
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    cache = load_cluster_cache()
    return state, cache

def _ha_rig_temps(state, cache, fresh_ttl):
    """GPU temps of all rigs marked 'boiler': returns (values[], any_stale, names).
    Temps come from the cluster stats cache, so they keep flowing even when
    the SSH routes to a rig are broken (only staleness is visible)."""
    values = []
    freshest_ok = True
    any_rig = False
    for rig in state.get("rigs", []):
        if rig.get("location") != "boiler":
            continue
        any_rig = True
        entry = cache.get("rigs", {}).get(rig.get("id"), {})
        ts = int(entry.get("fetched_at") or 0)
        if not ts or (time.time() - ts) > fresh_ttl:
            freshest_ok = False
            continue
        stats = entry.get("stats") or {}
        for g in stats.get("gpus") or []:
            try:
                t = float(g.get("temp", 0) or 0)
            except (TypeError, ValueError):
                continue
            if t > 0:
                values.append(t)
    return values, freshest_ok, any_rig

def _ha_powercycle_tick():
    conf = _ha_load_conf()
    if not _ha_conf_valid(conf):
        return
    state, cache = _ha_cluster_rigs()
    _ha_pull_layout(conf, state)   # rooms + plugs are pulled from Home Assistant
    self_id = state.get("self_id")
    st = _ha_load_state()
    power = st.setdefault("power", {})
    now = time.time()
    for rig in state.get("rigs", []):
        rid = rig.get("id")
        if not rid or rid == self_id:
            continue  # self: this panel IS running, its own plug is not ours to cycle
        rec = power.setdefault(rid, {})
        if not _ha_is_powercycle_rig(rig):
            rec.pop("down_since", None)
            continue
        name = rig.get("name") or rid
        if _ha_reachable(rig):
            if rec.get("down_since") or rec.get("warned"):
                rec["down_since"] = None
                rec.pop("warned", None)
                rec.pop("last_result", None)
                _ha_save_state(st)
                record_metrics_event("success",
                    "HA powercycle: %s is reachable again - plug cycle cancelled" % name)
            continue
        if not rec.get("down_since"):
            rec["down_since"] = int(now)
            _ha_save_state(st)
        if now - int(rec.get("down_since") or 0) < HA_POWER_DOWN:
            continue
        if now - float(rec.get("last_cycle") or 0) < HA_POWER_GAP:
            rec["last_result"] = "waiting rate limit"
            _ha_save_state(st)
            continue
        down_min = int((now - int(rec.get("down_since") or 0)) / 60)
        # Final gate right before acting: read the plug from HA and confirm it
        # is ON. off/unavailable = someone else is already cycling it or the
        # entity is dead; either way touching it would make things worse.
        plug_state, _age, err = _ha_fan_power_state_for_entity(conf, rig.get("ha_plug"))
        st["last_probe"] = {"ts": int(now), "ok": bool(plug_state), "err": err or ""}
        if plug_state != "on":
            rec["last_result"] = "plug not on (%s%s) - skipped" % (plug_state or "?",
                                 (": " + err) if err else "")
            rec["down_since"] = None  # restart the window: next tick re-awaits 5 min
            if not rec.get("warned"):
                rec["warned"] = True
                record_metrics_event("warning",
                    "HA powercycle skipped for %s: smart plug is not ON%s" % (name, (" (%s)" % err) if err else ""))
            _ha_save_state(st)
            continue
        # Reachability re-check immediately before the command (rig may have
        # recovered during the 5 min window / the HA round trip)
        if _ha_reachable(rig):
            rec["down_since"] = None
            rec["last_result"] = "recovered before cycle"
            _ha_save_state(st)
            logging.info(f"HA powercycle: rig '{name}' recovered before the plug cycle, aborted")
            continue
        record_metrics_event("danger",
            f"Rig {name} unreachable for {down_min} min - power cycle via Home Assistant plug ({rig.get('ha_plug')})")
        rec["last_cycle"] = int(now)
        rec["last_result"] = "cycling..."
        _ha_save_state(st)
        ok_off, err_off = _ha_service(conf, "turn_off", rig.get("ha_plug"))
        if not ok_off:
            rec["last_result"] = "failed to turn plug off (%s)" % err_off
            record_metrics_event("danger", f"HA powercycle {name}: turn OFF failed ({err_off})")
            _ha_save_state(st)
            continue
        time.sleep(HA_POWER_DWELL)
        # Verify the plug really went off, then power back up
        plug_state, _age, _e2 = _ha_fan_power_state_for_entity(conf, rig.get("ha_plug"))
        if plug_state == "on":
            ok_off, err_off = _ha_service(conf, "turn_off", rig.get("ha_plug"))
            if ok_off:
                time.sleep(5)
        ok_on, err_on = _ha_service(conf, "turn_on", rig.get("ha_plug"))
        if ok_on:
            rec["last_result"] = "power cycled (off %ds -> on)" % HA_POWER_DWELL
            rec["down_since"] = None
            rec.pop("warned", None)
            record_metrics_event("success",
                f"HA powercycle {name}: plug off {HA_POWER_DWELL}s -> on. Rig should boot in 1-3 min.")
        else:
            rec["last_result"] = "plug off ok, turn ON failed (%s)" % err_on
            record_metrics_event("danger",
                f"HA powercycle {name}: plug was turned OFF but turn ON failed ({err_on}) - CHECK THE PLUG IN HA")
        _ha_save_state(st)

def _ha_fan_power_state_for_entity(conf, entity_id):
    if not conf.get("fan_power_entity") and not entity_id:
        return None, 0, "not configured"
    ent = entity_id or conf.get("fan_power_entity")
    st, err = _ha_state_entity(conf, ent)
    if st is None:
        return None, 0, err
    ts = _ha_state_ts(st)
    age = max(0.0, time.time() - ts) if ts else 0
    return str(st.get("state") or "unknown"), age, ""

def _ha_fan_tick():
    conf = _ha_load_conf()
    if not _ha_conf_valid(conf):
        return
    if not conf.get("enabled", True):
        return
    now = time.time()
    st = _ha_load_state()
    fan = st.setdefault("fan", {})
    # One connection probe per tick; HA unreadable -> hands off completely
    ok, err = _ha_probe_state(conf)
    fan["last_check"] = int(now)
    fan["last_err"] = "" if ok else err
    st["last_probe"] = {"ts": int(now), "ok": bool(ok), "err": "" if ok else err}
    if not ok:
        _ha_save_state(st)
        return
    state, cache = _ha_cluster_rigs()
    layout = _ha_pull_layout(conf, state)
    if not layout.get("ok"):
        fan["last_reason"] = layout.get("err") or "HA entities unreadable"
        _ha_save_state(st)
        return
    master = layout.get("master_plug") or ""
    fan["master_plug"] = master
    fan["room_sensor"] = layout.get("room_sensor") or ""
    if not master:
        fan["last_reason"] = "fan plug not found in HA (switch 'Вентилятор')"
        _ha_save_state(st)
        return
    plug_state, age, ent_err = _ha_fan_power_state_for_entity(conf, master)
    if plug_state not in ("on", "off"):
        fan["last_reason"] = "plug state unknown%s" % (": " + ent_err if ent_err else "")
        _ha_save_state(st)
        return
    temp_on, fresh_on, _n1 = _ha_rig_temps(state, cache, HA_FAN_ON_FRESH)
    temp_off, fresh_off, n_boiler = _ha_rig_temps(state, cache, HA_FAN_OFF_FRESH)
    if n_boiler == 0:
        fan["last_reason"] = "no rigs marked boiler room (К) in Home Assistant"
        _ha_save_state(st)
        return
    any_hot = bool(temp_on) and max(temp_on) > HA_FAN_ON_TEMP
    avg = (sum(temp_off) / len(temp_off)) if temp_off else None

    room_val = layout.get("room_value")
    room_state = layout.get("room_state") or ""
    fan.update({"avg_temp": round(avg, 1) if avg is not None else None,
                "max_temp": round(max(temp_on), 1) if temp_on else None,
                "room_temp": round(room_val, 1) if room_val is not None else None,
                "room_err": "" if room_val is not None else (room_state or "dark")})
    # Decision (28 Oct redesign): while the room temperature sensor shows a
    # value the user's HA automations own the fan completely - the panel must
    # not fight them. Only a DARK room sensor hands the fan over to the panel,
    # which then holds the cards in the 57-62°C band: ON above 62, OFF below
    # 57, never running longer than the cap. Outdoor temperature is ignored
    # by design ("не важно какая температура на улице").
    if room_val is not None:
        fan["last_reason"] = ("room sensor alive (%.1f°C) - HA automations manage the fan"
                              % room_val)
        _ha_save_state(st)
        return
    # Rate limiting: count automatic ON starts per calendar day
    day = time.strftime("%Y%m%d", time.localtime(now))
    if fan.get("day") != day:
        fan["day"] = day
        fan["cycles"] = 0

    reason = ""
    action = None
    if plug_state == "on":
        if age >= HA_FAN_MAX_ON:
            action = "off"
            reason = "run cap %d min reached (room sensor dark, panel holds the band)" % int(HA_FAN_MAX_ON / 60)
        elif temp_off and fresh_off and max(temp_off) < HA_FAN_OFF_TEMP:
            action = "off"
            reason = "all К cards < %.0f°C - cooled down" % HA_FAN_OFF_TEMP
    else:
        if not any_hot:
            reason = "asleep: cards ≤ %.0f°C" % HA_FAN_ON_TEMP
        elif not fresh_on:
            reason = "waiting: no fresh temperature data from К-rigs"
        elif age < HA_FAN_MIN_DWELL:
            reason = "waiting anti-flapping dwell (%ds/%ds)" % (int(age), HA_FAN_MIN_DWELL)
        elif int(fan.get("cycles") or 0) >= HA_FAN_MAX_CYCLES:
            reason = "daily cycle cap reached"
        else:
            action = "on"
            reason = ("room sensor dark + hot cards: max %.1f°C > %.0f°C - cooling to the band" %
                      (max(temp_on), HA_FAN_ON_TEMP))

    if action:
        ok_act, act_err = _ha_fan_apply(conf, action == "on", master)
        fan["last_action"] = action
        fan["last_ts"] = int(now)
        fan["last_reason"] = reason
        if action == "on":
            fan["cycles"] = int(fan.get("cycles") or 0) + 1
        if ok_act:
            record_metrics_event("info" if action == "on" else "success",
                                 "Boiler room fan assist: turned %s (%s)" % (action.upper(), reason))
        else:
            record_metrics_event("danger",
                                 "Boiler room fan assist: failed to turn %s (%s)" % (action.upper(), act_err))
    else:
        fan["last_reason"] = reason
    _ha_save_state(st)

def _ha_fan_apply(conf, turn_on, master=None):
    """Master plug (+ legacy stored channel). Returns (ok, err)."""
    master = master or conf.get("fan_power_entity")
    channel = conf.get("fan_on_channel") or ""
    errs = []
    if turn_on:
        ok, err = _ha_service(conf, "turn_on", master)
        if not ok:
            return False, err
        if channel:
            ok2, err2 = _ha_service(conf, "turn_on", channel)
            if not ok2:
                errs.append("channel: " + err2)
    else:
        ok, err = _ha_service(conf, "turn_off", master)
        if not ok:
            return False, err
        if channel:
            ok2, err2 = _ha_service(conf, "turn_off", channel)
            if not ok2:
                errs.append("channel: " + err2)
    if errs:
        return False, "; ".join(errs)
    return True, ""

def _ha_worker_loop(interval, tick, name):
    time.sleep(HA_BOOT_DELAY)
    while True:
        try:
            tick()
        except Exception as e:
            logging.error(f"HA {name} worker error: {e}")
        time.sleep(interval)

def start_ha_workers():
    threading.Thread(target=_ha_worker_loop, args=(HA_POWER_INTERVAL, _ha_powercycle_tick, "powercycle"),
                     daemon=True, name="ha-powercycle").start()
    threading.Thread(target=_ha_worker_loop, args=(HA_FAN_INTERVAL, _ha_fan_tick, "fan"),
                     daemon=True, name="ha-fan").start()
    logging.info("Home Assistant workers started (powercycle + fan assist, inert until configured)")

# ---------------- HA API endpoints ----------------

def _ha_public():
    cfg = _ha_load_conf()
    st = _ha_load_state()
    token = str(cfg.get("token") or "")
    now = time.time()
    # Status comes from the stored last probe (workers refresh it every tick)
    # - GET must never block on an unreachable HA for 8s
    probe = st.get("last_probe") or {}
    fan = st.get("fan", {})
    probe_fresh = probe and (now - float(probe.get("ts") or 0)) < 900
    fan_fresh = fan.get("last_check") and (now - int(fan.get("last_check"))) < 900
    if not _ha_conf_valid(cfg):
        connected, err = False, "not configured"
    elif probe_fresh:
        connected, err = bool(probe.get("ok")), str(probe.get("err") or "")
    elif fan_fresh:
        connected, err = not fan.get("last_err"), str(fan.get("last_err") or "")
    else:
        connected, err = False, "no fresh status yet - workers idle or HA unreachable"
    return {
        "configured": _ha_conf_valid(cfg),
        "enabled": bool(cfg.get("enabled", True)),
        "url": _ha_base_url(cfg.get("url")),
        "token_masked": ("********" if token else ""),
        "fan_power_entity": cfg.get("fan_power_entity", ""),
        "fan_on_channel": cfg.get("fan_on_channel", ""),
        "boiler_sensor_entity": cfg.get("boiler_sensor_entity", ""),
        "outdoor_sensor_entity": cfg.get("outdoor_sensor_entity", ""),
        "fan_channel_entities": cfg.get("fan_channel_entities", []),
        "connected": connected,
        "connect_err": "" if connected else err,
        "checked_at": int(probe.get("ts") or fan.get("last_check") or 0),
        "power": st.get("power", {}),
        "fan_worker": fan,
    }

@app.route('/api/ha', methods=['GET'])
def api_ha():
    return jsonify({"success": True, "ha": _ha_public_full()})


def _ha_public_full():
    pub = _ha_public()
    state, cache = _ha_cluster_rigs()
    rigs = []
    for r in state.get("rigs", []):
        rigs.append({
            "id": r.get("id"), "name": r.get("name", r.get("id")),
            "is_self": r.get("id") == state.get("self_id"),
            "location": r.get("location", ""),
            "ha_plug": r.get("ha_plug", ""),
            "ha_enabled": bool(r.get("ha_enabled", True)),
            "online": bool(cache.get("rigs", {}).get(r.get("id"), {}).get("online")) if not r["id"] == state["self_id"] else True,
        })
    pub["rigs"] = rigs
    return pub


@app.route('/api/ha/refresh', methods=['POST'])
def api_ha_refresh():
    """On-demand layout re-pull (the settings modal calls it when opened):
    probe HA, pull rooms/plugs/fan entities, return the fresh public view."""
    conf = _ha_load_conf()
    if not _ha_conf_valid(conf):
        return jsonify({"success": False, "message": "Home Assistant is not configured",
                        "ha": _ha_public_full()}), 200
    ok, err = _ha_probe_state(conf)
    st = _ha_load_state()
    st["last_probe"] = {"ts": int(time.time()), "ok": bool(ok), "err": "" if ok else err}
    fan = st.setdefault("fan", {})
    fan["last_check"] = int(time.time())
    if ok:
        state, _cache = _ha_cluster_rigs()
        layout = _ha_pull_layout(conf, state)
        if layout.get("ok"):
            fan["master_plug"] = layout.get("master_plug") or ""
            fan["room_sensor"] = layout.get("room_sensor") or ""
            room_val = layout.get("room_value")
            fan["room_temp"] = round(room_val, 1) if room_val is not None else None
            fan["room_err"] = "" if room_val is not None else (layout.get("room_state") or "dark")
        else:
            fan["last_reason"] = layout.get("err") or "HA entities unreadable"
    _ha_save_state(st)
    return jsonify({"success": True, "ha": _ha_public_full()})

@app.route('/api/ha/test', methods=['POST'])
def api_ha_test():
    data = request.get_json(silent=True) or {}
    cfg = _ha_load_conf()
    url = data.get("url")
    token = data.get("token")
    if url:
        cfg["url"] = url
    if token and token != "********":
        cfg["token"] = token
    ok, msg = _ha_check(cfg)
    return jsonify({"success": ok, "message": msg})

@app.route('/api/ha/config', methods=['POST'])
def api_ha_config():
    data = request.get_json(silent=True) or {}
    cfg = _ha_load_conf()
    # Token mask keeps the saved secret (same convention as SSH passwords)
    if data.get("token") and data.get("token") != "********":
        cfg["token"] = str(data["token"]).strip()
    if "url" in data:
        u = _ha_base_url(data.get("url"))
        if u and not u.startswith(("http://", "https://")):
            return jsonify({"success": False, "message": "Bad URL"}), 400
        cfg["url"] = u
    for key, label in (("fan_power_entity", "fan master plug"), ("fan_on_channel", "fan channel"),
                       ("boiler_sensor_entity", "boiler room sensor"), ("outdoor_sensor_entity", "outdoor sensor")):
        if key in data:
            v = str(data.get(key) or "").strip()
            if v and not HA_ENTITY_ID_RE.match(v):
                return jsonify({"success": False, "message": "Bad entity id: %s" % v}), 400
            cfg[key] = v
    if "enabled" in data:
        cfg["enabled"] = bool(data.get("enabled"))
    ent_list = data.get("fan_channel_entities")
    if isinstance(ent_list, list):
        clean = []
        for e in ent_list[:8]:
            e = str(e or "").strip()
            if e and not HA_ENTITY_ID_RE.match(e):
                return jsonify({"success": False, "message": "Bad entity id: %s" % e}), 400
            if e:
                clean.append(e)
        cfg["fan_channel_entities"] = clean
    if not _ha_conf_valid(cfg):
        return jsonify({"success": False, "message": "URL and long-lived token are required"}), 400
    _ha_save_conf(cfg)
    ok, msg = _ha_check(cfg)
    st = _ha_load_state()
    st["last_probe"] = {"ts": int(time.time()), "ok": bool(ok), "err": "" if ok else msg}
    _ha_save_state(st)
    record_metrics_event("info", "Home Assistant connection saved" + (" and verified" if ok else " (probe failed: %s)" % msg))
    pub = _ha_public()
    return jsonify({"success": True, "message": "Saved. " + msg, "ha": pub})

@app.route('/api/ha/entities', methods=['GET'])
def api_ha_entities():
    cfg = _ha_load_conf()
    if not _ha_conf_valid(cfg):
        return jsonify({"success": False, "message": "Home Assistant is not configured"}), 400
    ok, data, code, err = _ha_api(cfg, "GET", "/api/states", timeout=HA_CALL_TIMEOUT)
    if not ok or not isinstance(data, list):
        return jsonify({"success": False, "message": "Cannot read entities (%s)" % (err or code)}), 200
    out = []
    for e in data:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain not in HA_DOMAINS:
            continue
        out.append({
            "entity_id": eid,
            "state": str(e.get("state") or ""),
            "name": str((e.get("attributes") or {}).get("friendly_name") or eid),
        })
        if len(out) >= 400:
            break
    return jsonify({"success": True, "entities": out})

@app.route('/api/ha/location', methods=['POST'])
def api_ha_location():
    data = request.get_json(silent=True) or {}
    rig_id = str(data.get("rig_id") or "")
    if not rig_id:
        return jsonify({"success": False, "message": "rig_id required"}), 400
    loc = str(data.get("location") or "")
    if loc not in ("boiler", "house", "none", ""):
        return jsonify({"success": False, "message": "Bad location"}), 400
    plug = str(data.get("ha_plug") or "").strip()
    if plug and not HA_ENTITY_ID_RE.match(plug):
        return jsonify({"success": False, "message": "Bad plug entity id"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig = _find_rig(state, rig_id)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found"}), 404
    rig["location"] = loc
    if plug or "ha_plug" in data:
        rig["ha_plug"] = plug
    if "enabled" in data:
        rig["ha_enabled"] = bool(data.get("enabled"))
    rig["updated_at"] = int(time.time())
    save_cluster_state(state)
    label = {"boiler": "кочегарка (К)", "house": "дом (Д)", "none": "жилой дом (—)"}.get(loc, "—")
    if "enabled" in data and not loc:
        label = "HA powercycle %s" % ("ON" if rig.get("ha_enabled", True) else "OFF")
    record_metrics_event("info", "HA: %s -> %s" % (rig.get("name") or rig_id, label))
    return jsonify({"success": True, "message": "Saved (%s)" % label})


@app.route('/api/ha/enabled', methods=['POST'])
def api_ha_enabled():
    """Per-rig auto-powercycle switch only. Rooms and plugs are pulled from
    Home Assistant - the panel never edits them by hand (28 Oct redesign)."""
    data = request.get_json(silent=True) or {}
    rig_id = str(data.get("rig_id") or "")
    if not rig_id:
        return jsonify({"success": False, "message": "rig_id required"}), 400
    state = load_cluster_state()
    _CURRENT_SELF_ID["value"] = state["self_id"]
    rig = _find_rig(state, rig_id)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found"}), 404
    rig["ha_enabled"] = bool(data.get("enabled"))
    rig["updated_at"] = int(time.time())
    save_cluster_state(state)
    record_metrics_event("info", "HA powercycle %s for %s" %
                         ("ON" if rig["ha_enabled"] else "OFF", rig.get("name") or rig_id))
    return jsonify({"success": True, "message": "Auto powercycle %s" %
                    ("ON" if rig["ha_enabled"] else "OFF")})

@app.route('/api/ha/share', methods=['POST'])
def api_ha_share():
    cfg = _ha_load_conf()
    if not _ha_conf_valid(cfg):
        return jsonify({"success": False, "message": "Home Assistant is not configured here"}), 400
    state, _cache = _ha_cluster_rigs()
    body = dict(cfg)  # full config incl. token -  wires travel inside the cluster SSH channel
    results = []
    for rig in state.get("rigs", []):
        if rig.get("id") == state["self_id"]:
            continue
        ok, data, code, err, access_name = cluster_remote_api(rig, "POST", "api/ha/config", body=body, timeout=60)
        results.append({"rig": rig.get("name") or rig.get("id"),
                        "ok": bool(ok and isinstance(data, dict) and data.get("success")),
                        "message": (data.get("message") if isinstance(data, dict) and data.get("message") else err)})
    record_metrics_event("info", "HA connection shared with %d/%d rigs" %
                         (sum(1 for r in results if r.get("ok")), len(results)))
    return jsonify({"success": True, "share": results})

@app.route('/api/ha/powercycle', methods=['POST'])
def api_ha_powercycle():
    """Manual plug cycle for one rig (skips the 5-min wait and the rate limit,
    but never the plug-state and reachability gates)."""
    data = request.get_json(silent=True) or {}
    rig_id = str(data.get("rig_id") or "")
    conf = _ha_load_conf()
    if not _ha_conf_valid(conf):
        return jsonify({"success": False, "message": "Home Assistant is not configured"}), 400
    state, _cache = _ha_cluster_rigs()
    rig = _find_rig(state, rig_id)
    if rig is None:
        return jsonify({"success": False, "message": "Rig not found"}), 404
    if rig.get("id") == state["self_id"]:
        return jsonify({"success": False, "message": "Cannot power cycle this own rig"}), 400
    if rig.get("location") not in ("boiler", "house"):
        return jsonify({"success": False, "message": "Rig is marked as a lived-in place (—) - powercycle disabled"}), 403
    plug = rig.get("ha_plug")
    if not plug:
        return jsonify({"success": False, "message": "No smart plug is mapped for this rig"}), 400
    plug_state, _age, err = _ha_fan_power_state_for_entity(conf, plug)
    if plug_state != "on":
        return jsonify({"success": False,
                        "message": "Plug is %s (%s) - nothing to cycle" % (plug_state or "?", err)}), 409
    name = rig.get("name") or rig_id
    record_metrics_event("danger", f"HA powercycle (manual) for {name}: plug ({plug}) OFF {HA_POWER_DWELL}s -> ON")
    ok_off, err_off = _ha_service(conf, "turn_off", plug)
    if not ok_off:
        return jsonify({"success": False, "message": "turn OFF failed: " + err_off}), 200
    time.sleep(HA_POWER_DWELL)
    ok_on, err_on = _ha_service(conf, "turn_on", plug)
    if ok_on:
        record_metrics_event("success", f"HA powercycle (manual) {name}: done")
        return jsonify({"success": True, "message": "Plug off %ds -> on. Rig boots in 1-3 min." % HA_POWER_DWELL})
    record_metrics_event("danger", f"HA powercycle (manual) {name}: turn ON failed ({err_on})")
    return jsonify({"success": False, "message": "Plug is OFF but turn ON FAILED: %s - check it in HA!" % err_on})

@app.route('/api/ha/fan', methods=['POST'])
def api_ha_fan():
    """Manual fan on/off - an explicit user override of the worker. The master
    plug is pulled from Home Assistant (the switch named 'Вентилятор')."""
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in ("on", "off"):
        return jsonify({"success": False, "message": "action must be on|off"}), 400
    conf = _ha_load_conf()
    if not _ha_conf_valid(conf):
        return jsonify({"success": False, "message": "Home Assistant is not configured"}), 400
    state, _cache = _ha_cluster_rigs()
    layout = _ha_pull_layout(conf, state)
    master = layout.get("master_plug") or conf.get("fan_power_entity") or ""
    if not master:
        return jsonify({"success": False,
                        "message": "Fan plug not found in Home Assistant (switch 'Вентилятор')"}), 400
    ok, err = _ha_fan_apply(conf, action == "on", master)
    record_metrics_event("info" if ok else "danger",
                         "Boiler room fan manual: %s%s" % (action.upper(), (" failed: " + err) if not ok else ""))
    return jsonify({"success": ok, "message": ("Fan turned %s" % action.upper()) if ok else err})

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
        "location": rig.get("location", ""),
        "ha_plug": rig.get("ha_plug", ""),
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
    # Per-route connectivity summary for the Cluster tab dots (lan/netbird/jump)
    routes = []
    for a in _ordered_rig_accesses(rig):
        prio, cls = _access_route_class(a)
        h = _get_route_health(_route_health_key(rig.get("id"), a))
        routes.append({
            "cls": cls,
            "name": a.get("name", ""),
            "host": a.get("host", ""),
            "state": h.get("state", "unknown"),
            "probe": h.get("probe", "unknown"),
            "latency_ms": h.get("latency_ms", 0),
            "probe_ms": h.get("probe_ms", 0),
            "err": h.get("err", "")
        })
    view["routes"] = routes
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
                # UI resubmits unchanged secrets as the '********' mask — never store it
                if password == "********":
                    if not existing.get("password"):
                        return jsonify({"success": False, "message": "Dashboard password of the remote rig is required."}), 400
                else:
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

def _probe_tcp(host, port, timeout=4):
    """Quick TCP reachability probe (no SSH). Returns True if connect succeeded."""
    try:
        with socket.create_connection((str(host), int(port or 22)), timeout=timeout):
            return True
    except Exception:
        return False

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
    for access in _ordered_rig_accesses(rig):
        key = _route_health_key(rig.get("id"), access)
        # jump routes: probe the jump first - unreachable means 'client-side route'
        if str(access.get("type", "direct")) == "jump":
            resolved = resolve_jump_host(dict(access))
            if not _probe_tcp(resolved.get("jump_host"), resolved.get("jump_port", 22)):
                _mark_route_down(key, "jump host unreachable from this rig")
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
        if ok and "__OK__" in out:
            _mark_route_up(key)
        else:
            _mark_route_down(key, ssh_err or "SSH test failed")
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
    # Flask's <path:subpath> does not include the query string - forward it
    # explicitly so GET filters (?name=&ver=, ?date=&days=) reach the peer API
    qs = request.query_string.decode("utf-8", "replace")
    if qs:
        subpath = "%s?%s" % (subpath, qs)
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

    # Start the route health prober (Cluster tab per-route dots + failback data)
    start_route_prober()

    # Start the metrics history sampler (worker Statistics tab)
    start_metrics_sampler()

    # Start the local config guard worker (Local mode enforcement: boot + every 10 min)
    start_guard_worker()

    # Start the self-heal worker (miner package repair, API host fix, stale screen)
    start_heal_worker()

    # Start the OC recovery worker (Xid death-loop self-healing)
    start_oc_recovery_worker()

    # Start the network self-heal worker (wifi/link recovery, reboot last resort)
    start_net_heal_worker()

    # Install/update the persistent flight recorder (survives reboots, one folder)
    start_blackbox_worker()

    # Start the Home Assistant bridge workers (smart plug powercycle + fan
    # assist). Both are inert until /api/ha/config is filled in.
    start_ha_workers()

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
    print(f" -> ACCESS PASSWORD: {'*' * len(str(app.config['ACCESS_PASSWORD']))} (see /hive-config/dashboard.key)")
    print(f" -> DASHBOARD ADDRESS: http://{local_ip}:{port}")
    print("="*60 + "\n")
    
    if USE_WAITRESS:
        logging.info(f"Starting Waitress production WSGI server on http://{local_ip}:{port}")
        serve(app, host='0.0.0.0', port=port, threads=8)
    else:
        logging.warning("Waitress package not found. Falling back to Flask built-in development server.")
        app.run(host='0.0.0.0', port=port, debug=False)
