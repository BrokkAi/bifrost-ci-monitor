import React from "react";
import ReactDOM from "react-dom/client";
import {
  QueryClient,
  QueryClientProvider,
  keepPreviousData,
} from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "sonner";
import { Tooltip } from "radix-ui";
import App from "./App";
import { ApiError } from "./api/client";
import "./index.css";
const client = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: () =>
        document.visibilityState === "visible" ? 15000 : false,
      refetchIntervalInBackground: false,
      staleTime: 10000,
      placeholderData: keepPreviousData,
      retry: (count, error) =>
        count < 1 &&
        (!(error instanceof ApiError) ||
          (error.status >= 500 && error.status !== 503)),
      refetchOnWindowFocus: true,
    },
    mutations: { retry: false },
  },
});
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <Tooltip.Provider>
          <App />
          <Toaster richColors position="bottom-right" />
        </Tooltip.Provider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
