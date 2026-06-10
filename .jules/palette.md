## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-06-10 - Missing ARIA Labels on Icon-only DaisyUI Buttons
**Learning:** Found a recurring accessibility issue where interactive elements disguised as icon-only buttons (using daisyUI classes `.btn-circle` and `.btn-square` on `<button>`, `<a>`, and `<label>` tags) relied solely on visual SVG icons or `title` attributes. Tooltips/title attributes are often not reliably announced by all screen readers, making these critical UI controls (like panic reset, file attachments, and theme toggles) inaccessible to visually impaired users.
**Action:** Always ensure that any element acting as an icon-only button includes a descriptive `aria-label` attribute, regardless of whether it's technically a `<button>`, `<a>`, or `<label>`, and regardless of whether it already has a `title` attribute for mouse users.
