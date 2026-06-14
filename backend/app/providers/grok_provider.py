"""Grok provider via CDP attach to the per-profile VNC Chromium.

Why CDP attach instead of launch_persistent_context:
- Grok is behind Cloudflare bot protection.
- A fresh Playwright Chromium gets `403 Just-a-moment...` immediately.
- The VNC container's Chromium has cf_clearance from the admin's manual login,
  passes Cloudflare, and is reused for every job for this profile.
- We attach via http://<container_name>:9222 and drive an existing tab.
"""

import asyncio
import os
import re
import time
import uuid

import asyncio as _asyncio_for_lock

import httpx
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from app.browser import vnc_manager
from app.providers.base import JobInput, JobResult, Provider, ResultFile
from app.providers.grok_api_client import GrokAPIClient, GrokAPIError

# Per-profile cache of x-statsig-id. The Statsig SDK rotates its stableID
# rarely — caching for an hour cuts the ~3s page-open cost out of nearly
# every job after the first one in a session.
_STATSIG_CACHE: dict[str, tuple[str, float]] = {}
_STATSIG_TTL_S = 3600.0

# Per-profile cache of (cookies_dict, user_agent). Each job currently
# opens its own Playwright connection just to ctx.cookies() — under burst
# (16+ concurrent jobs hit the same Chromium) that pile of CDP clients
# crashes the browser. Caching the cookie jar lets only the first job
# pay the connect cost, and the rest reuse them. Cookies for the
# session change rarely — sso/sso-rw last days, cf_clearance ~2h.
_COOKIES_CACHE: dict[str, tuple[dict[str, str], str, float]] = {}
_COOKIES_TTL_S = 600.0  # 10 min — short enough that stale cookies are
                        # rare; cache miss triggers a sub-second CDP read.

# Per-profile capture lock. Without this, when N concurrent jobs land on
# the same profile and the statsig cache is cold, each one opens its own
# /imagine page simultaneously inside the SAME Chromium and the browser
# crashes (TargetClosedError). Lock makes the first job do the capture
# while the others wait; they then all see the warm cache.
_STATSIG_LOCKS: dict[str, _asyncio_for_lock.Lock] = {}

# Per-profile "API path is unhealthy" sticky flag. Set when the HTTP API
# attempt (/conversations/new) returns provider_blocked / 403 — for the
# next GROK_API_BLOCK_COOLDOWN_SEC seconds every job on this profile
# skips the API attempt and goes straight to the Playwright browser
# fallback. Eliminates the ~15s wasted on the doomed API round-trip.
# Cleared automatically by the cooldown expiring; a successful API
# call (none observed during cooldown) would also clear it implicitly
# the next time the entry's timestamp is older than cooldown.
_API_BLOCKED_UNTIL: dict[str, float] = {}
_API_BLOCK_COUNT: dict[str, int] = {}  # consecutive block count per profile


def _statsig_lock(cache_key: str) -> _asyncio_for_lock.Lock:
    lk = _STATSIG_LOCKS.get(cache_key)
    if lk is None:
        lk = _asyncio_for_lock.Lock()
        _STATSIG_LOCKS[cache_key] = lk
    return lk


# Per-profile navigation lock: page.goto() on a busy Chromium triggers
# a render-thread storm if many concurrent tabs each try to bootstrap React
# at once. Serializing the navigation step alone keeps tail latency bounded.
# Other phases (typing, polling) remain fully concurrent.
_NAV_LOCKS: dict[str, _asyncio_for_lock.Lock] = {}


def _nav_lock(profile_id: str) -> _asyncio_for_lock.Lock:
    lock = _NAV_LOCKS.get(profile_id)
    if lock is None:
        lock = _asyncio_for_lock.Lock()
        _NAV_LOCKS[profile_id] = lock
    return lock


PROMPT_TEXTAREA = [
    # Grok Imagine page uses tiptap (ProseMirror) rich editor — no textarea.
    "div.tiptap.ProseMirror[contenteditable='true']",
    "div.ProseMirror[contenteditable='true']",
    "div.tiptap[contenteditable='true']",
    "[contenteditable='true'][role='textbox']",
    "textarea[placeholder*='Ask']",
    "textarea[aria-label*='Ask Grok']",
    "textarea[aria-label*='Grok']",
    "textarea[data-testid*='input']",
    "textarea",
    "div[contenteditable='true']",
]

IMAGE_RESULT_SELECTORS = [
    "article img[src*='imgen']",
    "article img[src*='assets.grok']",
    "div[data-testid*='image'] img",
    "img[alt*='Generated']",
    "main img:not([alt*='avatar']):not([alt*='Avatar'])",
]

# Substrings (lower-cased) that indicate Grok refused/rate-limited the request.
# Matched against page innerText after submit. Order matters — most specific first.
RATE_LIMIT_HINTS = [
    # Specific phrases Grok ONLY emits when actually blocking a request.
    # Generic substrings like "rate limit" (alone) match unrelated UI text
    # (sidebar history, tooltip 'Pro has higher rate limits', etc.) and
    # cause false-positive failures.
    "you've reached your daily limit",
    "you have reached your daily limit",
    "you have reached your limit",
    "daily limit reached",
    "daily limit has been reached",
    "rate limit exceeded",
    "rate limit reached",
    "you have been rate limited",
    "you've been rate limited",
    "too many requests",
    "try again in a few",
    "try again in 1",  # 'try again in 1 minute', '... 1 hour'
    "please slow down",
    "monthly limit",
    "out of credits",
    "quota exceeded",
]

PRO_REQUIRED_HINTS = [
    # Phrases that explicitly tell the user a Pro subscription is required
    # for THIS specific feature. Generic upsell strings ("upgrade to ...")
    # are too broad — they appear in every Grok page's sidebar.
    "requires a subscription",
    "requires grok pro",
    "requires supergrok",
    "available with grok pro",
    "available with supergrok",
    "subscribe to access",
    "this feature is only available",
    "premium feature",
]


async def _cdp_discover(cdp_endpoint: str, *, attempts: int = 4, delay: float = 0.5) -> tuple[str, str]:
    """Fetch /json/version with retry. Returns (ws_url_rewritten, user_agent).

    Chromium sometimes briefly returns an empty body or 500 from
    /json/version when DevTools is mid-handshake (workers spawning tabs,
    GC running, page navigating away). A single GET-and-decode fails
    JSON parse — observed as `[network_error] CDP discovery: Expecting
    value: line 1 column 1 (char 0)` cancelling jobs that would have
    worked 200ms later.

    Retry up to `attempts` times with `delay` between, then bubble the
    last error. The total budget (4×0.5s = 2s) is small compared to a
    job's hard cap (5-8 min), so the cost of a bad cycle is negligible.
    """
    import asyncio as _asyncio
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=10) as cli:
                resp = await cli.get(f"{cdp_endpoint}/json/version")
                # Treat 5xx + empty body + non-JSON all as transient.
                if resp.status_code >= 500 or not resp.text.strip():
                    raise RuntimeError(f"transient: status={resp.status_code} body_len={len(resp.text)}")
                version = resp.json()
            ws_url = version.get("webSocketDebuggerUrl", "")
            ua = version.get("User-Agent", "")
            if not ws_url:
                raise RuntimeError("no webSocketDebuggerUrl in response")
            host = cdp_endpoint.replace("http://", "").rstrip("/")
            return re.sub(r"ws://[^/]+", f"ws://{host}", ws_url), ua
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if i < attempts - 1:
                await _asyncio.sleep(delay)
    raise last_err or RuntimeError("CDP discovery: unknown")


class GrokProvider(Provider):
    name = "grok"

    GROK_HOME = "https://grok.com/"
    GROK_IMAGINE = "https://grok.com/imagine"
    GROK_IMAGINE_VIDEO = "https://grok.com/imagine/video"
    NAV_TIMEOUT_MS = 45000
    # Timeouts tuned by media type. Videos take longer to render than images.
    # If a job hasn't produced media in this window, we give up and retry.
    # Bumped 120→180s / 240→360s after observing prod runs where Grok's
    # generation queue under shared-account load took ~90-150s for images
    # and 180-300s for videos. The old timeouts caused premature retries
    # that compounded the upstream backlog.
    # 90s — happy-path image jobs settle within 30s now that the loop
    # commits on the first-match grace window (see commit 57bf9d7). The
    # old 180s budget was a hedge against the loop running the full
    # timeout when the URL flickered, which it no longer does. Profiles
    # that genuinely don't render in 90s are stuck — rotate to a sibling
    # rather than burning the budget on the same dead session.
    IMAGE_TIMEOUT_MS = int(os.environ.get("GROK_IMAGE_TIMEOUT_MS", "90000"))
    VIDEO_TIMEOUT_MS = 360000  # 6 min — Grok video can take 90-300s

    async def run(self, job: JobInput) -> JobResult:
        # When the job is pinned to a GrokProject we route the prompt
        # through chat mode (grok.com/project/<slug>) using a slash
        # command, so chat history / presets stay scoped to that project.
        # Otherwise fall back to the legacy /imagine studio flow.
        # Video still uses /imagine because chat-mode slash commands
        # don't always trigger the video pipeline reliably.
        if job.job_type in ("image", "video"):
            # Try the pure-HTTP API path first for image jobs — it bypasses
            # the entire Playwright stack and runs ~10x faster with <1% the
            # RAM. The path returns None on any setup failure (cookie
            # extraction, CDP not ready) so a failure here never strands
            # the job — the Playwright flows below pick up cleanly.
            #
            # ...unless this profile got a 403 / provider_blocked from the
            # API path within the last GROK_API_BLOCK_COOLDOWN_SEC seconds.
            # In that case skip the API attempt outright — it's near-
            # certain to 403 again, and the ~15s round-trip to find out
            # is pure waste on the critical path. Browser fallback takes
            # over immediately.
            # WARP readiness check — only if VNC is already running.
            # Don't spawn here; the downstream paths handle spawning themselves.
            _pid = self._profile_id_from_path(job.profile_path)
            _vnc_info = vnc_manager.get_for_profile(_pid)
            if _vnc_info and _vnc_info.get("running"):
                _cdp_ep = _vnc_info.get("cdp_endpoint", "")
                _warp_tag = f"warp:{_pid[:8]}"
                _warp_ok = await self._wait_warp_ready(_cdp_ep, _warp_tag, timeout=30)
                if not _warp_ok:
                    return JobResult(
                        success=False, error_code="network_error",
                        error_message="VNC proxy (WARP) not ready after 30s — container may be starting up.",
                        retryable=True,
                    )

            if job.job_type == "image" and not self._api_path_blocked(job.profile_path):
                api_result = await self._run_image_via_api(job)
                if api_result is not None:
                    return api_result

            # Video API path stays OFF by default. Empirical retest with
            # curl_cffi (Chrome 124 TLS impersonation) confirmed CF is no
            # longer mangling the request — but Grok's app server still
            # rejects every videoize body variant we send with
            # `invalid-parent-post`. The Playwright /imagine studio flow
            # on the same profile, same minute, same cookies works
            # — strong signal Grok server-side requires an active
            # WebSocket / SSE session our stateless videoize POST can't
            # hold. Until that channel is reverse-engineered, the API
            # attempt only burns ~30s on the critical path (3 body
            # variants × ~10s each) and trips an api-blocked cooldown.
            # Net effect: pointless waste, so default OFF.
            #
            # Re-enable for further debugging via GROK_VIDEO_API_ENABLED=1.
            video_api_enabled = os.getenv("GROK_VIDEO_API_ENABLED", "0").lower() in (
                "1", "true", "yes"
            )
            if (
                job.job_type == "video"
                and video_api_enabled
                and not self._api_path_blocked(job.profile_path)
            ):
                api_result = await self._run_video_via_api(job)
                if api_result is not None:
                    return api_result

            # Project-scoped chat flow. Now supports image-to-image too
            # (uploads the reference inside the chat composer before typing
            # the slash command). Video still goes to Imagine studio
            # because Grok project chat doesn't have a slash-command for
            # video gen in current builds.
            if (
                job.grok_project_id
                and job.job_type == "image"
            ):
                result = await self._run_image_in_project(job)
                if result is not None:
                    return result
            return await self._run_image(job)
        return JobResult(success=False, error_code="unsupported_job_type",
                         error_message=f"Grok provider unsupported job_type: {job.job_type}")

    @staticmethod
    async def _wait_warp_ready(cdp_endpoint: str, tag: str, timeout: int = 45) -> bool:
        """Wait up to `timeout` seconds for WARP proxy to be ready in VNC container.

        Two-phase check:
          1. CDP must be responsive (Chromium is up).
          2. SOCKS5 proxy at 127.0.0.1:40000 inside the container must be able
             to reach the internet (WARP daemon is connected).

        Phase 2 is tested by docker exec'ing a curl command inside the VNC
        container. This catches the case where Chromium is alive but WARP
        hasn't finished connecting yet (ERR_PROXY_CONNECTION_FAILED).
        """
        import asyncio
        import httpx
        import docker

        # Phase 1: wait for CDP
        cdp_ready = False
        for attempt in range(min(timeout // 3, 5)):
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    resp = await client.get(f"{cdp_endpoint}/json/version")
                    if resp.status_code == 200 and resp.json().get("Browser"):
                        cdp_ready = True
                        break
            except Exception:
                pass
            await asyncio.sleep(3)
            if attempt == 0:
                print(f"[{tag}] waiting for CDP...", flush=True)

        if not cdp_ready:
            print(f"[{tag}] CDP not ready after check — skipping WARP test", flush=True)
            return False

        # Phase 2: test actual WARP proxy inside the container.
        # Extract container name from cdp_endpoint: http://grokflow-vnc-XXXXX:9222
        try:
            host_part = cdp_endpoint.split("//")[1].split(":")[0]
        except (IndexError, AttributeError):
            # Can't parse — assume ready (fallback to old behavior)
            return True

        cli = docker.from_env()
        remaining = timeout - 15  # already spent ~15s on CDP checks
        for attempt in range(max(remaining // 5, 1)):
            try:
                container = cli.containers.get(host_part)
                exit_code, output = container.exec_run(
                    ["curl", "-sS", "-m", "5", "-x", "socks5h://127.0.0.1:40000",
                     "-o", "/dev/null", "-w", "%{http_code}", "https://grok.com/"],
                    demux=False,
                )
                # Any HTTP response (even 403) means proxy is working.
                # Only connection failures (exit_code != 0, no http_code) mean WARP is down.
                http_code = output.decode().strip() if output else ""
                if exit_code == 0 and http_code.isdigit() and int(http_code) > 0:
                    return True
            except Exception as exc:
                if attempt == 0:
                    print(f"[{tag}] WARP proxy test error: {exc}", flush=True)
            await asyncio.sleep(5)
            if attempt == 0:
                print(f"[{tag}] waiting for WARP proxy...", flush=True)

        print(f"[{tag}] WARP proxy not ready after {timeout}s", flush=True)
        return False

    @staticmethod
    def _profile_id_from_path(profile_path: str) -> str:
        return profile_path.rstrip("/").split("/")[-1]

    @staticmethod
    def _api_path_blocked(profile_path: str) -> bool:
        """True iff this profile is in the post-403 cooldown window.

        Cooldown duration tunable via GROK_API_BLOCK_COOLDOWN_SEC
        (default 300s = 5 min). Cleared lazily — if the recorded
        timestamp + cooldown is in the past, the entry is removed
        and we return False.
        """
        profile_id = profile_path.rstrip("/").split("/")[-1]
        deadline = _API_BLOCKED_UNTIL.get(profile_id)
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            _API_BLOCKED_UNTIL.pop(profile_id, None)
            _API_BLOCK_COUNT.pop(profile_id, None)  # reset consecutive counter
            return False
        return True

    @staticmethod
    def _mark_api_blocked(profile_path: str) -> None:
        """Record that this profile just got a 403 / provider_blocked on
        the API path. Subsequent jobs within the cooldown window skip
        the API attempt and go straight to the Playwright fallback.

        Uses exponential backoff: each consecutive block doubles the
        cooldown (300 -> 600 -> 1200 -> max 3600s). Counter resets
        when _api_path_blocked returns False (cooldown expired without
        another block)."""
        profile_id = profile_path.rstrip("/").split("/")[-1]
        base_cooldown = float(os.environ.get("GROK_API_BLOCK_COOLDOWN_SEC", "300"))
        max_cooldown = float(os.environ.get("GROK_API_BLOCK_MAX_COOLDOWN_SEC", "3600"))
        count = _API_BLOCK_COUNT.get(profile_id, 0) + 1
        _API_BLOCK_COUNT[profile_id] = count
        cooldown = min(base_cooldown * (2 ** (count - 1)), max_cooldown)
        _API_BLOCKED_UNTIL[profile_id] = time.monotonic() + cooldown
        print(
            f"[grok][api:{profile_id[:8]}] marked api-blocked for {int(cooldown)}s (consecutive={count})",
            flush=True,
        )

    @staticmethod
    def _log(tag: str, *args) -> None:
        # Print to worker stdout so we can correlate with `docker logs`.
        # JobLog requires a DB session we don't carry into the provider, so
        # stdout is the cheap channel; the worker writes summary JobLog rows.
        msg = " ".join(str(a) for a in args)
        print(f"[grok][{tag}] {msg}", flush=True)

    def _ensure_vnc_running(self, job: JobInput) -> dict | None:
        """Idempotent spawn — returns the VNC info dict on success, None if the
        container couldn't be brought up in time. The actual cookies live in
        the bind-mounted /config volume so re-spawning is safe: Chromium picks
        up the same session that the admin's Auto-login established.

        Without this, a job that hits a profile whose VNC container was reaped
        by idle-cleanup (default 2h) fails with `cookie_expired`. With it the
        system is self-healing — the admin's manual Auto-login only needs to
        run once per profile, ever.
        """
        profile_id = self._profile_id_from_path(job.profile_path)
        info = vnc_manager.get_for_profile(profile_id)
        if info and info.get("running"):
            return info
        # Not running — spawn. start_for_profile is idempotent (returns the
        # existing container if a race put one up between our check and call)
        # and blocks until novnc + CDP are ready (up to 60s).
        self._log(profile_id[:8], "VNC not running — spawning…")
        try:
            spawned = vnc_manager.start_for_profile(
                profile_id, job.profile_path, self.GROK_HOME,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(profile_id[:8], f"VNC spawn failed: {exc!r}")
            return None
        if not spawned.get("ready"):
            self._log(profile_id[:8], "VNC spawned but not ready in time")
            return None
        # Re-query to get the canonical info shape (cdp_endpoint etc.)
        return vnc_manager.get_for_profile(profile_id)

    async def _capture_statsig_id(self, ctx, tag: str, *, for_video: bool = False) -> str | None:
        """Snatch the `x-statsig-id` header off any outgoing /rest/* request.

        Statsig's client SDK computes this header on every fetch using a
        stableID + sdkInfo signature. We can't replicate the algorithm in
        Python without reverse-engineering the bundle, but we can observe
        a real request as it leaves the browser and copy the header.

        Strategy:
          1. Attach a request listener to the existing context (so the
             whole browser's traffic flows past us).
          2. Open a blank tab and call `/rest/rate-limits` via fetch from
             page JS — this is a tiny benign endpoint the Grok UI pings
             every few seconds, so triggering it manually is invisible.
          3. The Statsig SDK injects `x-statsig-id` into the request.
             Capture it from the listener, close the tab, return.

        Returns None on any failure — caller falls back to Playwright.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()

        def on_request(request) -> None:  # noqa: ANN001 — Playwright type
            if future.done():
                return
            if "/rest/" not in request.url:
                return
            sid = request.headers.get("x-statsig-id")
            if sid:
                future.set_result(sid)

        ctx.on("request", on_request)
        page = None
        try:
            page = await ctx.new_page()
            # For video jobs, navigate to /imagine FIRST. Grok's server
            # appears to gate video gen on whether the session is in an
            # "Imagine studio active" state — the captured working cURL
            # carries `referer: /imagine` and a session set by Imagine
            # studio. Without warming this path, subsequent video POSTs
            # fail with `invalid-parent-post` even with valid parentPostId.
            target_url = (
                "https://grok.com/imagine"
                if for_video
                else "https://grok.com/"
            )
            try:
                await page.goto(
                    target_url, wait_until="domcontentloaded", timeout=8000
                )
                # Let Imagine studio JS finish hydrating + register the
                # session — empirical 1.5s covers cold load on a fresh tab.
                if for_video:
                    await asyncio.sleep(1.5)
            except PWTimeout:
                pass

            # Trigger a benign /rest/* call so the Statsig header lands on
            # the wire. rate-limits is the cheapest such endpoint.
            try:
                await page.evaluate(
                    "fetch('/rest/rate-limits', {credentials: 'include'})"
                    ".catch(() => {})"
                )
            except Exception:  # noqa: BLE001
                pass

            try:
                sid = await asyncio.wait_for(future, timeout=6.0)
                self._log(tag, f"captured x-statsig-id (len={len(sid)})")
                return sid
            except asyncio.TimeoutError:
                self._log(tag, "no x-statsig-id observed within 6s")
                return None
        finally:
            try:
                ctx.remove_listener("request", on_request)
            except Exception:  # noqa: BLE001
                pass
            if page is not None:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _build_api_session(
        self, job: JobInput
    ) -> tuple[GrokAPIClient, str, str] | None:
        """Prepare a GrokAPIClient bound to this profile's live cookies + statsig.

        Returns `(client, profile_id, tag)` on success, or None to signal
        "fall back to Playwright" (no VNC, no cookies, no statsig, etc.).

        Shared by image + video paths so the cookie/statsig extraction is
        only written once.
        """
        if os.getenv("GROK_API_ENABLED", "true").lower() in ("0", "false", "no"):
            return None
        profile_id = self._profile_id_from_path(job.profile_path)
        tag = f"api:{profile_id[:8]}"
        info = self._ensure_vnc_running(job)
        if not info:
            return None
        cdp_endpoint = info["cdp_endpoint"]

        # WARP readiness check: wait for proxy to be functional
        # before attempting any CDP/API calls. Avoids ERR_PROXY_CONNECTION_FAILED
        # on freshly spawned VNC containers.
        tag_warp = f"warp:{self._profile_id_from_path(job.profile_path)[:8]}"
        if not await self._wait_warp_ready(cdp_endpoint, tag_warp, timeout=30):
            self._log(tag_warp, "VNC proxy not ready — skipping API path")
            return None

        # Retry-with-backoff helper handles the transient empty-body case
        # we used to fail on. See `_cdp_discover` docstring.
        try:
            ws_url, browser_ua = await _cdp_discover(cdp_endpoint)
        except Exception as exc:  # noqa: BLE001
            self._log(tag, f"CDP discovery error: {exc}")
            return None

        cookies_dict: dict[str, str] = {}
        statsig_id: str | None = None
        ua: str = ""
        is_video = job.job_type == "video"
        # Cache statsig separately for image vs video — they need different
        # session contexts (chat home vs /imagine studio).
        cache_key = f"{profile_id}:video" if is_video else profile_id

        # ── Fast path: everything cached → skip Playwright entirely ──
        # In burst load (10+ concurrent jobs hitting the same profile),
        # opening a CDP connection per job is what overloads Chromium.
        # If both caches are warm, we don't need the browser at all.
        cached_statsig = _STATSIG_CACHE.get(cache_key)
        cached_cookies = _COOKIES_CACHE.get(profile_id)
        statsig_warm = (
            cached_statsig
            and time.monotonic() - cached_statsig[1] < _STATSIG_TTL_S
        )
        cookies_warm = (
            cached_cookies
            and time.monotonic() - cached_cookies[2] < _COOKIES_TTL_S
        )
        if statsig_warm and cookies_warm:
            statsig_id = cached_statsig[0]
            cookies_dict = dict(cached_cookies[0])  # defensive copy
            ua = cached_cookies[1]
            self._log(tag, "all caches warm — skipped CDP connect")
        else:
            # ── Slow path: open Playwright once, refill both caches ──
            # Serialize the refill per profile so concurrent jobs don't
            # all stampede the same Chromium with simultaneous CDP opens.
            async with _statsig_lock(cache_key):
                # Re-check after lock — another worker may have just
                # populated both caches while we waited.
                cached_statsig = _STATSIG_CACHE.get(cache_key)
                cached_cookies = _COOKIES_CACHE.get(profile_id)
                if (
                    cached_statsig and time.monotonic() - cached_statsig[1] < _STATSIG_TTL_S
                    and cached_cookies and time.monotonic() - cached_cookies[2] < _COOKIES_TTL_S
                ):
                    statsig_id = cached_statsig[0]
                    cookies_dict = dict(cached_cookies[0])
                    ua = cached_cookies[1]
                    self._log(tag, "caches warmed by sibling — skipped CDP")
                else:
                    try:
                        async with async_playwright() as p:
                            try:
                                browser = await p.chromium.connect_over_cdp(ws_url, timeout=12000)
                            except Exception as exc:  # noqa: BLE001
                                self._log(tag, f"connect_over_cdp failed: {exc}")
                                return None
                            # Important: do NOT call browser.close() — that
                            # would terminate the remote Chromium.
                            ctx = browser.contexts[0] if browser.contexts else None
                            if ctx is None:
                                self._log(tag, "no browser context — fallback")
                                return None

                            # Capture statsig (cold path).
                            statsig_id = await self._capture_statsig_id(ctx, tag, for_video=is_video)
                            if statsig_id:
                                _STATSIG_CACHE[cache_key] = (statsig_id, time.monotonic())

                            # Extract cookies fresh.
                            raw_cookies = await ctx.cookies("https://grok.com")
                            cookies_dict = {c["name"]: c["value"] for c in raw_cookies}
                            ua = (browser_ua or "").replace("HeadlessChrome", "Chrome")
                            if cookies_dict and ua:
                                _COOKIES_CACHE[profile_id] = (
                                    dict(cookies_dict), ua, time.monotonic(),
                                )
                            self._log(tag, "cold refill: opened CDP, repopulated caches")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"cookie/statsig extraction failed: {exc}")
                        return None

        if "sso" not in cookies_dict:
            self._log(tag, "no `sso` cookie — fallback to Playwright path")
            return None

        if not ua:
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            )

        self._log(
            tag,
            f"calling /conversations/new (job_type={job.job_type}, "
            f"project={job.grok_project_id}, cookies={len(cookies_dict)}, "
            f"statsig={'yes' if statsig_id else 'no'})",
        )

        if not statsig_id:
            self._log(tag, "no x-statsig-id available — skipping API path")
            return None

        # Video can take ~3 min on Grok's queue; use the bigger timeout.
        timeout_s = (
            self.VIDEO_TIMEOUT_MS if job.job_type == "video"
            else self.IMAGE_TIMEOUT_MS
        ) / 1000

        client = GrokAPIClient(
            cookies=cookies_dict, user_agent=ua,
            x_statsig_id=statsig_id, timeout=timeout_s,
        )
        return client, profile_id, tag

    async def _run_video_via_api(self, job: JobInput) -> JobResult | None:
        """Pure-HTTP video generation. Always image-to-video.

        Grok's video endpoint requires an existing image post — there's no
        pure text-to-video. Flow:
          1. If the job carries an image attachment, upload it directly.
          2. Otherwise call imagine() to generate one from the prompt, then
             upload the result so videoize() has something to reference.
          3. POST /conversations/new with modelName=imagine-video-gen, the
             uploaded image's fileMetadataId as parentPostId, and the
             asset URL embedded in the message body.

        Returns None on any failure → Playwright video flow takes over.
        """
        session = await self._build_api_session(job)
        if session is None:
            return None
        client, profile_id, tag = session

        opts = job.options or {}
        aspect = str(opts.get("aspect_ratio") or opts.get("aspect") or "3:2")
        # Grok's videoize endpoint only accepts the literal strings 480p,
        # 720p, 1080p — passing anything else (eg the partner-facing
        # "standard" / "high" / "low" labels we get on the client API)
        # returns 400 "Resolution must be ... got <label>" and the
        # worker falls back to Playwright. Normalise here so the API
        # path actually runs.
        quality_raw = str(opts.get("resolution") or opts.get("quality") or "720p").lower()
        _RES_MAP = {
            "480p": "480p", "low": "480p", "draft": "480p",
            "720p": "720p", "standard": "720p", "medium": "720p", "hd": "720p", "high": "720p",
            "1080p": "1080p", "full": "1080p", "fhd": "1080p", "ultra": "1080p",
        }
        quality = _RES_MAP.get(quality_raw, "720p")
        try:
            duration = int(opts.get("duration") or opts.get("video_length") or 10)
        except (TypeError, ValueError):
            duration = 10
        mode = str(opts.get("mode") or "custom")

        try:
            # ── Source image strategy
            # Path A: user-supplied attachment → must go through /upload-file
            # because we don't have an imageUuid for it. The current Grok
            # backend rejects /upload-file IDs with `invalid-parent-post`
            # when used as parentPostId, so this path is best-effort.
            # Path B (preferred): no attachment → generate via /imagine and
            # use the resulting imageUuid as parentPostId directly. Skips
            # /upload-file entirely (which Grok was rejecting in tests).
            # Source image: must produce a `users/<uid>/<id>/content` URL
            # because Grok's video endpoint won't accept `/generated/.../
            # image.jpg` paths. Two ways to get a /content URL:
            #   A. Use job.attachments → upload_file → fileMetadataId.
            #   B. No attachment → /imagine → download bytes → upload them.
            # Both routes funnel through /upload-file so the upstream sees
            # a "user-uploaded" asset, which is what video gen requires.
            if job.attachments:
                att = job.attachments[0]
                image_bytes = att.bytes
                image_mime = att.mime or "image/jpeg"
                image_name = att.name or "input.jpg"
                self._log(tag, f"using job attachment ({len(image_bytes)} B)")
            else:
                self._log(tag, "no attachment — generating source via /imagine then uploading")
                imagine_results = await client.imagine(
                    prompt=job.prompt,
                    project_id=job.grok_project_id,
                    log=lambda m: self._log(tag, m),
                )
                if not imagine_results:
                    self._log(tag, "imagine returned nothing — fallback")
                    return None
                image_bytes = imagine_results[0]
                image_mime = "image/jpeg"
                image_name = f"{uuid.uuid4()}.jpg"

            meta = await client.upload_file(
                content=image_bytes,
                filename=image_name,
                mime=image_mime,
                log=lambda m: self._log(tag, m),
            )
            parent_id = meta["fileMetadataId"]
            asset_path = meta["fileUri"]

            # ── Trigger video generation referencing the source image
            video_bytes_list = await client.videoize(
                prompt=job.prompt,
                file_metadata_id=parent_id,
                file_uri=asset_path,
                aspect_ratio=aspect,
                resolution=quality,
                duration=duration,
                mode=mode,
                log=lambda m: self._log(tag, m),
            )
        except GrokAPIError as exc:
            self._log(tag, f"API error: {exc.code} — {exc.message}")
            if exc.code == "provider_blocked":
                _STATSIG_CACHE.pop(profile_id, None)
                self._mark_api_blocked(job.profile_path)
            # `rate_limited` with "quota exhausted" / "invalid-parent-post"
            # is Grok's server-side rejection of every videoize body
            # variant — not a real CF block, but functionally the same:
            # the next video job on this profile will burn ~30s
            # retrying the same 3 variants before falling back. Trip the
            # cooldown so subsequent video jobs skip the API attempt
            # entirely until the bug is reverse-engineered.
            if exc.code == "rate_limited" and (
                "quota exhausted" in (exc.message or "")
                or "invalid-parent-post" in (exc.message or "")
            ):
                self._mark_api_blocked(job.profile_path)
            if exc.code == "cookie_expired":
                return JobResult(
                    success=False,
                    error_code="cookie_expired",
                    error_message=exc.message,
                    retryable=False,
                )
            return None

        files = [
            ResultFile(bytes=b, name=f"video-{i}.mp4", mime="video/mp4")
            for i, b in enumerate(video_bytes_list)
        ]
        return JobResult(success=True, files=files,
                         extra={"path": "api", "type": "video",
                                "count": len(files)})

    async def _run_image_via_api(self, job: JobInput) -> JobResult | None:
        """Pure-HTTP /imagine via grok.com/rest/app-chat/conversations/new.

        Strategy:
          1. Ensure the VNC Chromium is up (it owns the cf_clearance + sso
             cookies that were established during admin Auto-login).
          2. Attach via CDP, pull live cookies + UA out of the context.
          3. POST through GrokAPIClient and stream-parse the response.

        Returns:
          - JobResult on definitive success or definitive failure (4xx etc.)
          - None when we couldn't even set up the API call (no VNC, no
            cookies). Caller falls back to the Playwright pipelines.

        We intentionally swallow GrokAPIError(provider_blocked) and return
        None instead of failing the job: a 403 here usually means Grok
        rotated their statsig token, and the Playwright path can still
        complete because it runs inside a real browser session.

        Gated behind GROK_API_ENABLED env var. Default `true` now that
        x-statsig-id capture works; set `GROK_API_ENABLED=0` to roll back
        to pure-Playwright operation without redeploying.

        Image-to-image: when `job.attachments` is present, we upload the
        reference via `client.upload_file()` first, then pass the resulting
        `{fileMetadataId, fileUri}` to `imagine()`. The asset URL gets
        inlined in the message (videoize wire format) so Grok binds the
        image to the Imagine pipeline. If Grok rejects this shape, the
        caller falls back to Playwright as usual.
        """
        session = await self._build_api_session(job)
        if session is None:
            return None
        client, profile_id, tag = session

        attachment_meta: dict[str, str] | None = None
        if job.attachments:
            try:
                att = job.attachments[0]
                self._log(tag, f"i2i: uploading {att.name} ({len(att.bytes)} bytes)")
                attachment_meta = await client.upload_file(
                    content=att.bytes,
                    filename=att.name,
                    mime=att.mime,
                    log=lambda m: self._log(tag, m),
                )
            except GrokAPIError as exc:
                self._log(tag, f"i2i upload error: {exc.code} — {exc.message}")
                # Terminal errors must surface to the user immediately —
                # they're not going to fix themselves on retry and the
                # Playwright fallback will hit the same wall (Grok blocks
                # the same image whether we upload via REST or DOM).
                if exc.code in {"content_moderated", "cookie_expired"}:
                    return JobResult(
                        success=False,
                        error_code=exc.code,
                        error_message=exc.message,
                        retryable=False,
                    )
                # Other upload failures (network, unknown_error) → fall
                # through to Playwright in case it's a transient REST glitch.
                return None

        retry_after_refresh = False
        try:
            image_bytes_list = await client.imagine(
                prompt=job.prompt,
                project_id=job.grok_project_id,
                attachment=attachment_meta,
                log=lambda m: self._log(tag, m),
            )
        except GrokAPIError as exc:
            self._log(tag, f"API error: {exc.code} — {exc.message}")
            # Bust BOTH caches on any API failure — the most common cause
            # is a stale cf_clearance or rotated statsig token. Forcing
            # the next call to do a fresh CDP capture is way cheaper than
            # falling all the way through to a 180s Playwright timeout.
            _STATSIG_CACHE.pop(profile_id, None)
            _STATSIG_CACHE.pop(f"{profile_id}:video", None)
            _COOKIES_CACHE.pop(profile_id, None)
            if exc.code == "provider_blocked":
                # On a burst of concurrent jobs, sibling jobs reuse one cached
                # statsig token; when it goes stale they all 403 at once. Instead
                # of immediately benching the profile (which dumps every queued
                # job onto the slower Playwright path and trips the DOM tab
                # limit), re-capture fresh cookies + statsig and retry the API
                # ONCE. Only a second consecutive block benches the profile.
                retry_after_refresh = True
            elif exc.code == "cookie_expired":
                return JobResult(
                    success=False,
                    error_code="cookie_expired",
                    error_message=exc.message,
                    retryable=False,
                )
            else:
                return None

        if retry_after_refresh:
            refreshed = await self._build_api_session(job)
            if refreshed is None:
                # Couldn't even rebuild the session — let Playwright try.
                self._mark_api_blocked(job.profile_path)
                return None
            retry_client, _retry_pid, retry_tag = refreshed
            try:
                self._log(retry_tag, "provider_blocked — retrying API once with fresh statsig/cookies")
                image_bytes_list = await retry_client.imagine(
                    prompt=job.prompt,
                    project_id=job.grok_project_id,
                    attachment=attachment_meta,
                    log=lambda m: self._log(retry_tag, m),
                )
            except GrokAPIError as exc2:
                self._log(retry_tag, f"API retry error: {exc2.code} — {exc2.message}")
                _STATSIG_CACHE.pop(profile_id, None)
                _STATSIG_CACHE.pop(f"{profile_id}:video", None)
                _COOKIES_CACHE.pop(profile_id, None)
                if exc2.code == "provider_blocked":
                    # Second block in a row → genuine CF/statsig wall, bench it.
                    self._mark_api_blocked(job.profile_path)
                elif exc2.code == "cookie_expired":
                    return JobResult(
                        success=False,
                        error_code="cookie_expired",
                        error_message=exc2.message,
                        retryable=False,
                    )
                return None

        files = [
            ResultFile(bytes=b, name=f"image-{i}.jpg", mime="image/jpeg")
            for i, b in enumerate(image_bytes_list)
        ]
        return JobResult(success=True, files=files,
                         extra={"path": "api", "count": len(files)})

    async def _run_image_in_project(self, job: JobInput) -> JobResult | None:
        """Project-scoped image generation via chat-mode `/imagine` slash.

        Returns:
          - JobResult on success/failure of the project flow
          - None when we can't even get into the project page; caller
            then falls back to legacy /imagine studio (so a routing
            blip doesn't strand the job)

        Selectors here are best-effort against Grok's chat UI which
        we don't fully control. Heavy logging at each step makes UI
        drift easy to spot in worker logs.
        """
        profile_id = self._profile_id_from_path(job.profile_path)
        tag = f"prj:{profile_id[:8]}"
        # Project chat ALSO opens a Chromium tab — gate it on the same
        # DOM slot pool as `_run_image`. Worker no longer pre-acquires
        # for image, so this is the actual cap.
        from app.core.profile_slots import (
            acquire_image_dom_slot, release_image_dom_slot,
        )
        if not await acquire_image_dom_slot(profile_id):
            self._log(tag, "DOM slot full — skipping project chat path")
            return None  # caller falls back; worker will requeue
        try:
            return await self._run_image_in_project_inner(job, tag, profile_id)
        finally:
            await release_image_dom_slot(profile_id)

    async def _run_image_in_project_inner(self, job, tag: str, profile_id: str):
        info = self._ensure_vnc_running(job)
        if not info:
            return None
        cdp_endpoint = info["cdp_endpoint"]

        try:
            ws_url, _ = await _cdp_discover(cdp_endpoint)
        except Exception as exc:  # noqa: BLE001
            self._log(tag, f"CDP discovery error: {exc}")
            return None

        target_url = f"https://grok.com/project/{job.grok_project_id}"
        self._log(tag, f"goto {target_url}")

        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(ws_url, timeout=12000)
            except Exception as exc:  # noqa: BLE001
                self._log(tag, f"connect_over_cdp failed: {exc}")
                return None

            page = None
            try:
                ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await ctx.new_page()
                lock = _nav_lock(profile_id)
                async with lock:
                    try:
                        await page.goto(target_url, wait_until="domcontentloaded",
                                        timeout=self.NAV_TIMEOUT_MS)
                    except PWTimeout:
                        self._log(tag, "nav timeout — falling back to /imagine")
                        return None
                    await asyncio.sleep(2)  # SPA hydrate

                # Verify the project page loaded — sometimes Grok 404s on
                # bad slugs and shows an "Error finding ID …" page. If we
                # see that, abort and let caller fall back.
                err_loc = page.locator("text=/error finding|not found/i").first
                if await err_loc.count() > 0:
                    snippet = await page.evaluate(
                        "() => document.body.innerText.slice(0, 200)"
                    )
                    self._log(tag, f"project page errored: {snippet!r} — fallback to /imagine")
                    return None

                # Locate the chat input. Grok's chat uses a contenteditable
                # div in current builds; fall back to <textarea>.
                input_candidates = [
                    "div[contenteditable='true'][role='textbox']",
                    "textarea[placeholder*='ask' i]",
                    "textarea[placeholder*='message' i]",
                    "textarea",
                    "[contenteditable='true']",
                ]
                chat_input = None
                for sel in input_candidates:
                    loc = page.locator(sel).first
                    try:
                        await loc.wait_for(timeout=4000, state="visible")
                        chat_input = loc
                        self._log(tag, f"chat input found via: {sel}")
                        break
                    except PWTimeout:
                        continue
                if chat_input is None:
                    self._log(tag, "no chat input — fallback to /imagine")
                    return None

                # Image-to-image: upload the reference BEFORE typing so the
                # chat composer attaches it inline. Same hidden <input
                # type='file'> as the Imagine-studio path uses, just inside
                # the project page DOM.
                if job.attachments:
                    try:
                        await self._attach_files(page, job.attachments)
                        # React re-renders the composer after a successful
                        # upload — re-find the input handle so subsequent
                        # type calls don't hit a detached element.
                        await asyncio.sleep(2.5)
                        for sel in input_candidates:
                            loc = page.locator(sel).first
                            try:
                                await loc.wait_for(timeout=4000, state="visible")
                                chat_input = loc
                                break
                            except PWTimeout:
                                continue
                        self._log(tag, "input image attached in project chat")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"project attach failed: {exc} — fallback")
                        return None

                # Send "/imagine <prompt>". Grok's project chat respects
                # this slash command on Imagine-capable accounts.
                slash_prompt = f"/imagine {job.prompt}"
                try:
                    await chat_input.click()
                    # Empty the field in case React kept any draft.
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                    await chat_input.type(slash_prompt, delay=12)
                except Exception as exc:  # noqa: BLE001
                    self._log(tag, f"typing failed: {exc} — fallback")
                    return None

                # Brief pause so React registers the input + send button
                # enables, then submit via Enter.
                await asyncio.sleep(0.6)
                await page.keyboard.press("Enter")
                self._log(tag, f"submitted /imagine (len={len(job.prompt)})")

                # Poll for an image to appear in the chat. ONLY accept
                # URLs whose path contains `/generated/` — that's where
                # Grok stores AI-rendered output. Without this filter we
                # used to match `assets.grok.com/<project>/content` (the
                # project sidebar icon) and download THAT instead of the
                # actual generated image, producing tiny WebP files
                # totally unrelated to the prompt.
                # Grok renders in TWO phases at the same chat bubble:
                #   1. Within ~3-5s an /generated/ URL appears holding a
                #      low-res placeholder (~24 KB JPEG, smeared mosaic).
                #   2. 20-45s later the bubble's <img src> swaps to a
                #      different /generated/ URL holding the final
                #      ~200 KB-1 MB image.
                # Old code broke on the FIRST /generated/ match — it
                # downloaded the phase-1 mosaic, marked the job success,
                # and partners got blur as their result. Fix: collect
                # URLs over a settling window and only return the URL
                # that's been the LATEST observed for STABLE_WINDOW_SEC.
                timeout_s = self.IMAGE_TIMEOUT_MS / 1000
                # How long the same final-URL must remain "latest" before
                # we commit. Originally 12s — empirically that proved too
                # generous: when Grok streams the result the <img src>
                # toggles between phase-1 / phase-2 / cleared a few times
                # before settling, and a 12s stability window never holds,
                # so the loop ran the full timeout (150s+) before the
                # worker retried. 6s catches the typical settle pattern
                # and still skips the early blur thumbnail.
                STABLE_WINDOW_SEC = float(os.environ.get(
                    "GROK_IMAGE_STABLE_WINDOW_SEC", "6",
                ))
                # Floor on total wait — Grok almost never finishes in <8s
                # even when the phase-1 URL is up. Don't even consider a
                # match before this so we never short-circuit out of the
                # phase-2 swap. Halved from 20s.
                MIN_WAIT_SEC = float(os.environ.get(
                    "GROK_IMAGE_MIN_WAIT_SEC", "10",
                ))
                start = time.monotonic()
                found_url: str | None = None
                last_log = -30
                latest_url: str | None = None
                latest_seen_at: float | None = None
                first_match_at: float | None = None
                # Once a /generated/ URL appears in the DOM at all, commit
                # to whatever's latest after a short grace period. The
                # earlier "stay stable for N seconds" approach drove the
                # loop into the full 150s timeout whenever Grok flickered
                # the <img src> (which it does, often) — the URL is the
                # right one even when the DOM toggles it on and off.
                FIRST_MATCH_GRACE = float(os.environ.get(
                    "GROK_IMAGE_FIRST_MATCH_GRACE", "8",
                ))
                while time.monotonic() - start < timeout_s:
                    elapsed = int(time.monotonic() - start)
                    try:
                        result = await page.evaluate(
                            """() => {
                                // REQUIRE /generated/ in path — only
                                // segment that appears on AI-rendered
                                // output, not project icons / avatars.
                                const all = Array.from(document.querySelectorAll('img'))
                                    .map(i => i.src)
                                    .filter(s => s && s.includes('assets.grok'));
                                const matched = all.filter(s => /\\/generated\\//i.test(s));
                                return { matched, all };
                            }"""
                        )
                        urls = result.get("matched", [])
                        all_grok_urls = result.get("all", [])
                    except Exception:  # noqa: BLE001
                        urls = []
                        all_grok_urls = []
                    if urls:
                        # Track the LAST URL in DOM order — chat appends
                        # new bubbles to the bottom, so the freshest
                        # render is at the end of the list.
                        current_last = urls[-1]
                        now = time.monotonic()
                        if first_match_at is None:
                            first_match_at = now
                        if current_last != latest_url:
                            if latest_url is not None:
                                self._log(
                                    tag,
                                    f"image url swapped after {elapsed}s: "
                                    f"…{latest_url[-40:]} → …{current_last[-40:]}",
                                )
                            latest_url = current_last
                            latest_seen_at = now
                        # Commit when (a) we've waited the floor AND
                        # (b) either the URL has been stable for the
                        # short stable window, OR the grace period since
                        # FIRST sighting expired (catches flickering DOM).
                        stable_ok = (
                            latest_seen_at is not None
                            and (now - latest_seen_at) >= STABLE_WINDOW_SEC
                        )
                        grace_ok = (
                            first_match_at is not None
                            and (now - first_match_at) >= FIRST_MATCH_GRACE
                        )
                        if elapsed >= MIN_WAIT_SEC and (stable_ok or grace_ok):
                            found_url = latest_url
                            self._log(
                                tag,
                                f"image url committed after {elapsed}s "
                                f"(reason={'stable' if stable_ok else 'grace'}): "
                                f"{found_url[:80]}…",
                            )
                            break
                    if elapsed - last_log >= 30:
                        # Dump any Grok-CDN URLs we DID see so we can
                        # debug filter false-negatives without a fresh
                        # capture. Cap at 3 per cycle to keep logs sane.
                        sample = (all_grok_urls or [])[:3]
                        if sample:
                            self._log(tag, f"polling chat… {elapsed}s — non-match urls: {sample}")
                        else:
                            self._log(tag, f"polling chat… {elapsed}s — 0 grok assets yet")
                        last_log = elapsed
                    # Fast-fail: if 60s passed with zero generated assets,
                    # Grok is likely ignoring this submit (rate-limited or
                    # quota exhausted). Bail early instead of waiting the
                    # full timeout — saves ~30s per doomed attempt.
                    if elapsed >= 60 and first_match_at is None:
                        self._log(tag, f"fast-fail: {elapsed}s in project chat, 0 generated assets — Grok ignoring submit")
                        return JobResult(
                            success=False, error_code="rate_limited",
                            error_message=(
                                f"Project chat: no generated image after {elapsed}s. "
                                "Grok đang throttle hoặc hết quota."
                            ),
                            retryable=True,
                        )
                    await asyncio.sleep(3)

                if not found_url:
                    return JobResult(
                        success=False, error_code="timeout",
                        error_message=(
                            f"Project /imagine: no image in chat after "
                            f"{int(timeout_s)}s. Account có thể chưa Pro "
                            "hoặc Grok đang queue."
                        ),
                        retryable=True,
                    )

                # Pull the image bytes through the SAME Chromium so it
                # carries the auth cookies Grok requires on its asset CDN.
                # Tightened timeout 60s -> 25s and split into connect/read
                # phases — a stalled CDN connect used to occupy a worker
                # for the full minute, blocking sibling jobs even when
                # the asset would have failed instantly.
                download_t0 = time.monotonic()
                try:
                    cookies = await ctx.cookies("https://grok.com")
                    jar = {c["name"]: c["value"] for c in cookies}
                    timeout_cfg = httpx.Timeout(connect=8.0, read=25.0, write=25.0, pool=8.0)
                    async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True) as cli:
                        r = await cli.get(found_url, cookies=jar)
                    dl_sec = int(time.monotonic() - download_t0)
                    if r.status_code != 200 or not r.content:
                        return JobResult(
                            success=False, error_code="network_error",
                            error_message=f"Image download {r.status_code} (len={len(r.content)}) after {dl_sec}s",
                            retryable=True,
                        )
                    self._log(tag, f"image downloaded in {dl_sec}s ({len(r.content)} bytes)")
                    mime = r.headers.get("content-type", "image/png").split(";")[0]
                    ext = mime.split("/")[-1] or "png"
                    return JobResult(
                        success=True,
                        files=[ResultFile(
                            bytes=r.content,
                            name=f"grok-project-{int(time.time())}.{ext}",
                            mime=mime,
                            source_url=found_url,
                        )],
                    )
                except Exception as exc:  # noqa: BLE001
                    dl_sec = int(time.monotonic() - download_t0)
                    return JobResult(
                        success=False, error_code="network_error",
                        error_message=f"download failed after {dl_sec}s: {exc}",
                        retryable=True,
                    )
            finally:
                # Close the WORKING tab + scoop up any result-page strays
                # ("grok.com/imagine/post/<id>") that this job may have left
                # behind. Each lingering tab = ~100MB Chromium RAM, so
                # leaving them across many jobs balloons VNC memory.
                # Only close tabs whose URL clearly maps to this job's
                # output (`/imagine/post/`) — never touch sibling job tabs.
                try:
                    if page:
                        try:
                            await asyncio.wait_for(page.close(), timeout=3)
                        except Exception as exc:  # noqa: BLE001
                            self._log(tag, f"page.close timeout/error: {exc}")
                    # Sweep result pages still in the context — these are
                    # what we see leaking in CDP /json after the job done.
                    try:
                        for p in list(ctx.pages):
                            if p is page:
                                continue
                            u = (p.url or "")
                            if "/imagine/post/" in u:
                                try:
                                    await asyncio.wait_for(p.close(), timeout=3)
                                    self._log(tag, f"GC closed result tab: …{u[-40:]}")
                                except Exception:  # noqa: BLE001
                                    pass
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await browser.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    async def _run_image(self, job: JobInput) -> JobResult:
        profile_id = self._profile_id_from_path(job.profile_path)
        tag = f"{job.job_type[:3]}:{profile_id[:8]}"
        # Image jobs reach this DOM path only when API + project-chat
        # both fell through. Acquire a DOM slot now so concurrent
        # Chromium tabs stay capped at the profile's max — RAM-bounded.
        # Video paths still acquire upstream in the worker, so don't
        # double-acquire when called for video.
        dom_slot_taken = False
        if job.job_type == "image":
            from app.core.profile_slots import acquire_image_dom_slot
            dom_slot_taken = await acquire_image_dom_slot(profile_id)
            if not dom_slot_taken:
                return JobResult(
                    success=False, error_code="rate_limited",
                    error_message="DOM tab limit reached on this profile — retry shortly.",
                    retryable=True,
                )
        try:
            return await self._run_image_inner(job, tag, profile_id)
        finally:
            if dom_slot_taken:
                from app.core.profile_slots import release_image_dom_slot
                await release_image_dom_slot(profile_id)

    async def _run_image_inner(self, job: JobInput, tag: str, profile_id: str) -> JobResult:
        info = self._ensure_vnc_running(job)
        if not info:
            return JobResult(
                success=False, error_code="cookie_expired",
                error_message="VNC browser not running and auto-spawn failed. Admin must Auto-login this profile first.",
            )

        cdp_endpoint = info["cdp_endpoint"]  # e.g. http://grokflow-vnc-xxx:9223
        prompt_text = self._compose_prompt(job)

        # _cdp_discover retries up to 4× with 500ms backoff to ride out
        # transient empty-body / 5xx responses from a busy Chromium.
        try:
            ws_url, _ = await _cdp_discover(cdp_endpoint)
        except Exception as exc:  # noqa: BLE001
            return JobResult(success=False, error_code="network_error",
                             error_message=f"CDP discovery: {exc}", retryable=True)

        page = None
        self._log(tag, "start: connect_over_cdp")
        try:
            async with async_playwright() as p:
                # Bigger CDP-connect timeout: when Chromium is overloaded the
                # initial WS handshake can take >5s. We catch PWTimeout below
                # and map to rate_limited (long backoff) instead of looping
                # immediately.
                try:
                    # 12s — long enough for an idle Chromium, short enough
                    # that we fail fast and retry instead of hanging forever
                    # when ghost WS sessions are queued.
                    browser = await p.chromium.connect_over_cdp(ws_url, timeout=12000)
                except PWTimeout:
                    return JobResult(
                        success=False, error_code="rate_limited",
                        error_message=(
                            "CDP connect timed out — Chromium busy or has stale "
                            "WS sessions from a cancelled run. Sẽ retry sau "
                            "backoff để Chromium phục hồi."
                        ),
                        retryable=True,
                    )
                context = browser.contexts[0] if browser.contexts else None
                if not context:
                    await browser.disconnect()
                    return JobResult(success=False, error_code="browser_crashed",
                                     error_message="No browser context found",
                                     retryable=True)

                # Stale-tab GC. CRITICAL constraints:
                #   1) Other concurrent worker tasks may be mid-evaluate on a
                #      tab — closing it surfaces as TargetClosedError.
                #   2) `await context.new_page()` returns a tab on
                #      about:blank for a few hundred ms before its goto()
                #      starts. We must NOT close those — another job may have
                #      just created it.
                # So the rule is: close only tabs that are clearly stale —
                # parked on a non-grok URL (e.g., abandoned redirect) OR on
                # grok.com with an empty body for some time. Skip about:blank
                # entirely; they're either brand new or already invisible.
                # Open the working tab FIRST, then close stale ones. Closing
                # all tabs before opening the new one would leave Chromium
                # with zero targets and Target.createTarget fails with
                # 'Failed to open a new tab' — observed when our GC was
                # aggressive enough to take the homepage tab too.
                #
                # Snapshot the existing tab list so we know which to close
                # AFTER our new tab is up.
                old_pages_to_close = list(context.pages)
                page = await context.new_page()
                self._log(tag, "new tab opened")

                try:
                    gc_count = 0
                    for old in old_pages_to_close:
                        try:
                            if old is page:
                                continue  # never close ourselves
                            u = old.url or ""
                            if not u or u == "about:blank":
                                continue  # may belong to a sibling worker
                            if "grok.com" not in u and "x.ai" not in u:
                                continue  # not ours
                            # Only close STALE tabs — never close active job tabs
                            # Active: /imagine (prompt page), /imagine/post/ (polling result)
                            # Stale: homepage (/), /chat/, /share/, old /project/ tabs
                            if u.rstrip('/').endswith('grok.com') or '/chat/' in u or '/share/' in u:
                                await asyncio.wait_for(old.close(), timeout=3)
                                gc_count += 1
                        except Exception:  # noqa: BLE001
                            pass
                    if gc_count:
                        self._log(tag, f"tab-GC closed {gc_count} grok.com tab(s) after opening new")
                except Exception:  # noqa: BLE001
                    pass

                # Always navigate to /imagine — Grok's `/project/<slug>` URL
                # is a chat view, not the Imagine studio, and our DOM
                # selectors (prompt bar, mode toggle, aspect dropdown)
                # don't match there. Project context is still recorded on
                # Job.project_id for audit; per-project chat history will
                # need a different integration (likely slash-command in
                # chat + parser) once we research Grok's project↔imagine
                # bridge. The /imagine/video URL was deprecated in late
                # 2025 — image vs video is selected via in-page toggle.
                target_url = self.GROK_IMAGINE
                self._log(
                    tag,
                    f"goto {target_url} (job_type={job.job_type}"
                    + (f", project_pin={job.grok_project_id}" if job.grok_project_id else "")
                    + ")"
                )
                # Serialize the goto step across concurrent jobs on this
                # Chromium — N parallel React boots can deadlock the renderer
                # and trigger ERR_ABORTED / TimeoutError storms.
                lock = _nav_lock(profile_id)
                try:
                    async with lock:
                        await page.goto(target_url, wait_until="domcontentloaded",
                                        timeout=self.NAV_TIMEOUT_MS)
                        await asyncio.sleep(2)  # let SPA render
                except PWTimeout:
                    # Navigation timeout means Chromium itself is overloaded.
                    # Mark this as rate_limited (longer backoff) so we don't
                    # immediately spawn another tab and worsen the cascade.
                    return JobResult(
                        success=False, error_code="rate_limited",
                        error_message=("Navigation timed out — Chromium is overloaded. "
                                       "Reduce profile.max_concurrent_jobs or wait."),
                        retryable=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    if "ERR_ABORTED" in msg or "ERR_FAILED" in msg or "net::" in msg:
                        return JobResult(
                            success=False, error_code="rate_limited",
                            error_message=f"Navigation aborted: {msg[:120]}",
                            retryable=True,
                        )
                    raise

                # Cheap pre-checks BEFORE the 30s prompt-bar wait — fail
                # fast on the known non-recoverable states (Cloudflare /
                # login redirect) instead of burning the full wait window.
                title_pre = await page.title()
                if "Just a moment" in title_pre or "Cloudflare" in title_pre:
                    return JobResult(success=False, error_code="cookie_expired",
                                     error_message="Cloudflare challenge — re-login via Auto login")
                if any(kw in page.url.lower() for kw in ("login", "sign-in", "signin", "auth")):
                    return JobResult(success=False, error_code="cookie_expired",
                                     error_message=f"Redirected to login: {page.url}")

                # Wait for the prompt-bar to actually render before any radio
                # click attempts. Without this, on a busy Chromium the React
                # tree isn't mounted yet → the Image/Video radios + duration
                # buttons don't exist → all our clicks no-op silently.
                #
                # 30s (was 15s): production Grok pages occasionally take
                # 20-25s to hydrate when the VPS is under load OR Grok's CDN
                # is slow. A 15s ceiling tripped legit jobs into a fake
                # rate_limited rotation → profile churn for no reason.
                # Configurable via PROMPT_BAR_TIMEOUT_MS env for ops tuning.
                _bar_timeout_ms = int(os.getenv("PROMPT_BAR_TIMEOUT_MS", "30000"))
                try:
                    await page.wait_for_selector(
                        "[role=radio], button[aria-label='Submit']",
                        timeout=_bar_timeout_ms, state="visible",
                    )
                except PWTimeout:
                    # One last CF / login redirect probe — Grok sometimes
                    # injects the challenge mid-load, so the title check
                    # earlier missed it.
                    title_late = await page.title()
                    if "Just a moment" in title_late or "Cloudflare" in title_late:
                        return JobResult(success=False, error_code="cookie_expired",
                                         error_message="Cloudflare challenge — re-login via Auto login")
                    return JobResult(
                        success=False, error_code="rate_limited",
                        error_message=f"Prompt bar didn't render in {_bar_timeout_ms // 1000}s — Chromium overloaded.",
                        retryable=True,
                    )

                content = (await page.content()).lower()
                if "captcha" in content:
                    return JobResult(success=False, error_code="captcha_required",
                                     error_message="Captcha detected")

                # Check for "Sign in / Sign up" buttons → not logged in
                body_text = await page.evaluate("() => document.body.innerText.slice(0, 200)")
                if "Sign in" in body_text and "Sign up" in body_text and not any(
                    "How can I help" in body_text and kw in body_text for kw in ()
                ):
                    # Likely not logged in — but Grok shows Sign in/up even when logged in sometimes
                    pass  # don't block; let it try

                # Dismiss cookie banner if present
                for label in ("Reject All", "Accept All Cookies", "Allow All"):
                    try:
                        btn = await page.query_selector(f"button:has-text('{label}')")
                        if btn and await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(0.5)
                            break
                    except Exception:  # noqa: BLE001
                        continue

                # Switch Image / Video mode via the radio toggle in the
                # prompt bar. Buttons have role=radio + aria-checked. We
                # only click if the target isn't already checked, and we
                # verify the switch happened.
                want_video = job.job_type == "video"
                target_tab = "Video" if want_video else "Image"
                try:
                    switched = await page.evaluate(
                        """(target) => {
                            const radios = Array.from(document.querySelectorAll(
                                "[role=radio]"
                            )).filter(b => b.offsetParent !== null);
                            const b = radios.find(r => (r.innerText||"").trim() === target);
                            if (!b) return { found: false };
                            const already = b.getAttribute("aria-checked") === "true";
                            if (!already) b.click();
                            return { found: true, already, after: b.getAttribute("aria-checked") };
                        }""",
                        target_tab,
                    )
                    self._log(tag, f"mode toggle '{target_tab}': {switched}")
                    await asyncio.sleep(0.6)
                except Exception as exc:  # noqa: BLE001
                    self._log(tag, f"mode toggle failed: {exc}")

                # Apply UI controls from options. Prefer explicit aspect/quality/
                # duration over the size-derived ratio.
                opts = job.options or {}
                ratio = opts.get("aspect_ratio") or opts.get("ratio") or opts.get("aspect") or self._size_to_ratio(opts.get("size") or "")
                if ratio:
                    try:
                        await self._set_aspect_ratio(page, ratio)
                        self._log(tag, f"aspect set to {ratio}")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"aspect set failed: {exc}")
                # Mode-specific toggles match Grok's prompt-bar buttons:
                #   IMAGE mode → 'Speed' / 'Quality' (radio pair)
                #   VIDEO mode → '480p' / '720p' (resolution radio) and
                #                '6s' / '10s' (duration radio)
                if not want_video:
                    quality = (opts.get("quality") or "").strip().lower()
                    if quality in ("speed", "quality"):
                        try:
                            await self._set_segmented(
                                page, "Speed" if quality == "speed" else "Quality"
                            )
                            self._log(tag, f"quality set to {quality}")
                        except Exception as exc:  # noqa: BLE001
                            self._log(tag, f"quality set failed: {exc}")
                else:
                    resolution = (opts.get("resolution") or "").strip().lower()
                    if resolution in ("480p", "720p"):
                        try:
                            await self._set_segmented(page, resolution)
                            self._log(tag, f"resolution set to {resolution}")
                        except Exception as exc:  # noqa: BLE001
                            self._log(tag, f"resolution set failed: {exc}")
                    if opts.get("duration"):
                        try:
                            await self._set_duration(page, int(opts["duration"]))
                            # Verify the duration radio is now aria-checked.
                            actual = await page.evaluate(
                                """() => {
                                    const r = Array.from(document.querySelectorAll(
                                        '[role=radio]'
                                    )).find(e => e.getAttribute('aria-checked') === 'true'
                                                 && /^\\d+s$/.test((e.innerText||'').trim()));
                                    return r ? r.innerText.trim() : null;
                                }"""
                            )
                            if actual and actual != f"{opts['duration']}s":
                                self._log(tag, f"WARN duration: requested {opts['duration']}s, Grok shows {actual}")
                            else:
                                self._log(tag, f"duration set to {opts['duration']}s (verified={actual})")
                        except Exception as exc:  # noqa: BLE001
                            self._log(tag, f"duration set failed: {exc}")

                prompt_el = await self._find_first(page, PROMPT_TEXTAREA, timeout_ms=15000)
                if not prompt_el:
                    prompt_el = await page.query_selector(".tiptap, .ProseMirror, [contenteditable='true']")
                if not prompt_el:
                    return JobResult(success=False, error_code="network_error",
                                     error_message="Prompt input not found — Grok UI changed",
                                     retryable=True)
                self._log(tag, "prompt input found")

                if job.attachments:
                    try:
                        await self._attach_files(page, job.attachments)
                        # Wait for the preview to render. Grok rebuilds the
                        # prompt-bar DOM after a successful upload, which
                        # invalidates our previous prompt_el handle.
                        await asyncio.sleep(2.5)
                        # Re-find the prompt input — old handle is now detached.
                        prompt_el = await self._find_first(
                            page, PROMPT_TEXTAREA, timeout_ms=10000,
                        ) or await page.query_selector(
                            ".tiptap.ProseMirror, .ProseMirror, [contenteditable='true']"
                        )
                        if not prompt_el:
                            return JobResult(
                                success=False, error_code="network_error",
                                error_message="Prompt input vanished after upload",
                                retryable=True,
                            )
                        self._log(tag, "input image attached, prompt re-resolved")
                    except Exception as exc:  # noqa: BLE001
                        return JobResult(success=False, error_code="network_error",
                                         error_message=f"Failed to attach input image: {exc}",
                                         retryable=True)

                seen_urls = await self._collect_image_urls(page)
                seen_video_urls = await self._collect_video_urls(page)

                # ProseMirror needs REAL keyboard events (keydown/press/up) to
                # trigger its keymap plugin and update the editor's internal
                # transaction state. Without that, even if pm.innerText shows
                # the text, React's onSubmit reads view.state.doc.textContent
                # (which stays empty) and the submit becomes a no-op.
                #
                # Strategy:
                #   1. click() to focus + place cursor inside the editor
                #   2. keyboard.type with small delay → fires real key events
                #      that ProseMirror's input plugin captures and dispatches
                #      transactions for
                #   3. verify by reading pm.innerText
                #   4. if still empty, fall back to paste event then insert_text
                try:
                    await prompt_el.click(timeout=2000)
                except Exception:  # noqa: BLE001
                    try:
                        await prompt_el.focus()
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(0.15)

                pm_text = ""
                # Strategy split by length:
                #   • >= 500 chars → PASTE first. keyboard.type at 12ms/char
                #     takes 55s for a 4.6k-char director-style prompt, and
                #     ProseMirror's IME composer fires onChange after every
                #     single key — by the time the loop ends, React's render
                #     queue is way behind and the Submit handler reads
                #     stale state, causing the "click registered but no
                #     /api/imagine call" symptom.
                #   • < 500 chars → keep keyboard.type. Short prompts are
                #     ~5s of typing and behave more naturally for Grok's
                #     anti-bot heuristics.
                # Both paths still run a paste-event fallback if the first
                # method left PM empty (e.g. paste blocked by CSP).
                use_paste_first = len(prompt_text) >= 500

                if use_paste_first:
                    # Single clipboard paste — one onChange in React,
                    # one transaction in ProseMirror. PM's clipboard
                    # plugin runs its own parser so the text shows up
                    # natively in the editor.
                    try:
                        await page.evaluate(
                            """(args) => {
                                const [el, text] = args;
                                if (!el) return false;
                                const dt = new DataTransfer();
                                dt.setData('text/plain', text);
                                const ev = new ClipboardEvent('paste', {
                                    bubbles: true, cancelable: true, clipboardData: dt,
                                });
                                try { el.focus(); } catch (e) {}
                                el.dispatchEvent(ev);
                                const inner = el.querySelector('[contenteditable="true"]') || el;
                                if (inner !== el) inner.dispatchEvent(ev);
                                return true;
                            }""",
                            [prompt_el, prompt_text],
                        )
                        await asyncio.sleep(0.4)
                        pm_text = await page.evaluate(
                            """() => {
                                const pm = document.querySelector('.tiptap.ProseMirror, .ProseMirror');
                                return pm ? (pm.innerText || '').trim() : '';
                            }"""
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"paste-first failed: {exc}")
                    self._log(tag, f"prompt pasted (len={len(prompt_text)}, pm_filled={bool(pm_text)})")

                if not pm_text:
                    # Either short prompt path, or paste-first didn't fill PM.
                    try:
                        await page.keyboard.type(prompt_text, delay=12)
                        await asyncio.sleep(0.3)
                        pm_text = await page.evaluate(
                            """() => {
                                const pm = document.querySelector('.tiptap.ProseMirror, .ProseMirror');
                                return pm ? (pm.innerText || '').trim() : '';
                            }"""
                        )
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"keyboard.type failed: {exc}")
                    self._log(tag, f"prompt typed via keyboard.type (len={len(prompt_text)}, pm_filled={bool(pm_text)})")

                # Fallback paste-event if BOTH paths above left PM empty.
                if not pm_text and not use_paste_first:
                    self._log(tag, "keyboard.type produced empty PM, trying paste event")
                    injected = await page.evaluate(
                        """(args) => {
                            const [el, text] = args;
                            if (!el) return false;
                            const dt = new DataTransfer();
                            dt.setData('text/plain', text);
                            const ev = new ClipboardEvent('paste', {
                                bubbles: true, cancelable: true, clipboardData: dt,
                            });
                            try { el.focus(); } catch (e) {}
                            const ok = el.dispatchEvent(ev);
                            const inner = el.querySelector('[contenteditable=\"true\"]') || el;
                            if (inner !== el) inner.dispatchEvent(ev);
                            return ok;
                        }""",
                        [prompt_el, prompt_text],
                    )
                    await asyncio.sleep(0.4)
                    pm_text = await page.evaluate(
                        """() => {
                            const pm = document.querySelector('.tiptap.ProseMirror, .ProseMirror');
                            return pm ? (pm.innerText || '').trim() : '';
                        }"""
                    )
                    self._log(tag, f"paste fallback: ok={injected}, pm_filled={bool(pm_text)}")

                if not pm_text:
                    # Detect logged-out state: page shows Sign in/Sign up AND
                    # editor is empty after a paste event → almost certainly
                    # session expired. Worker will mark profile need_login.
                    body_txt = await page.evaluate("() => (document.body.innerText || '').slice(0, 500)")
                    if "Sign in" in body_txt and "Sign up" in body_txt:
                        return JobResult(
                            success=False, error_code="cookie_expired",
                            error_message="Grok session expired — admin must Auto-login this profile.",
                        )
                    return JobResult(
                        success=False, error_code="network_error",
                        error_message="Failed to inject prompt into ProseMirror editor",
                        retryable=True,
                    )

                # Wait for Submit button to become enabled AND give React
                # state time to sync. PM's keymap fires onChange after a
                # short debounce (~150-300ms); without this wait the submit
                # handler may read an empty React state even when the DOM
                # has the typed text.
                #
                # Scale the initial wait with prompt length. Long prompts
                # (3000+ chars) saw React's controlled-input reconcilation
                # take 1-2s on slow VNCs; the old fixed 0.6s slept past
                # the DOM update but BEFORE React's onChange landed, so
                # the subsequent click fired with React state still empty.
                # Grok's onSubmit reads from React state, treats it as
                # blank, and silently no-ops the API call — manifesting
                # as the "submit clicked but generate_api_called=False"
                # symptom that fast-fails our worker.
                #
                # Heuristic: 0.6s baseline + 1 extra second per 2000 chars.
                # 4500-char prompts get ~3s, short prompts stay snappy.
                prompt_len = len(job.prompt or "")
                react_settle = 0.6 + min(4.0, prompt_len / 2000.0)
                await asyncio.sleep(react_settle)
                btn_enabled = False
                pm_state_ok = False
                for _ in range(40):
                    pm_state_ok = await page.evaluate(
                        f"""() => {{
                            // Check the actual PM textContent matches what
                            // we typed. If it does, React's state has the
                            // value too (PM mirrors state on every keystroke).
                            const pm = document.querySelector('.tiptap.ProseMirror, .ProseMirror, [contenteditable="true"]');
                            if (!pm) return false;
                            const txt = (pm.textContent || '').trim();
                            return txt.length >= {max(1, prompt_len - 10)};
                        }}"""
                    )
                    btn_enabled = await page.evaluate(
                        """() => {
                            const b = document.querySelector("button[aria-label='Submit']");
                            return !!b && !b.disabled && b.offsetParent !== null;
                        }"""
                    )
                    if btn_enabled and pm_state_ok:
                        break
                    await asyncio.sleep(0.25)
                if btn_enabled and not pm_state_ok:
                    self._log(tag, f"warn: btn enabled but PM textContent shorter than prompt ({prompt_len} chars expected)")

                # Submit chain ordered by trustedness — Grok's anti-bot
                # likely checks event.isTrusted in the React onSubmit:
                #   1. Real mouse click (CDP Input.dispatchMouseEvent →
                #      isTrusted=true). This is what a human would do.
                #   2. ElementHandle.click — also CDP-driven, trusted.
                #   3. Keyboard Enter on focused PM — trusted key event.
                #   4. JS .click() (last resort) — synthetic, isTrusted=false,
                #      Grok may ignore but try anyway.
                submitted = False

                if btn_enabled:
                    try:
                        btn = await page.query_selector("button[aria-label='Submit']:not([disabled])")
                        if btn:
                            bbox = await btn.bounding_box()
                            if bbox:
                                cx = bbox["x"] + bbox["width"] / 2
                                cy = bbox["y"] + bbox["height"] / 2
                                await page.mouse.click(cx, cy)
                                submitted = True
                                self._log(tag, f"submit via real mouse click @ ({cx:.0f},{cy:.0f})")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"mouse click failed: {exc}")

                if not submitted and btn_enabled:
                    try:
                        btn = await page.query_selector("button[aria-label='Submit']:not([disabled])")
                        if btn:
                            await btn.click(timeout=3000, force=True)
                            submitted = True
                            self._log(tag, "submit via ElementHandle force click")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"force click failed: {exc}")

                if not submitted:
                    try:
                        fresh_pm = await page.query_selector(
                            ".tiptap.ProseMirror, .ProseMirror, [contenteditable='true']"
                        )
                        if fresh_pm:
                            await fresh_pm.click(timeout=2000)
                            await asyncio.sleep(0.15)
                        await page.keyboard.press("Enter")
                        submitted = True
                        self._log(tag, "submit via Enter fallback")
                    except Exception as exc:  # noqa: BLE001
                        self._log(tag, f"Enter fallback failed: {exc}")

                if not submitted:
                    try:
                        submitted = await page.evaluate(
                            """() => {
                                const b = document.querySelector("button[aria-label='Submit']");
                                if (b && !b.disabled) { b.click(); return true; }
                                return false;
                            }"""
                        )
                        if submitted:
                            self._log(tag, "submit via JS click (last resort, may be ignored)")
                    except Exception:  # noqa: BLE001
                        pass

                if not submitted:
                    return JobResult(
                        success=False, error_code="timeout",
                        error_message=(
                            "Submit button never accepted click after 4 methods "
                            "(mouse.click, force click, Enter, JS click). "
                            "Likely Chromium overload — sẽ retry."
                        ),
                        retryable=True,
                    )

                # Track network calls right around submit so we can detect
                # silent shadow-bans (Grok's anti-abuse system that drops
                # generate calls but still fires telemetry, with no UI
                # feedback). Telemetry endpoints we expect regardless:
                #   /api/log_metric
                #   /_data/v1/a/t/?...
                # A real generate triggers calls to /api/conversation,
                # /api/imagine, or /rest/app-chat/* — none of which fire
                # when shadow-banned.
                generate_call_seen = {"hit": False}
                generate_patterns = re.compile(
                    r"/(api/(imagine|conversation|rest/app-chat)|rest/app-chat)/"
                )

                def _on_request(req):
                    try:
                        if generate_patterns.search(req.url):
                            generate_call_seen["hit"] = True
                    except Exception:  # noqa: BLE001
                        pass

                page.on("request", _on_request)

                navigated = False
                for _ in range(20):
                    await asyncio.sleep(1)
                    if "/imagine/post/" in (page.url or ""):
                        navigated = True
                        self._log(tag, f"navigated to {page.url}")
                        break
                if not navigated:
                    # Diagnostic only — log whether generate API was called.
                    # Do NOT fail the job on this signal alone (false positives
                    # are worse than waiting through the polling timeout).
                    self._log(
                        tag,
                        f"no /post/ nav after 20s, generate_api_called={generate_call_seen['hit']} — polling anyway",
                    )
                    # Body-text scan after submit-with-no-nav. Two failure
                    # modes Grok surfaces here, each maps to a different
                    # error_code so the worker handles them right:
                    #   • Content policy refusal → terminal (don't retry,
                    #     don't rotate — same prompt fails everywhere).
                    #   • Quota / rate limit → retryable + rotate.
                    body_tail = (await page.evaluate(
                        "() => (document.body.innerText || '').slice(-3000).toLowerCase()"
                    ))
                    # Content-policy refusal phrases (image + video gen).
                    # Grok's exact wording shifts month to month; this list
                    # is broad enough to catch the common refusal shapes.
                    moderation_phrases = [
                        "violates our content policy",
                        "violates the content policy",
                        "against our content policy",
                        "i can't generate", "i cannot generate",
                        "i'm not able to generate",
                        "i'm unable to generate",
                        "sorry, i can't", "sorry, i cannot",
                        "content policy", "content guidelines",
                        "policy violation",
                        "not allowed to", "i won't generate",
                        "request was flagged",
                        "moderated", "not safe for work",
                    ]
                    found_mod = next((h for h in moderation_phrases if h in body_tail), None)
                    if found_mod:
                        page.remove_listener("request", _on_request)
                        return JobResult(
                            success=False, error_code="content_moderated",
                            error_message=(
                                f"Grok từ chối prompt vì vi phạm content policy "
                                f"('{found_mod}'). Đổi prompt khác — không retry."
                            ),
                            retryable=False,
                        )
                    # Quota-text scan — only fail with rate_limited if Grok
                    # actually surfaced a throttle message. The sidebar
                    # 'Upgrade to SuperGrok' promo is permanent — exclude.
                    quota_phrases = [
                        "you've reached your", "you have reached your",
                        "daily limit reached", "daily limit has been reached",
                        # Grok's banner when account exhausted its quota
                        # — shows alongside "Upgrade to SuperGrok Heavy"
                        # upsell. The plain "Upgrade to SuperGrok" text
                        # alone lives permanently in the sidebar promo;
                        # match "rate limit reached" instead (only shows
                        # when actually rate-limited).
                        "rate limit reached",
                        "rate limit exceeded", "too many requests",
                        "try again in", "out of credits",
                        "quota exceeded", "please slow down", "monthly limit",
                    ]
                    found = next((h for h in quota_phrases if h in body_tail), None)
                    if found:
                        page.remove_listener("request", _on_request)
                        return JobResult(
                            success=False, error_code="rate_limited",
                            error_message=f"Grok rejected submit — '{found}'. Quota hết, không retry.",
                            retryable=False,
                        )
                # Keep the request listener attached during polling so the
                # watchdog can distinguish "Grok ignored the submit" (no
                # generation API call ever) from "Grok is generating but
                # slow" (call fired, just hasn't materialized DOM yet).
                # Listener gets cleaned up when the playwright context
                # tears down at function exit — no leak.

                # Poll BOTH images and videos simultaneously. For video jobs we
                # care about <video> elements with non-empty src; for image jobs
                # we care about <img> elements. Track stability separately.
                want_video = job.job_type == "video"
                # Adaptive timeout + stability window.
                #
                # Stability used to be 3s for image. That was too tight:
                # Grok now serves the result in two phases at the same
                # chat bubble — a ~24 KB blur placeholder appears within
                # 3-5s, then the final ~200 KB-1 MB image swaps in
                # 20-45s later. A 3s window let us commit on the blur
                # before the swap, and partners received the mosaic as
                # their "successful" result. 12s closes the swap reliably
                # without making fast jobs feel sluggish.
                #
                # Video stays at 6s — its update cycle is slower (only
                # one URL ever appears) so the old window was fine.
                timeout_ms = self.VIDEO_TIMEOUT_MS if want_video else self.IMAGE_TIMEOUT_MS
                deadline = time.monotonic() + timeout_ms / 1000
                STABILITY_SECONDS = 6.0 if want_video else float(
                    os.environ.get("GROK_IMAGE_STABLE_WINDOW_SEC", "6"),
                )
                last_change_at: float | None = None
                new_urls_set: set[str] = set()
                new_video_urls: set[str] = set()

                next_progress_log = time.monotonic() + 15
                # Fast-fail watchdog for "Grok ignored submit" — applies
                # to both image and video, with different head-rooms.
                #
                # Trigger: N seconds after submit with ZERO media on page
                # AND `generate_call_seen["hit"] == False` (network probe
                # never saw a /api/imagine or /api/conversations call).
                # That combo means Grok received the click but silently
                # dropped it — shadow-ban / CF block / statsig downgrade.
                # No amount of further polling helps; rotate now.
                #
                # Image: 45s (real images render in 15-30s, anything past
                #   45s with no API call is dead). Before this watchdog
                #   image jobs blocked the full 5-minute IMAGE_TIMEOUT
                #   and then surfaced as TargetClosedError when the page
                #   crashed mid-wait.
                # Video: 120s (genuine video renders take 60-150s under
                #   load, so we wait longer before declaring death).
                stuck_check_deadline = time.monotonic() + (120 if want_video else 45)

                while time.monotonic() < deadline:
                    cur_imgs = await self._collect_image_urls(page) - seen_urls
                    cur_vids = await self._collect_video_urls(page) - seen_video_urls

                    changed = False
                    if want_video and cur_vids and cur_vids != new_video_urls:
                        new_video_urls = cur_vids
                        changed = True
                    if cur_imgs and cur_imgs != new_urls_set:
                        new_urls_set = cur_imgs
                        changed = True

                    if changed:
                        last_change_at = time.monotonic()

                    target_set = new_video_urls if want_video else new_urls_set
                    if target_set and last_change_at and (time.monotonic() - last_change_at) >= STABILITY_SECONDS:
                        # Got media + stable for STABILITY window → done.
                        break

                    if time.monotonic() >= next_progress_log:
                        next_progress_log = time.monotonic() + 30
                        self._log(tag, f"polling… imgs={len(new_urls_set)} vids={len(new_video_urls)} elapsed={int(time.monotonic() - (deadline - timeout_ms/1000))}s")

                    # Watchdog: post-submit + N seconds + still no media
                    # of the wanted type. Two interpretations:
                    #   (a) generate_call_seen.hit == False → Grok never
                    #       hit the generation API since submit. Could be
                    #       silent throttle / shadow-ban / CF block.
                    #       Fast-fail with rate_limited → worker rotates.
                    #   (b) generate_call_seen.hit == True → generation
                    #       API DID fire; Grok just hasn't rendered the
                    #       result yet. DON'T fail — keep polling to the
                    #       full deadline.
                    # Watchdog only triggers once (sets deadline=None).
                    media_seen = bool(new_video_urls if want_video else new_urls_set)
                    if (
                        stuck_check_deadline is not None
                        and time.monotonic() >= stuck_check_deadline
                        and not media_seen
                    ):
                        stuck_check_deadline = None  # only check once
                        watchdog_secs = 120 if want_video else 45
                        if not generate_call_seen["hit"]:
                            self._log(tag, f"fast-fail: {watchdog_secs}s post-submit, no media, no generate API call — Grok ignored submit")
                            return JobResult(
                                success=False, error_code="rate_limited",
                                error_message="Grok không nhận submit (không có /api/imagine call) — profile có thể bị throttle. Rotating.",
                                retryable=True,
                            )
                        else:
                            self._log(tag, f"slow gen: {watchdog_secs}s post-submit but generate API call seen — waiting full {timeout_ms // 1000}s")

                    # In-loop text scanning was removed: it generated too many
                    # false-positives by matching phrases that appear in chat
                    # history sidebar, tooltips, or partial streaming responses.
                    # If Grok actually throttles, the polling loop will time
                    # out naturally with a clearer 'No new media within timeout'
                    # which the worker retries with backoff. Genuine quota text
                    # is still scanned BEFORE polling starts (right after submit
                    # if the URL didn't navigate to /post/).

                    # Tight poll loop — 1s catches new image URLs sooner.
                    await asyncio.sleep(1)

                if not new_urls_set and not new_video_urls:
                    err_msg = "No new media within timeout"
                    if want_video:
                        err_msg += " (Grok video may need Pro/Heavy subscription)"
                    return JobResult(success=False, error_code="timeout",
                                     error_message=err_msg, retryable=True)

                # Video preset (Fun / Custom / Spicy) — Grok shows these as a
                # row of buttons under the rendered video. Clicking one
                # triggers a regenerate at the new preset; we wait for the
                # NEW URL set then keep ONLY the regenerated videos.
                mode = (opts.get("mode") or "").strip().lower()
                if want_video and new_video_urls and mode and mode != "normal":
                    label_map = {
                        "fun":    ["Fun"],
                        "custom": ["Custom"],
                        "spicy":  ["Spicy", "Spicy mode", "18+"],
                    }
                    targets = label_map.get(mode, [])
                    clicked = False
                    if targets:
                        try:
                            clicked = await page.evaluate(
                                """(targets) => {
                                    const visible = (e) => e && e.offsetParent !== null;
                                    const all = Array.from(document.querySelectorAll(
                                      'button, [role=button], [role=tab], [role=radio]'
                                    ));
                                    for (const t of targets) {
                                      const m = all.find(e => (e.innerText || '').trim() === t && visible(e));
                                      if (m && !m.disabled) { m.click(); return t; }
                                    }
                                    return null;
                                }""",
                                targets,
                            )
                        except Exception:  # noqa: BLE001
                            clicked = False
                    if clicked:
                        self._log(tag, f"video preset '{clicked}' clicked, awaiting regen")
                        before = set(new_video_urls)
                        regen_deadline = time.monotonic() + 180  # 3 min cap
                        regen_stable: float | None = None
                        regen_set: set[str] = set()
                        while time.monotonic() < regen_deadline:
                            cur = await self._collect_video_urls(page)
                            new = cur - before - seen_video_urls
                            if new and new != regen_set:
                                regen_set = new
                                regen_stable = time.monotonic()
                            if regen_set and regen_stable and (time.monotonic() - regen_stable) >= 6.0:
                                break
                            await asyncio.sleep(2)
                        if regen_set:
                            self._log(tag, f"preset regen produced {len(regen_set)} new video(s)")
                            new_video_urls = regen_set  # keep ONLY the preset version
                        else:
                            self._log(tag, f"preset '{mode}' clicked but no new video — falling back to original")
                    else:
                        self._log(tag, f"preset '{mode}' button not available (account tier?)")

                cookies = await context.cookies()
                jar = httpx.Cookies()
                for c in cookies:
                    jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
                ua = await page.evaluate("() => navigator.userAgent")

                downloaded: list[ResultFile] = []
                async with httpx.AsyncClient(cookies=jar, follow_redirects=True, timeout=120) as client:
                    # Download videos first (priority for video jobs)
                    for idx, vurl in enumerate(sorted(new_video_urls)):
                        try:
                            resp = await client.get(vurl, headers={"Referer": target_url, "User-Agent": ua})
                            if resp.status_code != 200:
                                continue
                            mime = resp.headers.get("content-type", "video/mp4").split(";")[0].strip()
                            ext = "mp4" if "mp4" in mime else "webm"
                            downloaded.append(ResultFile(
                                bytes=resp.content,
                                name=f"grok_video_{int(time.time())}_{idx + 1}.{ext}",
                                mime=mime,
                                source_url=vurl,
                            ))
                        except Exception:  # noqa: BLE001
                            continue
                    # Then images (could be input preview echoed back, or output)
                    for idx, img_url in enumerate(sorted(new_urls_set)):
                        try:
                            if img_url.startswith("data:image/"):
                                import base64 as _base64
                                header, base64_data = img_url.split(",", 1)
                                mime = header.split(";", 1)[0].replace("data:", "")
                                ext = self._ext_for_mime(mime)
                                img_bytes = _base64.b64decode(base64_data)
                                downloaded.append(ResultFile(
                                    bytes=img_bytes,
                                    name=f"grok_image_{int(time.time())}_{idx + 1}.{ext}",
                                    mime=mime,
                                    source_url="data:image/...",
                                ))
                            else:
                                resp = await client.get(img_url, headers={"Referer": target_url, "User-Agent": ua})
                                if resp.status_code != 200:
                                    continue
                                mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
                                ext = self._ext_for_mime(mime)
                                downloaded.append(ResultFile(
                                    bytes=resp.content,
                                    name=f"grok_image_{int(time.time())}_{idx + 1}.{ext}",
                                    mime=mime,
                                    source_url=img_url,
                                ))
                        except Exception:  # noqa: BLE001
                            continue

                if not downloaded:
                    return JobResult(success=False, error_code="network_error",
                                     error_message="All media downloads failed",
                                     retryable=True)

                return JobResult(
                    success=True,
                    files=downloaded,
                    extra={
                        "image_count": len(new_urls_set),
                        "video_count": len(new_video_urls),
                        "source_urls": list(new_urls_set | new_video_urls),
                    },
                )

        except PWTimeout as e:
            return JobResult(success=False, error_code="timeout",
                             error_message=str(e), retryable=True)
        except Exception as exc:  # noqa: BLE001
            return JobResult(success=False, error_code="unknown_error",
                             error_message=f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            # Always close the per-job tab so memory is released — keeps Chromium
            # available for next job. The browser object itself is just a CDP
            # connection; closing it doesn't kill the underlying Chromium.
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            # Also sweep any stray result tabs that this job's submit-then-
            # result navigation may have spawned. Each lingering tab eats
            # ~150-300MB Chromium RAM.
            #
            # Grok URL patterns we close:
            #   /imagine/post/<id>         — legacy result URL (pre Q2 2026)
            #   /project/<id>?chat=<id>    — current shape after a job
            #   /chat/<id>, /share/<id>    — direct chat / share links
            #
            # Sibling jobs' /imagine prompt tabs DON'T match any of these
            # until they themselves finish, so this is collision-safe with
            # concurrent workers on the same profile.
            _STALE = ("/imagine/post/", "?chat=", "/chat/", "/share/")
            try:
                if 'context' in locals() and context is not None:
                    for p in list(context.pages):
                        if p is page:
                            continue
                        u = (p.url or "")
                        if any(s in u for s in _STALE):
                            try:
                                await asyncio.wait_for(p.close(), timeout=3)
                                self._log(tag, f"GC closed result tab: …{u[-40:]}")
                            except Exception:  # noqa: BLE001
                                pass
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _collect_image_urls(page) -> set[str]:
        # Grok's generated-output URLs as of late 2025/early 2026:
        #   assets.grok.com/users/<userId>/generated/<jobId>/image.(jpg|png|webp)
        # Older URLs we still keep around as fallbacks:
        #   imagine-public.x.ai/imagine-public/images/<uuid>.(jpg|png)
        #   imgen.<...>
        # The user's saved-gallery / favorites and uploaded preview live at:
        #   assets.grok.com/users/<userId>/<favId>/content (no /generated/ segment)
        # We disambiguate via the URL path itself + the prompt-bar form
        # container (which holds the 'Most recent favorite' thumbnail).
        urls = await page.evaluate(
            """() => {
                const out = new Set();
                const generated = [
                    /assets\\.grok\\.com\\/users\\/[^/]+\\/generated\\//,
                    /imagine-public\\.x\\.ai\\/imagine-public\\/images\\//,
                    /imgen\\./,
                ];
                const altExclude = [
                    /favorite/i,
                    /avatar/i, /emoji/i,
                ];
                const urlExclude = [
                    /cookielaw|onetrust/i,
                    /share-images\\//,
                    /share-videos\\/.*thumbnail/,
                    /\\/content(\\?|$)/,  // favorites/uploads use /content, generations use /image.{ext}
                ];
                document.querySelectorAll('img').forEach(i => {
                    const s = i.src || '';
                    const isBase64 = s.startsWith('data:image/');
                    if (!s.startsWith('http') && !isBase64) return;
                    if (!isBase64) {
                        if (urlExclude.some(rx => rx.test(s))) return;
                        if (!generated.some(rx => rx.test(s))) return;
                    }
                    if (altExclude.some(rx => rx.test(i.alt || ''))) return;
                    // Skip the prompt-bar attach preview (always inside <form>)
                    // and the 'Most recent favorite' thumbnail (inside <form>
                    // with role=button). Real result images sit inside the
                    // chat <article>/<main> grid; some are wrapped in clickable
                    // <button> thumbnails so we DO accept those as long as
                    // they're not inside a <form>.
                    if (i.closest('form, aside, header, nav')) return;
                    out.add(s);
                });
                return Array.from(out);
            }"""
        )
        return set(urls)

    @staticmethod
    async def _collect_video_urls(page) -> set[str]:
        urls = await page.evaluate(
            """() => {
                const out = new Set();
                document.querySelectorAll('video, video source').forEach(v => {
                    const s = v.src || (v.currentSrc) || '';
                    if (s.startsWith('http')) out.add(s);
                });
                document.querySelectorAll('a[href*=".mp4"], a[href*=".webm"]').forEach(a => {
                    if (a.href.startsWith('http')) out.add(a.href);
                });
                return Array.from(out);
            }"""
        )
        return set(urls)

    @staticmethod
    def _size_to_ratio(size: str) -> str | None:
        """Convert 1024x576 → '16:9', 1024x1024 → '1:1', etc."""
        try:
            w, h = (int(x) for x in size.split("x"))
        except (ValueError, ZeroDivisionError):
            return None
        candidates = {
            (1, 1): "1:1",
            (16, 9): "16:9", (9, 16): "9:16",
            (4, 3): "4:3", (3, 4): "3:4",
            (3, 2): "3:2", (2, 3): "2:3",
        }
        actual = w / h
        best = None
        best_diff = 999.0
        for (rw, rh), name in candidates.items():
            diff = abs((rw / rh) - actual)
            if diff < best_diff:
                best_diff = diff
                best = name
        return best

    @staticmethod
    async def _set_aspect_ratio(page, ratio: str) -> None:
        """Open Aspect Ratio popover, click target, then ALWAYS close popover.

        The popover (radix-popper) intercepts pointer events when open. If we
        leave it open, subsequent clicks (Submit, tiptap focus) fail with
        "subtree intercepts pointer events". Press Escape unconditionally.
        """
        btn = await page.query_selector("button[aria-label='Aspect Ratio']")
        if not btn:
            return
        try:
            await btn.click()
            await asyncio.sleep(0.7)
            # Map our short ratios to Grok's likely option labels.
            label_candidates = {
                "1:1": ["1:1", "Square"],
                "16:9": ["16:9", "Widescreen", "Landscape"],
                "9:16": ["9:16", "Portrait", "Vertical"],
                "4:3": ["4:3"],
                "3:4": ["3:4"],
                "3:2": ["3:2"],
                "2:3": ["2:3"],
            }
            targets = label_candidates.get(ratio, [ratio])
            await page.evaluate(
                """(targets) => {
                    const els = Array.from(document.querySelectorAll('[role=menuitem], [role=option], button, span'));
                    for (const t of targets) {
                        const m = els.find(e => (e.innerText || '').trim() === t && e.offsetParent !== null);
                        if (m) { m.click(); return true; }
                    }
                    return false;
                }""",
                targets,
            )
            await asyncio.sleep(0.3)
        finally:
            # Always dismiss any lingering popover so it doesn't block the page.
            try:
                await page.keyboard.press("Escape")
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _click_by_label(page, candidates: list[str]) -> str | None:
        """Click the first visible button/radio whose innerText exactly
        matches one of `candidates`. Uses Playwright's real CDP click
        (trusted event) so React state managers actually fire — JS
        `b.click()` produces isTrusted=false which Grok's radio handlers
        sometimes ignore, leaving e.g. duration stuck on the previous
        selection (the '10s clicked but Grok rendered 6s' bug).
        """
        # Find the matching element from the page first
        try:
            handle = await page.evaluate_handle(
                """(targets) => {
                    const visible = (el) => el && el.offsetParent !== null;
                    const els = Array.from(document.querySelectorAll(
                        'button, [role=tab], [role=radio], [role=menuitem], [role=option]'
                    ));
                    for (const t of targets) {
                        const m = els.find(e => (e.innerText || '').trim() === t && visible(e));
                        if (m) return m;
                    }
                    return null;
                }""",
                candidates,
            )
        except Exception:  # noqa: BLE001
            return None
        try:
            el = handle.as_element()
            if el is None:
                return None
            # bbox + real mouse click → trusted event
            bbox = await el.bounding_box()
            if bbox:
                cx = bbox["x"] + bbox["width"] / 2
                cy = bbox["y"] + bbox["height"] / 2
                await page.mouse.click(cx, cy)
                await asyncio.sleep(0.4)
                # Read the matched label back so we can log what we hit
                matched = await el.evaluate("(e) => (e.innerText || '').trim()")
                return matched
            # Fallback: ElementHandle.click (still trusted via CDP)
            await el.click(timeout=3000, force=True)
            await asyncio.sleep(0.4)
            return await el.evaluate("(e) => (e.innerText || '').trim()")
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _set_segmented(page, label: str) -> None:
        await GrokProvider._click_by_label(page, [label])

    @staticmethod
    async def _set_duration(page, seconds: int) -> None:
        # Grok's video bar uses '6s' / '10s' exactly. Keep older label
        # variants as fallbacks in case of UI revisions.
        await GrokProvider._click_by_label(
            page,
            [f"{seconds}s", f"{seconds} sec", f"{seconds} seconds"],
        )

    @staticmethod
    async def _attach_files(page, attachments: list) -> None:
        """Attach reference images on Grok Imagine.

        Grok keeps a hidden <input type='file' name='files' accept='image/*'>
        inside the prompt-bar form, plus a visible 'Upload' button (legacy
        builds called it 'Attach') that opens the OS file chooser. We try the
        direct hidden-input path first because it's resilient to UI shuffles.
        """
        import os
        import tempfile

        tmp_paths: list[str] = []
        for att in attachments:
            fd, path = tempfile.mkstemp(prefix="grokflow_in_", suffix=f"_{att.name}")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(att.bytes)
                tmp_paths.append(path)
            except Exception:
                os.close(fd)
                raise

        # Path A: hidden input set_input_files. Works for current Grok layout
        # (input is class='hidden' but visible to Playwright).
        last_err: Exception | None = None
        for sel in (
            "input[type='file'][name='files']",
            "input[type='file'][accept*='image']",
            "input[type='file']",
        ):
            try:
                el = await page.query_selector(sel)
                if el:
                    await el.set_input_files(tmp_paths)
                    return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue

        # Path B: trigger file chooser by clicking Upload/Attach button.
        for label in ("Upload", "Attach", "Add image", "Add file"):
            btn = await page.query_selector(f"button[aria-label='{label}']")
            if not btn:
                continue
            try:
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                chooser = await fc_info.value
                await chooser.set_files(tmp_paths)
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                continue

        raise RuntimeError(
            f"No upload affordance found on Grok UI (last error: {last_err})"
        )

    @staticmethod
    def _compose_prompt(job: JobInput) -> str:
        opts = job.options or {}
        prompt = job.prompt.strip()
        hints = []
        size = opts.get("size")
        if size and not re.search(r"\d+x\d+|\d+:\d+", prompt):
            try:
                w, h = (int(x) for x in size.split("x"))
                if abs(w - h) < 10:
                    hints.append("square 1:1 aspect")
                elif w > h:
                    hints.append("16:9 landscape")
                else:
                    hints.append("9:16 portrait")
            except (ValueError, ZeroDivisionError):
                hints.append(f"size {size}")
        style = opts.get("style")
        if style and style != "natural":
            hints.append(f"{style} style")
        n = opts.get("n", 1)
        if n and n > 1:
            hints.append(f"generate {n} variations")
        if hints:
            prompt = f"{prompt} ({', '.join(hints)})"
        return prompt

    @staticmethod
    def _ext_for_mime(mime: str) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
        }.get(mime.lower(), "bin")

    @staticmethod
    async def _find_first(page, selectors: list[str], timeout_ms: int):
        for sel in selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=timeout_ms, state="visible")
                if el:
                    return el
            except PWTimeout:
                continue
        return None
