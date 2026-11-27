(() => {
  const root = document.documentElement;
  const themeButton = document.querySelector(".theme-toggle");
  const applyTheme = (theme) => {
    root.dataset.theme = theme;
    if (themeButton) {
      const dark = theme === "dark";
      themeButton.setAttribute("aria-pressed", String(dark));
      themeButton.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    }
  };
  applyTheme(root.dataset.theme || "light");
  themeButton?.addEventListener("click", () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("campuscare-theme", next);
    applyTheme(next);
  });

  document.querySelectorAll("[data-count-for]").forEach((counter) => {
    const input = document.getElementById(counter.dataset.countFor);
    if (!input) return;
    const update = () => {
      counter.textContent = `${input.value.length} / ${input.maxLength}`;
    };
    input.addEventListener("input", update);
    update();
  });

  const fileInput = document.querySelector("[data-image-preview-input]");
  const preview = document.querySelector("[data-image-preview]");
  if (fileInput && preview) {
    fileInput.addEventListener("change", () => {
      preview.replaceChildren();
      const file = fileInput.files?.[0];
      if (!file) return;
      const image = document.createElement("img");
      image.alt = "Selected issue attachment preview";
      image.className = "upload-preview-image";
      image.src = URL.createObjectURL(file);
      image.addEventListener("load", () => URL.revokeObjectURL(image.src), { once: true });
      preview.append(image);
    });
  }

  const deadlineToggle = document.querySelector("[data-custom-deadline-toggle]");
  const deadlineInput = document.querySelector("[data-custom-deadline-input]");
  if (deadlineToggle && deadlineInput) {
    const syncDeadlineInput = () => {
      deadlineInput.disabled = !deadlineToggle.checked;
      deadlineInput.required = deadlineToggle.checked;
    };
    deadlineToggle.addEventListener("change", syncDeadlineInput);
    syncDeadlineInput();
  }

  document.querySelectorAll("form[method='post']").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!form.checkValidity()) return;
      const button = form.querySelector("button[type='submit']");
      if (!button || button.disabled) return;
      form.setAttribute("aria-busy", "true");
      button.disabled = true;
      button.dataset.originalText = button.textContent.trim();
      button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Working…';
    });
  });

  const updateCountdowns = () => {
    document.querySelectorAll("[data-countdown]").forEach((element) => {
      const due = Date.parse(element.dataset.countdown);
      if (Number.isNaN(due)) return;
      const seconds = Math.trunc((due - Date.now()) / 1000);
      const abs = Math.abs(seconds);
      const days = Math.floor(abs / 86400);
      const hours = Math.floor((abs % 86400) / 3600);
      const minutes = Math.max(1, Math.floor((abs % 3600) / 60));
      const time = days ? `${days}d` : hours ? `${hours}h` : `${minutes}m`;
      element.textContent = seconds < 0 ? `Overdue by ${time}` : `Due in ${time}`;
      element.classList.toggle("urgency-overdue", seconds < 0);
      element.classList.toggle("urgency-soon", seconds >= 0 && seconds <= 86400);
      element.classList.toggle("urgency-on-track", seconds > 86400);
    });
  };
  updateCountdowns();
  window.setInterval(updateCountdowns, 60000);

  window.setTimeout(() => {
    document.querySelectorAll(".app-toast").forEach((element) => {
      bootstrap.Toast.getOrCreateInstance(element, { delay: 6000 }).hide();
    });
  }, 6000);
})();
