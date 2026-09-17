import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/app/App";
import "@/design/tokens.css";
import "@/design/skins.css";
import "@/design/base.css";

const el = document.getElementById("root");
if (!el) throw new Error("缺少 #root 挂载点");

createRoot(el).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
