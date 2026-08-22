## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-05-24 - Icon-Only Buttons Require ARIA Labels
**Learning:** While `title` attributes provide helpful tooltips on hover, they are not always reliably announced by all screen readers. Icon-only interactive elements (like buttons or labels acting as buttons) must have an explicit `aria-label` to ensure full accessibility.
**Action:** Always verify that buttons containing only SVG icons have an `aria-label` attribute, even if a `title` attribute is already present.
