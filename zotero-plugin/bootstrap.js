/* global Zotero, Services */

var CorpusScribe;

function install() {}

async function startup({ id, version, rootURI }) {
  await Zotero.initializationPromise;

  Services.scriptloader.loadSubScript(rootURI + "content/corpus-scribe.js");
  CorpusScribe = globalThis.CorpusScribe;
  CorpusScribe.init({ id, version, rootURI });

  Zotero.PreferencePanes.register({
    pluginID: id,
    src: rootURI + "content/preferences.xhtml",
    label: "Corpus Scribe",
  });

  CorpusScribe.addToAllWindows();
}

function onMainWindowLoad({ window }) {
  if (CorpusScribe) {
    CorpusScribe.addToWindow(window);
  }
}

function onMainWindowUnload({ window }) {
  if (CorpusScribe) {
    CorpusScribe.removeFromWindow(window);
  }
}

function shutdown() {
  if (CorpusScribe) {
    CorpusScribe.removeFromAllWindows();
    CorpusScribe = undefined;
  }
  globalThis.CorpusScribe = undefined;
}

function uninstall() {}
