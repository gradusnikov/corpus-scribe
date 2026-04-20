const DEFAULTS = {
  apiBase: "http://127.0.0.1:5000",
  apiKey: "api-key-1234",
  readerUrl: "http://127.0.0.1:1420",
  lastLabel: "",
};

const NEW_LABEL_VALUE = "__new__";

const btnOpenScribe = document.getElementById("btn-open-scribe");
const btnSave = document.getElementById("btn-save");
const statusEl = document.getElementById("status");
const pageInfoEl = document.getElementById("page-info");
const connectionDot = document.getElementById("connection-dot");
const labelSelect = document.getElementById("label-select");
const labelNewInput = document.getElementById("label-new");
const labelNewCancel = document.getElementById("label-new-cancel");

const cfgApiBase = document.getElementById("cfg-api-base");
const cfgApiKey = document.getElementById("cfg-api-key");
const cfgReaderUrl = document.getElementById("cfg-reader-url");

function getTrimmedValue(el) {
  return el.value.trim();
}

function setStorage(values) {
  return new Promise((resolve) => {
    chrome.storage.local.set(values, resolve);
  });
}

function getSelectedLabel() {
  if (!labelNewInput.classList.contains("hidden")) {
    return getTrimmedValue(labelNewInput);
  }
  const v = labelSelect.value;
  return v === NEW_LABEL_VALUE ? "" : v;
}

// ---------------------------------------------------------------------------
// Label dropdown
// ---------------------------------------------------------------------------

function renderLabelSelect(labels, selected) {
  labelSelect.replaceChildren();
  const sorted = [...new Set(labels.map((l) => l.trim()).filter(Boolean))].sort((a, b) =>
    a.localeCompare(b, undefined, { sensitivity: "base" }),
  );
  if (!sorted.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.disabled = true;
    opt.selected = true;
    opt.textContent = "No labels - create one below";
    labelSelect.appendChild(opt);
  }
  for (const label of sorted) {
    const opt = document.createElement("option");
    opt.value = label;
    opt.textContent = label;
    if (label === selected) opt.selected = true;
    labelSelect.appendChild(opt);
  }
  const divider = document.createElement("option");
  divider.disabled = true;
  divider.textContent = "----------";
  labelSelect.appendChild(divider);
  const newOpt = document.createElement("option");
  newOpt.value = NEW_LABEL_VALUE;
  newOpt.textContent = "+ New label...";
  labelSelect.appendChild(newOpt);
}

labelSelect.addEventListener("change", () => {
  if (labelSelect.value === NEW_LABEL_VALUE) {
    labelSelect.classList.add("hidden");
    labelNewInput.classList.remove("hidden");
    labelNewCancel.classList.remove("hidden");
    labelNewInput.focus();
    return;
  }
  setStorage({ lastLabel: labelSelect.value });
});

labelNewCancel.addEventListener("click", () => {
  labelNewInput.classList.add("hidden");
  labelNewCancel.classList.add("hidden");
  labelSelect.classList.remove("hidden");
  labelSelect.value = labelSelect.querySelector("option:not([disabled])")?.value || "";
});

labelNewInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    labelNewCancel.click();
  }
});

// ---------------------------------------------------------------------------
// Detection helpers
// ---------------------------------------------------------------------------

function isLikelyPdfUrl(url) {
  return /(?:\.pdf(?:$|[?#])|\/pdf(?:\/|$))/i.test(url || "");
}

function isArxivAbsUrl(url) {
  return /^(https?:\/\/arxiv\.org)\/abs\/[^/?#]+/i.test(url || "");
}

function isPmcOrPubmedArticleUrl(url) {
  return /^(https?:\/\/(?:(?:www\.)?ncbi\.nlm\.nih\.gov\/(?:pmc\/articles\/PMC\d+|pubmed\/\d+)|pmc\.ncbi\.nlm\.nih\.gov\/articles\/PMC\d+))(?:[/?#]|$)/i.test(
    url || "",
  );
}

async function detectSourcePdfUrl(tab) {
  if (!tab?.id || !tab?.url || tab.url.startsWith("chrome://")) return null;
  try {
    const [result] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        const metaPdf = document.querySelector('meta[name="citation_pdf_url"]');
        if (metaPdf && metaPdf.content) return metaPdf.content;
        const altLink = document.querySelector(
          'link[rel~="alternate"][type="application/pdf"]',
        );
        if (altLink && altLink.href) return altLink.href;
        const pageUrl = window.location.href;
        const pmcMatch = pageUrl.match(
          /^(https?:\/\/(?:www\.)?ncbi\.nlm\.nih\.gov)\/pmc\/articles\/(PMC\d+)/i,
        );
        if (pmcMatch) {
          const pdfAnchor = document.querySelector(
            'a[href*="/pmc/articles/"][href$=".pdf"], a.int-view[href$=".pdf"]',
          );
          if (pdfAnchor && pdfAnchor.href) return pdfAnchor.href;
        }
        const sdLink = document.querySelector(
          'a.pdf-download-btn-link, a.download-pdf-link, a[aria-label*="Download PDF"]',
        );
        if (sdLink && sdLink.href) return sdLink.href;
        const springerLink = document.querySelector(
          'a[data-track-action="download pdf"], a.c-pdf-download__link',
        );
        if (springerLink && springerLink.href) return springerLink.href;
        return null;
      },
    });
    const candidate = result?.result;
    if (!candidate) return null;
    try {
      return new URL(candidate, tab.url).href;
    } catch {
      return candidate;
    }
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Backend API
// ---------------------------------------------------------------------------

async function fetchExistingLabels(apiBase, apiKey) {
  try {
    const resp = await fetch(`${apiBase}/labels?apiKey=${encodeURIComponent(apiKey)}`);
    if (!resp.ok) return [];
    const data = await resp.json();
    return Array.isArray(data.labels) ? data.labels : [];
  } catch {
    return [];
  }
}

async function fetchCapabilities(apiBase, apiKey) {
  try {
    const resp = await fetch(`${apiBase}/capabilities?apiKey=${encodeURIComponent(apiKey)}`);
    if (!resp.ok) return null;
    return await resp.json();
  } catch {
    return null;
  }
}

async function lookupExistingArticle(apiBase, apiKey, url) {
  if (!url) return null;
  try {
    const resp = await fetch(
      `${apiBase}/lookup_url?apiKey=${encodeURIComponent(apiKey)}&url=${encodeURIComponent(url)}`,
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    return data && data.exists ? data : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Connection check + page info
// ---------------------------------------------------------------------------

async function checkConnection(apiBase, apiKey) {
  const caps = await fetchCapabilities(apiBase, apiKey);
  if (caps) {
    connectionDot.className = "dot dot-ok";
    connectionDot.title = "Backend connected";
    return caps;
  }
  connectionDot.className = "dot dot-error";
  connectionDot.title = "Cannot reach backend";
  return null;
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function updatePageInfo() {
  const cfg = await getConfig();
  const tab = await getActiveTab();
  if (!tab || !tab.url || tab.url.startsWith("chrome://")) {
    pageInfoEl.textContent = "";
    return;
  }

  const title = tab.title || new URL(tab.url).hostname;
  const host = new URL(tab.url).hostname.replace(/^www\./, "");
  let type = "Web article";

  if (isLikelyPdfUrl(tab.url)) {
    type = "PDF";
  } else if (isArxivAbsUrl(tab.url)) {
    type = "arXiv article";
  } else if (isPmcOrPubmedArticleUrl(tab.url)) {
    type = "PMC / PubMed";
  } else {
    const detectedPdf = await detectSourcePdfUrl(tab);
    if (detectedPdf) {
      type = "Article (source PDF available)";
    }
  }

  pageInfoEl.textContent = `${type} - ${host}`;
}

// ---------------------------------------------------------------------------
// Page data capture
// ---------------------------------------------------------------------------

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

async function getPdfData(overrideUrl = null) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url || tab.url.startsWith("chrome://")) {
    throw new Error("Cannot access this PDF");
  }

  const pdfUrl = overrideUrl || tab.url;
  const cookies = await chrome.cookies.getAll({ url: pdfUrl });
  const cookieDict = cookies.reduce((acc, c) => {
    if (!acc[c.domain]) acc[c.domain] = {};
    acc[c.domain][c.name] = c.value;
    return acc;
  }, {});

  const rawName = pdfUrl.split("/").pop()?.split(/[?#]/)[0] || "";
  const sourceName = rawName && rawName.toLowerCase().endsWith(".pdf") ? rawName : `${rawName || "document"}.pdf`;

  return {
    url: pdfUrl,
    cookies: cookieDict,
    sourceName,
  };
}

// ---------------------------------------------------------------------------
// Save
// ---------------------------------------------------------------------------

function setStatus(msg, cls) {
  statusEl.textContent = msg;
  statusEl.className = "status " + (cls || "");
}

function setButtonsDisabled(disabled) {
  btnOpenScribe.disabled = disabled;
  btnSave.disabled = disabled;
}

async function getConfig() {
  const stored = await new Promise((resolve) => {
    chrome.storage.local.get(DEFAULTS, resolve);
  });
  return {
    apiBase: getTrimmedValue(cfgApiBase) || stored.apiBase,
    apiKey: getTrimmedValue(cfgApiKey) || stored.apiKey,
    readerUrl: getTrimmedValue(cfgReaderUrl) || stored.readerUrl,
  };
}

async function saveCurrentPage({ allowReuse = true } = {}) {
  setButtonsDisabled(true);
  setStatus("Checking...");

  try {
    const cfg = await getConfig();
    const label = getSelectedLabel();
    if (!label) {
      throw new Error("Choose or create a label first");
    }

    const tab = await getActiveTab();
    let savePdfNatively = isLikelyPdfUrl(tab?.url || "");
    let detectedPdfUrl = null;
    const preferArticleSave = isArxivAbsUrl(tab?.url || "") || isPmcOrPubmedArticleUrl(tab?.url || "");
    if (!savePdfNatively && tab?.url && !preferArticleSave) {
      detectedPdfUrl = await detectSourcePdfUrl(tab);
      if (detectedPdfUrl) {
        savePdfNatively = true;
      }
    }

    if (allowReuse && tab?.url && !preferArticleSave) {
      const existing = await lookupExistingArticle(cfg.apiBase, cfg.apiKey, tab.url);
      if (existing) {
        setStatus(`Already saved: ${existing.title}`, "success");
        return { data: existing, cfg, reused: true };
      }
      if (detectedPdfUrl) {
        const existingPdf = await lookupExistingArticle(cfg.apiBase, cfg.apiKey, detectedPdfUrl);
        if (existingPdf) {
          setStatus(`Already saved: ${existingPdf.title}`, "success");
          return { data: existingPdf, cfg, reused: true };
        }
      }
    }

    setStatus(savePdfNatively ? "Downloading PDF..." : "Extracting article...");
    const pageData = savePdfNatively
      ? await getPdfData(detectedPdfUrl)
      : await getPageData();

    const body = {
      apiKey: cfg.apiKey,
      label,
      ...pageData,
    };

    const resp = await fetch(`${cfg.apiBase}${savePdfNatively ? "/save_pdf" : "/save_local"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err || `HTTP ${resp.status}`);
    }

    const data = await resp.json();

    await setStorage({ lastLabel: label });

    // Refresh labels from backend (the new label now exists on disk)
    const backendLabels = await fetchExistingLabels(cfg.apiBase, cfg.apiKey);
    renderLabelSelect(backendLabels, label);

    const engine = data?.metadata?.ocr_engine;
    const suffix = savePdfNatively && engine ? ` (${engine})` : "";
    setStatus(`Saved: ${data.title}${suffix}`, "success");
    chrome.notifications.create({
      type: "basic",
      iconUrl: "assets/icons/128.png",
      title: "Saved",
      message: `"${data.title}" saved to ${label}${suffix}`,
    });
    return { data, cfg, reused: false };
  } catch (err) {
    setStatus(err.message, "error");
    chrome.notifications.create({
      type: "basic",
      iconUrl: "assets/icons/128.png",
      title: "Error",
      message: err.message,
    });
    return null;
  } finally {
    setButtonsDisabled(false);
  }
}

async function openInCorpusScribe(articlePath, readerBase, label) {
  const base = (readerBase || "").replace(/\/+$/, "");
  if (!base) {
    setStatus("Corpus Scribe URL not set", "error");
    return;
  }
  let fragment = `open=${encodeURIComponent(articlePath)}`;
  if (label) fragment += `&label=${encodeURIComponent(label)}`;
  const targetUrl = `${base}/#${fragment}`;
  const existing = await chrome.tabs.query({ url: `${base}/*` });
  if (existing && existing.length > 0) {
    const tab = existing[0];
    await chrome.tabs.update(tab.id, { url: targetUrl, active: true });
    if (tab.windowId) {
      await chrome.windows.update(tab.windowId, { focused: true });
    }
    return;
  }
  await chrome.tabs.create({ url: targetUrl });
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

btnSave.addEventListener("click", () => {
  void saveCurrentPage();
});

btnOpenScribe.addEventListener("click", async () => {
  const result = await saveCurrentPage();
  if (!result) return;
  const articlePath = result.data?.md || result.data?.primary;
  if (!articlePath) {
    setStatus("Saved, but article path missing", "error");
    return;
  }
  await openInCorpusScribe(articlePath, result.cfg.readerUrl, getSelectedLabel());
});

for (const el of [cfgApiBase, cfgApiKey, cfgReaderUrl]) {
  el.addEventListener("change", () => {
    const values = {
      apiBase: getTrimmedValue(cfgApiBase),
      apiKey: getTrimmedValue(cfgApiKey),
      readerUrl: getTrimmedValue(cfgReaderUrl),
    };
    setStorage(values);
  });
}

for (const el of [cfgApiBase, cfgApiKey]) {
  el.addEventListener("change", async () => {
    const cfg = await getConfig();
    const caps = await checkConnection(cfg.apiBase, cfg.apiKey);
    if (caps) {
      const backendLabels = await fetchExistingLabels(cfg.apiBase, cfg.apiKey);
      renderLabelSelect(backendLabels, getSelectedLabel());
    }
    await updatePageInfo();
  });
}

chrome.tabs.onActivated.addListener(() => void updatePageInfo());
chrome.tabs.onUpdated.addListener(() => void updatePageInfo());

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------

async function loadPopupState() {
  const cfg = await new Promise((resolve) => {
    chrome.storage.local.get(DEFAULTS, resolve);
  });

  cfgApiBase.value = cfg.apiBase;
  cfgApiKey.value = cfg.apiKey;
  cfgReaderUrl.value = cfg.readerUrl;

  const caps = await checkConnection(cfg.apiBase, cfg.apiKey);

  const backendLabels = caps
    ? await fetchExistingLabels(cfg.apiBase, cfg.apiKey)
    : [];
  renderLabelSelect(backendLabels, cfg.lastLabel || "");
  await updatePageInfo();
}

loadPopupState();
