// ════════════════════════════════════════════════════════════
// FLOATING-LOGOS HERO — three cooperating motion layers
//
//   Layer 1  Idle drift      — each chip orbits its own anchor forever
//   Layer 2  Cursor repulsion — chips push away from the pointer, in-zone
//   Layer 3  Scroll convergence — chips fly in + merge, focal zooms past
//
// How the layers cooperate WITHOUT fighting over one transform:
//   • Layer 3 writes the CSS `transform` matrix (GSAP x/y/scale/opacity).
//   • Layers 1 & 2 write the SEPARATE CSS `translate`/`rotate` properties.
//     A per-frame composer folds their shared state into each chip directly:
//       translate = (drift + repulsion) * motion ; rotate = drift * motion.
//     (Direct writes — not CSS `calc(var())` — because WebKit won't re-resolve
//     a calc() that references a changing custom property in those props.)
//   • The scroll timeline animates motion 1 → 0 as the chips converge,
//     so idle + repulsion fade out cleanly while Layer 3 pulls them in.
//
// Hard rules honored:
//   • Plain self-hosted JS (vendor/gsap + vendor/ScrollTrigger), no CDN,
//     no eval/Function, no network → runs under script-src 'self'.
//   • No inline scripts/handlers; external module only.
//   • Everything is gated by gsap.matchMedia() and reverts cleanly.
//       – Idle drift:  any width, motion allowed.
//       – Repulsion:   motion allowed AND a fine hover pointer (desktop).
//       – Convergence: ≥769px AND motion allowed (mobile keeps static hero).
//     prefers-reduced-motion → NONE of them run; CSS fallbacks render the
//     hero exactly as authored (no flash, no hidden content, fully usable).
//   • transform / translate / rotate / opacity only (GPU) → 60fps target.
// ════════════════════════════════════════════════════════════
(function () {
    "use strict";

    if (typeof gsap === "undefined" || typeof ScrollTrigger === "undefined") return;
    if (!document.getElementById("hero")) return;

    gsap.registerPlugin(ScrollTrigger);

    // ── Tunable knobs ────────────────────────────────────────────
    // Scroll convergence (Layer 3)
    const PIN_DISTANCE   = 2200;  // px of scroll the pinned sequence consumes
    const SCRUB          = 1;     // seconds of catch-up smoothing
    const LOGO_STAGGER   = 0.04;  // gap between chip convergences
    const MOTION_DAMP    = 0.40;  // fraction of timeline over which idle/repulsion fade out

    // Idle drift (Layer 1)
    const DRIFT_MIN      = 15;    // px — min orbit amplitude
    const DRIFT_MAX      = 25;    // px — max orbit amplitude
    const DRIFT_ROT      = 5;     // deg — max sway
    const DRIFT_DUR_MIN  = 3.0;   // s
    const DRIFT_DUR_MAX  = 5.5;   // s

    // Cursor repulsion (Layer 2)
    const REP_RADIUS     = 150;   // px — influence radius around a chip
    const REP_MAX        = 38;    // px — max push (kept < zone so chips never escape)
    const REP_STIFFNESS  = 0.07;  // spring acceleration toward target (low = slow, graceful escape)
    const REP_DAMPING     = 0.82; // velocity retention (overshoot + settle)

    const logos     = gsap.utils.toArray(".hero-logo");
    const heroLogos = document.getElementById("heroLogos");
    if (!logos.length) return;

    const mm = gsap.matchMedia();

    // ── Shared motion state ──────────────────────────────────────
    // Layers 1 & 2 compose into translate/rotate which JS writes DIRECTLY to
    // each chip every frame. We can't lean on CSS `translate: calc(var(--x)…)`
    // because WebKit does not re-resolve that declaration when the custom
    // property changes, so the drift/repulsion never render. Layer 3 keeps
    // writing the `transform` matrix (independent property) and is unaffected.
    //   translate = (drift + repulsion) * motion ; rotate = drift * motion
    const drift  = logos.map(() => ({ x: 0, y: 0, r: 0 })); // idle orbit  (Layer 1)
    const rep    = logos.map(() => ({ x: 0, y: 0 }));        // cursor push (Layer 2)
    const motion = { value: 1 };                             // scroll damp (Layer 3)

    // ════════════════════════════════════════════════════════════
    // LAYER 1 — IDLE DRIFT  (any width, motion allowed)
    // Each chip orbits its anchor on an ellipse: drift.x and drift.y
    // tween on DIFFERENT durations (+ randomized start progress) so no two
    // chips sync and each traces its own slow loop. Rotation sways separately.
    // ════════════════════════════════════════════════════════════
    mm.add("(prefers-reduced-motion: no-preference)", () => {
        logos.forEach((logo, i) => {
            const s  = drift[i];
            const ax = gsap.utils.random(DRIFT_MIN, DRIFT_MAX);
            const ay = gsap.utils.random(DRIFT_MIN, DRIFT_MAX);
            const ar = gsap.utils.random(DRIFT_ROT * 0.6, DRIFT_ROT);

            gsap.fromTo(s, { x: -ax }, {
                x: ax,
                duration: gsap.utils.random(DRIFT_DUR_MIN, DRIFT_DUR_MAX),
                ease: "sine.inOut", repeat: -1, yoyo: true,
            }).progress(Math.random());

            gsap.fromTo(s, { y: -ay }, {
                y: ay,
                duration: gsap.utils.random(DRIFT_DUR_MIN, DRIFT_DUR_MAX),
                ease: "sine.inOut", repeat: -1, yoyo: true,
            }).progress(Math.random());

            gsap.fromTo(s, { r: -ar }, {
                r: ar,
                duration: gsap.utils.random(DRIFT_DUR_MIN + 1, DRIFT_DUR_MAX + 1.5),
                ease: "sine.inOut", repeat: -1, yoyo: true,
            }).progress(Math.random());
        });

        // Compose drift + repulsion + scroll-damp and write each chip's final
        // translate/rotate directly (the reliable path in WebKit). Runs whenever
        // motion is allowed, so idle drift works even without a hover pointer.
        const compose = () => {
            const m = motion.value;
            for (let i = 0; i < logos.length; i++) {
                const d = drift[i], r = rep[i];
                logos[i].style.translate = ((d.x + r.x) * m) + "px " + ((d.y + r.y) * m) + "px";
                logos[i].style.rotate    = (d.r * m) + "deg";
            }
        };
        gsap.ticker.add(compose);

        return () => {
            gsap.ticker.remove(compose);
            logos.forEach(l => { l.style.removeProperty("translate"); l.style.removeProperty("rotate"); });
        };
    });

    // ════════════════════════════════════════════════════════════
    // LAYER 2 — CURSOR REPULSION  (motion allowed + fine hover pointer)
    // Spring rep.x/rep.y away from the pointer, capped at REP_MAX so a
    // chip never leaves its zone; it's additive on the drift (composed by the
    // Layer 1 ticker) and returns to the drifting anchor when the cursor leaves.
    // ════════════════════════════════════════════════════════════
    mm.add("(prefers-reduced-motion: no-preference) and (hover: hover) and (pointer: fine)", () => {
        let pointerX = -9999, pointerY = -9999;

        // Per-chip state: cached layout anchor offset + spring pos/velocity.
        // The spring result is written into the shared `rep` slot, which the
        // Layer 1 composer folds into the chip's translate each frame.
        const items = logos.map((logo, i) => ({
            logo,
            rep: rep[i],                       // shared repulsion offset for this chip
            offX: 0, offY: 0,                  // chip center offset within #heroLogos
            x: 0, y: 0, vx: 0, vy: 0,          // spring state
        }));

        // offsetLeft/Top ignore transforms → true home center, no per-frame thrash.
        function measure() {
            items.forEach(it => {
                it.offX = it.logo.offsetLeft + it.logo.offsetWidth  / 2;
                it.offY = it.logo.offsetTop  + it.logo.offsetHeight / 2;
            });
        }
        measure();

        const onMove = e => { pointerX = e.clientX; pointerY = e.clientY; };
        window.addEventListener("pointermove", onMove, { passive: true });

        // One container rect read per frame (not per chip) keeps anchors correct
        // even if the page is mid-scroll, at minimal layout cost.
        function tick() {
            const base = heroLogos.getBoundingClientRect();
            for (let i = 0; i < items.length; i++) {
                const it = items[i];
                const cx = base.left + it.offX;
                const cy = base.top  + it.offY;
                const dx = cx - pointerX;
                const dy = cy - pointerY;
                const dist = Math.hypot(dx, dy);

                let tx = 0, ty = 0;
                if (dist < REP_RADIUS && dist > 0.001) {
                    const push = (1 - dist / REP_RADIUS) * REP_MAX;
                    tx = (dx / dist) * push;
                    ty = (dy / dist) * push;
                }
                // Critically-ish damped spring → natural overshoot + settle.
                it.vx = (it.vx + (tx - it.x) * REP_STIFFNESS) * REP_DAMPING;
                it.vy = (it.vy + (ty - it.y) * REP_STIFFNESS) * REP_DAMPING;
                it.x += it.vx;
                it.y += it.vy;
                it.rep.x = it.x;
                it.rep.y = it.y;
            }
        }
        gsap.ticker.add(tick);

        window.addEventListener("resize", measure);
        ScrollTrigger.addEventListener("refresh", measure);

        return () => {
            gsap.ticker.remove(tick);
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("resize", measure);
            ScrollTrigger.removeEventListener("refresh", measure);
            items.forEach(it => { it.rep.x = 0; it.rep.y = 0; });
        };
    });

    // ════════════════════════════════════════════════════════════
    // LAYER 3 — SCROLL CONVERGENCE  (≥769px, motion allowed)
    // Pinned, scrubbed timeline: logos fly in + merge, focal zooms past,
    // hero dissolves, then ScrollTrigger unpins into the content.
    // ════════════════════════════════════════════════════════════
    mm.add("(min-width: 769px) and (prefers-reduced-motion: no-preference)", () => {
        const hero    = document.getElementById("hero");
        const focal   = document.getElementById("hero-focal");
        const note    = hero.querySelector(".hero-logos-note");
        const textEls = [
            hero.querySelector(".hero-eyebrow"),
            hero.querySelector(".hero-headline"),
            hero.querySelector(".hero-sub"),
            hero.querySelector(".hero-cta"),
        ].filter(Boolean);

        if (!focal) return;

        const centerX = el => { const r = el.getBoundingClientRect(); return r.left + r.width  / 2; };
        const centerY = el => { const r = el.getBoundingClientRect(); return r.top  + r.height / 2; };

        const tl = gsap.timeline({
            defaults: { ease: "none" },
            scrollTrigger: {
                trigger: hero,
                start: "top top",
                end: "+=" + PIN_DISTANCE,
                pin: true,
                anticipatePin: 1,
                scrub: SCRUB,
                invalidateOnRefresh: true,
            },
        });

        // Damp Layers 1 & 2 to zero as the chips converge, so they merge cleanly.
        // The composer reads motion.value and scales drift + repulsion by it.
        tl.fromTo(motion, { value: 1 },
            { value: 0, duration: MOTION_DAMP, ease: "power1.in" }, 0);

        // ── Phase 1 — converge (0 → 0.55) ───────────────────────
        tl.to(logos, {
            x:       (i, t) => centerX(focal) - centerX(t),
            y:       (i, t) => centerY(focal) - centerY(t),
            scale:   0.25,
            opacity: 0,
            ease:    "power1.in",
            stagger: LOGO_STAGGER,
            duration: 0.55,
        }, 0);

        tl.fromTo(focal, { scale: 1 },
            { scale: 1.15, duration: 0.5, ease: "power1.inOut" }, 0.30);

        // ── Phase 2 — black hole: hero content falls INTO the focal (0.50 → 0.90) ──
        // Same convergence pattern as Phase 1's logos, applied to the text + CTA +
        // disclaimer. The focal itself does NOT move, scale, or fade here — it's the
        // fixed singularity; everything else shrinks toward its center and fades on
        // the way in. (The hero — focal included — fades in Phase 3.)
        tl.to([...textEls, note].filter(Boolean), {
            x:       (i, t) => centerX(focal) - centerX(t),
            y:       (i, t) => centerY(focal) - centerY(t),
            scale:   0.1,
            opacity: 0,
            ease:    "power1.in",
            stagger: LOGO_STAGGER,
            duration: 0.40,
        }, 0.50);

        // ── Phase 3 — reveal + release (0.90 → 1.0) ─────────────
        tl.to(hero, { opacity: 0, duration: 0.10 }, 0.90);

        // On revert (e.g. crossing the 769px breakpoint) hand full motion back
        // to the composer so drift/repulsion resume at their idle amplitude.
        return () => { motion.value = 1; };
    });

    // Recompute pin/positions once fonts + logo SVGs have settled.
    window.addEventListener("load", () => ScrollTrigger.refresh());
})();
