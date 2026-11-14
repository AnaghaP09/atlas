import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AppProvider } from "./app/context";
import { bootstrapIdentity } from "./identity/bootstrap";
import { RouterProvider } from "./lib/router";
import { applyTheme, storedTheme } from "./lib/theme";
import "./styles.css";

applyTheme(storedTheme());

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

void bootstrapIdentity().then((boot) => {
  if (boot.kind === "redirect") {
    window.location.assign(boot.to);
    return;
  }
  createRoot(root).render(
    <StrictMode>
      <AppProvider identity={boot.identity}>
        <RouterProvider>
          <App />
        </RouterProvider>
      </AppProvider>
    </StrictMode>,
  );
});
