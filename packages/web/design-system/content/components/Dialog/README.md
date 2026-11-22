Dialog is a modal on the native `<dialog>` element: `showModal()` makes the page inert, Escape closes, and focus returns to the opener.

- **Consumer provides:** `open`, `onClose`, `title`, optional `description`, body, `footer` buttons.

- **`onSubmit`:** makes the body and action row one form; a `type="submit"` footer button and Enter in a single-line field submit it.
