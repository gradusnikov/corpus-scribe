import { memo, startTransition, useDeferredValue, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import './App.css'

const browserApiBase = import.meta.env.VITE_DESKTOP_API_BASE ?? 'http://127.0.0.1:5000'

type DocumentSummary = {
  id: string
  title: string
  label: string | null
  articlePath: string
  notesPath: string | null
  bibPath: string | null
  highlightsPath: string | null
  highlightCount: number
  readingPdfPath: string | null
  sourcePdfPath: string | null
  sourceSite: string | null
  ingestedAt: string | null
  rating: number
  url: string | null
  canonicalUrl: string | null
  excerpt: string
}

type LibraryIndex = {
  root: string
  labels: string[]
  documents: DocumentSummary[]
}

type DocumentDetail = {
  summary: DocumentSummary
  markdown: string
  notesMarkdown: string
  highlights: Highlight[]
  bibliography: string
}

type SearchRequest = {
  root: string | null
  query: string
  label: string | null
}

type Highlight = {
  id: string
  text: string
  createdAt: string
  startOffset?: number
  endOffset?: number
}

type PendingSelection = {
  text: string
  startOffset: number
  endOffset: number
}

type NotesMode = 'markdown' | 'preview'

const emptyLibrary: LibraryIndex = {
  root: '',
  labels: [],
  documents: [],
}

const persistedRootKey = 'corpus-scribe.desktop.root'

async function desktopCommand<T>(command: string, payload: Record<string, unknown> = {}) {
  switch (command) {
    case 'default_library_root': {
      const response = await fetch(`${browserApiBase}/desktop/default_root`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to load default root')
      }
      return (data.root ?? null) as T
    }
    case 'scan_library': {
      const params = new URLSearchParams()
      if (payload.root) {
        params.set('root', String(payload.root))
      }
      const response = await fetch(`${browserApiBase}/desktop/library?${params.toString()}`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to scan library')
      }
      return ({ root: data.root, labels: data.labels, documents: data.documents } satisfies LibraryIndex) as T
    }
    case 'search_documents': {
      const response = await fetch(`${browserApiBase}/desktop/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload.request ?? {}),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to search library')
      }
      return (data.documents ?? []) as T
    }
    case 'load_document': {
      const params = new URLSearchParams()
      if (payload.articlePath) {
        params.set('articlePath', String(payload.articlePath))
      }
      const response = await fetch(`${browserApiBase}/desktop/document?${params.toString()}`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to load document')
      }
      return data.detail as T
    }
    case 'save_notes': {
      const response = await fetch(`${browserApiBase}/desktop/notes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          notesMarkdown: payload.notesMarkdown,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to save notes')
      }
      return data.notesPath as T
    }
    case 'generate_notes': {
      const response = await fetch(`${browserApiBase}/desktop/notes/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to generate notes')
      }
      return data as T
    }
    case 'save_highlights': {
      const response = await fetch(`${browserApiBase}/desktop/highlights`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          highlights: payload.highlights,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to save highlights')
      }
      return data as T
    }
    case 'update_rating': {
      const response = await fetch(`${browserApiBase}/desktop/rating`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          rating: payload.rating,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to update rating')
      }
      return data as T
    }
    default:
      throw new Error(`Unsupported desktop command in browser mode: ${command}`)
  }
}

function decodeFileUrlPath(value: string) {
  if (value.startsWith('file://')) {
    try {
      const url = new URL(value)
      return decodeURIComponent(url.pathname)
    } catch {
      return value.replace(/^file:\/+/, '/')
    }
  }
  return value
}

function articleDirectory(articlePath?: string | null) {
  if (!articlePath) {
    return ''
  }
  const normalized = articlePath.replace(/\\/g, '/')
  const lastSlash = normalized.lastIndexOf('/')
  return lastSlash >= 0 ? normalized.slice(0, lastSlash) : normalized
}

function joinArticlePath(baseDir: string, relativePath: string) {
  const normalizedBase = baseDir.replace(/\\/g, '/').replace(/\/+$/, '')
  const normalizedRelative = relativePath.replace(/\\/g, '/').replace(/^\.?\//, '')
  return `${normalizedBase}/${normalizedRelative}`
}

function toAssetUrl(value?: string | null, articlePath?: string | null) {
  if (!value) {
    return value
  }
  if (value.startsWith('http://') || value.startsWith('https://') || value.startsWith('data:')) {
    return value
  }
  if (value.startsWith('file://')) {
    const decoded = decodeFileUrlPath(value)
    return `${browserApiBase}/desktop/file?path=${encodeURIComponent(decoded)}`
  }

  const baseDir = articleDirectory(articlePath)
  const resolved = baseDir ? joinArticlePath(baseDir, value) : value
  if (resolved.startsWith('/')) {
    return `${browserApiBase}/desktop/file?path=${encodeURIComponent(resolved)}`
  }
  return `${browserApiBase}/desktop/file?path=${encodeURIComponent(resolved)}`
}

function resolveExternalHref(href?: string | null, sourceSite?: string | null) {
  if (!href) {
    return null
  }
  if (href.startsWith('http://') || href.startsWith('https://')) {
    return href
  }
  if (href.startsWith('//')) {
    return `https:${href}`
  }
  if (href.startsWith('/') && sourceSite) {
    return `https://${sourceSite}${href}`
  }
  return null
}

function normalizeReaderMarkdown(value: string) {
  return value
    .replace(/[\u2000-\u200a\u202f\u205f\u3000]/g, ' ')
    .replace(/[\u200b-\u200d\ufeff]/g, '')
    .replace(/\\\[((?:.|\n)*?)\\\]/g, (_, math: string) => `$$${math.trim()}$$`)
    .replace(/\\\((.+?)\\\)/g, (_, math: string) => `$${math.trim()}$`)
}

function quoteMarkdown(text: string) {
  return text
    .split('\n')
    .map((line) => `> ${line}`)
    .join('\n')
}

function splitLongSection(section: string, maxLength = 12000) {
  if (section.length <= maxLength) {
    return [section]
  }

  const paragraphs = section.split(/\n{2,}/g).map((part) => part.trim()).filter(Boolean)
  const chunks: string[] = []
  let current = ''

  for (const paragraph of paragraphs) {
    const next = current ? `${current}\n\n${paragraph}` : paragraph
    if (next.length > maxLength && current) {
      chunks.push(current)
      current = paragraph
      continue
    }
    current = next
  }

  if (current) {
    chunks.push(current)
  }

  return chunks.length ? chunks : [section]
}

function splitMarkdownIntoChunks(markdown: string, maxLength = 12000) {
  const normalized = markdown.trim()
  if (!normalized) {
    return []
  }

  const rawSections = normalized
    .split(/\n(?=#{1,6}\s)/g)
    .map((section) => section.trim())
    .filter(Boolean)

  const chunks: string[] = []
  let current = ''

  for (const section of rawSections) {
    const next = current ? `${current}\n\n${section}` : section
    if (next.length > maxLength && current) {
      chunks.push(...splitLongSection(current, maxLength))
      current = section
      continue
    }
    current = next
  }

  if (current) {
    chunks.push(...splitLongSection(current, maxLength))
  }

  return chunks.length ? chunks : [normalized]
}

function collectHighlightTextNodes(root: HTMLElement) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const text = node.textContent ?? ''
      if (!text) {
        return NodeFilter.FILTER_REJECT
      }
      const parent = node.parentElement
      if (!parent) {
        return NodeFilter.FILTER_REJECT
      }
      if (parent.closest('code, pre, script, style, .chunk-controls')) {
        return NodeFilter.FILTER_REJECT
      }
      return NodeFilter.FILTER_ACCEPT
    },
  })

  const nodes: Text[] = []
  let current = walker.nextNode()
  while (current) {
    nodes.push(current as Text)
    current = walker.nextNode()
  }
  return nodes
}

function unwrapHighlightMarks(root: HTMLElement) {
  const marks = Array.from(root.querySelectorAll('mark.inline-highlight'))
  for (const mark of marks) {
    const parent = mark.parentNode
    if (!parent) {
      continue
    }
    while (mark.firstChild) {
      parent.insertBefore(mark.firstChild, mark)
    }
    parent.removeChild(mark)
    parent.normalize()
  }
}

function resolveHighlightOffsets(root: HTMLElement, highlight: Highlight) {
  if (
    typeof highlight.startOffset === 'number' &&
    typeof highlight.endOffset === 'number' &&
    highlight.endOffset > highlight.startOffset
  ) {
    return { startOffset: highlight.startOffset, endOffset: highlight.endOffset }
  }

  const text = highlight.text.trim()
  if (!text) {
    return null
  }

  const articleText = root.textContent ?? ''
  const startOffset = articleText.indexOf(text)
  if (startOffset < 0) {
    return null
  }
  return { startOffset, endOffset: startOffset + text.length }
}

function applyInlineHighlights(root: HTMLElement, highlights: Highlight[]) {
  unwrapHighlightMarks(root)
  if (!highlights.length) {
    return
  }

  const resolved = highlights
    .map((highlight) => {
      const offsets = resolveHighlightOffsets(root, highlight)
      if (!offsets) {
        return null
      }
      return { highlight, ...offsets }
    })
    .filter((value): value is { highlight: Highlight; startOffset: number; endOffset: number } => Boolean(value))
    .sort((left, right) => right.startOffset - left.startOffset)

  for (const entry of resolved) {
    const textNodes = collectHighlightTextNodes(root)
    let accumulated = 0
    let startNode: Text | null = null
    let endNode: Text | null = null
    let startInnerOffset = 0
    let endInnerOffset = 0

    for (const node of textNodes) {
      const textLength = node.textContent?.length ?? 0
      const nextAccumulated = accumulated + textLength

      if (!startNode && entry.startOffset >= accumulated && entry.startOffset < nextAccumulated) {
        startNode = node
        startInnerOffset = entry.startOffset - accumulated
      }

      if (!endNode && entry.endOffset > accumulated && entry.endOffset <= nextAccumulated) {
        endNode = node
        endInnerOffset = entry.endOffset - accumulated
      }

      accumulated = nextAccumulated
      if (startNode && endNode) {
        break
      }
    }

    if (!startNode || !endNode) {
      continue
    }

    const range = document.createRange()
    range.setStart(startNode, startInnerOffset)
    range.setEnd(endNode, endInnerOffset)

    if (range.collapsed || !range.toString().trim()) {
      continue
    }

    const mark = document.createElement('mark')
    mark.className = 'inline-highlight'
    mark.dataset.highlightId = entry.highlight.id

    const contents = range.extractContents()
    mark.appendChild(contents)
    range.insertNode(mark)
    root.normalize()
  }
}

function getSelectionHighlightCandidate(root: HTMLElement): PendingSelection | null {
  const selection = window.getSelection()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
    return null
  }

  const range = selection.getRangeAt(0)
  const text = selection.toString().trim()
  if (text.length < 12) {
    return null
  }

  if (!(range.startContainer instanceof Text) || !(range.endContainer instanceof Text)) {
    return null
  }
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) {
    return null
  }

  const textNodes = collectHighlightTextNodes(root)
  let accumulated = 0
  let startOffset: number | null = null
  let endOffset: number | null = null

  for (const node of textNodes) {
    const textLength = node.textContent?.length ?? 0
    if (node === range.startContainer) {
      startOffset = accumulated + range.startOffset
    }
    if (node === range.endContainer) {
      endOffset = accumulated + range.endOffset
      break
    }
    accumulated += textLength
  }

  if (startOffset == null || endOffset == null || endOffset <= startOffset) {
    return null
  }

  return { text, startOffset, endOffset }
}

type ReaderContentProps = {
  loading: boolean
  markdown: string
  highlights: Highlight[]
  articlePath?: string | null
  sourceSite?: string | null
}

const ReaderContent = memo(function ReaderContent({
  loading,
  markdown,
  highlights,
  articlePath,
  sourceSite,
}: ReaderContentProps) {
  const [visibleChunkCount, setVisibleChunkCount] = useState(1)
  const chunks = useMemo(() => splitMarkdownIntoChunks(markdown), [markdown])
  const articleRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    setVisibleChunkCount(1)
  }, [markdown])

  useEffect(() => {
    if (!articleRef.current) {
      return
    }
    applyInlineHighlights(articleRef.current, highlights)
  }, [highlights, markdown, visibleChunkCount])

  if (loading) {
    return <div className="empty-state">Loading document…</div>
  }

  if (!markdown) {
    return <div className="empty-state">Choose a document from the library.</div>
  }

  return (
    <article className="markdown-body" ref={articleRef}>
      {chunks.slice(0, visibleChunkCount).map((chunk, index) => (
        <section className="markdown-chunk" key={`${index}-${chunk.slice(0, 32)}`}>
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkMath]}
            rehypePlugins={[[rehypeKatex, { output: 'html', throwOnError: false, strict: 'ignore' }]]}
            urlTransform={(url) => url}
            components={{
              img: ({ node: _node, src = '', alt = '', title, ...props }) => (
                <img
                  src={toAssetUrl(src, articlePath) ?? src}
                  alt={alt}
                  title={title}
                  loading="lazy"
                  decoding="async"
                  {...props}
                />
              ),
              a: ({ node: _node, href = '', children, title, ...props }) => (
                <a
                  href={resolveExternalHref(href, sourceSite) ?? '#'}
                  title={title}
                  onClick={(event) => {
                    const target = resolveExternalHref(href, sourceSite)
                    if (!target) {
                      event.preventDefault()
                      return
                    }
                    event.preventDefault()
                    window.open(target, '_blank', 'noopener,noreferrer')
                  }}
                  {...props}
                >
                  {children}
                </a>
              ),
            }}
          >
            {chunk}
          </ReactMarkdown>
        </section>
      ))}
      {visibleChunkCount < chunks.length ? (
        <div className="chunk-controls">
          <button
            className="ghost-button"
            onClick={() => setVisibleChunkCount((current) => Math.min(current + 1, chunks.length))}
          >
            Load next section
          </button>
          <button
            className="ghost-button"
            onClick={() => setVisibleChunkCount(chunks.length)}
          >
            Load full article
          </button>
          <span className="chunk-hint">
            Showing {visibleChunkCount} of {chunks.length} sections.
          </span>
        </div>
      ) : null}
    </article>
  )
})

function App() {
  const [rootPath, setRootPath] = useState('')
  const [library, setLibrary] = useState<LibraryIndex>(emptyLibrary)
  const [selectedLabel, setSelectedLabel] = useState('all')
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<DocumentSummary[] | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<DocumentDetail | null>(null)
  const [notesDraft, setNotesDraft] = useState('')
  const [notesMode, setNotesMode] = useState<NotesMode>('markdown')
  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null)
  const [highlights, setHighlights] = useState<Highlight[]>([])
  const [loadingLibrary, setLoadingLibrary] = useState(false)
  const [searching, setSearching] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [savingNotes, setSavingNotes] = useState(false)
  const [generatingNotes, setGeneratingNotes] = useState(false)
  const [savingHighlights, setSavingHighlights] = useState(false)
  const [status, setStatus] = useState('Loading library…')
  const [leftPaneWidth, setLeftPaneWidth] = useState(320)
  const [rightPaneWidth, setRightPaneWidth] = useState(400)
  const deferredSearch = useDeferredValue(search.trim().toLowerCase())
  const dragStateRef = useRef<{ pane: 'left' | 'right'; pointerId: number } | null>(null)

  useEffect(() => {
    void initializeLibrary()
  }, [])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      setNotesDraft('')
      setHighlights([])
      setPendingSelection(null)
      return
    }
    const summary = resolveDocumentSummary(activeId)
    if (summary) {
      void loadDocument(summary)
    }
  }, [activeId, library.documents, searchResults])

  useEffect(() => {
    const handleMouseUp = () => {
      const root = document.querySelector('.markdown-body')
      if (!(root instanceof HTMLElement)) {
        setPendingSelection(null)
        return
      }
      setPendingSelection(getSelectionHighlightCandidate(root))
    }

    window.addEventListener('mouseup', handleMouseUp)
    return () => window.removeEventListener('mouseup', handleMouseUp)
  }, [])

  useEffect(() => {
    void runSearch()
  }, [deferredSearch, selectedLabel, library.root])

  async function initializeLibrary() {
    try {
      const persistedRoot = window.localStorage.getItem(persistedRootKey)?.trim() ?? ''
      const defaultRoot = await desktopCommand<string | null>('default_library_root')
      const nextRoot = persistedRoot || defaultRoot || ''
      setRootPath(nextRoot)
      await refreshLibrary(nextRoot)
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  async function refreshLibrary(nextRoot = rootPath) {
    setLoadingLibrary(true)
    setStatus('Refreshing library…')
    try {
      const loaded = await desktopCommand<LibraryIndex>('scan_library', { root: nextRoot || null })
      setLibrary(loaded)
      setRootPath(loaded.root)
      window.localStorage.setItem(persistedRootKey, loaded.root)

      startTransition(() => {
        setActiveId((current) => {
          if (current && loaded.documents.some((doc) => doc.id === current)) {
            return current
          }
          return null
        })
      })

      setStatus(`Loaded ${loaded.documents.length} documents from ${loaded.root}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setLoadingLibrary(false)
    }
  }

  async function loadDocument(summary: DocumentSummary) {
    setLoadingDetail(true)
    setStatus(`Loading ${summary.title}…`)
    try {
      const loaded = await desktopCommand<DocumentDetail>('load_document', {
        articlePath: summary.articlePath,
      })
      setDetail(loaded)
      setNotesDraft(loaded.notesMarkdown)
      setHighlights(loaded.highlights)
      setPendingSelection(null)
      setStatus(`Opened ${loaded.summary.title}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setLoadingDetail(false)
    }
  }

  async function runSearch() {
    if (!library.root) {
      return
    }

    if (!deferredSearch) {
      setSearchResults(null)
      return
    }

    setSearching(true)
    try {
      const request: SearchRequest = {
        root: library.root || null,
        query: deferredSearch,
        label: selectedLabel === 'all' ? null : selectedLabel,
      }
      const results = await desktopCommand<DocumentSummary[]>('search_documents', { request })
      setSearchResults(results)
      setStatus(`Found ${results.length} matches for “${search.trim()}”`)
    } catch (error) {
      setSearchResults([])
      setStatus(stringifyError(error))
    } finally {
      setSearching(false)
    }
  }

  function applyNotesPathLocally(documentId: string, notesPath: string | null) {
    const update = (doc: DocumentSummary) =>
      doc.id === documentId ? { ...doc, notesPath } : doc
    setDetail((current) =>
      current ? { ...current, summary: { ...current.summary, notesPath } } : current,
    )
    setLibrary((current) => ({ ...current, documents: current.documents.map(update) }))
    setSearchResults((current) => (current ? current.map(update) : current))
  }

  async function saveNotes() {
    if (!detail) {
      return
    }
    setSavingNotes(true)
    setStatus('Saving notes…')
    try {
      const notesPath = await desktopCommand<string>('save_notes', {
        articlePath: detail.summary.articlePath,
        notesMarkdown: notesDraft,
      })
      applyNotesPathLocally(detail.summary.id, notesPath)
      setStatus(`Saved notes to ${notesPath}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setSavingNotes(false)
    }
  }

  async function generateNotes() {
    if (!detail) {
      return
    }
    setGeneratingNotes(true)
    setStatus('Generating notes…')
    try {
      const response = await desktopCommand<{ notesPath: string; notesMarkdown: string }>('generate_notes', {
        articlePath: detail.summary.articlePath,
      })
      setNotesDraft(response.notesMarkdown)
      setNotesMode('preview')
      applyNotesPathLocally(detail.summary.id, response.notesPath)
      setStatus(`Generated notes at ${response.notesPath}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setGeneratingNotes(false)
    }
  }

  async function persistHighlights(nextHighlights: Highlight[], successMessage: string) {
    if (!detail) {
      return
    }
    setSavingHighlights(true)
    try {
      const response = await desktopCommand<{ highlightsPath: string; highlights: Highlight[] }>('save_highlights', {
        articlePath: detail.summary.articlePath,
        highlights: nextHighlights,
      })
      setHighlights(response.highlights)
      const documentId = detail.summary.id
      const nextCount = response.highlights.length
      const applyHighlightMeta = (doc: DocumentSummary) =>
        doc.id === documentId
          ? {
              ...doc,
              highlightCount: nextCount,
              highlightsPath: response.highlightsPath,
            }
          : doc
      setDetail((current) =>
        current
          ? {
              ...current,
              highlights: response.highlights,
              summary: {
                ...current.summary,
                highlightCount: nextCount,
                highlightsPath: response.highlightsPath,
              },
            }
          : current,
      )
      setLibrary((current) => ({
        ...current,
        documents: current.documents.map(applyHighlightMeta),
      }))
      setSearchResults((current) => (current ? current.map(applyHighlightMeta) : current))
      setStatus(successMessage || `Saved highlights to ${response.highlightsPath}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setSavingHighlights(false)
    }
  }

  async function addHighlightFromSelection() {
    if (!detail || !pendingSelection?.text.trim()) {
      return
    }
    const createdAt = new Date().toISOString()
    const nextHighlights: Highlight[] = [
      {
        id: `${createdAt}-${Math.random().toString(36).slice(2, 8)}`,
        text: pendingSelection.text.trim(),
        createdAt,
        startOffset: pendingSelection.startOffset,
        endOffset: pendingSelection.endOffset,
      },
      ...highlights,
    ]
    setPendingSelection(null)
    window.getSelection()?.removeAllRanges()
    await persistHighlights(nextHighlights, 'Saved highlight')
  }

  async function deleteHighlight(highlightId: string) {
    const nextHighlights = highlights.filter((entry) => entry.id !== highlightId)
    await persistHighlights(nextHighlights, 'Deleted highlight')
  }

  function closeDocument() {
    setActiveId(null)
    setStatus('Closed document')
  }

  function buildDownloadUrl(path: string) {
    return `${browserApiBase}/desktop/file?path=${encodeURIComponent(path)}&download=1`
  }

  async function copyTextToClipboard(text: string, label: string) {
    const value = (text ?? '').toString()
    if (!value.trim()) {
      setStatus(`Nothing to copy for ${label}`)
      return
    }
    try {
      await navigator.clipboard.writeText(value)
      setStatus(`Copied ${label}`)
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  function scrollToHighlight(highlightId: string) {
    const mark = document.querySelector(
      `.markdown-body mark.inline-highlight[data-highlight-id="${CSS.escape(highlightId)}"]`,
    )
    if (!(mark instanceof HTMLElement)) {
      setStatus('Could not locate highlight in the current view')
      return
    }
    mark.scrollIntoView({ behavior: 'smooth', block: 'center' })
    mark.classList.add('is-flashing')
    window.setTimeout(() => mark.classList.remove('is-flashing'), 1200)
  }

  async function updateRating(nextRating: number) {
    if (!detail) {
      return
    }
    const previousRating = detail.summary.rating ?? 0
    const documentId = detail.summary.id
    setDetail((current) =>
      current ? { ...current, summary: { ...current.summary, rating: nextRating } } : current,
    )
    setLibrary((current) => ({
      ...current,
      documents: current.documents.map((doc) =>
        doc.id === documentId ? { ...doc, rating: nextRating } : doc,
      ),
    }))
    setSearchResults((current) =>
      current
        ? current.map((doc) => (doc.id === documentId ? { ...doc, rating: nextRating } : doc))
        : current,
    )
    try {
      await desktopCommand<{ rating: number }>('update_rating', {
        articlePath: detail.summary.articlePath,
        rating: nextRating,
      })
      setStatus(
        nextRating === 0
          ? 'Cleared rating'
          : `Saved rating: ${nextRating} star${nextRating === 1 ? '' : 's'}`,
      )
    } catch (error) {
      setDetail((current) =>
        current ? { ...current, summary: { ...current.summary, rating: previousRating } } : current,
      )
      setLibrary((current) => ({
        ...current,
        documents: current.documents.map((doc) =>
          doc.id === documentId ? { ...doc, rating: previousRating } : doc,
        ),
      }))
      setSearchResults((current) =>
        current
          ? current.map((doc) =>
              doc.id === documentId ? { ...doc, rating: previousRating } : doc,
            )
          : current,
      )
      setStatus(stringifyError(error))
    }
  }

  function insertSelectionAsQuote() {
    if (!pendingSelection?.text) {
      return
    }
    const quoted = quoteMarkdown(pendingSelection.text)
    const separator = notesDraft.trim().length ? '\n\n' : ''
    setNotesDraft((current) => `${current.trimEnd()}${separator}${quoted}\n`)
    setPendingSelection(null)
    window.getSelection()?.removeAllRanges()
  }

  function insertHighlightAsQuote(highlight: Highlight) {
    const quoted = quoteMarkdown(highlight.text)
    const separator = notesDraft.trim().length ? '\n\n' : ''
    setNotesDraft((current) => `${current.trimEnd()}${separator}${quoted}\n`)
  }

  function resolveDocumentSummary(documentId: string) {
    return (
      searchResults?.find((entry) => entry.id === documentId) ??
      library.documents.find((entry) => entry.id === documentId)
    )
  }

  function onSplitterPointerDown(pane: 'left' | 'right', event: React.PointerEvent<HTMLDivElement>) {
    if (window.innerWidth <= 1080) {
      return
    }
    dragStateRef.current = { pane, pointerId: event.pointerId }
    event.currentTarget.setPointerCapture(event.pointerId)
    document.body.classList.add('is-resizing')
  }

  function onSplitterPointerMove(event: React.PointerEvent<HTMLDivElement>) {
    if (!dragStateRef.current || dragStateRef.current.pointerId !== event.pointerId) {
      return
    }

    const minSidebarWidth = 260
    const minReaderWidth = 460
    const minNotesWidth = 300
    const gutterWidth = 20
    const shellWidth = window.innerWidth

    if (dragStateRef.current.pane === 'left') {
      const nextLeft = Math.min(
        Math.max(event.clientX, minSidebarWidth),
        shellWidth - rightPaneWidth - minReaderWidth - gutterWidth,
      )
      setLeftPaneWidth(nextLeft)
      return
    }

    const nextRight = Math.min(
      Math.max(shellWidth - event.clientX, minNotesWidth),
      shellWidth - leftPaneWidth - minReaderWidth - gutterWidth,
    )
    setRightPaneWidth(nextRight)
  }

  function onSplitterPointerUp(event: React.PointerEvent<HTMLDivElement>) {
    if (!dragStateRef.current || dragStateRef.current.pointerId !== event.pointerId) {
      return
    }
    dragStateRef.current = null
    event.currentTarget.releasePointerCapture(event.pointerId)
    document.body.classList.remove('is-resizing')
  }

  const visibleDocuments = deferredSearch
    ? searchResults ?? []
    : library.documents.filter((doc) => {
        if (selectedLabel !== 'all' && (doc.label ?? 'unlabeled') !== selectedLabel) {
          return false
        }
        return true
      })

  const shellStyle = {
    '--left-pane-width': `${leftPaneWidth}px`,
    '--right-pane-width': `${rightPaneWidth}px`,
  } as CSSProperties & Record<'--left-pane-width' | '--right-pane-width', string>
  const renderedMarkdown = useMemo(
    () => (detail ? normalizeReaderMarkdown(detail.markdown) : ''),
    [detail?.markdown],
  )
  const renderedNotesMarkdown = useMemo(
    () => normalizeReaderMarkdown(notesDraft),
    [notesDraft],
  )

  return (
    <div className="shell" style={shellStyle}>
      <aside className="sidebar">
        <div className="sidebar-scroll">
          <div className="brand">
            <p className="eyebrow">Desktop Reader</p>
            <h1>Corpus Scribe</h1>
            <p className="muted">
              Browse corpus bundles, read markdown, and keep notes beside the source.
            </p>
          </div>

          <div className="panel">
            <label className="field-label" htmlFor="root-path">Corpus root</label>
            <div className="inline-row">
              <input
                id="root-path"
                className="text-input"
                value={rootPath}
                onChange={(event) => setRootPath(event.target.value)}
                placeholder="/path/to/output"
              />
              <button className="ghost-button" onClick={() => void refreshLibrary()} disabled={loadingLibrary}>
                Reload
              </button>
            </div>
          </div>

          <div className="panel">
            <label className="field-label" htmlFor="search">Search</label>
            <input
              id="search"
              className="text-input"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Full-text search title, content, notes…"
            />

            <label className="field-label" htmlFor="label-filter">Label</label>
            <select
              id="label-filter"
              className="text-input"
              value={selectedLabel}
              onChange={(event) => setSelectedLabel(event.target.value)}
            >
              <option value="all">All labels</option>
              {library.labels.map((label) => (
                <option key={label} value={label}>
                  {label}
                </option>
              ))}
            </select>

            {deferredSearch ? (
              <p className="search-hint">
                {searching ? 'Searching local index…' : 'Using local full-text index.'}
              </p>
            ) : (
              <p className="search-hint">Browsing current library snapshot.</p>
            )}
          </div>

          <div className="document-list">
            {visibleDocuments.map((doc) => (
              <button
                key={doc.id}
                className={`document-card ${doc.id === activeId ? 'active' : ''}`}
                onClick={() => startTransition(() => setActiveId(doc.id))}
              >
                <div className="document-card-header">
                  <span className="document-label">{doc.label ?? 'unlabeled'}</span>
                  {doc.rating > 0 ? <StarRating value={doc.rating} /> : null}
                </div>
                <strong>{doc.title}</strong>
                <span className="document-meta">{doc.sourceSite ?? 'local'} {doc.ingestedAt ? `• ${doc.ingestedAt.slice(0, 10)}` : ''}</span>
                <p>{doc.excerpt}</p>
              </button>
            ))}
            {!visibleDocuments.length ? (
              <div className="empty-state">No documents match the current filter.</div>
            ) : null}
          </div>
        </div>
      </aside>

      <div
        className="splitter"
        role="separator"
        aria-label="Resize sidebar"
        onPointerDown={(event) => onSplitterPointerDown('left', event)}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
      />

      <main className="reader-pane">
        <header className="reader-header">
          <div className="reader-header-main">
            <p className="eyebrow">Reader</p>
            <h2>{detail?.summary.title ?? 'No document selected'}</h2>
            {detail ? (
              <div className="reader-header-row">
                <StarRating
                  value={detail.summary.rating ?? 0}
                  onChange={(next) => void updateRating(next)}
                  size="lg"
                  ariaLabel="Rate this article"
                />
                {detail.summary.url ? (
                  <a
                    className="reader-source-link"
                    href={detail.summary.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    title={detail.summary.url}
                  >
                    Open source{detail.summary.sourceSite ? ` · ${detail.summary.sourceSite}` : ''}
                  </a>
                ) : null}
              </div>
            ) : null}
          </div>
          <div className="reader-meta">
            {detail?.summary.readingPdfPath ? (
              <a
                className="badge badge-link"
                href={buildDownloadUrl(detail.summary.readingPdfPath)}
                target="_blank"
                rel="noopener noreferrer"
              >
                Download reading PDF
              </a>
            ) : null}
            {detail?.summary.sourcePdfPath ? (
              <a
                className="badge badge-link"
                href={buildDownloadUrl(detail.summary.sourcePdfPath)}
                target="_blank"
                rel="noopener noreferrer"
              >
                Download source PDF
              </a>
            ) : null}
            {detail?.summary.bibPath ? <Badge>bibliography</Badge> : null}
            {detail?.summary.highlightCount ? <Badge>{detail.summary.highlightCount} highlights</Badge> : null}
            {detail ? (
              <button
                className="ghost-button reader-close-button"
                onClick={closeDocument}
                aria-label="Close document"
              >
                Close
              </button>
            ) : null}
          </div>
        </header>

        {pendingSelection ? (
          <div className="selection-banner">
            <span>Selection ready to save or quote.</span>
            <div className="inline-row">
              <button className="ghost-button" onClick={() => void addHighlightFromSelection()} disabled={savingHighlights}>
                {savingHighlights ? 'Saving…' : 'Add highlight'}
              </button>
              <button className="ghost-button" onClick={insertSelectionAsQuote}>
                Quote selection
              </button>
            </div>
          </div>
        ) : null}

        <section className="markdown-card">
          <ReaderContent
            loading={loadingDetail}
            markdown={renderedMarkdown}
            highlights={highlights}
            articlePath={detail?.summary.articlePath}
            sourceSite={detail?.summary.sourceSite}
          />
        </section>
      </main>

      <div
        className="splitter"
        role="separator"
        aria-label="Resize notes pane"
        onPointerDown={(event) => onSplitterPointerDown('right', event)}
        onPointerMove={onSplitterPointerMove}
        onPointerUp={onSplitterPointerUp}
      />

      <aside className="notes-pane">
        <header className="notes-header">
          <div>
            <p className="eyebrow">Notes</p>
            <h2>Working notes</h2>
          </div>
          <div className="inline-row notes-actions">
            <button className="ghost-button" onClick={() => void generateNotes()} disabled={!detail || generatingNotes}>
              {generatingNotes ? 'Generating…' : 'Generate notes'}
            </button>
            <button className="primary-button" onClick={() => void saveNotes()} disabled={!detail || savingNotes}>
              {savingNotes ? 'Saving…' : 'Save notes'}
            </button>
          </div>
        </header>

        <div className="notes-scroll">
          <section className="info-card">
            <div className="notes-mode-row">
              <div className="notes-mode-toggle">
                <button
                  className={`ghost-button ${notesMode === 'markdown' ? 'is-active' : ''}`}
                  onClick={() => setNotesMode('markdown')}
                >
                  Markdown
                </button>
                <button
                  className={`ghost-button ${notesMode === 'preview' ? 'is-active' : ''}`}
                  onClick={() => setNotesMode('preview')}
                >
                  Preview
                </button>
              </div>
              <span className="search-hint">Markdown is canonical. Preview is read-only.</span>
            </div>

            {notesMode === 'markdown' ? (
              <textarea
                className="notes-editor"
                value={notesDraft}
                onChange={(event) => setNotesDraft(event.target.value)}
                placeholder="Write notes here. Use markdown. Select text in the article and quote it into this pane."
              />
            ) : (
              <div className="notes-preview markdown-card">
                {notesDraft.trim() ? (
                  <div className="markdown-body notes-preview-body">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkMath]}
                      rehypePlugins={[[rehypeKatex, { output: 'html', throwOnError: false, strict: 'ignore' }]]}
                      urlTransform={(url) => url}
                      components={{
                        img: ({ node: _node, src = '', alt = '', title, ...props }) => (
                          <img
                            src={toAssetUrl(src, detail?.summary.articlePath) ?? src}
                            alt={alt}
                            title={title}
                            loading="lazy"
                            decoding="async"
                            {...props}
                          />
                        ),
                        a: ({ node: _node, href = '', children, title, ...props }) => (
                          <a
                            href={resolveExternalHref(href, detail?.summary.sourceSite) ?? '#'}
                            title={title}
                            onClick={(event) => {
                              const target = resolveExternalHref(href, detail?.summary.sourceSite)
                              if (!target) {
                                event.preventDefault()
                                return
                              }
                              event.preventDefault()
                              window.open(target, '_blank', 'noopener,noreferrer')
                            }}
                            {...props}
                          >
                            {children}
                          </a>
                        ),
                      }}
                    >
                      {renderedNotesMarkdown}
                    </ReactMarkdown>
                  </div>
                ) : (
                  <div className="empty-state">No notes yet. Switch to Markdown to start writing.</div>
                )}
              </div>
            )}
          </section>

          <section className="info-card">
            <h3>Highlights</h3>
            {highlights.length ? (
              <div className="highlight-list">
                {highlights.map((highlight) => (
                  <article
                    className="highlight-card"
                    key={highlight.id}
                    onClick={() => scrollToHighlight(highlight.id)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        scrollToHighlight(highlight.id)
                      }
                    }}
                  >
                    <p>{highlight.text}</p>
                    <div className="highlight-actions">
                      <span className="highlight-meta">{formatHighlightDate(highlight.createdAt)}</span>
                      <div className="inline-row">
                        <button
                          className="ghost-button highlight-button"
                          onClick={(event) => {
                            event.stopPropagation()
                            void copyTextToClipboard(highlight.text, 'highlight')
                          }}
                        >
                          Copy
                        </button>
                        <button
                          className="ghost-button highlight-button"
                          onClick={(event) => {
                            event.stopPropagation()
                            insertHighlightAsQuote(highlight)
                          }}
                        >
                          Quote
                        </button>
                        <button
                          className="ghost-button highlight-button"
                          onClick={(event) => {
                            event.stopPropagation()
                            void deleteHighlight(highlight.id)
                          }}
                          disabled={savingHighlights}
                        >
                          Delete
                        </button>
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="search-hint">Select text in the article, then add it as a highlight.</p>
            )}
          </section>

          <section className="info-card">
            <div className="bibliography-header">
              <h3>Bibliography</h3>
              {detail?.bibliography ? (
                <button
                  className="ghost-button highlight-button"
                  onClick={() => void copyTextToClipboard(detail.bibliography, 'bibliography')}
                >
                  Copy
                </button>
              ) : null}
            </div>
            <pre>{detail?.bibliography || 'No bibliography file available.'}</pre>
          </section>
        </div>

        <footer className="status-bar">{status}</footer>
      </aside>
    </div>
  )
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="badge">{children}</span>
}

type StarRatingProps = {
  value: number
  onChange?: (rating: number) => void
  size?: 'sm' | 'lg'
  ariaLabel?: string
}

function StarRating({ value, onChange, size = 'sm', ariaLabel }: StarRatingProps) {
  const [hoverValue, setHoverValue] = useState<number | null>(null)
  const safeValue = Math.max(0, Math.min(5, Math.round(value || 0)))
  const interactive = typeof onChange === 'function'
  const displayValue = hoverValue ?? safeValue

  const classes = [
    'star-rating',
    size === 'lg' ? 'star-rating-lg' : '',
    interactive ? 'star-rating-interactive' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div
      className={classes}
      role={interactive ? 'radiogroup' : 'img'}
      aria-label={ariaLabel ?? `Rating: ${safeValue} out of 5`}
      onMouseLeave={interactive ? () => setHoverValue(null) : undefined}
    >
      {[1, 2, 3, 4, 5].map((position) => {
        const filled = position <= displayValue
        const className = `star ${filled ? 'filled' : ''}`
        if (!interactive) {
          return (
            <span key={position} className={className} aria-hidden="true">
              ★
            </span>
          )
        }
        return (
          <button
            key={position}
            type="button"
            className={className}
            onClick={(event) => {
              event.preventDefault()
              event.stopPropagation()
              onChange?.(safeValue === position ? 0 : position)
            }}
            onMouseEnter={() => setHoverValue(position)}
            aria-label={`Rate ${position} star${position === 1 ? '' : 's'}`}
            aria-pressed={safeValue >= position}
          >
            ★
          </button>
        )
      })}
    </div>
  )
}

function stringifyError(error: unknown) {
  if (typeof error === 'string') {
    return error
  }
  if (error instanceof Error) {
    return error.message
  }
  return 'Unexpected error'
}

function formatHighlightDate(value: string) {
  if (!value) {
    return 'saved'
  }
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) {
    return value
  }
  return parsed.toLocaleString()
}

export default App
