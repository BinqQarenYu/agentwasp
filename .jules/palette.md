## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-05-19 - ARIA Labels on Icon-Only Links and Labels Styled as Buttons
**Learning:** In the dashboard's daisyUI setup, many icon-only controls are implemented as `<a>` or `<label>` elements styled with `.btn-circle` or `.btn-square`. While they often have `title` attributes for sighted users, these are not reliably announced by all screen readers as the primary accessible name.
**Action:** When adding accessibility to "buttons", always search for `.btn-circle` and `.btn-square` regardless of the underlying HTML tag (e.g., `<a>`, `<label>`), and explicitly add an `aria-label`.
