// ════════════════════════════════════════
// TASK FILTER BAR
// Moved out of an inline <script> in
// tasks/show_all_tasks.html so script-src needs no
// 'unsafe-inline'. The filter value travels on
// data-filter instead of an onclick argument.
// ════════════════════════════════════════
(function () {
    const buttons = document.querySelectorAll(".filter-btn");
    const countEl = document.getElementById("filter-count");
    if (!buttons.length || !countEl) return;

    const cards = document.querySelectorAll(".task-card");

    function filterTasks(filter, btn) {
        buttons.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");

        let visible = 0;

        cards.forEach(card => {
            const status   = card.dataset.status;
            const priority = card.dataset.priority;

            let show = false;
            if (filter === "all")       show = true;
            if (filter === "pending")   show = status === "pending";
            if (filter === "completed") show = status === "completed";
            if (filter === "high")      show = priority === "high";

            card.style.display = show ? "flex" : "none";
            if (show) visible++;
        });

        countEl.textContent = `${visible} task${visible !== 1 ? "s" : ""}`;
    }

    buttons.forEach(btn => {
        btn.addEventListener("click", () => filterTasks(btn.dataset.filter, btn));
    });
})();
