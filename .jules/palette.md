## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-05-25 - Icon-only links and labels need ARIA labels too
**Learning:** In the Wasp dashboard, icon-only buttons are often implemented as `<a>` or `<label>` tags disguised with `.btn-ghost` classes (e.g. back buttons or attachment toggles), rather than actual `<button>` elements. These frequently omit `aria-label`s.
**Action:** When auditing for icon-only button accessibility, search for UI elements styled with `btn-circle`, `btn-square`, or `btn-ghost` across all tag types (`<button>`, `<a>`, `<label>`), not just button elements, and ensure they all include descriptive `aria-label` attributes.
