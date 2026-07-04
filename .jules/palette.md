## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-07-04 - Missing ARIA Labels on DaisyUI Icon Buttons
**Learning:** Found that multiple icon-only buttons using DaisyUI classes like `btn-circle` and `btn-square` were missing `aria-label` attributes in the dashboard HTML templates. The `title` attribute alone is insufficient for screen readers.
**Action:** When adding or reviewing icon-only buttons using daisyUI or similar CSS frameworks, always explicitly add an `aria-label` attribute describing the action.
