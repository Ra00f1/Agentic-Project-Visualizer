import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { BackendGate } from "./components/BackendGate";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <BackendGate>
      <App />
    </BackendGate>
  </React.StrictMode>,
);
