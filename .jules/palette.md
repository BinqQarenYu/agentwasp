## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Ensure ARIA labels on icon-only buttons
**Learning:** Found multiple instances where `.btn-square` and `.btn-circle` components lacked `aria-label`s. This is a common pattern for icon-only buttons across the app's HTML templates.
**Action:** Always verify accessibility compliance when modifying or adding icon-only control buttons, especially when using standard daisyUI/Tailwind structural classes (`btn-square`, `btn-circle`).
