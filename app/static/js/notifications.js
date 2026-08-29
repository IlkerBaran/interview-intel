// ════════════════════════════════════════
// NOTIFICATIONS PAGE
// Loaded only on /notifications. Handles:
//   - relative timestamps (once, on load — no ticking)
//   - mark single notification read (AJAX)
//   - mark all read (AJAX, batch)
//   - delete single notification (AJAX)
//   - clear all READ notifications (AJAX, batch)
//   - "Load more" pagination (AJAX HTML fragment, keyset cursor)
//   - navbar bell badge sync from server unread_count
//
// Counts are never computed here. Every mutation returns the authoritative
// unread_count and read_count recomputed after its commit, so the badge and
// the "Clear read" button stay correct without this file tracking state or
// guessing whether a deleted row happened to be unread.
// ════════════════════════════════════════
(function () {
    const root = document.getElementById("notif-root");
    if (!root) return;

    const csrf         = root.dataset.csrf;
    const moreUrl      = root.dataset.moreUrl;
    const list         = document.getElementById("notif-list");
    const markAllBtn   = document.getElementById("mark-all-btn");
    const clearReadBtn = document.getElementById("clear-read-btn");
    const loadMoreBtn  = document.getElementById("load-more-btn");

    // ── Relative time ──────────────────────
    // Converts <time datetime="ISO"> text to "2 hours ago".
    // Leaves the absolute fallback in place if parsing fails.
    function relativeLabel(iso) {
        const then = new Date(iso);
        if (isNaN(then.getTime())) return null;

        let secs = (Date.now() - then.getTime()) / 1000;
        if (secs < 0) secs = 0;

        if (secs < 45) return "just now";
        const mins = secs / 60;
        if (mins < 60) {
            const m = Math.floor(mins);
            return m + " minute" + (m !== 1 ? "s" : "") + " ago";
        }
        const hrs = mins / 60;
        if (hrs < 24) {
            const h = Math.floor(hrs);
            return h + " hour" + (h !== 1 ? "s" : "") + " ago";
        }
        const days = hrs / 24;
        if (days < 7) {
            const d = Math.floor(days);
            return d + " day" + (d !== 1 ? "s" : "") + " ago";
        }
        if (days < 28) {
            const w = Math.floor(days / 7);
            return w + " week" + (w !== 1 ? "s" : "") + " ago";
        }
        return null; // keep absolute fallback for old items
    }

    function applyRelativeTime(scope) {
        // Localize absolute fallbacks first — the server renders them in UTC, and
        // items older than the relative window keep that text. Scoped, so this
        // also covers fragments inserted by "Load more".
        if (window.IITime) window.IITime.apply(scope);

        scope.querySelectorAll("time[datetime]").forEach(el => {
            if (el.dataset.relativized) return;
            const label = relativeLabel(el.getAttribute("datetime"));
            if (label) el.textContent = label;
            el.dataset.relativized = "1";
        });
    }

    // ── Navbar badge sync ──────────────────
    function syncBadge(count) {
        const bell = document.querySelector(".nav-bell");
        if (!bell) return;
        let badge = bell.querySelector(".nav-bell-badge");
        if (count > 0) {
            if (!badge) {
                badge = document.createElement("span");
                badge.className = "nav-bell-badge";
                badge.style.cssText = "position:absolute;top:-4px;right:-8px;" +
                    "background:#c0392b;color:#fff;font-size:11px;font-weight:600;" +
                    "line-height:1;padding:2px 5px;border-radius:10px;min-width:16px;" +
                    "text-align:center;";
                bell.appendChild(badge);
            }
            badge.textContent = count > 99 ? "99+" : count;
        } else if (badge) {
            badge.remove();
        }
    }

    // ── "Clear read" visibility ────────────
    // Driven by the server's read_count, not by counting loaded rows: read
    // notifications can exist on pages "Load more" has not fetched yet.
    function syncClearReadBtn(count) {
        if (!clearReadBtn) return;
        clearReadBtn.style.display = count > 0 ? "" : "none";
    }

    // ── AJAX helpers ───────────────────────
    function postJSON(url, opts) {
        return fetch(url, Object.assign({
            method: "POST",
            headers: { "X-CSRFToken": csrf },
            credentials: "same-origin"
        }, opts || {})).then(r => r.ok ? r.json() : Promise.reject(r));
    }

    function getJSON(url) {
        return fetch(url, {
            credentials: "same-origin"
        }).then(r => r.ok ? r.json() : Promise.reject(r));
    }

    // ── Mark single read (delegated → also covers appended rows) ──
    // No preventDefault: a card with an href navigates to its source message, a
    // card without one stays put. keepalive is what makes the first case work —
    // without it the browser cancels the in-flight POST on unload and the
    // notification silently stays unread. sendBeacon is not an option here: it
    // cannot set X-CSRFToken, which mark_read requires.
    list.addEventListener("click", function (e) {
        const card = e.target.closest(".notif-card");
        if (!card || !card.classList.contains("unread")) return;

        postJSON(card.dataset.url, { keepalive: true }).then(data => {
            if (!data.ok) return;
            card.classList.remove("unread");
            card.classList.add("read");
            syncBadge(data.unread_count);
            if (data.unread_count === 0 && markAllBtn) {
                markAllBtn.style.display = "none";
            }
            // This row just became read, so "Clear read" may need to appear.
            syncClearReadBtn(data.read_count);
        }).catch(() => {});
    });

    // ── Delete single (delegated → also covers appended rows) ──
    // The button is a SIBLING of the card, so the mark-read handler above
    // already returns early for these clicks — its closest(".notif-card") is
    // null. preventDefault is still belt-and-braces against a future wrapper
    // that navigates.
    list.addEventListener("click", function (e) {
        const btn = e.target.closest(".notif-delete");
        if (!btn) return;

        e.preventDefault();
        e.stopPropagation();

        postJSON(btn.dataset.deleteUrl).then(data => {
            if (!data.ok) return;

            const row = btn.closest(".notif-row");
            if (row) row.remove();

            // Deleting an unread row lowers the badge; deleting a read row
            // leaves it alone. Both cases are covered by using the server's
            // post-commit count rather than adjusting it here.
            syncBadge(data.unread_count);
            if (data.unread_count === 0 && markAllBtn) {
                markAllBtn.style.display = "none";
            }
            syncClearReadBtn(data.read_count);
        }).catch(() => {});
    });

    // ── Mark all read ──────────────────────
    if (markAllBtn) {
        markAllBtn.addEventListener("click", function () {
            postJSON(markAllBtn.dataset.url).then(data => {
                if (!data.ok) return;
                list.querySelectorAll(".notif-card.unread").forEach(c => {
                    c.classList.remove("unread");
                    c.classList.add("read");
                });
                syncBadge(0);
                markAllBtn.style.display = "none";
                // Everything is read now, so "Clear read" becomes available.
                syncClearReadBtn(data.read_count);
            }).catch(() => {});
        });
    }

    // ── Clear read (batch) ─────────────────
    // Removes only rows whose card is already .read. Unread rows are never
    // touched server-side, so nothing the user has not seen can be lost and
    // the bell count does not move.
    if (clearReadBtn) {
        clearReadBtn.addEventListener("click", function () {
            postJSON(clearReadBtn.dataset.url).then(data => {
                if (!data.ok) return;

                list.querySelectorAll(".notif-card.read").forEach(card => {
                    const row = card.closest(".notif-row");
                    if (row) row.remove();
                });

                syncBadge(data.unread_count);
                syncClearReadBtn(data.read_count);
            }).catch(() => {});
        });
    }

    // ── Load more (keyset cursor, no-space "ISO,id" — passed through) ──
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener("click", function () {
            const cursor = loadMoreBtn.dataset.nextCursor;
            if (!cursor) { loadMoreBtn.style.display = "none"; return; }
            loadMoreBtn.disabled = true;

            getJSON(moreUrl + "?cursor=" + encodeURIComponent(cursor)).then(data => {
                if (!data.ok) return;

                const tmp = document.createElement("div");
                tmp.innerHTML = data.html;
                applyRelativeTime(tmp);
                while (tmp.firstElementChild) {
                    list.appendChild(tmp.firstElementChild);
                }

                if (data.has_more && data.next_cursor) {
                    loadMoreBtn.dataset.nextCursor = data.next_cursor;
                    loadMoreBtn.disabled = false;
                } else {
                    loadMoreBtn.style.display = "none";
                }
            }).catch(() => {
                loadMoreBtn.disabled = false;
            });
        });
    }

    // ── Initial pass ───────────────────────
    applyRelativeTime(document);
})();
