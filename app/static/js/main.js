const toggle = document.getElementById("nav-toggle");
const nav = document.getElementById("nav-right");

if (toggle && nav) {
    toggle.addEventListener("click", () => {
        nav.classList.toggle("open");
    });
}