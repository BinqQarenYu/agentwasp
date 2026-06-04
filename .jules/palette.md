## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-06-04 - ARIA labels missing on icon-only interactive elements using daisyUI classes
**Learning:** Found multiple instances where interactive elements (`button`, `a`, `label`) disguised as icon-only buttons via daisyUI utility classes (`btn-square`, `btn-circle`) lacked `aria-label` attributes. This pattern prevents screen readers from understanding the control's purpose.
**Action:** Always verify that icon-only interactive elements using `.btn-circle` or `.btn-square` include explicit `aria-label`s, as tooltips (`title`) or implicit styling do not guarantee robust accessibility.
