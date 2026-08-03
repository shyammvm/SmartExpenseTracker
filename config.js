// ---- Configuration: same values used across all pages ----
const CONFIG = {
  apiUrl: "https://web-production-7c3e8.up.railway.app",
  secret: "lalalalala5times"
};

// Wraps fetch with the auth header and consistent error handling, so each
// page just calls apiFetch('/path', {...}) instead of repeating this.
async function apiFetch(path, options = {}) {
  const res = await fetch(`${CONFIG.apiUrl}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'x-endpoint-secret': CONFIG.secret,
      ...(options.headers || {})
    }
  });
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
    // ignore if backend endpoint not available yet
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', updateConflictsBadge);
} else {
  updateConflictsBadge();
}
