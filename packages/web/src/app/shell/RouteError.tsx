import { isRouteErrorResponse, useRouteError } from "react-router";
import { ErrorState, PageHeader } from "../../lib/ui";

/** Error boundary for a page: a plain message, Reload, and the raw error collapsed. */
export function RouteError() {
  const error = useRouteError();
  const detail = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? `${error.name}: ${error.message}`
      : String(error);
  return (
    <>
      <PageHeader title="This page could not be shown" />
      <ErrorState
        title="Something went wrong on this page"
        message="Reload the page to try again. If it keeps happening, open another page from the sidebar and report what you were doing."
        onRetry={() => window.location.reload()}
        retryLabel="Reload"
        detail={detail}
      />
    </>
  );
}
