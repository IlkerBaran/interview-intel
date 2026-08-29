// ════════════════════════════════════════
// MESSAGE FILTER BAR
// Moved out of an inline <script> in
// messages/index.html so script-src needs no
// 'unsafe-inline'.
//
// Guarded on the two selects, NOT on .message-card:
// show_archived_messages.html also renders
// .message-card but has no filter bar.
// ════════════════════════════════════════
(function () {
    const categorySelect = document.getElementById("category-filter");
    const urgencySelect  = document.getElementById("urgency-filter");
    const countEl        = document.getElementById("filter-count");
    if (!categorySelect || !urgencySelect || !countEl) return;

    const cards = document.querySelectorAll(".message-card");

    function filterMessages() {
        const categoryFilter = categorySelect.value;
        const urgencyFilter  = urgencySelect.value;

        let visible = 0;

        cards.forEach(card => {
            const catMatch = !categoryFilter || card.dataset.category === categoryFilter;
            const urgMatch = !urgencyFilter  || card.dataset.urgency  === urgencyFilter;

            if (catMatch && urgMatch) {
                card.style.display = "block";
                visible++;
            } else {
                card.style.display = "none";
            }
        });

        countEl.textContent = `${visible} email${visible !== 1 ? "s" : ""}`;
    }

    categorySelect.addEventListener("change", filterMessages);
    urgencySelect.addEventListener("change", filterMessages);
})();
