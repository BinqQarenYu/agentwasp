## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2026-07-02 - Adding aria-labels to Icon-Only Buttons
**Learning:** Found multiple instances where interactive UI elements (like `<button>`, `<a>`, and `<label>`) rely entirely on SVG icons without visible text or an `aria-label`. These lack screen-reader context, causing accessibility issues. The problem is common across daisyUI-like circular/square buttons (`btn-circle`, `btn-square`).
**Action:** Always ensure that icon-only interactive elements include descriptive `aria-label` attributes to make their actions explicitly clear to assistive technologies, avoiding ambiguous interactions.
