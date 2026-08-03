// ---- Configuration: same values used across all pages ----
const CONFIG = {
  apiUrl: "https://web-production-7c3e8.up.railway.app",
};

function getSecret() {
  return localStorage.getItem("app_passcode") || "";
}

function showPasscodeModal(errorMsg = "") {
  if (document.getElementById("passcodeOverlay")) {
    const errEl = document.getElementById("passcodeError");
    if (errEl) errEl.textContent = errorMsg;
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "passcode-overlay";
  overlay.id = "passcodeOverlay";
  overlay.innerHTML = `
    <div class="passcode-card">
      <header style="text-align:center;margin-bottom:8px;">
        <div class="eyebrow">SECURITY LOCK</div>
        <h1>App Passcode</h1>
      </header>
      <div style="font-size:11px;color:var(--ink-faint);letter-spacing:1.5px;text-transform:uppercase;">Enter key to access your ledger</div>
      <input type="password" id="passcodeInput" placeholder="••••••••" autofocus autocomplete="current-password">
      <button type="button" id="passcodeBtn">Unlock Ledger</button>
      <div class="passcode-error" id="passcodeError">${errorMsg}</div>
    </div>
  `;
  document.body.prepend(overlay);

  const input = document.getElementById("passcodeInput");
  const btn = document.getElementById("passcodeBtn");
  const submit = () => {
    const val = input.value.trim();
    if (!val) {
      document.getElementById("passcodeError").textContent = "Enter a passcode first";
      return;
    }
    localStorage.setItem("app_passcode", val);
    overlay.remove();
    location.reload();
  };

  btn.addEventListener("click", submit);
  input.addEventListener("keyup", (e) => {
    if (e.key === "Enter") submit();
  });
}

// Wraps fetch with the auth header and consistent error handling
async function apiFetch(path, options = {}) {
  const secret = getSecret();
  if (!secret) {
    showPasscodeModal();
    throw new Error("Passcode required");
  }

  const res = await fetch(`${CONFIG.apiUrl}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'x-endpoint-secret': secret,
      ...(options.headers || {})
    }
  });

  if (res.status === 401) {
    localStorage.removeItem("app_passcode");
    showPasscodeModal("Unauthorized: Invalid passcode");
    throw new Error("Unauthorized: Invalid passcode");
  }

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function formatRupees(n) {
  return `₹${Number(n).toFixed(2)}`;
}

async function updateConflictsBadge() {
  try {
    const data = await apiFetch('/conflicts/count');
    const badgeEl = document.getElementById('conflictBadge');
    if (badgeEl) {
      if (data.count > 0) {
        badgeEl.textContent = data.count;
        badgeEl.style.display = 'inline-block';
      } else {
        badgeEl.style.display = 'none';
      }
    }
  } catch (e) {
    // ignore if locked or error
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    if (!getSecret()) showPasscodeModal();
    else updateConflictsBadge();
  });
} else {
  if (!getSecret()) showPasscodeModal();
  else updateConflictsBadge();
}

