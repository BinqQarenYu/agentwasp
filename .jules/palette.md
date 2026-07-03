## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Adding ARIA labels to button-styled anchors and labels
**Learning:** Found an accessibility pattern where icon-only interactive elements (like htmx submission anchors and SVG triggers) using `.btn-circle` and `.btn-square` lacked aria-labels for screen readers across the dashboard templates.
**Action:** Next time looking for accessibility quick-wins, explicitly grep for `.btn-circle` or `.btn-square` and ensure they carry descriptive `aria-label`s, especially those not utilizing traditional `<button>` tags.
