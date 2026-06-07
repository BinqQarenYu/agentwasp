## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-06-07 - Screen Reader Accessibility for Icon-Only Buttons
**Learning:** Adding a `title` attribute to icon-only buttons (like the agent action buttons) is insufficient for screen readers; while `title` provides a visual tooltip on hover, `aria-label` is required to ensure screen reader users hear a description of the button's purpose instead of just "button".
**Action:** When creating or reviewing icon-only buttons, always verify that `aria-label` is included alongside `title`.
