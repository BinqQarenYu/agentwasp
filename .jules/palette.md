## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2025-03-09 - Core Navigation Missing ARIA Labels
**Learning:** Core layout navigation controls in this app use icon-only buttons with `title` attributes but lack `aria-label`s, which affects touch/mobile screen readers where titles do not surface easily.
**Action:** When working on navigation components or layout structures, ensure all icon-only buttons or toggles feature semantic `aria-label`s in addition to their `title`s to improve touch accessibility.
