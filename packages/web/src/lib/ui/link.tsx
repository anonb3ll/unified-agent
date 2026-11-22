import { createContext, useContext, type AnchorHTMLAttributes, type ComponentType, type ReactNode, type Ref } from "react";

/**
 * Props every link in `src/lib/ui` renders with. `href` is an app path
 * ("/work") or an absolute URL. `reloadDocument` asks for a full page load
 * (for a page outside the client router).
 */
export type UiLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & {
  href: string;
  reloadDocument?: boolean;
  ref?: Ref<HTMLAnchorElement>;
};

export type UiLinkComponent = ComponentType<UiLinkProps>;

function PlainLink({ reloadDocument: _reload, ref, ...props }: UiLinkProps) {
  void _reload;
  return <a ref={ref} {...props} />;
}

const LinkContext = createContext<UiLinkComponent>(PlainLink);

/**
 * `src/ui` does not depend on a router. The app supplies its router link here,
 * so primitives render real `<a href>` elements that navigate client side,
 * while the design-system bundle and tests fall back to a plain anchor.
 */
export function LinkProvider({ component, children }: { component: UiLinkComponent; children: ReactNode }) {
  return <LinkContext.Provider value={component}>{children}</LinkContext.Provider>;
}

/** A real anchor that uses the app's router when one is provided. */
export function UiLink(props: UiLinkProps) {
  const Component = useContext(LinkContext);
  return <Component {...props} />;
}
