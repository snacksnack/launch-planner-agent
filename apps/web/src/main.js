// Placeholder entrypoint. The interactive Gantt (critical path, provenance
// detail panel, freeze-window shading) is built in P1.7 (RC1-188).
import "./style.css";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

const app = document.querySelector("#app");
app.innerHTML = `
  <main>
    <h1>launch-planner-agent</h1>
    <p class="tagline">LLM proposes, Python validates, human approves.</p>
    <p class="status">Gantt UI scaffold — the interactive timeline lands in P1.7.</p>
    <p class="health">API: <span id="health">checking…</span></p>
  </main>
`;

fetch(`${API_BASE}/healthz`)
  .then((r) => r.json())
  .then((d) => {
    document.querySelector("#health").textContent = `${d.status} (${d.environment})`;
  })
  .catch(() => {
    document.querySelector("#health").textContent = "unreachable";
  });
