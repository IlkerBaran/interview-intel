// ════════════════════════════════════════
// NEW MESSAGE FORM — CHAR COUNTER
// Moved out of an inline <script> in
// messages/new_message.html so script-src needs no
// 'unsafe-inline'.
// ════════════════════════════════════════
(function () {
    const textarea  = document.getElementById("raw_text");
    const counter   = document.getElementById("char-count");
    const submitBtn = document.getElementById("submit-btn");

    // The form is not rendered once the analysis quota is spent, so these can
    // legitimately be absent. A Jinja {% if %} used to keep the inline script
    // from rendering at all; this guard replaces it.
    if (!textarea || !counter || !submitBtn) return;

    const WARN_AT   = 50;   // warn if below this
    const MIN_CHARS = 20;   // must match form validator

    function updateCount() {
        const len = textarea.value.length;
        counter.textContent = len === 1 ? "1 character" : `${len} characters`;

        // warn color if too short
        if (len > 0 && len < WARN_AT) {
            counter.classList.add("warn");
        } else {
            counter.classList.remove("warn");
        }

        // disable submit if below minimum
        submitBtn.disabled = len < MIN_CHARS;
    }

    // run on load in case of pre-filled value
    updateCount();
    textarea.addEventListener("input", updateCount);

    // disable submit on form submit to prevent double submission
    textarea.closest("form").addEventListener("submit", function () {
        submitBtn.disabled = true;
        submitBtn.textContent = "Analyzing...";
    });
})();
