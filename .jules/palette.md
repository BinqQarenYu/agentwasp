## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-08-17 - Missing ARIA Labels on Icon Buttons in DaisyUI Layouts
 **Learning:** In `containers/agent-core/src/dashboard/templates/base.html`, `btn-circle`, `btn-square` and `btn-ghost` classes combined with SVGs are frequently used as icon-only interactive elements (both `<button>` and `<a>` styled as buttons), but they often lack accessible names, making them invisible or confusing to screen readers.
 **Action:** Proactively search for `<button class="... btn-circle/square/ghost ..."><svg>...</button>` and `<a class="... btn-circle/square/ghost ..."><svg>...</a>` patterns and ensure they include `aria-label` or `title` tags.
