const result = document.querySelector("#result");
document.querySelector("#parse").addEventListener("click", async () => {
  const raw = document.querySelector("#code").value.trim();
  result.textContent = "Checking…";
  const response = await fetch(`/api/v1/scan/parse?raw=${encodeURIComponent(raw)}`, { method: "POST" });
  const payload = await response.json();
  result.textContent = JSON.stringify(payload, null, 2);
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js");

