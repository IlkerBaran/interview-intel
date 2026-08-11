// ════════════════════════════════════════
// LOCAL TIME RENDERING
// Loaded on every page from base.html.
//
// Server timestamps are UTC. The localtime() Jinja macro emits them as
//   <time data-localtime="PRESET" datetime="2026-08-10T05:50:16.423525Z">…</time>
// with a server-rendered UTC fallback as the element's text. This rewrites that
// text into the viewer's own timezone, whatever it is.
//
// The trailing Z on the datetime attribute is load-bearing: without a timezone
// designator the browser parses a date-time string as LOCAL, silently shifting
// every value by the viewer's offset. Never emit .isoformat() straight into
// markup — use the utc_iso filter.
//
// Exposes window.IITime so scripts that own their own <time> elements
// (notifications.js) can reuse the same formatting.
// ════════════════════════════════════════
(function () {
    "use strict";

    var PRESETS = {
        datetime:       { year: "numeric", month: "long",  day: "numeric",
                          hour: "numeric", minute: "2-digit" },
        datetime_short: { year: "numeric", month: "short", day: "numeric",
                          hour: "2-digit", minute: "2-digit", hour12: false },
        date:           { year: "numeric", month: "short", day: "numeric" },
        date_short:     { month: "short",  day: "numeric" },
        month_day_year: { year: "numeric", month: "long",  day: "numeric" }
    };

    // undefined locale => the browser's own locale. Do not hardcode "en-US".
    function format(iso, preset) {
        if (!iso) return null;
        var d = new Date(iso);
        if (isNaN(d.getTime())) return null;
        try {
            return new Intl.DateTimeFormat(
                undefined, PRESETS[preset] || PRESETS.date
            ).format(d);
        } catch (e) {
            return null;   // Intl unavailable — keep the labelled UTC fallback
        }
    }

    // Idempotent via data-localized, and scoped, so AJAX-inserted fragments can
    // be localized on arrival without re-touching what is already on the page.
    function apply(scope) {
        (scope || document)
            .querySelectorAll("time[data-localtime]")
            .forEach(function (el) {
                if (el.dataset.localized) return;
                var text = format(el.getAttribute("datetime"), el.dataset.localtime);
                if (text) el.textContent = text;
                el.dataset.localized = "1";
            });
    }

    window.IITime = { format: format, apply: apply };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { apply(document); });
    } else {
        apply(document);
    }
})();
