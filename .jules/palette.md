## 2024-05-17 - Missing ARIA Labels on Dialog/Modal Close Buttons
**Learning:** Found a recurring pattern where close (`✕`) buttons in dialogs and modal backdrops were missing `aria-label`s, rendering them ambiguous to screen reader users. The application relies heavily on modals and drawers for its UI, making this a critical accessibility gap.
**Action:** Always ensure that icon-only buttons (like `✕`) and invisible backdrop close buttons have descriptive `aria-label` attributes (e.g., `aria-label="Close modal"`) when building or reviewing new modal dialogs.

## 2024-05-19 - Added ARIA labels to icon-only "close" and "delete" buttons
**Learning:** Found several modal dialogs, chips, and overlays throughout the dashboard templates that used icon-only buttons (containing just an "✕" character) for closing or removing elements without any accessible name (aria-label).
**Action:** Always ensure that any button containing only an icon or symbol (e.g. ✕, SVG) includes a descriptive `aria-label` attribute so screen readers can correctly identify its purpose.

## 2024-09-05 - Icon-only links and labels require ARIA labels
**Learning:** In this application, elements like `<a>` and `<label>` are frequently styled as icon-only buttons using classes like `.btn-ghost` and `.btn-circle` or `.btn-square`. These elements, while visually acting as buttons, do not natively convey their purpose to screen readers when only containing SVGs or text symbols like `?`, leading to accessibility gaps.
**Action:** Always ensure that icon-only interactive elements, regardless of their HTML tag (`<button>`, `<a>`, or `<label>`), include descriptive `aria-label` attributes to explicitly define their function for screen reader users.
