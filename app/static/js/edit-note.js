// ════════════════════════════════════════
// EDIT NOTE — CHAR COUNTER
// Moved out of an inline <script> in
// messages/edit_note.html so script-src needs no
// 'unsafe-inline'.
// ════════════════════════════════════════
(function () {
    const textarea = document.getElementById("note-textarea");
    const counter  = document.getElementById("char-count");
    if (!textarea || !counter) return;

    const MAX_CHARS = 400;   // matches Length(max=400) in app/forms/message_forms.py:95

    function updateCount() {
        const len = textarea.value.length;
        counter.textContent = `${len} / ${MAX_CHARS}`;

        if (len > MAX_CHARS * 0.9) {
            counter.classList.add("warn");
        } else {
            counter.classList.remove("warn");
        }
    }

    updateCount();
    textarea.addEventListener("input", updateCount);
})();
