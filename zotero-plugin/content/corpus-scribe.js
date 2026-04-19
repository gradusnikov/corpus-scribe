/* global Zotero, Services, IOUtils, ChromeUtils */

var CorpusScribe = {
  id: null,
  version: null,
  rootURI: null,
  addedElementIDs: new Map(),

  init({ id, version, rootURI }) {
    this.id = id;
    this.version = version;
    this.rootURI = rootURI;
  },

  log(msg) {
    try {
      Zotero.debug("Corpus Scribe: " + msg);
    } catch (_) {
      // ignore logging failures
    }
  },

  getPref(key, defaultValue) {
    try {
      const value = Zotero.Prefs.get("extensions.corpus-scribe." + key, true);
      if (value === undefined || value === null || value === "") {
        return defaultValue;
      }
      return value;
    } catch (_) {
      return defaultValue;
    }
  },

  setPref(key, value) {
    try {
      Zotero.Prefs.set("extensions.corpus-scribe." + key, value, true);
    } catch (err) {
      this.log("setPref failed: " + err);
    }
  },

  addToAllWindows() {
    const windows = Zotero.getMainWindows();
    for (const window of windows) {
      if (!window.ZoteroPane) continue;
      this.addToWindow(window);
    }
  },

  removeFromAllWindows() {
    const windows = Zotero.getMainWindows();
    for (const window of windows) {
      if (!window.ZoteroPane) continue;
      this.removeFromWindow(window);
    }
  },

  addToWindow(window) {
    const doc = window.document;
    const menu = doc.getElementById("zotero-itemmenu");
    if (!menu) return;

    if (this.addedElementIDs.has(window)) {
      this.removeFromWindow(window);
    }

    const ids = [];

    const separator = doc.createXULElement("menuseparator");
    separator.id = "corpus-scribe-separator";
    separator.classList.add("corpus-scribe");
    menu.appendChild(separator);
    ids.push(separator.id);

    const menuItem = doc.createXULElement("menuitem");
    menuItem.id = "corpus-scribe-send";
    menuItem.classList.add("corpus-scribe");
    menuItem.setAttribute("label", "Send to Corpus Scribe");
    menuItem.addEventListener("command", () => {
      this.sendSelectedItems(window).catch((err) => {
        this.log("send failed: " + err);
        try {
          Services.prompt.alert(window, "Corpus Scribe", String(err && err.message || err));
        } catch (_) {
          window.alert("Corpus Scribe error: " + (err && err.message || err));
        }
      });
    });
    menu.appendChild(menuItem);
    ids.push(menuItem.id);

    this.addedElementIDs.set(window, ids);
  },

  removeFromWindow(window) {
    const ids = this.addedElementIDs.get(window) || [];
    const doc = window.document;
    for (const id of ids) {
      const el = doc.getElementById(id);
      if (el) el.remove();
    }
    this.addedElementIDs.delete(window);
  },

  async sendSelectedItems(window) {
    const selected = window.ZoteroPane.getSelectedItems() || [];
    const items = selected.filter((item) => item.isRegularItem());
    if (items.length === 0) {
      Services.prompt.alert(
        window,
        "Corpus Scribe",
        "Select at least one Zotero item (not an attachment).",
      );
      return;
    }

    const apiBase = (this.getPref("apiBase", "") || "").replace(/\/+$/, "");
    const apiKey = this.getPref("apiKey", "");
    if (!apiBase || !apiKey) {
      Services.prompt.alert(
        window,
        "Corpus Scribe",
        "Configure the API base URL and API key in the Corpus Scribe preferences pane first.",
      );
      return;
    }

    const label = await this.promptForLabel(window, apiBase, apiKey);
    if (!label) return;

    const progress = new Zotero.ProgressWindow({ closeOnClick: true });
    progress.changeHeadline("Corpus Scribe");
    progress.show();

    let success = 0;
    let failed = 0;
    for (const item of items) {
      const title = item.getField("title") || "Untitled";
      try {
        progress.addDescription("Sending: " + title);
        await this.sendItem(window, item, label, apiBase, apiKey);
        progress.addDescription("Sent: " + title);
        success += 1;
      } catch (err) {
        this.log(`send failed for "${title}": ${err}`);
        progress.addDescription(
          "Failed: " + title + " — " + (err && err.message || err),
        );
        failed += 1;
      }
    }

    progress.addDescription(`Done. Sent ${success}, failed ${failed}.`);
    progress.startCloseTimer(6000);
  },

  // Zotero 8's bootstrap sandbox does not reliably expose fetch/FormData/Blob,
  // but a main-window chrome context always does. Route HTTP through a window.
  xhrJson(window, method, url) {
    return new Promise((resolve, reject) => {
      const xhr = new window.XMLHttpRequest();
      xhr.open(method, url, true);
      xhr.responseType = "text";
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
          } catch (err) {
            reject(err);
          }
        } else {
          reject(new Error(`HTTP ${xhr.status}: ${xhr.responseText || xhr.statusText}`));
        }
      };
      xhr.onerror = () => reject(new Error("Network error: " + url));
      xhr.send();
    });
  },

  xhrUpload(window, url, formData) {
    return new Promise((resolve, reject) => {
      const xhr = new window.XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.responseType = "text";
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(xhr.responseText ? JSON.parse(xhr.responseText) : {});
          } catch (err) {
            reject(err);
          }
        } else {
          reject(new Error(`HTTP ${xhr.status}: ${xhr.responseText || xhr.statusText}`));
        }
      };
      xhr.onerror = () => reject(new Error("Network error: " + url));
      xhr.send(formData);
    });
  },

  async readPdfBytes(filePath) {
    if (typeof IOUtils !== "undefined" && IOUtils && typeof IOUtils.read === "function") {
      return IOUtils.read(filePath);
    }
    // Fallback for older builds: read as a binary string and convert.
    const binStr = await Zotero.File.getBinaryContentsAsync(filePath);
    const out = new Uint8Array(binStr.length);
    for (let i = 0; i < binStr.length; i += 1) {
      out[i] = binStr.charCodeAt(i) & 0xff;
    }
    return out;
  },

  async promptForLabel(window, apiBase, apiKey) {
    let labels = [];
    try {
      const data = await this.xhrJson(
        window,
        "GET",
        `${apiBase}/labels?apiKey=${encodeURIComponent(apiKey)}`,
      );
      if (data && Array.isArray(data.labels)) {
        labels = data.labels;
      }
    } catch (err) {
      this.log("failed to load labels: " + err);
    }

    const defaultLabel = this.getPref("defaultLabel", "") || "";
    const hint = labels.length
      ? `Existing labels:\n  ${labels.join("\n  ")}\n\nLabel to file these items under:`
      : "Label to file these items under:";

    const result = { value: defaultLabel };
    const ok = Services.prompt.prompt(
      window,
      "Corpus Scribe",
      hint,
      result,
      null,
      { value: false },
    );
    if (!ok) return null;
    const label = (result.value || "").trim();
    if (!label) return null;
    this.setPref("defaultLabel", label);
    return label;
  },

  async findPdfAttachment(item) {
    const attachmentIDs = item.getAttachments() || [];
    for (const id of attachmentIDs) {
      const att = await Zotero.Items.getAsync(id);
      if (!att) continue;
      if (att.attachmentContentType === "application/pdf") {
        return att;
      }
    }
    return null;
  },

  async sendItem(window, item, label, apiBase, apiKey) {
    const attachment = await this.findPdfAttachment(item);
    if (!attachment) {
      throw new Error("No PDF attachment on this item");
    }
    const filePath = await attachment.getFilePathAsync();
    if (!filePath) {
      throw new Error("PDF attachment file is not available locally");
    }

    const bytes = await this.readPdfBytes(filePath);
    const fileName = filePath.split(/[\\/]/).pop() || "document.pdf";

    const metadata = this.buildMetadata(item);
    const note = await this.collectWorkingNotes(item);

    // Use the window's FormData/Blob so the payload is constructed in a
    // context where these Web APIs are guaranteed to exist (Zotero 8's
    // bootstrap sandbox does not expose them).
    const form = new window.FormData();
    form.append(
      "file",
      new window.Blob([bytes], { type: "application/pdf" }),
      fileName,
    );
    form.append("apiKey", apiKey);
    form.append("label", label);
    form.append("pageSize", this.getPref("pageSize", "a5"));
    form.append("sourceName", fileName);
    form.append("metadata", JSON.stringify(metadata));
    if (note) {
      form.append("note", note);
    }

    return this.xhrUpload(window, `${apiBase}/save_pdf_upload`, form);
  },

  buildMetadata(item) {
    const creators = (item.getCreators && item.getCreators()) || [];
    const authors = creators
      .map((c) => {
        if (c.name) return c.name;
        return [c.firstName, c.lastName].filter(Boolean).join(" ");
      })
      .filter(Boolean);

    const field = (key) => {
      try {
        const value = item.getField(key);
        return (value == null ? "" : String(value)).trim();
      } catch (_) {
        return "";
      }
    };

    return {
      title: field("title"),
      author: authors.join(", "),
      doi: field("DOI"),
      url: field("url"),
      date: field("date"),
      container_title: field("publicationTitle") || field("bookTitle"),
      publisher: field("publisher"),
      volume: field("volume"),
      issue: field("issue"),
      pages: field("pages"),
      abstract: field("abstractNote"),
      zoteroKey: item.key || "",
      itemType: item.itemType || "",
    };
  },

  async collectWorkingNotes(item) {
    const noteIDs = item.getNotes ? item.getNotes() : [];
    if (!noteIDs || noteIDs.length === 0) return "";

    const pieces = [];
    for (const id of noteIDs) {
      const note = await Zotero.Items.getAsync(id);
      if (!note) continue;
      const html = note.getNote ? note.getNote() : "";
      const md = this.htmlToMarkdown(html);
      if (md.trim()) {
        pieces.push(md.trim());
      }
    }
    return pieces.join("\n\n---\n\n");
  },

  htmlToMarkdown(html) {
    if (!html) return "";
    let text = String(html);
    text = text.replace(/<br\s*\/?>/gi, "\n");
    text = text.replace(/<\/p>\s*<p[^>]*>/gi, "\n\n");
    text = text.replace(/<p[^>]*>/gi, "");
    text = text.replace(/<\/p>/gi, "\n\n");
    text = text.replace(/<h1[^>]*>/gi, "# ");
    text = text.replace(/<h2[^>]*>/gi, "## ");
    text = text.replace(/<h3[^>]*>/gi, "### ");
    text = text.replace(/<h4[^>]*>/gi, "#### ");
    text = text.replace(/<h5[^>]*>/gi, "##### ");
    text = text.replace(/<h6[^>]*>/gi, "###### ");
    text = text.replace(/<\/h[1-6]>/gi, "\n\n");
    text = text.replace(/<(strong|b)[^>]*>/gi, "**");
    text = text.replace(/<\/(strong|b)>/gi, "**");
    text = text.replace(/<(em|i)[^>]*>/gi, "*");
    text = text.replace(/<\/(em|i)>/gi, "*");
    text = text.replace(/<li[^>]*>/gi, "- ");
    text = text.replace(/<\/li>/gi, "\n");
    text = text.replace(/<\/?(ul|ol)[^>]*>/gi, "\n");
    text = text.replace(
      /<a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi,
      "[$2]($1)",
    );
    text = text.replace(/<blockquote[^>]*>/gi, "> ");
    text = text.replace(/<\/blockquote>/gi, "\n");
    text = text.replace(/<pre[^>]*>/gi, "\n```\n");
    text = text.replace(/<\/pre>/gi, "\n```\n");
    text = text.replace(/<code[^>]*>/gi, "`");
    text = text.replace(/<\/code>/gi, "`");
    text = text.replace(/<[^>]+>/g, "");
    text = text.replace(/&nbsp;/g, " ");
    text = text.replace(/&amp;/g, "&");
    text = text.replace(/&lt;/g, "<");
    text = text.replace(/&gt;/g, ">");
    text = text.replace(/&quot;/g, '"');
    text = text.replace(/&#39;/g, "'");
    text = text.replace(/\n{3,}/g, "\n\n");
    return text.trim();
  },
};

globalThis.CorpusScribe = CorpusScribe;
