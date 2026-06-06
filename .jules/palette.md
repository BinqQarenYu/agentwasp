## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-06-06 - daisyUI Disguised Buttons Require Manual ARIA
**Learning:** In the Wasp dashboard, interactive elements often use `<a>` or `<label>` tags disguised as buttons using daisyUI classes like `.btn-ghost`, `.btn-circle`, or `.btn-square`. These lack intrinsic accessibility roles or labels when they only contain SVG icons.
**Action:** Always verify that elements with daisyUI icon-button classes include an explicit `aria-label` or `title`, regardless of whether they are actual `<button>` elements, especially when they act as critical interactive toggles (like the mobile sidebar toggle).
