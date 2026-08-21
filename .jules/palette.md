## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2024-05-19 - Added ARIA labels to icon-only "agent action" buttons
**Learning:** Found several agent action buttons (Resume, Pause, Archive, Delete) in the agents list template that used icon-only buttons without any accessible name (`aria-label`). While they had a `title` attribute, an explicit `aria-label` provides a stronger, more robust accessible name for screen readers.
**Action:** Always ensure that any button containing only an icon or symbol includes a descriptive `aria-label` attribute, even if a `title` is present, so screen readers can correctly identify its purpose.
