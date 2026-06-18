## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Adding ARIA labels to icon-only DaisyUI buttons
**Learning:** Found several icon-only interactive elements (`<button>`, `<a>`, and `<label>` configured as buttons via `.btn.btn-ghost.btn-circle`/`.btn-square` classes) lacking accessible names in `base.html`. The existing `title` attributes act as tooltips but are insufficient for full screen-reader accessibility.
**Action:** Always ensure any UI elements with `.btn-circle` or `.btn-square` consisting entirely of SVGs include clear `aria-label`s for screen readers.
