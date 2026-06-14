## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2026-06-14 - Missing ARIA Labels on Icon-Only Buttons
**Learning:** In the daisyUI component system, icon-only buttons (styled with `btn-circle` or `btn-square` along with an SVG) often lack accessible names if an `aria-label` is not explicitly provided. Tooltips (`title`) are insufficient for comprehensive screen reader accessibility.
**Action:** When working with daisyUI icon-only buttons (or any links disguised as buttons), always verify the presence of a descriptive `aria-label` attribute.
