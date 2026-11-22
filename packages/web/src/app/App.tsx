import { useState } from "react";
import { RouterProvider } from "react-router";
import { createAppRouter } from "./router";

/**
 * The Control Center (spec §3), served at `/ui/`. The theme and the root
 * QueryClient (whoami) come from `src/main.tsx`; owner data lives in the
 * owner epoch's own client (`lib/epoch.tsx`).
 */
export function App() {
  const [router] = useState(createAppRouter);
  return <RouterProvider router={router} />;
}

export default App;
