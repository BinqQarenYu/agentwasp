## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-09-05 - Icon-only links and labels require ARIA labels
**Learning:** In this application, elements like `<a>` and `<label>` are frequently styled as icon-only buttons using classes like `.btn-ghost` and `.btn-circle` or `.btn-square`. These elements, while visually acting as buttons, do not natively convey their purpose to screen readers when only containing SVGs or text symbols like `?`, leading to accessibility gaps.
**Action:** Always ensure that icon-only interactive elements, regardless of their HTML tag (`<button>`, `<a>`, or `<label>`), include descriptive `aria-label` attributes to explicitly define their function for screen reader users.
## 2024-09-06 - Missing ARIA Labels on Ghost Buttons
**Learning:** Found multiple instances where `.btn-ghost` classes were used for icon-only action buttons (like delete) without any `aria-label` or `title` attributes, making them inaccessible to screen readers and confusing for users lacking hover tooltips.
**Action:** Always add descriptive `aria-label` and `title` attributes when creating icon-only buttons using `.btn-ghost`, `.btn-circle`, or `.btn-square`.
