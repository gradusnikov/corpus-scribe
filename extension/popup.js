const DEFAULTS = {
  apiBase: "http://127.0.0.1:5000",
  apiKey: "api-key-1234",
  readerUrl: "http://127.0.0.1:1420",
  pageSize: "a5",
  lastLabel: "",
  labelHistory: [],
};

const btnOpenScribe = document.getElementById("btn-open-scribe");
const btnSave = document.getElementById("btn-save");
const statusEl = document.getElementById("status");
const docHintEl = document.getElementById("doc-hint");
const pageSizeEl = document.getElementById("page-size");
const labelEl = document.getElementById("label-name");
const labelOptionsEl = document.getElementById("label-options");

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

function collectConfigFromInputs() {
  return {
    apiBase: getTrimmedValue(cfgApiBase),
    apiKey: getTrimmedValue(cfgApiKey),
    readerUrl: getTrimmedValue(cfgReaderUrl),
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

  const detectedPdf = await detectSourcePdfUrl(tab);
  if (detectedPdf && !isPmcOrPubmedArticleUrl(tab.url)) {
    const capabilities = await fetchCapabilities(cfg.apiBase, cfg.apiKey);
    const engine = capabilities?.pdfOcr?.engine || "pdftotext";
    const extra = engine === "mistral" ? "Mistral OCR" : "pdftotext fallback";
    setDocHint(`Detected: Web article (source PDF available)\nExtraction: ${extra}`);
    return;
  }

  if (detectedPdf && isPmcOrPubmedArticleUrl(tab.url)) {
    setDocHint("Detected: PMC/PubMed article\nExtraction: HTML article (preferred over source PDF)");
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
  cfgReaderUrl.value = cfg.readerUrl;
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
  [cfgReaderUrl, "readerUrl"],
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
  btnOpenScribe.disabled = disabled;
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

async function saveCurrentPage({ allowReuse = true } = {}) {
  setButtonsDisabled(true);
  setStatus("Checking...");

  try {
    const cfg = await getConfig();
    if (!cfg.lastLabel) {
      throw new Error("Choose a label before saving");
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
        const existingPdf = await lookupExistingArticle(
          cfg.apiBase,
          cfg.apiKey,
          detectedPdfUrl,
        );
        if (existingPdf) {
          setStatus(`Already saved: ${existingPdf.title}`, "success");
          return { data: existingPdf, cfg, reused: true };
        }
      }
    }

    setStatus(savePdfNatively ? "Downloading source PDF..." : "Extracting article...");
    const pageData = savePdfNatively
      ? await getPdfData(detectedPdfUrl)
      : await getPageData();

    if (savePdfNatively && !detectedPdfUrl) {
      setStatus("Downloading PDF...");
    }

    const body = {
      apiKey: cfg.apiKey,
      pageSize: cfg.pageSize,
      label: cfg.lastLabel,
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

    const labels = uniqueLabels([cfg.lastLabel, ...(cfg.labelHistory || []), ...(await fetchExistingLabels(cfg.apiBase, cfg.apiKey))]);
    await setStorage({
      apiBase: cfg.apiBase,
      apiKey: cfg.apiKey,
      readerUrl: cfg.readerUrl,
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

async function openInCorpusScribe(articlePath, readerBase) {
  const base = (readerBase || "").replace(/\/+$/, "");
  if (!base) {
    setStatus("Corpus Scribe URL not set", "error");
    return;
  }
  const targetUrl = `${base}/#open=${encodeURIComponent(articlePath)}`;
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
  await openInCorpusScribe(articlePath, result.cfg.readerUrl);
});
chrome.tabs.onActivated.addListener(() => void updateDocHint());
chrome.tabs.onUpdated.addListener(() => void updateDocHint());
