const DEFAULTS = {
  apiBase: "http://127.0.0.1:5000",
  apiKey: "api-key-1234",
  email: "",
  kindleEmail: "",
  pageSize: "a5",
  lastLabel: "",
  labelHistory: [],
};

const btnKindle = document.getElementById("btn-kindle");
const btnSave = document.getElementById("btn-save");
const statusEl = document.getElementById("status");
const docHintEl = document.getElementById("doc-hint");
const pageSizeEl = document.getElementById("page-size");
const labelEl = document.getElementById("label-name");
const labelOptionsEl = document.getElementById("label-options");

const cfgApiBase = document.getElementById("cfg-api-base");
const cfgApiKey = document.getElementById("cfg-api-key");
const cfgEmail = document.getElementById("cfg-email");
const cfgKindleEmail = document.getElementById("cfg-kindle-email");

function getTrimmedValue(el) {
  return el.value.trim();
}

function setStorage(values) {
  return new Promise((resolve) => {
    chrome.storage.local.set(values, resolve);
  });
}

function collectConfigFromInputs() {
  return {
    apiBase: getTrimmedValue(cfgApiBase),
    apiKey: getTrimmedValue(cfgApiKey),
    email: getTrimmedValue(cfgEmail),
    kindleEmail: getTrimmedValue(cfgKindleEmail),
    pageSize: pageSizeEl.value,
    lastLabel: getTrimmedValue(labelEl),
  };
}

function uniqueLabels(labels) {
  return [...new Set(labels.map((label) => label.trim()).filter(Boolean))].sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: "base" }),
  );
}

function renderLabelOptions(labels) {
  labelOptionsEl.replaceChildren();
  for (const label of uniqueLabels(labels)) {
    const option = document.createElement("option");
    option.value = label;
    labelOptionsEl.appendChild(option);
  }
}

function setDocHint(msg) {
  docHintEl.textContent = msg || "";
}

function isLikelyPdfUrl(url) {
  return /(?:\.pdf(?:$|[?#])|\/pdf(?:\/|$))/i.test(url || "");
}

async function fetchExistingLabels(apiBase, apiKey) {
  try {
    const resp = await fetch(`${apiBase}/labels?apiKey=${encodeURIComponent(apiKey)}`);
    if (!resp.ok) {
      return [];
    }
    const data = await resp.json();
    return Array.isArray(data.labels) ? data.labels : [];
  } catch {
    return [];
  }
}

async function fetchCapabilities(apiBase, apiKey) {
  try {
    const resp = await fetch(`${apiBase}/capabilities?apiKey=${encodeURIComponent(apiKey)}`);
    if (!resp.ok) {
      return { pdfOcr: { available: false, engine: "pdftotext", fallback: "pdftotext" } };
    }
    return await resp.json();
  } catch {
    return { pdfOcr: { available: false, engine: "pdftotext", fallback: "pdftotext" } };
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function updateDocHint() {
  const cfg = await getConfig();
  const tab = await getActiveTab();
  if (!tab || !tab.url || tab.url.startsWith("chrome://")) {
    setDocHint("");
    return;
  }

  if (isLikelyPdfUrl(tab.url)) {
    const capabilities = await fetchCapabilities(cfg.apiBase, cfg.apiKey);
    const engine = capabilities?.pdfOcr?.engine || "pdftotext";
    const extra = engine === "mistral" ? "Mistral OCR" : "pdftotext fallback";
    setDocHint(`Detected: PDF\nExtraction: ${extra}`);
    return;
  }

  setDocHint("Detected: Web article");
}

async function loadPopupState() {
  const cfg = await new Promise((resolve) => {
    chrome.storage.local.get(DEFAULTS, resolve);
  });

  pageSizeEl.value = cfg.pageSize;
  cfgApiBase.value = cfg.apiBase;
  cfgApiKey.value = cfg.apiKey;
  cfgEmail.value = cfg.email;
  cfgKindleEmail.value = cfg.kindleEmail;
  labelEl.value = cfg.lastLabel;

  const backendLabels = await fetchExistingLabels(cfg.apiBase, cfg.apiKey);
  renderLabelOptions([...(cfg.labelHistory || []), ...backendLabels]);
  await updateDocHint();
}

loadPopupState();

// Auto-save on change
for (const [el, key] of [
  [pageSizeEl, "pageSize"],
  [cfgApiBase, "apiBase"],
  [cfgApiKey, "apiKey"],
  [cfgEmail, "email"],
  [cfgKindleEmail, "kindleEmail"],
]) {
  el.addEventListener("change", () => {
    setStorage({ [key]: getTrimmedValue(el) });
  });
}

for (const el of [cfgApiBase, cfgApiKey]) {
  el.addEventListener("change", async () => {
    const cfg = collectConfigFromInputs();
    const backendLabels = await fetchExistingLabels(cfg.apiBase, cfg.apiKey);
    chrome.storage.local.get(DEFAULTS, (stored) => {
      renderLabelOptions([...(stored.labelHistory || []), ...backendLabels]);
    });
    await updateDocHint();
  });
}

function setStatus(msg, cls) {
  statusEl.textContent = msg;
  statusEl.className = "status " + (cls || "");
}

function setButtonsDisabled(disabled) {
  btnKindle.disabled = disabled;
  btnSave.disabled = disabled;
}

async function getConfig() {
  const stored = await new Promise((resolve) => {
    chrome.storage.local.get(DEFAULTS, resolve);
  });
  return {
    ...stored,
    ...collectConfigFromInputs(),
  };
}

async function getPageData() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.url.startsWith("chrome://")) {
    throw new Error("Cannot capture this page");
  }

  const cookies = await chrome.cookies.getAll({ url: tab.url });
  const cookieDict = cookies.reduce((acc, c) => {
    if (!acc[c.domain]) acc[c.domain] = {};
    acc[c.domain][c.name] = c.value;
    return acc;
  }, {});

  const [result] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => document.querySelector("html").outerHTML,
  });

  return {
    html: result.result,
    url: tab.url,
    cookies: cookieDict,
  };
}

async function getPdfData() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || tab.url.startsWith("chrome://")) {
    throw new Error("Cannot access this PDF");
  }

  const cookies = await chrome.cookies.getAll({ url: tab.url });
  const cookieDict = cookies.reduce((acc, c) => {
    if (!acc[c.domain]) acc[c.domain] = {};
    acc[c.domain][c.name] = c.value;
    return acc;
  }, {});

  const rawName = tab.url.split("/").pop()?.split(/[?#]/)[0] || "";
  const sourceName = rawName && rawName.toLowerCase().endsWith(".pdf") ? rawName : `${rawName || "document"}.pdf`;

  return {
    url: tab.url,
    cookies: cookieDict,
    sourceName,
  };
}

async function sendToEndpoint(endpoint) {
  setButtonsDisabled(true);
  setStatus("Extracting article...");

  try {
    const cfg = await getConfig();
    const tab = await getActiveTab();
    const savePdfNatively = endpoint === "/save_local" && isLikelyPdfUrl(tab?.url || "");
    const pageData = savePdfNatively ? await getPdfData() : await getPageData();
    if (endpoint === "/save_local" && !cfg.lastLabel) {
      throw new Error("Choose a label before saving");
    }

    if (savePdfNatively) {
      setStatus("Downloading PDF...");
    }

    const body = {
      apiKey: cfg.apiKey,
      email: cfg.email,
      kindleEmail: cfg.kindleEmail,
      pageSize: cfg.pageSize,
      label: cfg.lastLabel,
      ...pageData,
    };

    const resp = await fetch(`${cfg.apiBase}${savePdfNatively ? "/save_pdf" : endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err || `HTTP ${resp.status}`);
    }

    const data = await resp.json();

    if (endpoint === "/generate_pdf") {
      setStatus("Sent to Kindle!", "success");
      chrome.notifications.create({
        type: "basic",
        iconUrl: "assets/icons/128.png",
        title: "Success",
        message: `"${data.title}" sent to Kindle`,
      });
    } else {
      const labels = uniqueLabels([cfg.lastLabel, ...(cfg.labelHistory || []), ...(await fetchExistingLabels(cfg.apiBase, cfg.apiKey))]);
      await setStorage({
        apiBase: cfg.apiBase,
        apiKey: cfg.apiKey,
        email: cfg.email,
        kindleEmail: cfg.kindleEmail,
        pageSize: cfg.pageSize,
        lastLabel: cfg.lastLabel,
        labelHistory: labels,
      });
      renderLabelOptions(labels);
      const engine = data?.metadata?.ocr_engine;
      const suffix = savePdfNatively && engine ? ` (${engine})` : "";
      setStatus(`Saved: ${data.title}${suffix}`, "success");
      chrome.notifications.create({
        type: "basic",
        iconUrl: "assets/icons/128.png",
        title: "Saved",
        message: `PDF + MD saved to "${cfg.lastLabel}" for "${data.title}"${suffix}`,
      });
    }
  } catch (err) {
    setStatus(err.message, "error");
    chrome.notifications.create({
      type: "basic",
      iconUrl: "assets/icons/128.png",
      title: "Error",
      message: err.message,
    });
  } finally {
    setButtonsDisabled(false);
  }
}

btnKindle.addEventListener("click", () => sendToEndpoint("/generate_pdf"));
btnSave.addEventListener("click", () => sendToEndpoint("/save_local"));
chrome.tabs.onActivated.addListener(() => void updateDocHint());
chrome.tabs.onUpdated.addListener(() => void updateDocHint());
