## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Added aria-label attributes to icon-only buttons
**Learning:** Across the dashboard templates (`base.html`, `chat.html`, `workspaces_visual.html`, `scheduler.html`), icon-only buttons using classes like `btn-circle`, `btn-square`, or `btn-ghost` often lack descriptive text for screen readers.
**Action:** When adding or modifying icon-only buttons in the UI, always remember to add an `aria-label` attribute with a concise description of the button's action.
