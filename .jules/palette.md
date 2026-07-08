## 2024-07-08 - Icon-Only Buttons Missing ARIA Labels
**Learning:** Found multiple instances of icon-only buttons (`.btn-circle`, `.btn-square`) in the templates using `title` attributes but missing required `aria-label`s for screen readers. The `title` attribute is often insufficient for robust accessibility as it's not consistently announced by screen readers or accessible via keyboard focus in all browsers.
**Action:** When auditing templates, always ensure `.btn-circle`, `.btn-square`, and `.btn-ghost` items with only icons inside have an explicit `aria-label`.
