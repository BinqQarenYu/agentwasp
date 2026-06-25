## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2026-06-25 - [A11y] Added explicit ARIA labels to icon-only controls
**Learning:** Found several global control elements (like `<label>`, `<a>`, and `<button>`) disguised as icon buttons using daisyUI classes like `.btn-square` and `.btn-circle`. These lacked `aria-label` attributes, making them opaque to screen readers despite having `title` attributes (which are insufficient for accessibility).
**Action:** Always ensure that any interactive element primarily consisting of an icon or generic text like '?' includes a descriptive `aria-label` to provide proper context for assistive technologies.
