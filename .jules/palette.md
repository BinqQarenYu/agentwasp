## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Added ARIA labels to icon-only buttons
**Learning:** Found several icon-only buttons (`<button>`, `<a>`, `<label>`) across dashboard templates (like `base.html`, `chat.html`, `workspaces_visual.html`, `scheduler.html`) that only relied on visual icons (SVGs) and `title` attributes. `title` attributes are not sufficient for providing accessible names to screen readers.
**Action:** Always ensure that icon-only interactive elements include descriptive `aria-label` attributes to ensure keyboard and screen reader accessibility, even if a `title` attribute is already present.
