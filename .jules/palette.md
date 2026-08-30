## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-05-18 - Missing ARIA Labels on Icon-Only Buttons
**Learning:** Icon-only buttons (including `<label>` and `<a>` tags disguised as buttons using classes like `.btn-ghost`, `.btn-circle`, or `.btn-square`) in this codebase frequently rely on `title` attributes or have no accessibility labeling, severely impacting screen reader experiences.
**Action:** Always ensure that icon-only interactive elements explicitly define an `aria-label` attribute, regardless of whether a `title` attribute is present.
