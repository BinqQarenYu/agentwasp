## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## $(date +%Y-%m-%d) - Consistent aria-label Application for Icon-Only Buttons
**Learning:** Found multiple instances where icon-only buttons (`btn-square`, `btn-circle`) lacked `aria-label` attributes across different template files (`base.html`, `workspaces_visual.html`, `chat.html`), affecting screen reader accessibility. Some of these were `<label>` or `<a>` tags acting as buttons.
**Action:** When creating or reviewing UI components, consistently apply `aria-label` to *any* interactive element (button, link, label) that relies solely on visual icons for its affordance.
