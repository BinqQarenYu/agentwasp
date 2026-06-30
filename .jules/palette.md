## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2026-06-30 - DaisyUI Icon-Only Button Accessibility Pattern
**Learning:** This application heavily uses DaisyUI's `btn-square` and `btn-circle` utility classes to create icon-only interactive elements across various tags (`button`, `a`, `label`). Developers often omit `aria-label` on these elements since the visual icon (usually an inline SVG) is clear to sighted users, creating a widespread accessibility anti-pattern.
**Action:** When working on this application's dashboard templates, proactively search for `btn-square` and `btn-circle` classes to identify and fix missing `aria-label` attributes on icon-only interactive elements.
