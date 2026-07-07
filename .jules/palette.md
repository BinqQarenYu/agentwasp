## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-07-07 - Accessible Icon-Only Buttons
**Learning:** Found multiple icon-only buttons disguised using generic classes like `.btn-ghost` and `.btn-circle` in the main base template (`base.html`) lacking descriptive labels for screen readers. Since the elements only contain `<svg>` tags (like theme toggles and reset buttons), they were invisible to assistive technologies.
**Action:** When adding or encountering interactive elements with only icons or visual indicators (especially `.btn-ghost`, `.btn-circle`, `.btn-square`), always explicitly add an `aria-label` attribute describing the action to ensure full keyboard and screen reader accessibility.
