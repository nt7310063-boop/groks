"""Per-profile persistent VNC container with CDP-exposed Chromium.

When admin clicks Auto-login on a profile, we spawn a `grokflow/chrome-vnc`
container (custom image) named after the profile. It runs:
  - Xvfb (virtual display)
  - x11vnc + websockify + noVNC (admin views via iframe)
  - Chromium with --remote-debugging-port=9223 (worker attaches via CDP)

The container STAYS RUNNING after admin closes the modal, because:
  - The Chromium process holds the cf_clearance + Grok session that bypassed
    Cloudflare's bot challenge during real user login.
  - The worker connects to that running Chromium via CDP to drive jobs.
  - Spawning a fresh headless Chromium per job re-triggers the challenge → 403.

Lifecycle:
  start_for_profile() → run if not exists, else just return URL (idempotent)
  stop_for_profile()  → admin "Stop browser" — destroys container, profile
                         goes to need_login (next job needs re-login)
"""

import os
import threading
import time
import traceback
from typing import Any

import docker
from docker.errors import APIError, NotFound


def _trace_destroy(where: str, name: str) -> None:
    """Tag every VNC container removal with the calling code path.
    When a VNC vanishes mysteriously this log is the only signal that
    tells us WHICH branch did it — without the tag, vnc-events just
    says 'destroy <name>' with no attribution to the Python caller."""
    caller = "".join(traceback.format_stack(limit=10)[:-1])
    print(f"[vnc] destroy via {where}: {name}\n{caller}", flush=True)

VNC_IMAGE = os.environ.get("VNC_IMAGE", "grokflow/chrome-vnc:latest")
NETWORK_NAME = os.environ.get("VNC_NETWORK", "grokflow_default")


def _client() -> docker.DockerClient:
    return docker.from_env()


def _container_name(profile_id: str) -> str:
    short = str(profile_id).replace("-", "")[:12]
    return f"grokflow-vnc-{short}"


# Per-profile spawn lock. React StrictMode (and double-clicks) fire
# start-vnc-session twice in a row; without a lock the second call can race
# into the create() and either 409-conflict or destroy the first call's
# brand-new container. The lock serialises spawns per profile so the second
# caller sees the container already created and reuses it.
_SPAWN_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(profile_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _SPAWN_LOCKS.get(profile_id)
        if lk is None:
            lk = threading.Lock()
            _SPAWN_LOCKS[profile_id] = lk
        return lk


def _container_to_host_path(profile_path: str) -> str:
    in_container = os.environ.get("PROFILE_BASE_PATH", "/app/browser_profiles")
    on_host = os.environ.get("PROFILE_BASE_PATH_HOST", in_container)
    if profile_path.startswith(in_container):
        return profile_path.replace(in_container, on_host, 1)
    return profile_path


def _fix_profile_perms(host_profile_path: str, puid: int = 1000, pgid: int = 1000) -> None:
    cli = _client()
    try:
        cli.containers.run(
            "alpine:latest",
            command=["sh", "-c", f"chown -R {puid}:{pgid} /target && chmod -R u+rwX /target"],
            volumes={host_profile_path: {"bind": "/target", "mode": "rw"}},
            remove=True,
            detach=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[vnc] perm fix failed: {exc}", flush=True)


def get_for_profile(profile_id: str) -> dict[str, Any] | None:
    cli = _client()
    name = _container_name(profile_id)
    try:
        c = cli.containers.get(name)
        c.reload()
        return {
            "container_name": name,
            "running": c.status == "running",
            "started_at": c.attrs.get("State", {}).get("StartedAt"),
            "cdp_endpoint": f"http://{name}:9223",
        }
    except NotFound:
        return None


def stop_for_profile(profile_id: str) -> None:
    cli = _client()
    name = _container_name(profile_id)
    try:
        c = cli.containers.get(name)
        _trace_destroy("stop_for_profile", name)
        c.stop(timeout=5)
        c.remove(force=True)
    except NotFound:
        pass
    except APIError as exc:
        print(f"[vnc] stop error for {name}: {exc}", flush=True)


def _reuse_info(c) -> dict[str, Any]:
    """Build the start_for_profile() return dict from an existing container."""
    return {
        "container_name": c.name,
        "container_id": c.id,
        "ws_path": "/vnc/",
        "cdp_endpoint": f"http://{c.name}:9223",
        "reused": True,
        "ready": c.status == "running",
    }


def start_for_profile(profile_id: str, profile_path: str, provider_url: str) -> dict[str, Any]:
    """Spawn (or reuse) a persistent VNC+CDP container for this profile.

    Per-profile lock so concurrent callers don't race each other's create().
    """
    with _lock_for(str(profile_id)):
        return _start_locked(profile_id, profile_path, provider_url)


def _start_locked(profile_id: str, profile_path: str, provider_url: str) -> dict[str, Any]:
    cli = _client()
    name = _container_name(profile_id)

    try:
        c = cli.containers.get(name)
        c.reload()
        # Anything that's running, just-created, restarting, or paused is
        # something the caller should reuse — don't tear it down.
        if c.status in ("running", "created", "restarting", "paused"):
            return _reuse_info(c)
        # "exited" / "dead": try RESTART before destroy+recreate. The /config
        # volume holds cookies + cf_clearance — restarting Chromium against
        # the same volume resumes the logged-in Grok session instead of
        # forcing the admin to Auto-login again. Only fall through to
        # remove+create if restart itself fails (image gone, network
        # removed, kernel oom-killed the container shell etc.).
        try:
            print(f"[vnc] {name} status={c.status} — restarting in place to preserve session", flush=True)
            c.restart(timeout=5)
            # Give Chromium a moment to come back up before reporting reuse.
            for _ in range(15):
                c.reload()
                if c.status == "running":
                    break
                time.sleep(1)
            if c.status == "running":
                return _reuse_info(c)
            print(f"[vnc] {name} restart yielded status={c.status} — falling back to recreate", flush=True)
        except APIError as exc:
            print(f"[vnc] {name} restart failed: {exc} — falling back to recreate", flush=True)
        try:
            _trace_destroy("_start_locked pre-cleanup", name)
            c.remove(force=True)
        except (APIError, NotFound) as exc:
            print(f"[vnc] pre-cleanup remove failed for {name}: {exc}", flush=True)
    except NotFound:
        pass
    except APIError as exc:
        print(f"[vnc] get() error for {name}, will try to create anyway: {exc}", flush=True)

    host_profile_path = _container_to_host_path(profile_path)
    _fix_profile_perms(host_profile_path)

    # Resource caps: each Chromium tab on grok.com costs ~600MB once the
    # heavy React app + media decoders are loaded. With 4 concurrent slots
    # we need ~2.4GB for tabs + ~800MB for the rest of Chromium → 4GB cap.
    # Override via env if you scale slots beyond 4 or up to multiple profiles.
    mem_limit = os.environ.get("VNC_MEM_LIMIT", "10g")
    cpu_quota = int(os.environ.get("VNC_CPU_QUOTA", "500000"))  # 2.0 CPU
    run_kwargs = dict(
        image=VNC_IMAGE,
        name=name,
        environment={
            "STARTUP_URL": provider_url,
            "TZ": "Asia/Ho_Chi_Minh",
            # Proxy strategy for the spawned VNC's Chromium:
            #
            # Default (no env override): the kasmweb base image runs
            # `warp-svc` via supervisord, baking GROK_HTTP_PROXY=
            # socks5://127.0.0.1:40000 into the container so Chromium
            # routes through Cloudflare WARP. In practice WARP exits are
            # 104.x.x.x/172.x.x.x IPs which Grok now 403-blocks +
            # bursty connections through warp-svc see
            # ERR_PROXY_CONNECTION_FAILED in Chromium.
            #
            # 2 escape hatches via backend env. DISABLE is checked first
            # because backend's own entrypoint.sh also auto-exports
            # GROK_HTTP_PROXY=socks5://… when its WARP daemon comes up
            # — so if we honored that variable first the disable flag
            # would never win in practice.
            #
            #   GROK_VNC_DISABLE_PROXY=1 — force Chromium direct (no
            #     proxy). Beats any GROK_HTTP_PROXY value also present
            #     in the parent env. Best when the VPS IP isn't yet
            #     Grok-flagged and WARP IPs are blocked.
            #
            #   GROK_HTTP_PROXY=<scheme>://… — pass an explicit proxy
            #     URL (residential / mobile). Only takes effect when
            #     DISABLE is NOT set.
            **(
                # 'direct://' is Chromium syntax for "no proxy".
                # /launch-chromium.sh tests `[[ -n "$GROK_HTTP_PROXY" ]]`
                # so passing "" would fall through to the WARP fallback;
                # 'direct://' is non-empty so the test passes and
                # Chromium interprets the flag as bypass.
                {"GROK_HTTP_PROXY": "direct://"}
                if os.environ.get("GROK_VNC_DISABLE_PROXY") == "1"
                else (
                    {"GROK_HTTP_PROXY": os.environ["GROK_HTTP_PROXY"]}
                    if os.environ.get("GROK_HTTP_PROXY")
                    else {}
                )
            ),
            # V8 old-space cap for the renderer. 2048 MB lets Grok's React
            # preview hold a 7-10 MB user upload without OOM-killing the
            # page mid-job. Old 512 MB cap was crashing every image-to-image
            # job with >5 MB inputs (TargetClosedError on Page.evaluate
            # right after setInputFiles). Override via
            # GROK_VNC_CHROMIUM_HEAP_MB on the backend for larger inputs
            # or memory-constrained hosts.
            "CHROMIUM_HEAP_MB": os.environ.get("GROK_VNC_CHROMIUM_HEAP_MB", "6144"),
        },
        volumes={
            host_profile_path: {"bind": "/config", "mode": "rw"},
        },
        network=NETWORK_NAME,
        shm_size="2g",
        mem_limit=mem_limit,
        memswap_limit=mem_limit,  # disallow swap → predictable behavior
        cpu_period=100000,
        cpu_quota=cpu_quota,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
        labels={"grokflow.profile_id": str(profile_id)},
        security_opt=["seccomp=unconfined"],
        # CAP_NET_ADMIN + /dev/net/tun are required by Cloudflare WARP's
        # warp-svc daemon which runs inside the container at supervisord
        # priority 50. WARP gives chromium a SOCKS5 proxy at
        # 127.0.0.1:40000 that routes outbound traffic through CF's own
        # network — Turnstile is far more lenient on traffic-from-itself
        # than on raw VPS-IP traffic. Without these caps the daemon
        # logs an error and warp-init exits clean; chromium falls back
        # to direct connection (back to the original CF challenge wall).
        cap_add=["NET_ADMIN"],
        devices=["/dev/net/tun:/dev/net/tun:rwm"],
    )

    # Diagnostic: log the env dict actually being handed to docker-py so we
    # can see whether the WARP-disable / custom-proxy override is winning
    # the merge. Kept lightweight — only logs the proxy-relevant keys.
    print(
        f"[vnc] spawn name={name} env_proxy={run_kwargs['environment'].get('GROK_HTTP_PROXY', '<unset>')} "
        f"(backend GROK_HTTP_PROXY={os.environ.get('GROK_HTTP_PROXY', '<unset>')}, "
        f"GROK_VNC_DISABLE_PROXY={os.environ.get('GROK_VNC_DISABLE_PROXY', '<unset>')})",
        flush=True,
    )

    # 409 on create means a container with that name already exists. With
    # the per-profile lock this should be rare — but pre-cleanup can race
    # with `removing` state. Strategy: re-fetch the container; if it's
    # alive/usable (running / created / restarting / paused) just reuse it
    # rather than tearing down a sibling caller's work. Only when it's
    # actually dead do we force-remove + retry.
    try:
        container = cli.containers.run(**run_kwargs)
    except APIError as exc:
        if exc.response is None or exc.response.status_code != 409:
            raise
        try:
            existing = cli.containers.get(name)
            existing.reload()
            if existing.status in ("running", "created", "restarting", "paused"):
                print(f"[vnc] 409 on '{name}' — reusing healthy existing container ({existing.status})", flush=True)
                return _reuse_info(existing)
            print(f"[vnc] 409 on '{name}' (state={existing.status}) — force-removing + retry", flush=True)
            _trace_destroy("_start_locked 409-handler", name)
            existing.remove(force=True)
        except NotFound:
            print(f"[vnc] 409 on '{name}' but get() says NotFound — retrying", flush=True)
        except APIError as inner:
            print(f"[vnc] could not inspect/remove '{name}': {inner}", flush=True)
        container = cli.containers.run(**run_kwargs)

    deadline = time.monotonic() + 60
    novnc_ready = cdp_ready = False
    while time.monotonic() < deadline:
        try:
            container.reload()
            if container.status not in ("running", "created"):
                break
            if not novnc_ready:
                exit_code, _ = container.exec_run(
                    ["sh", "-c", "curl -fsS --max-time 2 http://localhost:6901/ >/dev/null 2>&1"],
                )
                if exit_code == 0:
                    novnc_ready = True
            if not cdp_ready:
                exit_code, _ = container.exec_run(
                    ["sh", "-c", "curl -fsS --max-time 2 http://localhost:9223/json/version >/dev/null 2>&1"],
                )
                if exit_code == 0:
                    cdp_ready = True
            if novnc_ready and cdp_ready:
                break
        except APIError:
            pass
        time.sleep(1)

    return {
        "container_name": name,
        "container_id": container.id,
        "ws_path": "/vnc/",
        "cdp_endpoint": f"http://{name}:9223",
        "reused": False,
        "ready": novnc_ready and cdp_ready,
        "novnc_ready": novnc_ready,
        "cdp_ready": cdp_ready,
    }


def stop() -> None:
    """Stop ALL grokflow-vnc-* containers (admin reset)."""
    cli = _client()
    for c in cli.containers.list(all=True, filters={"name": "grokflow-vnc-"}):
        try:
            _trace_destroy("stop() global reset", c.name)
            c.stop(timeout=5)
            c.remove(force=True)
        except APIError:
            pass


def reap_orphans(known_profile_ids: set[str]) -> list[str]:
    """Stop/remove grokflow-vnc-* containers on THIS env's network whose
    profile no longer exists in THIS env's DB.

    Critical: only touches containers attached to `NETWORK_NAME` (the env's
    VNC_NETWORK). Without this filter, the staging backend would see prod's
    VNC containers and reap them as orphans (their profile_id isn't in
    staging's DB) and vice-versa — every redeploy of either stack would
    annihilate the other's live sessions.

    Returns the list of orphan container names that were reaped.
    """
    cli = _client()
    reaped: list[str] = []
    for c in cli.containers.list(all=True, filters={"name": "grokflow-vnc-"}):
        nets = (c.attrs.get("NetworkSettings", {}) or {}).get("Networks") or {}
        if NETWORK_NAME not in nets:
            # Belongs to a different env (prod vs staging) — leave it alone.
            continue
        label_pid = (c.labels or {}).get("grokflow.profile_id")
        if label_pid and label_pid in known_profile_ids:
            continue
        # No profile_id label or profile_id not in DB → orphan for THIS env.
        _trace_destroy(
            f"reap_orphans(label_pid={label_pid!r}, known={len(known_profile_ids)})",
            c.name,
        )
        try:
            c.stop(timeout=3)
        except APIError:
            pass
        try:
            c.remove(force=True)
            reaped.append(c.name)
        except APIError:
            pass
    return reaped


def get_status() -> dict[str, Any]:
    cli = _client()
    running = []
    for c in cli.containers.list(filters={"name": "grokflow-vnc-"}):
        c.reload()
        running.append({
            "name": c.name,
            "profile_id": c.labels.get("grokflow.profile_id"),
            "started_at": c.attrs.get("State", {}).get("StartedAt"),
        })
    return {"running_count": len(running), "containers": running}
