## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-06-03 - Added missing aria-labels to template-based interactive elements
**Learning:** Icon-only interactive elements in this app's dashboard templates (like `<label>` tags disguised as buttons with `.btn-square`, or `<a>` tags with `.btn-circle`) often lack accessible names, making them difficult for screen readers to interpret.
**Action:** When adding or reviewing new icon-only controls (buttons, links, or styled labels) using utility classes like `.btn-circle` or `.btn-square`, always ensure an explicit `aria-label` attribute is provided to describe the action.
