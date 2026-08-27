## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-05-14 - Icon-only buttons lacking aria-labels
**Learning:** Found multiple instances of icon-only elements such as toggle themes, reset agent, open shortcut buttons in the UI missing `aria-label`s, which is critical for screen reader users to understand their purpose, especially for SVG-only icons inside `<a>`, `<button>` and `<label>` elements.
**Action:** Consistently review base templates and layout headers to ensure utility buttons all have `aria-label` attributes.
