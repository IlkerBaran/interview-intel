// ════════════════════════════════════════
// CONFIRM BEFORE DESTRUCTIVE SUBMIT
// Replaces onsubmit="return confirm(...)" on the
// delete forms so script-src needs no
// 'unsafe-inline'. The prompt travels on
// data-confirm.
//
// Three deliberate choices, all about failing
// CLOSED rather than open — a missed prompt means
// a silent delete:
//   1. Delegated from document, so no element has
//      to exist at bind time. notifications.js
//      appends server-rendered HTML after load.
//   2. Capture phase, so a form-level handler
//      calling stopPropagation cannot suppress it.
//   3. Registered FIRST in this file, so a throw
//      in any block below cannot skip it.
// ════════════════════════════════════════
document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form.matches || !form.matches("form[data-confirm]")) return;

    if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();   // matches the old `return false`
    }
}, true);


// ════════════════════════════════════════
// NAVBAR TOGGLE (mobile)
// Opens/closes the nav menu on small screens.
// ════════════════════════════════════════
const toggle = document.getElementById("nav-toggle");
const nav    = document.getElementById("nav-right");

if (toggle && nav) {
    toggle.addEventListener("click", () => {
        nav.classList.toggle("open");
    });
}


// ════════════════════════════════════════
// PASSWORD TOGGLE
// Switches input type between password and
// text, updating the button label accordingly.
// ════════════════════════════════════════
document.querySelectorAll(".password-toggle").forEach(button => {
    button.addEventListener("click", () => {
        const input = button.previousElementSibling;
        if (input.type === "password") {
            input.type         = "text";
            button.textContent = "Hide";
        } else {
            input.type         = "password";
            button.textContent = "Show";
        }
    });
});


// ════════════════════════════════════════
// HOME PAGE NAVBAR SCROLL
// On the home page the navbar is fixed and
// starts transparent over the cream hero, then
// fades to solid cream as the user scrolls.
// --nav-bg-alpha drives the background each
// frame; .scrolled switches text colors via CSS.
// ════════════════════════════════════════
(function () {
    const navbar = document.querySelector('.navbar');
    if (!navbar || !document.body.classList.contains('page-home')) return;

    let navbarHeight, navSnap;

    function recalc() {
        navbarHeight = navbar.offsetHeight;
        // Navbar turns solid roughly as the hero scrolls out of view.
        navSnap = Math.max(1, (window.innerHeight - navbarHeight) * 0.55);
        onScroll();
    }

    function onScroll() {
        const scrollY = window.scrollY;

        // Background alpha — fades in from 40% of navSnap, fully opaque at navSnap.
        const navFadeStart = navSnap * 0.40;
        const navBgAlpha   = Math.max(0, Math.min((scrollY - navFadeStart) / (navSnap - navFadeStart), 1));
        navbar.style.setProperty('--nav-bg-alpha', (navBgAlpha * 0.97).toFixed(3));
        navbar.style.borderBottomColor = 'rgba(227,221,211,' + navBgAlpha.toFixed(3) + ')';
        navbar.style.boxShadow = navBgAlpha > 0.01
            ? '0 1px 12px rgba(26,18,8,' + (navBgAlpha * 0.06).toFixed(3) + ')'
            : 'none';

        // Text colors: CSS transition handles the switch when .scrolled is toggled.
        navbar.classList.toggle('scrolled', scrollY >= navSnap);
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', recalc);
    recalc();
})();


// ════════════════════════════════════════
// HOME PAGE PORTAL ACCORDIONS
// Expand/collapse the disclosure panels.
// No-op on pages that have no .portal elements.
// ════════════════════════════════════════
document.querySelectorAll('.portal').forEach(function (portal) {
    const trigger = portal.querySelector('.portal-trigger');
    if (!trigger) return;
    trigger.addEventListener('click', function () {
        const isOpen = portal.classList.toggle('is-open');
        trigger.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });
});


// ════════════════════════════════════════
// FLASH MESSAGES
// Manual dismiss (× button) + auto-dismiss after
// 5s. Fades out, then removes the node — and the
// wrapper once the last flash is gone.
// ════════════════════════════════════════
(function () {
    const flashes = document.querySelectorAll('.flash');
    if (!flashes.length) return;

    const FADE_MS = 400;    // must match the .flash CSS transition
    const AUTO_MS = 5000;   // time on screen before auto-dismiss

    function dismiss(flash) {
        if (flash.classList.contains('flash-hide')) return;   // already going
        flash.classList.add('flash-hide');
        setTimeout(() => {
            const wrapper = flash.parentElement;
            flash.remove();
            if (wrapper && !wrapper.querySelector('.flash')) wrapper.remove();
        }, FADE_MS);
    }

    flashes.forEach(flash => {
        const closeBtn = flash.querySelector('.flash-close');
        if (closeBtn) closeBtn.addEventListener('click', () => dismiss(flash));
        setTimeout(() => dismiss(flash), AUTO_MS);
    });
})();


// ════════════════════════════════════════
// BACK LINK
// Replaces href="javascript:history.back()" on the
// session-expired page. Delegated for consistency;
// the href stays a real URL, so the link still
// works if this never runs.
// ════════════════════════════════════════
document.addEventListener("click", function (event) {
    if (!event.target.closest) return;
    const link = event.target.closest("a[data-history-back]");
    if (!link) return;

    if (window.history.length > 1) {
        event.preventDefault();
        window.history.back();
    }
});