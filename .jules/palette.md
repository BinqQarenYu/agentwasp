## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2026-06-16 - CSS-only Checkbox Toggles Accessibility
**Learning:** When using the CSS-only hidden checkbox pattern for UI toggles (like sidebar menus), the interactive element is often a `<label>` linked to the input. If this label is styled as an icon-only button without visible text, screen readers will read nothing or just "checkbox", leading to a confusing experience.
**Action:** Always verify that `<label>` elements acting as icon-only UI toggles have explicit `aria-label` attributes to describe their action, ensuring they function accessibly like standard buttons.
