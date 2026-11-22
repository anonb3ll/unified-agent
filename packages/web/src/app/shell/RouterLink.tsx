import { Link } from "react-router";
import type { UiLinkProps } from "../../lib/ui";

/**
 * The app's link for `src/ui` primitives: app paths navigate client side
 * through react-router (real `<a href="/ui/...">`), absolute URLs and
 * `reloadDocument` links are plain anchors.
 */
export function RouterLink({ href, reloadDocument, ref, ...rest }: UiLinkProps) {
  if (reloadDocument || /^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith("#")) {
    return <a ref={ref} href={href} {...rest} />;
  }
  return <Link ref={ref} to={href} {...rest} />;
}
