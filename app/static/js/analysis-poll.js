// ════════════════════════════════════════
// ANALYSIS POLLING
//
// Was inline in messages/show_message.html. Extracted so the demo replays the
// SAME poller rather than a lookalike — a separate mock would be demonstrating
// something that is not the app — and so neither page carries an inline script
// (see the Talisman/CSP TODO in app/__init__.py).
//
// Configured entirely from #analysis-status, which the shared
// analysis_status_banner() macro renders:
//
//   data-status-url  required. Endpoint returning {"status": "..."}. Absent →
//                    this script does nothing, which is how every completed page
//                    opts out without a guard.
//   data-poll-ms     optional. Poll interval; defaults to the production 3000.
//                    The demo sets 1000 so its 5-second replay reveals on the
//                    fifth poll rather than overshooting to 6s on the second.
//   data-reload-url  optional. Where to go on a terminal status. The real page
//                    omits it and reloads in place, because the server will then
//                    render the finished result from the database. The demo has
//                    no such state — its result lives at a different URL — so it
//                    names that URL here.
// ════════════════════════════════════════
(function () {
    "use strict";

    var el = document.getElementById("analysis-status");
    if (!el || !el.dataset.statusUrl) return;

    // Exact value strings — must match MessageStatus.value + the status endpoint.
    var STATUS_URL   = el.dataset.statusUrl;
    var POLL_MS      = parseInt(el.dataset.pollMs, 10) || 3000;
    var RELOAD_URL   = el.dataset.reloadUrl || null;
    var MAX_ATTEMPTS = 100;            // ~5 minutes at the default interval, then stop
    var attempts     = 0;

    // On COMPLETED or FAILED, a single full navigation renders the finished page
    // (or the FAILED banner) through the normal server template.
    function finish() {
        if (RELOAD_URL) {
            window.location.assign(RELOAD_URL);
        } else {
            window.location.reload();
        }
    }

    async function poll() {
        attempts += 1;
        try {
            const res = await fetch(STATUS_URL, { headers: { "Accept": "application/json" } });
            if (res.ok) {
                const data = await res.json();
                if (data.status === "completed" || data.status === "failed") {
                    finish();
                    return;
                }
            }
        } catch (e) { /* transient network error — keep polling */ }

        if (attempts < MAX_ATTEMPTS) {
            setTimeout(poll, POLL_MS);
        } else {
            el.textContent = "Still analyzing… refresh the page to check again.";
        }
    }
    setTimeout(poll, POLL_MS);
})();
