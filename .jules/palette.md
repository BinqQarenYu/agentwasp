## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## 2023-10-27 - Icon-only buttons accessibility improvements
**Learning:** Found multiple instances where buttons, links, and labels were styled as interactive icon-only elements (e.g., using `btn-square` or `btn-circle`) but lacked `aria-label` attributes, making them inaccessible to screen readers.
**Action:** Always add descriptive `aria-label` attributes when creating or modifying icon-only interactive elements to ensure proper accessibility for screen readers.
