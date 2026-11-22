LinkButton is a real link (`<a href>`) styled as a `Button`. Use it for "go to a page"; use `Button` for actions that change something.

- **Consumer provides:** `href`, the visible label, optional `variant`, `size`, `leftIcon`, `reloadDocument`.
- In the app it navigates client side through the router (`LinkProvider`); middle-click and copy link still work.
