## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.
## $(date +%Y-%m-%d) - Added ARIA labels to structural icon-only buttons
**Learning:** Structural navigation buttons (like sidebar toggles, theme toggles, and shortcut openers) in the dashboard template often relied on SVG icons without accessible names, making them invisible to screen readers.
**Action:** Ensure that all icon-only buttons, especially those that trigger critical structural or navigational changes (e.g. sidebar toggle, theme toggle, panic reset), explicitly include an `aria-label` attribute.
