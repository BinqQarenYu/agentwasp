1. **Fix missing aria-labels on icon-only buttons in `base.html`:**
   - Add `aria-label` attribute to the "Panic Reset" link (`<a href="/reset"...>`) because it's an icon-only element.
   - Add `aria-label` attribute to the "Keyboard shortcuts" button (`<button id="btn-shortcuts-open"...>`).
   - Add `aria-label` attribute to the "Toggle theme" button (`<button id="btn-theme-toggle-sidebar"...>`).

   These changes align with the standard to always include an `aria-label` attribute for icon-only interactive elements to provide accessibility for screen readers.

2. **Complete pre-commit steps:**
   - Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.

3. **Submit the change:**
   - I will submit the PR as the 'Palette' UX persona using `default_api:submit`.
