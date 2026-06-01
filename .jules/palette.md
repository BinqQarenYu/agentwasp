## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-06-01 - Missing ARIA Labels on Icon-only Ghost Buttons
**Learning:** Found a recurring accessibility pattern in this app's components where `btn-ghost` and `btn-circle` classes are used for icon-only buttons (`<svg>` elements) but frequently omit descriptive `aria-label` attributes, creating poor screen reader experiences.
**Action:** Always verify that components disguised as buttons (like `<a class="btn-circle btn-ghost">` or `<button class="btn-ghost">`) explicitly contain an `aria-label` when they only wrap icons. Added this pattern check to my default accessibility verification routines for templates.
