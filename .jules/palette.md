## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-08-26 - Added ARIA labels to daisyUI label and link buttons
**Learning:** Found instances where `<label>` and `<a>` elements were styled as icon-only buttons using daisyUI classes like `.btn-square` and `.btn-circle`, but lacked accessible names. For example, a hidden file input triggered by a `<label>` containing only an SVG.
**Action:** When using `<label>` or `<a>` tags as visually distinct buttons (especially icon-only ones), ensure they have explicit `aria-label` attributes to make their purpose clear to screen readers, instead of relying solely on visual context or the elements they wrap.
