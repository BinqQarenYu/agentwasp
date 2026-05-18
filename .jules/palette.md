## 2024-05-17 - Missing ARIA Labels on Dialog/Modal Close Buttons
**Learning:** Found a recurring pattern where close (`✕`) buttons in dialogs and modal backdrops were missing `aria-label`s, rendering them ambiguous to screen reader users. The application relies heavily on modals and drawers for its UI, making this a critical accessibility gap.
**Action:** Always ensure that icon-only buttons (like `✕`) and invisible backdrop close buttons have descriptive `aria-label` attributes (e.g., `aria-label="Close modal"`) when building or reviewing new modal dialogs.
