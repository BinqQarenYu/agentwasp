/* =========================================================
   WASP — agentwasp.com  |  app.js
   ========================================================= */

"use strict";

// ── Year ──────────────────────────────────────────────────
const yearEl = document.getElementById("year");
if (yearEl) yearEl.textContent = new Date().getFullYear();

// ── Toast utility ─────────────────────────────────────────
const toastEl = document.getElementById("toast");
let toastTimer = null;

function showToast(msg, type = "success", duration = 5000) {
  if (!toastEl) return;
  toastEl.textContent = msg;
  toastEl.className = `toast toast--${type}`;
  toastEl.removeAttribute("hidden");

  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.setAttribute("hidden", "");
  }, duration);
}

// ── Email subscribe form ───────────────────────────────────
const form       = document.getElementById("subscribeForm");
const emailInput = document.getElementById("emailInput");
const emailError = document.getElementById("emailError");
const formNote   = document.getElementById("formNote");
const submitBtn  = document.getElementById("subscribeBtn");
const btnLabel   = submitBtn?.querySelector(".btn__label");
const btnLoading = submitBtn?.querySelector(".btn__loading");

function isValidEmail(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

function setLoading(on) {
  if (!submitBtn) return;
  submitBtn.disabled = on;
  if (btnLabel)   btnLabel.hidden = on;
  if (btnLoading) btnLoading.hidden = !on;
}

function setFormNote(msg, type = "") {
  if (!formNote) return;
  formNote.textContent = msg;
  formNote.className = "form__note" + (type ? ` is-${type}` : "");
}

async function submitEmail(email) {
  setLoading(true);
  setFormNote("");

  try {
    const res = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email.trim() }),
      signal: AbortSignal.timeout(8000),
    });

    if (res.ok) {
      setFormNote("You're on the list. We'll be in touch.", "success");
      showToast("You're on the builder list.", "success");
      emailInput.value = "";
      // Store as confirmed in localStorage
      localStorage.setItem("wasp_subscribed", "1");
      localStorage.setItem("wasp_email", email.trim());
    } else {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || data.message || `Server error ${res.status}`);
    }
  } catch (err) {
    // Network error or server not available — save locally
    if (err.name === "TypeError" || err.name === "AbortError" || !navigator.onLine) {
      localStorage.setItem("wasp_email_pending", email.trim());
      setFormNote(
        "Saved locally — we'll add server-side sync soon. You're noted.",
        "warn"
      );
      showToast("Saved locally. Server-side coming soon.", "warn");
    } else {
      setFormNote(`Something went wrong: ${err.message}`, "error");
      showToast("Subscription failed. Try again.", "error");
    }
  } finally {
    setLoading(false);
  }
}

if (form) {
  // Pre-fill if already subscribed
  const prevEmail = localStorage.getItem("wasp_email") || localStorage.getItem("wasp_email_pending");
  if (prevEmail && emailInput) {
    emailInput.value = prevEmail;
    if (localStorage.getItem("wasp_subscribed")) {
      setFormNote("You're already on the builder list.", "success");
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = emailInput?.value ?? "";

    // Clear previous state
    emailInput?.classList.remove("is-error");
    if (emailError) emailError.hidden = true;
    setFormNote("");

    if (!isValidEmail(email)) {
      emailInput?.classList.add("is-error");
      if (emailError) emailError.hidden = false;
      emailInput?.focus();
      return;
    }

    await submitEmail(email);
  });

  // Live validation on blur
  emailInput?.addEventListener("blur", () => {
    const email = emailInput.value;
    if (email && !isValidEmail(email)) {
      emailInput.classList.add("is-error");
      if (emailError) emailError.hidden = false;
    } else {
      emailInput.classList.remove("is-error");
      if (emailError) emailError.hidden = true;
    }
  });

  emailInput?.addEventListener("input", () => {
    emailInput.classList.remove("is-error");
    if (emailError) emailError.hidden = true;
    if (formNote && formNote.classList.contains("is-error")) setFormNote("");
  });
}

// ── Smooth scroll for anchor links ────────────────────────
document.querySelectorAll('a[href^="#"]').forEach((anchor) => {
  anchor.addEventListener("click", (e) => {
    const target = document.querySelector(anchor.getAttribute("href"));
    if (!target) return;
    e.preventDefault();
    const navH = document.querySelector(".nav")?.offsetHeight ?? 0;
    const y = target.getBoundingClientRect().top + window.scrollY - navH - 16;
    window.scrollTo({ top: y, behavior: "smooth" });
    // Update focus for accessibility
    target.setAttribute("tabindex", "-1");
    target.focus({ preventScroll: true });
  });
});

// ── Nav active state on scroll ────────────────────────────
const navLinks = document.querySelectorAll(".nav__link[href^='#']");
const sections = [];
navLinks.forEach((link) => {
  const el = document.querySelector(link.getAttribute("href"));
  if (el) sections.push({ link, el });
});

function onScroll() {
  const offset = (document.querySelector(".nav")?.offsetHeight ?? 60) + 32;
  let active = null;
  for (const { el, link } of sections) {
    if (el.getBoundingClientRect().top <= offset) active = link;
  }
  navLinks.forEach((l) => l.classList.remove("is-active"));
  if (active) active.classList.add("is-active");
}

window.addEventListener("scroll", onScroll, { passive: true });

// ── Intersection-based fade-in ────────────────────────────
if ("IntersectionObserver" in window) {
  const style = document.createElement("style");
  style.textContent = `
    .fade-in { opacity: 0; transform: translateY(16px); transition: opacity 0.55s cubic-bezier(0.22,1,0.36,1), transform 0.55s cubic-bezier(0.22,1,0.36,1); }
    .fade-in.is-visible { opacity: 1; transform: none; }
  `;
  document.head.appendChild(style);

  const targets = document.querySelectorAll(
    ".why__item, .tile, .checklist__item, .changelog__entry, .stat-card"
  );
  targets.forEach((el, i) => {
    el.classList.add("fade-in");
    el.style.transitionDelay = `${Math.min(i * 40, 300)}ms`;
  });

  const io = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          io.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.1, rootMargin: "0px 0px -40px 0px" }
  );

  targets.forEach((el) => io.observe(el));
}
