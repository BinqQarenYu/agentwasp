## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2026-08-14 - Missing ARIA Labels on Icon-only Action Buttons
**Learning:** Icon-only action buttons (e.g., using `btn-circle`, `btn-ghost` with SVG icons) often lack `aria-label`s, which degrades the experience for screen-reader users, as they are not able to perceive the function of the button without it. We've seen this on `btn-shortcuts-open` and `btn-theme-toggle-sidebar` in the base template, `btn-zoom-reset`, `btn-detail-focus` and back links in workspaces visualization, and `a` tags disguised as buttons.
**Action:** When working on UI templates, verify that all interactive components (buttons, links disguised as buttons) that do not have text content have an `aria-label` providing context.
