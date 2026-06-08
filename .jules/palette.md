## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-06-08 - Icon-Only Link Buttons Missing ARIA Labels
**Learning:** In the WASP dashboard, several `.btn-circle` and `.btn-square` icon buttons are actually `<a>` or `<label>` tags rather than `<button>` tags. These often miss `aria-label` attributes even if they have `title` attributes.
**Action:** When auditing or adding icon-only UI elements, look closely at `<label>` and `<a>` tags with button classes to ensure they have explicit `aria-label` attributes for full screen reader accessibility.
