// ---- Configuration: same values used across all pages ----
const CONFIG = {
  apiUrl: (typeof window !== "undefined" && (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"))
    ? "http://127.0.0.1:8000"
    : "https://smartexpensetracker-vtkb.onrender.com",
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

function showLoadingToast(msg = "Connecting to server...") {
  let toast = document.getElementById("apiLoadingToast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "apiLoadingToast";
    toast.style.cssText = `
      position: fixed;
      top: 16px;
      right: 16px;
      background: #1f5c4f;
      color: #fff;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 12px;
      font-family: sans-serif;
      z-index: 9999;
      box-shadow: 0 4px 12px rgba(0,0,0,0.2);
    `;
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.style.display = "block";
}

function hideLoadingToast() {
  const toast = document.getElementById("apiLoadingToast");
  if (toast) toast.style.display = "none";
}

// Wraps fetch with the auth header, retries, and consistent error handling
async function apiFetch(path, options = {}, retries = 2) {
  const secret = getSecret();
  if (!secret) {
    showPasscodeModal();
    throw new Error("Passcode required");
  }

  const baseUrl = CONFIG.apiUrl.replace(/\/+$/, "");
  const url = `${baseUrl}${path}`;

  let toastTimer = setTimeout(() => {
    showLoadingToast("Server starting up, please wait...");
  }, 2500);

  for (let attempt = 0; attempt <= retries; attempt++) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 45000);

    try {
      const res = await fetch(url, {
        ...options,
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          'x-endpoint-secret': secret,
          ...(options.headers || {})
        }
      });
      clearTimeout(timeoutId);
      clearTimeout(toastTimer);
      hideLoadingToast();

      if (res.status === 401) {
        localStorage.removeItem("app_passcode");
        showPasscodeModal("Unauthorized: Invalid passcode");
        throw new Error("Unauthorized: Invalid passcode");
      }

      if ([502, 503, 504].includes(res.status) && attempt < retries) {
        showLoadingToast("Server waking up, retrying request...");
        await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
        continue;
      }

      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Request failed (${res.status})`);
      }
      return await res.json();
    } catch (err) {
      clearTimeout(timeoutId);
      if (attempt < retries && (err.name === 'AbortError' || err.name === 'TypeError' || err.message.includes('fetch'))) {
        showLoadingToast("Waking up Render server, retrying...");
        await new Promise(r => setTimeout(r, 3000 * (attempt + 1)));
        continue;
      }
      clearTimeout(toastTimer);
      hideLoadingToast();
      throw err;
    }
  }
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

// Theme management (Dark Mode / Warm Paper)
function applyTheme(theme) {
  if (theme === 'dark') {
    document.documentElement.classList.add('theme-dark');
  } else {
    document.documentElement.classList.remove('theme-dark');
  }
}

// Immediate theme application to prevent white flicker
const currentSavedTheme = (typeof localStorage !== 'undefined' && localStorage.getItem('app_theme')) || 'light';
applyTheme(currentSavedTheme);

function toggleTheme() {
  const isDark = document.documentElement.classList.contains('theme-dark');
  const next = isDark ? 'light' : 'dark';
  localStorage.setItem('app_theme', next);
  applyTheme(next);
  updateThemeButton();
}

function updateThemeButton() {
  const btn = document.getElementById('themeToggleBtn');
  if (btn) {
    const isDark = document.documentElement.classList.contains('theme-dark');
    btn.innerHTML = isDark ? '☀️' : '🌙';
    btn.title = isDark ? 'Switch to Warm Paper theme' : 'Switch to Dark Mode';
  }
}

function injectThemeButton() {
  const header = document.querySelector('.app-header');
  if (header && !document.getElementById('themeToggleBtn')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'themeToggleBtn';
    btn.className = 'theme-toggle-btn';
    btn.onclick = toggleTheme;
    header.appendChild(btn);
    updateThemeButton();
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function(m) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m];
  });
}

// Service Worker handling: unregister & purge cache on localhost, enable in production
if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
  const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
  if (isLocal) {
    navigator.serviceWorker.getRegistrations().then(registrations => {
      for (let reg of registrations) reg.unregister();
    });
    if (typeof caches !== 'undefined') {
      caches.keys().then(keys => {
        for (let key of keys) caches.delete(key);
      });
    }
  } else if (window.location.protocol.startsWith('http')) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('./sw.js').catch(err => {
        console.warn('SW registration skipped:', err);
      });
    });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    injectThemeButton();
    if (!getSecret()) showPasscodeModal();
    else updateConflictsBadge();
  });
} else {
  injectThemeButton();
  if (!getSecret()) showPasscodeModal();
  else updateConflictsBadge();
}


