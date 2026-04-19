import { memo, startTransition, useCallback, useDeferredValue, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { keymap, EditorView } from '@codemirror/view'
import { markdown } from '@codemirror/lang-markdown'
import { EditorSelection } from '@codemirror/state'
import ReactMarkdown, { type Components } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import './App.css'

const browserApiBase = import.meta.env.VITE_DESKTOP_API_BASE ?? 'http://127.0.0.1:5000'

const markdownRemarkPlugins = [remarkGfm, remarkMath]
const markdownRehypePlugins: [typeof rehypeKatex, { output: string; throwOnError: boolean; strict: string }][] = [
  [rehypeKatex, { output: 'htmlAndMathml', throwOnError: false, strict: 'ignore' }],
]
const markdownUrlTransform = (url: string) => url

type DocumentSummary = {
  id: string
  title: string
  label: string | null
  articlePath: string
  notesPath: string | null
  notesPending?: boolean
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
  authors: string | null
  year: string | null
  journal: string | null
  affinityScore?: number
}

type NotesGenerationStatus = {
  state: 'idle' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'error'
  notesMarkdown: string
  previewAvailable: boolean
  canStop: boolean
  message: string | null
  notesPath: string | null
  error: string | null
  updatedAt: string | null
}

type NotesGenerationStrategy = 'replace' | 'append' | 'fuse'

type LibraryIndex = {
  root: string
  labels: string[]
  documents: DocumentSummary[]
}

type FrontmatterValue = string | number | boolean | null | FrontmatterValue[]

type RelatedLink = {
  id: string
  targetPath: string
  targetTitle: string
  note: string
  createdAt: string
}

type DocumentDetail = {
  summary: DocumentSummary
  markdown: string
  notesMarkdown: string
  highlights: Highlight[]
  bibliography: string
  frontmatter: Record<string, FrontmatterValue>
  related: RelatedLink[]
}

type SearchRequest = {
  root: string | null
  query: string
  label: string | null
}

type SearchResponse = {
  documents: DocumentSummary[]
  total: number
  truncated: boolean
}

type Highlight = {
  id: string
  text: string
  createdAt: string
  startOffset?: number
  endOffset?: number
  comment?: string
  variant?: 'noise'
  kind?: 'element'
  elementType?: 'img' | 'table'
  elementIndex?: number
}

type PendingSelection = {
  text: string
  startOffset: number
  endOffset: number
}

type NotesMode = 'markdown' | 'preview'
type ReaderMode = 'rendered' | 'source'
type ThemeMode = 'light' | 'dark'
type HighlightsView = 'list' | 'single'
type NoiseVariant = 'highlight' | 'noise'
type HeadingEntry = { level: number; text: string; index: number; offset: number }
type SourceFindMatch = { from: number; to: number }
type FindTarget = 'reader' | 'notes'

const emptyLibrary: LibraryIndex = {
  root: '',
  labels: [],
  documents: [],
}

const persistedRootKey = 'corpus-scribe.desktop.root'
const persistedThemeKey = 'corpus-scribe.desktop.theme'
const persistedFontSizeKey = 'corpus-scribe.desktop.readerFontSize'
const persistedFocusKey = 'corpus-scribe.desktop.focusMode'
const persistedScrollKeyPrefix = 'corpus-scribe.desktop.scroll.'
const persistedLeftPaneKey = 'corpus-scribe.desktop.leftPaneWidth'
const persistedRightPaneKey = 'corpus-scribe.desktop.rightPaneWidth'
const persistedNotesTopKey = 'corpus-scribe.desktop.notesTopHeight'
const persistedLabelKey = 'corpus-scribe.desktop.selectedLabel'
const persistedNotesModeKey = 'corpus-scribe.desktop.notesMode'
const persistedReaderModeKey = 'corpus-scribe.desktop.readerMode'
const persistedHighlightsViewKey = 'corpus-scribe.desktop.highlightsView'
const persistedOpenTabsKey = 'corpus-scribe.desktop.openTabs'
const persistedHideNoiseKey = 'corpus-scribe.desktop.hideNoise'
const persistedRefsAreNoiseKey = 'corpus-scribe.desktop.refsAreNoise'
const persistedReadingPdfPageSizeKey = 'corpus-scribe.desktop.readingPdfPageSize'

const MIN_FONT_SIZE = 0.85
const MAX_FONT_SIZE = 1.5
const DEFAULT_FONT_SIZE = 1.06
const FONT_SIZE_STEP = 0.05
const DEFAULT_LEFT_PANE = 320
const DEFAULT_RIGHT_PANE = 400
const MIN_LEFT_PANE = 260
const MIN_RIGHT_PANE = 300
const DEFAULT_NOTES_TOP_HEIGHT = 360
const MIN_NOTES_TOP_HEIGHT = 180
const MIN_NOTES_BOTTOM_HEIGHT = 180

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
      const documents = Array.isArray(data.documents) ? data.documents : []
      const total =
        typeof data.total === 'number' && Number.isFinite(data.total)
          ? data.total
          : documents.length
      return ({ documents, total, truncated: Boolean(data.truncated) } satisfies SearchResponse) as T
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
    case 'save_document': {
      const response = await fetch(`${browserApiBase}/desktop/document`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          markdown: payload.markdown,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to save document')
      }
      return data as T
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
          strategy: payload.strategy ?? 'replace',
          existingNotes: payload.existingNotes ?? '',
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to generate notes')
      }
      return data as T
    }
    case 'notes_status': {
      const params = new URLSearchParams()
      if (payload.articlePath) {
        params.set('articlePath', String(payload.articlePath))
      }
      const response = await fetch(`${browserApiBase}/desktop/notes/status?${params.toString()}`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to load notes status')
      }
      return data as T
    }
    case 'cancel_notes': {
      const response = await fetch(`${browserApiBase}/desktop/notes/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to stop notes generation')
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
    case 'delete_document': {
      const response = await fetch(`${browserApiBase}/desktop/document/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to delete document')
      }
      return data as T
    }
    case 'change_label': {
      const response = await fetch(`${browserApiBase}/desktop/label`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          label: payload.label,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to change label')
      }
      return data as T
    }
    case 'reindex_library': {
      const response = await fetch(`${browserApiBase}/desktop/reindex`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ root: payload.root ?? null }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to rebuild index')
      }
      return data as T
    }
    case 'suggest_related': {
      const params = new URLSearchParams()
      if (payload.articlePath) params.set('articlePath', String(payload.articlePath))
      if (payload.root) params.set('root', String(payload.root))
      const response = await fetch(`${browserApiBase}/desktop/related/suggest?${params.toString()}`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to load suggestions')
      }
      return (data.suggestions ?? []) as T
    }
    case 'save_related': {
      const response = await fetch(`${browserApiBase}/desktop/related`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          items: payload.items,
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to save related links')
      }
      return data as T
    }
    case 'browse_directory': {
      const params = new URLSearchParams()
      if (payload.path) {
        params.set('path', String(payload.path))
      }
      const response = await fetch(`${browserApiBase}/desktop/browse?${params.toString()}`)
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to browse directory')
      }
      return {
        path: data.path as string,
        parent: (data.parent ?? null) as string | null,
        directories: (data.directories ?? []) as { name: string; path: string }[],
      } as T
    }
    case 'generate_reading_pdf': {
      const response = await fetch(`${browserApiBase}/desktop/reading_pdf`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          articlePath: payload.articlePath,
          pageSize: payload.pageSize,
          stripReferences: Boolean(payload.stripReferences),
        }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to generate reading PDF')
      }
      return {
        readingPdfPath: data.readingPdfPath as string,
        cached: Boolean(data.cached),
      } as T
    }
    case 'reveal_location': {
      const body: Record<string, unknown> = { path: payload.path }
      if (payload.launch === false) body.launch = false
      const response = await fetch(`${browserApiBase}/desktop/reveal`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to reveal location')
      }
      return {
        directoryPath: (data.directoryPath ?? '') as string,
        hostDirectoryPath: (data.hostDirectoryPath ?? null) as string | null,
        launched: Boolean(data.launched),
      } as T
    }
    case 'open_external_file': {
      const response = await fetch(`${browserApiBase}/desktop/open_external`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: payload.path }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) {
        throw new Error(data.message ?? 'Failed to open file')
      }
      return {
        path: (data.path ?? '') as string,
        launched: Boolean(data.launched),
      } as T
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
    .replace(/\\\[((?:.|\n)*?)\\\]/g, (_, content: string) => {
      const trimmed = content.trim()
      if (/^\[[^\]]+\]\([^)]+\)$/.test(trimmed)) {
        return trimmed
      }
      return `$$${trimmed}$$`
    })
    .replace(/\\\((.+?)\\\)/g, (_, math: string) => `$${math.trim()}$`)
}

function quoteMarkdown(text: string) {
  return text
    .split('\n')
    .map((line) => `> ${line}`)
    .join('\n')
}

function collectFindRanges(root: HTMLElement, needle: string): Range[] {
  const matches: Range[] = []
  if (!needle) return matches
  const lowered = needle.toLowerCase()
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.textContent || !node.textContent.trim()) {
        return NodeFilter.FILTER_REJECT
      }
      const parent = node.parentElement
      if (!parent) return NodeFilter.FILTER_REJECT
      if (parent.closest('script, style, .katex-html, .code-copy-button, .table-copy-button')) {
        return NodeFilter.FILTER_REJECT
      }
      return NodeFilter.FILTER_ACCEPT
    },
  })
  let current = walker.nextNode() as Text | null
  while (current) {
    const text = current.textContent ?? ''
    const lowerText = text.toLowerCase()
    let index = 0
    while (index < lowerText.length) {
      const found = lowerText.indexOf(lowered, index)
      if (found < 0) break
      const range = document.createRange()
      range.setStart(current, found)
      range.setEnd(current, found + needle.length)
      matches.push(range)
      index = found + Math.max(1, needle.length)
    }
    current = walker.nextNode() as Text | null
  }
  return matches
}

type HighlightConstructor = new (...ranges: Range[]) => object

function supportsCssHighlights(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof (window as unknown as { Highlight?: unknown }).Highlight === 'function' &&
    typeof CSS !== 'undefined' &&
    'highlights' in CSS
  )
}

function setFindHighlights(matches: Range[], activeIndex: number) {
  if (!supportsCssHighlights()) return
  const HighlightCtor = (window as unknown as { Highlight: HighlightConstructor }).Highlight
  const registry = (CSS as unknown as { highlights: Map<string, object> }).highlights
  if (!matches.length) {
    registry.delete('find-match')
    registry.delete('find-active')
    return
  }
  const activeRange = matches[activeIndex]
  const others = matches.filter((_, index) => index !== activeIndex)
  registry.set('find-match', new HighlightCtor(...others))
  if (activeRange) {
    registry.set('find-active', new HighlightCtor(activeRange))
  } else {
    registry.delete('find-active')
  }
}

function clearFindHighlights() {
  if (!supportsCssHighlights()) return
  const registry = (CSS as unknown as { highlights: Map<string, object> }).highlights
  registry.delete('find-match')
  registry.delete('find-active')
}

function parseHeadings(markdown: string): HeadingEntry[] {
  if (!markdown) return []
  const lines = markdown.split('\n')
  const headings: HeadingEntry[] = []
  let inCodeBlock = false
  let occurrence = 0
  let offset = 0
  for (const line of lines) {
    if (/^\s{0,3}```/.test(line)) {
      inCodeBlock = !inCodeBlock
      offset += line.length + 1
      continue
    }
    if (inCodeBlock) {
      offset += line.length + 1
      continue
    }
    const match = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line)
    if (match) {
      const level = match[1].length
      const text = match[2].trim()
      if (text) {
        headings.push({ level, text, index: occurrence, offset })
        occurrence += 1
      }
    }
    offset += line.length + 1
  }
  return headings
}

function collectSourceFindMatches(value: string, needle: string): SourceFindMatch[] {
  if (!value || !needle) return []
  const haystack = value.toLocaleLowerCase()
  const query = needle.toLocaleLowerCase()
  const matches: SourceFindMatch[] = []
  let searchFrom = 0
  while (searchFrom <= haystack.length) {
    const found = haystack.indexOf(query, searchFrom)
    if (found < 0) break
    matches.push({ from: found, to: found + query.length })
    searchFrom = found + Math.max(1, query.length)
  }
  return matches
}

type NotesTransformResult = { value: string; selStart: number; selEnd: number }
type NotesTransform = (value: string, selStart: number, selEnd: number) => NotesTransformResult

function wrapSelectionTransform(open: string, close: string, placeholder: string): NotesTransform {
  return (value, selStart, selEnd) => {
    const before = value.slice(0, selStart)
    const selected = value.slice(selStart, selEnd)
    const after = value.slice(selEnd)
    if (
      selected.length >= open.length + close.length &&
      selected.startsWith(open) &&
      selected.endsWith(close)
    ) {
      const inner = selected.slice(open.length, selected.length - close.length)
      return {
        value: before + inner + after,
        selStart: before.length,
        selEnd: before.length + inner.length,
      }
    }
    const body = selected || placeholder
    const next = before + open + body + close + after
    const innerStart = before.length + open.length
    return {
      value: next,
      selStart: innerStart,
      selEnd: innerStart + body.length,
    }
  }
}

function setLineHeadingTransform(level: number): NotesTransform {
  return (value, selStart, selEnd) => {
    const lineStart = value.lastIndexOf('\n', Math.max(0, selStart - 1)) + 1
    const lineEndIdx = value.indexOf('\n', selStart)
    const lineEnd = lineEndIdx === -1 ? value.length : lineEndIdx
    const line = value.slice(lineStart, lineEnd)
    const stripped = line.replace(/^\s{0,3}#{1,6}\s+/, '')
    const newLine = level === 0 ? stripped : `${'#'.repeat(level)} ${stripped}`
    const next = value.slice(0, lineStart) + newLine + value.slice(lineEnd)
    const delta = newLine.length - line.length
    return {
      value: next,
      selStart: Math.max(lineStart, selStart + delta),
      selEnd: Math.max(lineStart, selEnd + delta),
    }
  }
}

function insertLinkTransform(): NotesTransform {
  return (value, selStart, selEnd) => {
    const before = value.slice(0, selStart)
    const selected = value.slice(selStart, selEnd)
    const after = value.slice(selEnd)
    const label = selected || 'link text'
    const inserted = `[${label}](url)`
    const urlStart = before.length + 1 + label.length + 2
    return {
      value: before + inserted + after,
      selStart: urlStart,
      selEnd: urlStart + 3,
    }
  }
}

function insertCodeBlockTransform(): NotesTransform {
  return (value, selStart, selEnd) => {
    const before = value.slice(0, selStart)
    const selected = value.slice(selStart, selEnd)
    const after = value.slice(selEnd)
    let prefix = ''
    if (before.length && !before.endsWith('\n\n')) {
      prefix = before.endsWith('\n') ? '\n' : '\n\n'
    }
    let suffix = ''
    if (after.length && !after.startsWith('\n\n')) {
      suffix = after.startsWith('\n') ? '\n' : '\n\n'
    }
    const content = selected
    const block = `${prefix}\`\`\`\n${content}\n\`\`\`${suffix}`
    const next = before + block + after
    const innerStart = before.length + prefix.length + 4
    return {
      value: next,
      selStart: innerStart,
      selEnd: innerStart + content.length,
    }
  }
}

function insertBlockquoteTransform(): NotesTransform {
  return (value, selStart, selEnd) => {
    const lineStart = value.lastIndexOf('\n', Math.max(0, selStart - 1)) + 1
    const lineEndIdx = value.indexOf('\n', selEnd)
    const lineEnd = lineEndIdx === -1 ? value.length : lineEndIdx
    const block = value.slice(lineStart, lineEnd)
    const lines = block.split('\n')
    const allQuoted = lines.every((l) => l.startsWith('> ') || l === '')
    const transformed = (allQuoted
      ? lines.map((l) => (l.startsWith('> ') ? l.slice(2) : l))
      : lines.map((l) => (l.trim() ? `> ${l}` : l))
    ).join('\n')
    const next = value.slice(0, lineStart) + transformed + value.slice(lineEnd)
    return {
      value: next,
      selStart: lineStart,
      selEnd: lineStart + transformed.length,
    }
  }
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
      if (parent.closest('code, pre, script, style')) {
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

function applyElementNoise(root: HTMLElement, highlights: Highlight[]) {
  root.querySelectorAll('[data-element-noise="true"]').forEach((element) => {
    if (element instanceof HTMLElement) {
      delete element.dataset.elementNoise
      delete element.dataset.elementNoiseId
    }
  })
  const buckets: Record<string, HTMLElement[]> = {
    img: Array.from(root.querySelectorAll('img')),
    table: Array.from(root.querySelectorAll('table')),
  }
  for (const highlight of highlights) {
    if (highlight.kind !== 'element') continue
    const type = highlight.elementType
    if (type !== 'img' && type !== 'table') continue
    const index = highlight.elementIndex ?? -1
    if (index < 0) continue
    const target = buckets[type]?.[index]
    if (!target) continue
    target.dataset.elementNoise = 'true'
    target.dataset.elementNoiseId = highlight.id
  }
}

function findElementNoiseTarget(
  root: HTMLElement,
  element: HTMLElement,
): { type: 'img' | 'table'; index: number; element: HTMLElement } | null {
  const img = element.closest('img')
  if (img instanceof HTMLImageElement && root.contains(img)) {
    const list = Array.from(root.querySelectorAll('img'))
    const index = list.indexOf(img)
    if (index >= 0) return { type: 'img', index, element: img }
  }
  const table = element.closest('table')
  if (table instanceof HTMLTableElement && root.contains(table)) {
    const list = Array.from(root.querySelectorAll('table'))
    const index = list.indexOf(table)
    if (index >= 0) return { type: 'table', index, element: table }
  }
  return null
}

function applyInlineHighlights(root: HTMLElement, highlights: Highlight[]) {
  unwrapHighlightMarks(root)
  if (!highlights.length) {
    return
  }

  const resolved = highlights
    .filter((highlight) => highlight.kind !== 'element')
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
    try {
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
      const isNoise = entry.highlight.variant === 'noise'
      mark.className = isNoise ? 'inline-highlight inline-noise' : 'inline-highlight'
      mark.dataset.highlightId = entry.highlight.id
      if (isNoise) {
        mark.dataset.variant = 'noise'
        mark.title = 'Right-click to delete this noise mark'
      }

      const contents = range.extractContents()
      mark.appendChild(contents)
      range.insertNode(mark)
      root.normalize()
    } catch (error) {
      console.error('applyInlineHighlights: failed to wrap entry', entry.highlight.id, error)
    }
  }
}

function getSelectionHighlightCandidate(root: HTMLElement): PendingSelection | null {
  try {
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      return null
    }

    const range = selection.getRangeAt(0)
    const text = selection.toString().replace(/^\s+|\s+$/g, '')
    if (text.length < 12) {
      return null
    }

    if (!root.contains(range.commonAncestorContainer)) {
      return null
    }

    const textNodes = collectHighlightTextNodes(root)
    let accumulated = 0
    let startOffset: number | null = null
    let endOffset: number | null = null

    for (const node of textNodes) {
      const textLength = node.textContent?.length ?? 0
      let intersects = false
      try {
        intersects = range.intersectsNode(node)
      } catch {
        intersects = false
      }
      if (intersects) {
        let localStart = 0
        let localEnd = textLength
        if (node === range.startContainer && range.startContainer instanceof Text) {
          localStart = Math.max(0, Math.min(textLength, range.startOffset))
        } else {
          try {
            if (range.comparePoint(node, 0) === -1) {
              localStart = 0
            }
          } catch {
            // ignore — fall back to 0
          }
        }
        if (node === range.endContainer && range.endContainer instanceof Text) {
          localEnd = Math.max(0, Math.min(textLength, range.endOffset))
        } else {
          try {
            if (range.comparePoint(node, textLength) === 1) {
              localEnd = textLength
            }
          } catch {
            // ignore — fall back to full node
          }
        }
        if (localEnd > localStart) {
          if (startOffset == null) {
            startOffset = accumulated + localStart
          }
          endOffset = accumulated + localEnd
        }
      }
      accumulated += textLength
    }

    if (startOffset == null || endOffset == null || endOffset <= startOffset) {
      return null
    }

    return { text, startOffset, endOffset }
  } catch (error) {
    console.error('getSelectionHighlightCandidate failed', error)
    return null
  }
}

type ReaderContentProps = {
  loading: boolean
  markdown: string
  highlights: Highlight[]
  articlePath?: string | null
  sourceSite?: string | null
  theme: ThemeMode
  fontSize: number
  scrollHeadingRequest?: { index: number; ts: number } | null
  hideNoise?: boolean
  referencesAreNoise?: boolean
  onCopy: (text: string, label: string) => void
  onHighlightClick?: (highlightId: string) => void
  onImageOpen?: (src: string, alt: string, imageIndex: number | null) => void
  onDeleteNoise?: (highlightId: string) => void
  onToggleElementNoise?: (type: 'img' | 'table', index: number) => void
}

function CodeBlock({
  children,
  className,
  theme,
  onCopyText,
  ...rest
}: {
  children?: React.ReactNode
  className?: string
  theme: ThemeMode
  onCopyText: (text: string, label: string) => void
  [key: string]: unknown
}) {
  const text = useMemo(() => {
    if (typeof children === 'string') {
      return children
    }
    if (Array.isArray(children)) {
      return children.map((part: unknown) => (typeof part === 'string' ? part : '')).join('')
    }
    return ''
  }, [children])
  return (
    <div className="code-block-wrapper">
      <button
        type="button"
        className="code-copy-button"
        title="Copy code"
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          onCopyText(text, 'code block')
        }}
      >
        Copy
      </button>
      <CodeMirror
        className="code-block-editor"
        value={text}
        editable={false}
        basicSetup={{
          lineNumbers: false,
          foldGutter: false,
          highlightActiveLine: false,
          highlightActiveLineGutter: false,
        }}
        extensions={[]}
        theme={theme === 'dark' ? 'dark' : 'light'}
        {...rest}
      />
    </div>
  )
}

async function blobToPng(blob: Blob): Promise<Blob> {
  const bitmap = await (typeof createImageBitmap === 'function'
    ? createImageBitmap(blob)
    : Promise.reject(new Error('ImageBitmap not supported')))
  const canvas = document.createElement('canvas')
  canvas.width = bitmap.width
  canvas.height = bitmap.height
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    throw new Error('Canvas 2D context unavailable')
  }
  ctx.drawImage(bitmap, 0, 0)
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (out) => (out ? resolve(out) : reject(new Error('Canvas toBlob failed'))),
      'image/png',
    )
  })
}

function tableToCsv(table: HTMLTableElement | null): string {
  if (!table) return ''
  const rows: string[] = []
  for (const row of Array.from(table.querySelectorAll('tr'))) {
    const cells: string[] = []
    for (const cell of Array.from(row.querySelectorAll('th, td'))) {
      const text = (cell.textContent ?? '').replace(/\s+/g, ' ').trim()
      if (/[",\n]/.test(text)) {
        cells.push(`"${text.replace(/"/g, '""')}"`)
      } else {
        cells.push(text)
      }
    }
    if (cells.length) {
      rows.push(cells.join(','))
    }
  }
  return rows.join('\n')
}

function TableBlock({
  children,
  onCopyText,
  ...rest
}: {
  children?: React.ReactNode
  onCopyText: (text: string, label: string) => void
  [key: string]: unknown
}) {
  const tableRef = useRef<HTMLTableElement | null>(null)
  return (
    <div className="table-wrapper">
      <button
        type="button"
        className="table-copy-button"
        title="Copy table as CSV"
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          const csv = tableToCsv(tableRef.current)
          if (!csv) {
            onCopyText('', 'table')
            return
          }
          onCopyText(csv, 'table (CSV)')
        }}
      >
        Copy CSV
      </button>
      <table ref={tableRef} {...rest}>
        {children}
      </table>
    </div>
  )
}

const ReaderContent = memo(function ReaderContent({
  loading,
  markdown,
  highlights,
  articlePath,
  sourceSite,
  theme,
  fontSize,
  scrollHeadingRequest,
  hideNoise,
  referencesAreNoise,
  onCopy,
  onHighlightClick,
  onImageOpen,
  onDeleteNoise,
  onToggleElementNoise,
}: ReaderContentProps) {
  const chunks = useMemo(() => splitMarkdownIntoChunks(markdown), [markdown])
  const articleRef = useRef<HTMLElement | null>(null)
  const pendingHeadingIndexRef = useRef<number | null>(null)

  const onCopyRef = useRef(onCopy)
  const onImageOpenRef = useRef(onImageOpen)
  const onHighlightClickRef = useRef(onHighlightClick)
  const onDeleteNoiseRef = useRef(onDeleteNoise)
  const onToggleElementNoiseRef = useRef(onToggleElementNoise)

  useEffect(() => {
    onCopyRef.current = onCopy
    onImageOpenRef.current = onImageOpen
    onHighlightClickRef.current = onHighlightClick
    onDeleteNoiseRef.current = onDeleteNoise
    onToggleElementNoiseRef.current = onToggleElementNoise
  })

  const stableCopy = useCallback((text: string, label: string) => {
    onCopyRef.current(text, label)
  }, [])
  const stableImageOpen = useCallback((src: string, alt: string, imageIndex: number | null) => {
    onImageOpenRef.current?.(src, alt, imageIndex)
  }, [])

  useEffect(() => {
    if (!scrollHeadingRequest) return
    pendingHeadingIndexRef.current = scrollHeadingRequest.index
  }, [scrollHeadingRequest])

  useEffect(() => {
    const targetIndex = pendingHeadingIndexRef.current
    if (targetIndex == null) return
    const root = articleRef.current
    if (!root) return
    const headings = root.querySelectorAll('h1, h2, h3, h4, h5, h6')
    const target = headings[targetIndex]
    if (target instanceof HTMLElement) {
      pendingHeadingIndexRef.current = null
      target.scrollIntoView({ behavior: 'smooth', block: 'start' })
      target.classList.add('is-flashing')
      window.setTimeout(() => target.classList.remove('is-flashing'), 1200)
    }
  }, [markdown, scrollHeadingRequest])

  useEffect(() => {
    if (!articleRef.current) {
      return
    }
    applyInlineHighlights(articleRef.current, highlights)
    applyElementNoise(articleRef.current, highlights)
  }, [highlights, markdown])

  useEffect(() => {
    const root = articleRef.current
    if (!root) return
    root.querySelectorAll('.section-noise').forEach((element) => {
      element.classList.remove('section-noise')
    })
    if (!referencesAreNoise) return
    const flowElements: HTMLElement[] = []
    for (const section of Array.from(root.children)) {
      if (section instanceof HTMLElement) {
        for (const child of Array.from(section.children)) {
          if (child instanceof HTMLElement) {
            flowElements.push(child)
          }
        }
      }
    }
    const refHeadingIndex = flowElements.findIndex(
      (element) =>
        /^H[1-6]$/.test(element.tagName) &&
        /^(references?|bibliography|works\s+cited|citations?)\s*$/i.test(
          (element.textContent ?? '').trim(),
        ),
    )
    if (refHeadingIndex < 0) return
    const refHeading = flowElements[refHeadingIndex]
    const headingLevel = Number.parseInt(refHeading.tagName.substring(1), 10)
    refHeading.classList.add('section-noise')
    for (let i = refHeadingIndex + 1; i < flowElements.length; i++) {
      const element = flowElements[i]
      if (/^H[1-6]$/.test(element.tagName)) {
        const siblingLevel = Number.parseInt(element.tagName.substring(1), 10)
        if (siblingLevel <= headingLevel) break
      }
      element.classList.add('section-noise')
    }
  }, [markdown, referencesAreNoise, highlights])

  useEffect(() => {
    const root = articleRef.current
    if (!root) return
    const handleClick = (event: MouseEvent) => {
      const target = event.target
      if (!(target instanceof Element)) return
      if (target.closest('.code-copy-button, .table-copy-button')) {
        return
      }
      if (target.closest('.noise-delete-button')) {
        return
      }
      const mark = target.closest('mark.inline-highlight')
      if (mark instanceof HTMLElement) {
        const id = mark.dataset.highlightId
        const handler = onHighlightClickRef.current
        if (id && handler) {
          handler(id)
        }
      }
      const equation = target.closest('.katex-display, .katex')
      if (!equation) return
      const annotation = equation.querySelector('annotation[encoding="application/x-tex"]')
      const latex = annotation?.textContent?.trim()
      if (latex) {
        event.preventDefault()
        onCopyRef.current(latex, 'LaTeX')
      }
    }
    const handleContextMenu = (event: MouseEvent) => {
      const target = event.target
      if (!(target instanceof Element)) return
      const mark = target.closest('mark.inline-noise')
      if (mark instanceof HTMLElement) {
        const id = mark.dataset.highlightId
        const handler = onDeleteNoiseRef.current
        if (id && handler) {
          event.preventDefault()
          handler(id)
          return
        }
      }
      if (!(target instanceof HTMLElement)) return
      const found = findElementNoiseTarget(root, target)
      if (!found) return
      const toggleHandler = onToggleElementNoiseRef.current
      if (!toggleHandler) return
      event.preventDefault()
      toggleHandler(found.type, found.index)
    }
    root.addEventListener('click', handleClick)
    root.addEventListener('contextmenu', handleContextMenu)
    return () => {
      root.removeEventListener('click', handleClick)
      root.removeEventListener('contextmenu', handleContextMenu)
    }
  }, [markdown])

  const markdownComponents = useMemo<Components>(
    () => ({
      img: ({ node: _node, src = '', alt = '', title, ...props }) => {
        const resolvedSrc = toAssetUrl(typeof src === 'string' ? src : '', articlePath) ?? src
        const displayAlt = alt ?? ''
        return (
          <img
            src={typeof resolvedSrc === 'string' ? resolvedSrc : ''}
            alt={displayAlt}
            title={title ?? 'Click to view full size'}
            loading="lazy"
            decoding="async"
            onClick={(event) => {
              event.preventDefault()
              event.stopPropagation()
              const root = articleRef.current
              let idx: number | null = null
              if (root && event.currentTarget instanceof HTMLImageElement) {
                const list = Array.from(root.querySelectorAll('img'))
                const found = list.indexOf(event.currentTarget)
                if (found >= 0) idx = found
              }
              stableImageOpen(typeof resolvedSrc === 'string' ? resolvedSrc : '', displayAlt, idx)
            }}
            {...props}
          />
        )
      },
      table: ({ children }) => (
        <TableBlock onCopyText={stableCopy}>{children}</TableBlock>
      ),
      a: ({ node: _node, href = '', children, title, ...props }) => {
        const resolvedHref = resolveExternalHref(href, sourceSite)
        const finalHref = resolvedHref ?? href ?? '#'
        return (
          <a
            href={finalHref}
            title={title}
            target={resolvedHref ? '_blank' : undefined}
            rel={resolvedHref ? 'noopener noreferrer' : undefined}
            onClick={(event) => {
              if (!resolvedHref) {
                event.preventDefault()
                return
              }
              event.preventDefault()
              window.open(resolvedHref, '_blank', 'noopener,noreferrer')
            }}
            {...props}
          >
            {children}
          </a>
        )
      },
      pre: ({ children }) => <>{children}</>,
      code: ({ className, children, ...codeProps }) => {
        const content = children
        const asString =
          typeof content === 'string'
            ? content
            : Array.isArray(content)
            ? content.map((part: unknown) => (typeof part === 'string' ? part : '')).join('')
            : ''
        const isBlock =
          (className && className.includes('language-')) ||
          asString.includes('\n')
        if (!isBlock) {
          return (
            <code className={className} {...codeProps}>
              {children}
            </code>
          )
        }
        return (
          <CodeBlock className={className} theme={theme} onCopyText={stableCopy} {...codeProps}>
            {children}
          </CodeBlock>
        )
      },
    }),
    [articlePath, sourceSite, stableCopy, stableImageOpen, theme],
  )

  const renderedChunks = useMemo(
    () =>
      chunks.map((chunk, index) => (
        <section className="markdown-chunk" key={`${index}-${chunk.slice(0, 32)}`}>
          <ReactMarkdown
            remarkPlugins={markdownRemarkPlugins}
            rehypePlugins={markdownRehypePlugins}
            urlTransform={markdownUrlTransform}
            components={markdownComponents}
          >
            {chunk}
          </ReactMarkdown>
        </section>
      )),
    [chunks, markdownComponents],
  )

  if (loading) {
    return <div className="empty-state">Loading document…</div>
  }

  if (!markdown) {
    return <div className="empty-state">Choose a document from the library.</div>
  }

  const inlineStyle: CSSProperties = { fontSize: `${fontSize}rem` }

  return (
    <article
      className="markdown-body"
      ref={articleRef}
      style={inlineStyle}
      data-hide-noise={hideNoise ? 'true' : 'false'}
    >
      {renderedChunks}
    </article>
  )
})

function App() {
  const [rootPath, setRootPath] = useState('')
  const [library, setLibrary] = useState<LibraryIndex>(emptyLibrary)
  const [selectedLabel, setSelectedLabel] = useState<string>(() => {
    if (typeof window === 'undefined') return 'all'
    return window.localStorage.getItem(persistedLabelKey) || 'all'
  })
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<DocumentSummary[] | null>(null)
  const [searchTotal, setSearchTotal] = useState<number | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<DocumentDetail | null>(null)
  const [sourceDraft, setSourceDraft] = useState('')
  const [sourceSavedSnapshot, setSourceSavedSnapshot] = useState('')
  const [readerMode, setReaderMode] = useState<ReaderMode>(() => {
    if (typeof window === 'undefined') return 'rendered'
    return window.localStorage.getItem(persistedReaderModeKey) === 'source' ? 'source' : 'rendered'
  })
  const [notesDraft, setNotesDraft] = useState('')
  const [notesSavedSnapshot, setNotesSavedSnapshot] = useState('')
  const [notesMode, setNotesMode] = useState<NotesMode>(() => {
    if (typeof window === 'undefined') return 'markdown'
    const stored = window.localStorage.getItem(persistedNotesModeKey)
    return stored === 'preview' ? 'preview' : 'markdown'
  })
  const [pendingSelection, setPendingSelection] = useState<PendingSelection | null>(null)
  const [highlights, setHighlights] = useState<Highlight[]>([])
  const [relatedLinks, setRelatedLinks] = useState<RelatedLink[]>([])
  const [relatedPickerOpen, setRelatedPickerOpen] = useState(false)
  const [relatedPickerQuery, setRelatedPickerQuery] = useState('')
  const [relatedDraftNote, setRelatedDraftNote] = useState('')
  const [relatedSaving, setRelatedSaving] = useState(false)
  type RelatedSuggestion = DocumentSummary & { score: number; reasons: string[]; sharedTerms: string[] }
  const [relatedSuggestions, setRelatedSuggestions] = useState<RelatedSuggestion[]>([])
  const [loadingSuggestions, setLoadingSuggestions] = useState(false)
  const [loadingLibrary, setLoadingLibrary] = useState(false)
  const [reindexing, setReindexing] = useState(false)
  const [searching, setSearching] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [savingNotes, setSavingNotes] = useState(false)
  const [savingSource, setSavingSource] = useState(false)
  const [generatingNotes, setGeneratingNotes] = useState(false)
  const [notesPending, setNotesPending] = useState(false)
  const [notesPreviewReady, setNotesPreviewReady] = useState(true)
  const [notesGenerationState, setNotesGenerationState] = useState<NotesGenerationStatus['state']>('idle')
  const [notesAutoFollow, setNotesAutoFollow] = useState(true)
  const [notesEditorMountTick, setNotesEditorMountTick] = useState(0)
  const [savingHighlights, setSavingHighlights] = useState(false)
  const [status, setStatus] = useState('Loading library…')
  const [leftPaneWidth, setLeftPaneWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_LEFT_PANE
    const stored = Number(window.localStorage.getItem(persistedLeftPaneKey))
    return Number.isFinite(stored) && stored >= MIN_LEFT_PANE ? stored : DEFAULT_LEFT_PANE
  })
  const [rightPaneWidth, setRightPaneWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_RIGHT_PANE
    const stored = Number(window.localStorage.getItem(persistedRightPaneKey))
    return Number.isFinite(stored) && stored >= MIN_RIGHT_PANE ? stored : DEFAULT_RIGHT_PANE
  })
  const [notesTopHeight, setNotesTopHeight] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_NOTES_TOP_HEIGHT
    const stored = Number(window.localStorage.getItem(persistedNotesTopKey))
    return Number.isFinite(stored) && stored >= MIN_NOTES_TOP_HEIGHT ? stored : DEFAULT_NOTES_TOP_HEIGHT
  })
  const [focusedHighlight, setFocusedHighlight] = useState<{ id: string; ts: number } | null>(null)
  const [lightbox, setLightbox] = useState<{
    src: string
    alt: string
    imageIndex: number | null
  } | null>(null)
  const [infoOpen, setInfoOpen] = useState(false)
  const [infoLocation, setInfoLocation] = useState<{
    directoryPath: string
    hostDirectoryPath: string | null
  } | null>(null)
  const [rootPickerOpen, setRootPickerOpen] = useState(false)
  const [rootPickerPath, setRootPickerPath] = useState('')
  const [rootPickerParent, setRootPickerParent] = useState<string | null>(null)
  const [rootPickerEntries, setRootPickerEntries] = useState<{ name: string; path: string }[]>([])
  const [rootPickerLoading, setRootPickerLoading] = useState(false)
  const [rootPickerError, setRootPickerError] = useState<string | null>(null)
  const [findOpen, setFindOpen] = useState(false)
  const [findTarget, setFindTarget] = useState<FindTarget>('reader')
  const [findQuery, setFindQuery] = useState('')
  const [findMatchCount, setFindMatchCount] = useState(0)
  const [findCursor, setFindCursor] = useState(0)
  const findMatchesRef = useRef<Range[]>([])
  const sourceFindMatchesRef = useRef<SourceFindMatch[]>([])
  const findInputRef = useRef<HTMLInputElement | null>(null)
  const highlightCardRefs = useRef<Map<string, HTMLElement>>(new Map())
  const [theme, setTheme] = useState<ThemeMode>(() => {
    if (typeof window === 'undefined') return 'light'
    const stored = window.localStorage.getItem(persistedThemeKey)
    return stored === 'dark' ? 'dark' : 'light'
  })
  const [focusMode, setFocusMode] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem(persistedFocusKey) === 'true'
  })
  const [readerFontSize, setReaderFontSize] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_FONT_SIZE
    const stored = Number(window.localStorage.getItem(persistedFontSizeKey))
    return Number.isFinite(stored) && stored >= MIN_FONT_SIZE && stored <= MAX_FONT_SIZE
      ? stored
      : DEFAULT_FONT_SIZE
  })
  const [highlightCommentDraft, setHighlightCommentDraft] = useState<{ id: string; value: string } | null>(null)
  const [highlightsView, setHighlightsView] = useState<HighlightsView>(() => {
    if (typeof window === 'undefined') return 'list'
    return window.localStorage.getItem(persistedHighlightsViewKey) === 'single' ? 'single' : 'list'
  })
  const [highlightCursor, setHighlightCursor] = useState(0)
  const [hideNoise, setHideNoise] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem(persistedHideNoiseKey) === 'true'
  })
  const [referencesAreNoise, setReferencesAreNoise] = useState<boolean>(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem(persistedRefsAreNoiseKey) === 'true'
  })
  const [readingPdfPageSize, setReadingPdfPageSize] = useState<'a4' | 'a5'>(() => {
    if (typeof window === 'undefined') return 'a5'
    const stored = window.localStorage.getItem(persistedReadingPdfPageSizeKey)
    return stored === 'a4' ? 'a4' : 'a5'
  })
  const [generatingReadingPdf, setGeneratingReadingPdf] = useState(false)
  const [openDocIds, setOpenDocIds] = useState<string[]>(() => {
    if (typeof window === 'undefined') return []
    try {
      const raw = window.localStorage.getItem(persistedOpenTabsKey)
      if (!raw) return []
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.filter((value): value is string => typeof value === 'string')
      }
    } catch {
      // ignore corrupted cache
    }
    return []
  })
  const [switcherOpen, setSwitcherOpen] = useState(false)
  const [switcherQuery, setSwitcherQuery] = useState('')
  const [switcherCursor, setSwitcherCursor] = useState(0)
  const [tocOpen, setTocOpen] = useState(false)
  const [tocQuery, setTocQuery] = useState('')
  const [tocCursor, setTocCursor] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [notesStrategyDialogOpen, setNotesStrategyDialogOpen] = useState(false)
  const [pendingHashPath, setPendingHashPath] = useState<string | null>(null)
  const [scrollHeadingRequest, setScrollHeadingRequest] = useState<{ index: number; ts: number } | null>(null)
  const deferredSearch = useDeferredValue(search.trim())
  const dragStateRef = useRef<{ pane: 'left' | 'right' | 'notes'; pointerId: number } | null>(null)
  const readerScrollRef = useRef<HTMLDivElement | null>(null)
  const notesScrollRef = useRef<HTMLDivElement | null>(null)
  const notesPreviewRef = useRef<HTMLDivElement | null>(null)
  const notesEditorViewRef = useRef<EditorView | null>(null)
  const sourceEditorViewRef = useRef<EditorView | null>(null)
  const notesAutoScrollingRef = useRef(false)
  const activeDocIdRef = useRef<string | null>(null)
  const settingsMenuRef = useRef<HTMLDivElement | null>(null)
  const hashRefreshAttemptedRef = useRef<string | null>(null)
  const focusedArticlePathRef = useRef<string | null>(null)
  const notesDirtyRef = useRef(false)
  const sourceDirtyRef = useRef(false)

  useEffect(() => {
    void initializeLibrary()
  }, [])

  useEffect(() => {
    const parseHash = () => {
      const hash = window.location.hash
      if (!hash || !hash.startsWith('#')) return null
      const params = new URLSearchParams(hash.slice(1))
      return params.get('open')
    }
    const updateFromHash = () => {
      const path = parseHash()
      if (path) setPendingHashPath(path)
    }
    updateFromHash()
    window.addEventListener('hashchange', updateFromHash)
    return () => window.removeEventListener('hashchange', updateFromHash)
  }, [])

  useEffect(() => {
    if (!pendingHashPath) return
    if (loadingLibrary) return
    if (!library.root) return
    if (hashRefreshAttemptedRef.current === pendingHashPath) return
    const hasTarget = library.documents.some(
      (doc) => doc.articlePath === pendingHashPath,
    )
    if (!hasTarget) {
      hashRefreshAttemptedRef.current = pendingHashPath
      void refreshLibrary()
    }
  }, [pendingHashPath, library.root, library.documents, loadingLibrary])

  useEffect(() => {
    if (!pendingHashPath) return
    if (loadingLibrary) return
    const target = library.documents.find(
      (doc) => doc.articlePath === pendingHashPath,
    )
    const refreshed = hashRefreshAttemptedRef.current === pendingHashPath
    if (!target && !refreshed) return
    const path = pendingHashPath
    setPendingHashPath(null)
    hashRefreshAttemptedRef.current = null
    window.history.replaceState(
      null,
      '',
      window.location.pathname + window.location.search,
    )
    if (target) {
      void loadDocument(target)
    } else {
      void loadDocumentByPath(path)
    }
  }, [pendingHashPath, library.documents, loadingLibrary])

  useEffect(() => {
    if (typeof document === 'undefined') return
    document.documentElement.dataset.theme = theme
    window.localStorage.setItem(persistedThemeKey, theme)
  }, [theme])

  useEffect(() => {
    window.localStorage.setItem(persistedFocusKey, focusMode ? 'true' : 'false')
  }, [focusMode])

  useEffect(() => {
    if (!settingsOpen) return
    const handler = (event: MouseEvent) => {
      const menu = settingsMenuRef.current
      if (!menu) return
      if (menu.contains(event.target as Node)) return
      setSettingsOpen(false)
    }
    const keyHandler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSettingsOpen(false)
    }
    window.addEventListener('mousedown', handler)
    window.addEventListener('keydown', keyHandler)
    return () => {
      window.removeEventListener('mousedown', handler)
      window.removeEventListener('keydown', keyHandler)
    }
  }, [settingsOpen])

  useEffect(() => {
    window.localStorage.setItem(persistedFontSizeKey, String(readerFontSize))
  }, [readerFontSize])

  useEffect(() => {
    window.localStorage.setItem(persistedLeftPaneKey, String(leftPaneWidth))
  }, [leftPaneWidth])

  useEffect(() => {
    window.localStorage.setItem(persistedRightPaneKey, String(rightPaneWidth))
  }, [rightPaneWidth])

  useEffect(() => {
    window.localStorage.setItem(persistedNotesTopKey, String(notesTopHeight))
  }, [notesTopHeight])

  useEffect(() => {
    window.localStorage.setItem(persistedLabelKey, selectedLabel)
  }, [selectedLabel])

  useEffect(() => {
    window.localStorage.setItem(persistedNotesModeKey, notesMode)
  }, [notesMode])

  useEffect(() => {
    window.localStorage.setItem(persistedReaderModeKey, readerMode)
  }, [readerMode])

  useEffect(() => {
    if (readerMode === 'source') {
      setPendingSelection(null)
    }
  }, [readerMode])

  useEffect(() => {
    if (!notesPending || !notesAutoFollow) {
      return
    }
    const view = notesEditorViewRef.current
    if (!view) {
      return
    }
    notesAutoScrollingRef.current = true
    const scrollDOM = view.scrollDOM
    const nextScrollTop = Math.max(0, scrollDOM.scrollHeight - scrollDOM.clientHeight)
    scrollDOM.scrollTop = nextScrollTop
    window.requestAnimationFrame(() => {
      notesAutoScrollingRef.current = false
    })
  }, [notesDraft, notesPending, notesAutoFollow, notesEditorMountTick])

  useEffect(() => {
    const view = notesEditorViewRef.current
    if (!view) {
      return
    }
    const handleScroll = () => {
      if (!notesPending || notesAutoScrollingRef.current) {
        return
      }
      const scrollDOM = view.scrollDOM
      const distanceFromBottom = scrollDOM.scrollHeight - (scrollDOM.scrollTop + scrollDOM.clientHeight)
      setNotesAutoFollow(distanceFromBottom <= 8)
    }
    view.scrollDOM.addEventListener('scroll', handleScroll, { passive: true })
    return () => {
      view.scrollDOM.removeEventListener('scroll', handleScroll)
    }
  }, [notesMode, notesPending, notesEditorMountTick])

  useEffect(() => {
    window.localStorage.setItem(persistedHighlightsViewKey, highlightsView)
  }, [highlightsView])

  useEffect(() => {
    window.localStorage.setItem(persistedHideNoiseKey, hideNoise ? 'true' : 'false')
  }, [hideNoise])

  useEffect(() => {
    window.localStorage.setItem(persistedRefsAreNoiseKey, referencesAreNoise ? 'true' : 'false')
  }, [referencesAreNoise])

  useEffect(() => {
    window.localStorage.setItem(persistedReadingPdfPageSizeKey, readingPdfPageSize)
  }, [readingPdfPageSize])

  useEffect(() => {
    window.localStorage.setItem(persistedOpenTabsKey, JSON.stringify(openDocIds))
  }, [openDocIds])

  useEffect(() => {
    const idToPath = new Map(library.documents.map((doc) => [doc.id, doc.articlePath]))
    const openDocumentPaths = openDocIds
      .map((id) => idToPath.get(id))
      .filter((path): path is string => typeof path === 'string' && path.length > 0)
    const focusedDocumentPath = detail?.summary.articlePath ?? null
    const payload = {
      openDocumentPaths,
      focusedDocumentPath,
      labelFilter: selectedLabel,
    }
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      fetch(`${browserApiBase}/desktop/session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal: controller.signal,
      }).catch(() => {})
    }, 200)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [openDocIds, detail?.summary.articlePath, selectedLabel, library.documents])

  useEffect(() => {
    if (!activeId) return
    setOpenDocIds((current) => {
      const filtered = current.filter((id) => id !== activeId)
      return [activeId, ...filtered]
    })
  }, [activeId])

  useEffect(() => {
    focusedArticlePathRef.current = detail?.summary.articlePath ?? null
  }, [detail?.summary.articlePath])

  useEffect(() => {
    notesDirtyRef.current = notesDraft.trimEnd() !== notesSavedSnapshot.trimEnd()
  }, [notesDraft, notesSavedSnapshot])

  useEffect(() => {
    sourceDirtyRef.current = sourceDraft.trimEnd() !== sourceSavedSnapshot.trimEnd()
  }, [sourceDraft, sourceSavedSnapshot])

  // Silent refresh on backend-push events. A write from the MCP/CLI/another
  // client triggers /desktop/events; if the event targets the focused doc, we
  // refetch it. Dirty notes/source drafts are preserved so the user's
  // in-flight edits are never clobbered.
  useEffect(() => {
    const source = new EventSource(`${browserApiBase}/desktop/events`)
    const refetchTypes = new Set([
      'notes_updated',
      'highlights_updated',
      'rating_updated',
      'document_updated',
      'related_updated',
    ])
    source.onmessage = (raw) => {
      let event: { type?: string; articlePath?: string } | null = null
      try {
        event = JSON.parse(raw.data)
      } catch {
        return
      }
      if (!event || typeof event.type !== 'string') return
      if (event.type === 'library_synced' || event.type === 'label_changed') {
        void refreshLibrary()
        return
      }
      if (!refetchTypes.has(event.type)) return
      const targetPath = event.articlePath
      const currentPath = focusedArticlePathRef.current
      if (!targetPath || !currentPath || targetPath !== currentPath) return
      void (async () => {
        try {
          const loaded = await desktopCommand<DocumentDetail>('load_document', {
            articlePath: targetPath,
          })
          if (focusedArticlePathRef.current !== targetPath) return
          setDetail(loaded)
          setHighlights(loaded.highlights)
          setRelatedLinks(loaded.related ?? [])
          if (!sourceDirtyRef.current) {
            setSourceDraft(loaded.markdown)
          }
          setSourceSavedSnapshot(loaded.markdown)
          if (!notesDirtyRef.current) {
            setNotesDraft(loaded.notesMarkdown)
          }
          setNotesSavedSnapshot(loaded.notesMarkdown)
        } catch {
          // Silent — the user will see the stale state until next manual action.
        }
      })()
    }
    source.onerror = () => {
      // EventSource auto-reconnects; nothing to do here.
    }
    return () => {
      source.close()
    }
  }, [])

  useEffect(() => {
    const validIds = new Set(library.documents.map((doc) => doc.id))
    setOpenDocIds((current) => {
      if (!current.length) return current
      const next = current.filter((id) => validIds.has(id))
      return next.length === current.length ? current : next
    })
  }, [library.documents])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      const withMod = (event.ctrlKey || event.metaKey) && !event.altKey
      if (!withMod) return
      const key = event.key.toLowerCase()
      if (key === 'p' && !event.shiftKey) {
        event.preventDefault()
        setSwitcherQuery('')
        setSwitcherCursor(0)
        setSwitcherOpen((current) => !current)
        return
      }
      if (key === 'o' && event.shiftKey) {
        event.preventDefault()
        setTocQuery('')
        setTocCursor(0)
        setTocOpen((current) => !current)
        return
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    setHighlightCursor((current) => {
      if (!highlights.length) return 0
      return Math.min(current, highlights.length - 1)
    })
  }, [highlights])

  useEffect(() => {
    if (!focusedHighlight) return
    const idx = highlights.findIndex((entry) => entry.id === focusedHighlight.id)
    if (idx >= 0) {
      setHighlightCursor(idx)
    }
    const card = highlightCardRefs.current.get(focusedHighlight.id)
    if (!card) return
    card.scrollIntoView({ behavior: 'smooth', block: 'center' })
    card.classList.add('is-flashing')
    const timer = window.setTimeout(() => {
      card.classList.remove('is-flashing')
    }, 1200)
    return () => window.clearTimeout(timer)
  }, [focusedHighlight, highlights])

  useEffect(() => {
    if (!lightbox) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setLightbox(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightbox])

  useEffect(() => {
    if (!infoOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setInfoOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [infoOpen])

  useEffect(() => {
    if (!notesStrategyDialogOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setNotesStrategyDialogOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [notesStrategyDialogOpen])

  useEffect(() => {
    if (!infoOpen || !detail) {
      setInfoLocation(null)
      return
    }
    let cancelled = false
    void (async () => {
      try {
        const response = await desktopCommand<{
          directoryPath: string
          hostDirectoryPath: string | null
        }>('reveal_location', { path: detail.summary.articlePath, launch: false })
        if (cancelled) return
        setInfoLocation({
          directoryPath: response.directoryPath,
          hostDirectoryPath: response.hostDirectoryPath,
        })
      } catch {
        if (!cancelled) setInfoLocation(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [infoOpen, detail])

  useEffect(() => {
    if (!focusMode) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (infoOpen || lightbox || settingsOpen || switcherOpen || tocOpen || findOpen) return
      const target = event.target as HTMLElement | null
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return
      setFocusMode(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [focusMode, infoOpen, lightbox, settingsOpen, switcherOpen, tocOpen, findOpen])

  useEffect(() => {
    if (!detail) {
      setInfoOpen(false)
      setFindOpen(false)
      setFindQuery('')
      setNotesStrategyDialogOpen(false)
    }
  }, [detail?.summary.id])

  useEffect(() => {
    if (!findOpen) {
      findMatchesRef.current = []
      sourceFindMatchesRef.current = []
      setFindMatchCount(0)
      setFindCursor(0)
      clearFindHighlights()
      return
    }
    const needle = findQuery.trim()
    if (!needle) {
      findMatchesRef.current = []
      sourceFindMatchesRef.current = []
      setFindMatchCount(0)
      setFindCursor(0)
      clearFindHighlights()
      return
    }
    if (findTarget === 'notes') {
      if (notesMode === 'markdown') {
        clearFindHighlights()
        const matches = collectSourceFindMatches(notesDraft, needle)
        findMatchesRef.current = []
        sourceFindMatchesRef.current = matches
        setFindMatchCount(matches.length)
        setFindCursor((current) => {
          if (!matches.length) return 0
          return Math.min(current, matches.length - 1)
        })
        return
      }
      const root = notesPreviewRef.current
      if (!(root instanceof HTMLElement)) return
      const matches = collectFindRanges(root, needle)
      findMatchesRef.current = matches
      sourceFindMatchesRef.current = []
      setFindMatchCount(matches.length)
      setFindCursor((current) => {
        if (!matches.length) return 0
        return Math.min(current, matches.length - 1)
      })
      return
    }
    if (readerMode === 'source') {
      clearFindHighlights()
      const matches = collectSourceFindMatches(sourceDraft, needle)
      findMatchesRef.current = []
      sourceFindMatchesRef.current = matches
      setFindMatchCount(matches.length)
      setFindCursor((current) => {
        if (!matches.length) return 0
        return Math.min(current, matches.length - 1)
      })
      return
    }
    const root = document.querySelector('.reader-pane .markdown-body')
    if (!(root instanceof HTMLElement)) return
    const matches = collectFindRanges(root, needle)
    findMatchesRef.current = matches
    sourceFindMatchesRef.current = []
    setFindMatchCount(matches.length)
    setFindCursor((current) => {
      if (!matches.length) return 0
      return Math.min(current, matches.length - 1)
    })
  }, [findOpen, findQuery, findTarget, readerMode, sourceDraft, notesMode, notesDraft, detail?.markdown, highlights, referencesAreNoise])

  useEffect(() => {
    if (!findOpen) return
    if (findTarget === 'notes') {
      if (notesMode === 'markdown') {
        const matches = sourceFindMatchesRef.current
        const active = matches[findCursor]
        const view = notesEditorViewRef.current
        if (!view || !active) {
          return
        }
        view.dispatch({
          selection: EditorSelection.single(active.from, active.to),
          effects: EditorView.scrollIntoView(active.from, { y: 'center' }),
        })
        return
      }
      const matches = findMatchesRef.current
      if (!matches.length) {
        setFindHighlights([], 0)
        return
      }
      setFindHighlights(matches, findCursor)
      const active = matches[findCursor]
      const activeNode = active?.startContainer.parentElement
      if (activeNode) {
        activeNode.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }
      return
    }
    if (readerMode === 'source') {
      const matches = sourceFindMatchesRef.current
      const active = matches[findCursor]
      const view = sourceEditorViewRef.current
      if (!view || !active) {
        return
      }
      view.dispatch({
        selection: EditorSelection.single(active.from, active.to),
        effects: EditorView.scrollIntoView(active.from, { y: 'center' }),
      })
      return
    }
    const matches = findMatchesRef.current
    if (!matches.length) {
      setFindHighlights([], 0)
      return
    }
    setFindHighlights(matches, findCursor)
    const active = matches[findCursor]
    const activeNode = active?.startContainer.parentElement
    if (activeNode) {
      activeNode.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [findOpen, findMatchCount, findCursor, findTarget, readerMode, notesMode])

  useEffect(() => {
    if (!findOpen) return
    if (findInputRef.current) {
      findInputRef.current.focus()
      findInputRef.current.select()
    }
  }, [findOpen])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'f') return
      if (!detail) return
      event.preventDefault()
      const active = document.activeElement
      if (active instanceof HTMLElement && active.closest('.notes-pane')) {
        setFindTarget('notes')
      } else {
        setFindTarget('reader')
      }
      setFindOpen(true)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [detail])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return
      if (event.key.toLowerCase() !== 's') return
      if (!detail) return
      event.preventDefault()
      if (readerMode === 'source') {
        void saveSource()
        return
      }
      void saveNotes()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [detail, readerMode, sourceDraft, notesDraft])

  useEffect(() => {
    return () => {
      clearFindHighlights()
    }
  }, [])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      setNotesDraft('')
      setHighlights([])
      setPendingSelection(null)
      activeDocIdRef.current = null
      return
    }
    if (activeDocIdRef.current === activeId) {
      return
    }
    activeDocIdRef.current = activeId
    const summary = resolveDocumentSummary(activeId)
    if (summary) {
      void loadDocument(summary)
    }
  }, [activeId, library.documents, searchResults])

  const activeDocumentId = detail?.summary.id ?? null

  useEffect(() => {
    if (!activeDocumentId || !readerScrollRef.current) return
    const key = `${persistedScrollKeyPrefix}${activeDocumentId}`
    const stored = Number(window.localStorage.getItem(key))
    const container = readerScrollRef.current
    const raf = window.requestAnimationFrame(() => {
      container.scrollTop = Number.isFinite(stored) && stored > 0 ? stored : 0
    })
    return () => window.cancelAnimationFrame(raf)
  }, [activeDocumentId])

  useEffect(() => {
    const container = readerScrollRef.current
    if (!container) return
    const handleScroll = () => {
      const currentId = activeDocIdRef.current
      if (!currentId) return
      window.localStorage.setItem(
        `${persistedScrollKeyPrefix}${currentId}`,
        String(Math.max(0, Math.round(container.scrollTop))),
      )
    }
    container.addEventListener('scroll', handleScroll, { passive: true })
    return () => container.removeEventListener('scroll', handleScroll)
  }, [activeDocumentId])

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

  async function loadRootPickerPath(path: string | null) {
    setRootPickerLoading(true)
    setRootPickerError(null)
    try {
      const response = await desktopCommand<{
        path: string
        parent: string | null
        directories: { name: string; path: string }[]
      }>('browse_directory', { path: path ?? '' })
      setRootPickerPath(response.path)
      setRootPickerParent(response.parent)
      setRootPickerEntries(response.directories)
    } catch (error) {
      setRootPickerError(stringifyError(error))
    } finally {
      setRootPickerLoading(false)
    }
  }

  function openRootPicker() {
    const seed = rootPath.trim() || null
    setRootPickerOpen(true)
    void loadRootPickerPath(seed)
  }

  async function confirmRootPicker() {
    if (!rootPickerPath) return
    setRootPickerOpen(false)
    setRootPath(rootPickerPath)
    await refreshLibrary(rootPickerPath)
  }

  async function handleReindex() {
    if (reindexing) return
    setReindexing(true)
    setStatus('Rebuilding index…')
    try {
      const response = await desktopCommand<{ scanned: number; records: number; errors: number }>(
        'reindex_library',
        { root: rootPath || null },
      )
      const parts = [
        `Index rebuilt: ${response.records} records from ${response.scanned} documents`,
      ]
      if (response.errors) {
        parts.push(`${response.errors} error(s)`)
      }
      setStatus(parts.join(' — '))
      await refreshLibrary()
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setReindexing(false)
    }
  }

  async function loadDocumentByPath(articlePath: string, statusTitle?: string) {
    setLoadingDetail(true)
    setStatus(statusTitle ? `Loading ${statusTitle}…` : 'Loading…')
    try {
      const loaded = await desktopCommand<DocumentDetail>('load_document', {
        articlePath,
      })
      activeDocIdRef.current = loaded.summary.id
      setActiveId(loaded.summary.id)
      setDetail(loaded)
      setSourceDraft(loaded.markdown)
      setSourceSavedSnapshot(loaded.markdown)
      setNotesDraft(loaded.notesMarkdown)
      setNotesSavedSnapshot(loaded.notesMarkdown)
      setNotesPending(Boolean(loaded.summary.notesPending))
      setNotesPreviewReady(!loaded.summary.notesPending)
      setNotesGenerationState(loaded.summary.notesPending ? 'running' : 'idle')
      setNotesAutoFollow(Boolean(loaded.summary.notesPending))
      setHighlights(loaded.highlights)
      setRelatedLinks(loaded.related ?? [])
      setRelatedSuggestions([])
      setRelatedDraftNote('')
      setRelatedPickerQuery('')
      setPendingSelection(null)
      setStatus(`Opened ${loaded.summary.title}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setLoadingDetail(false)
    }
  }

  async function loadDocument(summary: DocumentSummary) {
    await loadDocumentByPath(summary.articlePath, summary.title)
  }

  async function runSearch() {
    if (!library.root) {
      return
    }

    if (!deferredSearch) {
      setSearchResults(null)
      setSearchTotal(null)
      return
    }

    setSearching(true)
    try {
      const request: SearchRequest = {
        root: library.root || null,
        query: deferredSearch,
        label: selectedLabel === 'all' ? null : selectedLabel,
      }
      const response = await desktopCommand<SearchResponse>('search_documents', { request })
      setSearchResults(response.documents)
      setSearchTotal(response.total)
      if (response.truncated) {
        setStatus(`Showing ${response.documents.length} of ${response.total} matches for “${search.trim()}”`)
      } else {
        setStatus(`Found ${response.total} matches for “${search.trim()}”`)
      }
    } catch (error) {
      setSearchResults([])
      setSearchTotal(0)
      setStatus(stringifyError(error))
    } finally {
      setSearching(false)
    }
  }

  function applyDocumentSummaryPatch(documentId: string, patch: Partial<DocumentSummary>) {
    const update = (doc: DocumentSummary) =>
      doc.id === documentId ? { ...doc, ...patch } : doc
    setDetail((current) =>
      current && current.summary.id === documentId
        ? { ...current, summary: { ...current.summary, ...patch } }
        : current,
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
      setNotesSavedSnapshot(notesDraft)
      setNotesPending(false)
      setNotesPreviewReady(true)
      setNotesGenerationState('completed')
      applyDocumentSummaryPatch(detail.summary.id, { notesPath, notesPending: false })
      setStatus(`Saved notes to ${notesPath}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setSavingNotes(false)
    }
  }

  async function saveSource() {
    if (!detail) {
      return
    }
    setSavingSource(true)
    setStatus('Saving article…')
    try {
      await desktopCommand<{ articlePath: string; markdown: string }>('save_document', {
        articlePath: detail.summary.articlePath,
        markdown: sourceDraft,
      })
      setSourceSavedSnapshot(sourceDraft)
      setDetail((current) => (current ? { ...current, markdown: sourceDraft } : current))
      setStatus(`Saved ${detail.summary.title}`)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setSavingSource(false)
    }
  }

  async function startNotesGeneration(strategy: NotesGenerationStrategy) {
    if (!detail) {
      return
    }
    setGeneratingNotes(true)
    setStatus('Generating notes…')
    try {
      const response = await desktopCommand<NotesGenerationStatus & { started?: boolean }>('generate_notes', {
        articlePath: detail.summary.articlePath,
        strategy,
        existingNotes: notesDraft,
      })
      setNotesDraft(response.notesMarkdown || '')
      setNotesPending(true)
      setNotesPreviewReady(false)
      setNotesGenerationState(response.state)
      setNotesAutoFollow(true)
      applyDocumentSummaryPatch(detail.summary.id, { notesPending: true })
      setStatus(response.message ?? 'Generating notes…')
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setGeneratingNotes(false)
    }
  }

  function generateNotes() {
    if (!detail) {
      return
    }
    if (notesDraft.trim()) {
      setNotesStrategyDialogOpen(true)
      return
    }
    void startNotesGeneration('replace')
  }

  async function stopNotesGeneration() {
    if (!detail || !notesPending) {
      return
    }
    try {
      const response = await desktopCommand<{ state: NotesGenerationStatus['state'] }>('cancel_notes', {
        articlePath: detail.summary.articlePath,
      })
      setNotesGenerationState(response.state)
      setStatus('Stopping LLM…')
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  useEffect(() => {
    if (!detail || !notesPending) {
      return
    }

    let cancelled = false
    const articlePath = detail.summary.articlePath
    const documentId = detail.summary.id

    const poll = async () => {
      try {
        const generation = await desktopCommand<NotesGenerationStatus>('notes_status', { articlePath })
        if (cancelled || activeDocIdRef.current !== documentId) {
          return
        }
        const pending = generation.state === 'running' || generation.state === 'cancelling'
        setNotesPending(pending)
        setNotesGenerationState(generation.state)
        if (generation.notesMarkdown) {
          setNotesDraft(generation.notesMarkdown)
        }
        applyDocumentSummaryPatch(documentId, {
          notesPath: generation.notesPath,
          notesPending: pending,
        })
        if (generation.state === 'completed' && generation.notesPath) {
          setNotesPending(false)
          setNotesPreviewReady(Boolean(generation.previewAvailable))
          setNotesSavedSnapshot(generation.notesMarkdown || '')
          setNotesAutoFollow(false)
          setStatus(`Working notes ready: ${detail.summary.title}`)
        } else if (generation.state === 'cancelled') {
          setNotesPending(false)
          setNotesPreviewReady(false)
          setNotesAutoFollow(false)
          setStatus(generation.message ?? 'LLM generation stopped.')
        } else if (generation.state === 'error') {
          setNotesPending(false)
          setNotesPreviewReady(false)
          setNotesAutoFollow(false)
          setStatus(generation.error ?? generation.message ?? 'Notes generation failed.')
        } else {
          setNotesPreviewReady(false)
          if (generation.message) {
            setStatus(generation.message)
          }
        }
      } catch (error) {
        if (!cancelled) {
          setNotesPending(false)
          setNotesPreviewReady(false)
          setStatus(stringifyError(error))
        }
      }
    }

    void poll()
    const intervalId = window.setInterval(() => {
      void poll()
    }, 2500)

    return () => {
      cancelled = true
      window.clearInterval(intervalId)
    }
  }, [detail, notesPending])

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

  async function persistRelated(next: RelatedLink[], successMessage: string) {
    if (!detail) return
    setRelatedSaving(true)
    try {
      const response = await desktopCommand<{ items: RelatedLink[]; relatedPath: string }>('save_related', {
        articlePath: detail.summary.articlePath,
        items: next,
      })
      setRelatedLinks(response.items)
      setDetail((current) =>
        current ? { ...current, related: response.items } : current,
      )
      setStatus(successMessage)
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setRelatedSaving(false)
    }
  }

  async function loadRelatedSuggestions() {
    if (!detail) return
    setLoadingSuggestions(true)
    try {
      const items = await desktopCommand<RelatedSuggestion[]>('suggest_related', {
        articlePath: detail.summary.articlePath,
        root: library.root || null,
      })
      setRelatedSuggestions(items)
      setStatus(items.length ? `Found ${items.length} suggestions` : 'No related documents found in library')
    } catch (error) {
      setStatus(stringifyError(error))
    } finally {
      setLoadingSuggestions(false)
    }
  }

  async function addRelatedLink(target: DocumentSummary) {
    if (!detail) return
    if (target.articlePath === detail.summary.articlePath) {
      setStatus('Cannot link a document to itself')
      return
    }
    if (relatedLinks.some((link) => link.targetPath === target.articlePath)) {
      setStatus('Already linked')
      return
    }
    const entry: RelatedLink = {
      id: `${target.id}-${Date.now()}`,
      targetPath: target.articlePath,
      targetTitle: target.title,
      note: relatedDraftNote.trim(),
      createdAt: new Date().toISOString(),
    }
    const next = [...relatedLinks, entry]
    setRelatedPickerOpen(false)
    setRelatedPickerQuery('')
    setRelatedDraftNote('')
    await persistRelated(next, `Linked ${target.title}`)
  }

  async function removeRelatedLink(targetPath: string) {
    const next = relatedLinks.filter((link) => link.targetPath !== targetPath)
    await persistRelated(next, 'Removed related link')
  }

  async function updateRelatedNote(targetPath: string, note: string) {
    const next = relatedLinks.map((link) =>
      link.targetPath === targetPath ? { ...link, note } : link,
    )
    setRelatedLinks(next)
  }

  async function commitRelatedNote(_targetPath: string) {
    await persistRelated(relatedLinks, 'Updated related note')
  }

  async function handleGenerateReadingPdf() {
    if (!detail || generatingReadingPdf) return
    setGeneratingReadingPdf(true)
    setStatus('Generating reading PDF…')
    try {
      const response = await desktopCommand<{ readingPdfPath: string; cached: boolean }>(
        'generate_reading_pdf',
        {
          articlePath: detail.summary.articlePath,
          pageSize: readingPdfPageSize,
          stripReferences: referencesAreNoise,
        },
      )
      setDetail((current) =>
        current
          ? {
              ...current,
              summary: { ...current.summary, readingPdfPath: response.readingPdfPath },
            }
          : current,
      )
      window.open(buildDownloadUrl(response.readingPdfPath), '_blank', 'noopener,noreferrer')
      setStatus(response.cached ? 'Reading PDF ready (cached)' : 'Reading PDF generated')
    } catch (error) {
      setStatus(`Failed to generate reading PDF: ${(error as Error).message}`)
    } finally {
      setGeneratingReadingPdf(false)
    }
  }

  async function addHighlightFromSelection(variant: NoiseVariant = 'highlight') {
    if (!detail || !pendingSelection?.text.trim()) {
      return
    }
    const createdAt = new Date().toISOString()
    const nextEntry: Highlight = {
      id: `${createdAt}-${Math.random().toString(36).slice(2, 8)}`,
      text: pendingSelection.text.trim(),
      createdAt,
      startOffset: pendingSelection.startOffset,
      endOffset: pendingSelection.endOffset,
    }
    if (variant === 'noise') {
      nextEntry.variant = 'noise'
    }
    const nextHighlights: Highlight[] = [nextEntry, ...highlights]
    setPendingSelection(null)
    window.getSelection()?.removeAllRanges()
    await persistHighlights(
      nextHighlights,
      variant === 'noise' ? 'Saved noise mark' : 'Saved highlight',
    )
  }

  async function toggleHighlightNoise(highlightId: string) {
    const target = highlights.find((entry) => entry.id === highlightId)
    if (!target) return
    const nextVariant: 'noise' | undefined = target.variant === 'noise' ? undefined : 'noise'
    const nextHighlights: Highlight[] = highlights.map((entry) => {
      if (entry.id !== highlightId) return entry
      const updated: Highlight = { ...entry }
      if (nextVariant) {
        updated.variant = nextVariant
      } else {
        delete updated.variant
      }
      return updated
    })
    await persistHighlights(
      nextHighlights,
      nextVariant === 'noise' ? 'Marked as noise' : 'Removed noise mark',
    )
  }

  async function deleteHighlight(highlightId: string) {
    const nextHighlights = highlights.filter((entry) => entry.id !== highlightId)
    if (highlightCommentDraft?.id === highlightId) {
      setHighlightCommentDraft(null)
    }
    await persistHighlights(nextHighlights, 'Deleted highlight')
  }

  async function toggleElementNoise(type: 'img' | 'table', index: number) {
    const existing = highlights.find(
      (entry) =>
        entry.kind === 'element' &&
        entry.elementType === type &&
        entry.elementIndex === index,
    )
    if (existing) {
      const nextHighlights = highlights.filter((entry) => entry.id !== existing.id)
      await persistHighlights(nextHighlights, `Removed ${type === 'img' ? 'image' : 'table'} noise`)
      return
    }
    const createdAt = new Date().toISOString()
    const nextEntry: Highlight = {
      id: `${createdAt}-${Math.random().toString(36).slice(2, 8)}`,
      text: type === 'img' ? `[image ${index + 1}]` : `[table ${index + 1}]`,
      createdAt,
      kind: 'element',
      elementType: type,
      elementIndex: index,
      variant: 'noise',
    }
    const nextHighlights: Highlight[] = [nextEntry, ...highlights]
    await persistHighlights(nextHighlights, `Marked ${type === 'img' ? 'image' : 'table'} as noise`)
  }

  function openHighlightComment(highlight: Highlight) {
    setHighlightCommentDraft({ id: highlight.id, value: highlight.comment ?? '' })
  }

  async function saveHighlightComment() {
    if (!highlightCommentDraft) return
    const target = highlights.find((entry) => entry.id === highlightCommentDraft.id)
    if (!target) {
      setHighlightCommentDraft(null)
      return
    }
    const trimmed = highlightCommentDraft.value.trim()
    const nextHighlights = highlights.map((entry) =>
      entry.id === highlightCommentDraft.id ? { ...entry, comment: trimmed || undefined } : entry,
    )
    setHighlightCommentDraft(null)
    await persistHighlights(
      nextHighlights,
      trimmed ? 'Saved highlight comment' : 'Cleared highlight comment',
    )
  }

  function closeDocument() {
    const currentId = detail?.summary.id ?? activeId
    if (currentId) {
      const remainingBuffers = openDocIds.filter((id) => id !== currentId)
      setOpenDocIds(remainingBuffers)
      const nextActive = remainingBuffers[0] ?? null
      setActiveId(nextActive)
    } else {
      setActiveId(null)
    }
    setStatus('Closed document')
  }

  function closeBufferEntry(id: string) {
    const remainingBuffers = openDocIds.filter((entry) => entry !== id)
    setOpenDocIds(remainingBuffers)
    if (activeId === id) {
      setActiveId(remainingBuffers[0] ?? null)
    }
  }

  async function deleteCurrentDocument() {
    if (!detail) return
    const title = detail.summary.title || 'this article'
    const confirmed = window.confirm(
      `Permanently delete "${title}" and all its sibling files? This cannot be undone.`,
    )
    if (!confirmed) return
    setStatus(`Deleting ${title}…`)
    try {
      await desktopCommand('delete_document', { articlePath: detail.summary.articlePath })
      try {
        window.localStorage.removeItem(`${persistedScrollKeyPrefix}${detail.summary.id}`)
      } catch {
        // ignore
      }
      setOpenDocIds((current) => current.filter((id) => id !== detail.summary.id))
      setActiveId(null)
      await refreshLibrary()
      setStatus(`Removed ${title}`)
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  function adjustFontSize(delta: number) {
    setReaderFontSize((current) => {
      const next = Number((current + delta).toFixed(3))
      if (next < MIN_FONT_SIZE) return MIN_FONT_SIZE
      if (next > MAX_FONT_SIZE) return MAX_FONT_SIZE
      return next
    })
  }

  function resetFontSize() {
    setReaderFontSize(DEFAULT_FONT_SIZE)
  }

  function toggleTheme() {
    setTheme((current) => (current === 'dark' ? 'light' : 'dark'))
  }

  function toggleFocusMode() {
    setFocusMode((current) => !current)
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

  async function handleRevealLocation() {
    if (!detail) return
    try {
      const response = await desktopCommand<{
        directoryPath: string
        hostDirectoryPath: string | null
        launched: boolean
      }>('reveal_location', { path: detail.summary.articlePath })
      const displayPath = response.hostDirectoryPath || response.directoryPath
      if (response.launched) {
        setStatus(`Opened ${displayPath}`)
        return
      }
      try {
        await navigator.clipboard.writeText(displayPath)
        setStatus(
          response.hostDirectoryPath
            ? `Copied host path to clipboard: ${displayPath}`
            : `Copied container path to clipboard: ${displayPath} (set HOST_OUTPUT_DIR_NATIVE to translate)`,
        )
      } catch {
        setStatus(`Location: ${displayPath}`)
      }
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  async function handleOpenExternalFile(path: string, label: string) {
    if (!path) return
    try {
      const response = await desktopCommand<{ path: string; launched: boolean }>(
        'open_external_file',
        { path },
      )
      if (response.launched) {
        setStatus(`Opened ${label}`)
        return
      }
      window.open(buildDownloadUrl(path), '_blank', 'noopener,noreferrer')
    } catch (error) {
      setStatus(stringifyError(error))
    }
  }

  async function copyImageToClipboard(src: string) {
    if (!src) {
      setStatus('Nothing to copy')
      return
    }
    try {
      if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
        throw new Error('Clipboard API unavailable in this browser')
      }
      const response = await fetch(src, { credentials: 'omit' })
      if (!response.ok) {
        throw new Error(`Failed to fetch image (${response.status})`)
      }
      const blob = await response.blob()
      const pngBlob = blob.type === 'image/png' ? blob : await blobToPng(blob)
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': pngBlob })])
      setStatus('Copied image to clipboard')
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

  function goToHighlight(nextIndex: number) {
    const list = highlights.filter((entry) => entry.variant !== 'noise' && entry.kind !== 'element')
    if (!list.length) return
    const total = list.length
    const normalized = ((nextIndex % total) + total) % total
    setHighlightCursor(normalized)
    const target = list[normalized]
    if (target) {
      scrollToHighlight(target.id)
    }
  }

  function renderHighlightCard(highlight: Highlight) {
    const isNoise = highlight.variant === 'noise'
    return (
      <article
        className={`highlight-card${isNoise ? ' is-noise' : ''}`}
        key={highlight.id}
        ref={(element) => {
          if (element) {
            highlightCardRefs.current.set(highlight.id, element)
          } else {
            highlightCardRefs.current.delete(highlight.id)
          }
        }}
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
        {isNoise ? <span className="highlight-badge">NOISE</span> : null}
        <p>{highlight.text}</p>
        {highlightCommentDraft?.id === highlight.id ? (
          <div
            className="highlight-comment-editor"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => event.stopPropagation()}
          >
            <textarea
              className="highlight-comment-input"
              value={highlightCommentDraft.value}
              onChange={(event) =>
                setHighlightCommentDraft({ id: highlight.id, value: event.target.value })
              }
              placeholder="Add a short comment or reminder for this highlight…"
              rows={2}
              autoFocus
            />
            <div className="inline-row">
              <button
                className="ghost-button highlight-button"
                onClick={() => void saveHighlightComment()}
                disabled={savingHighlights}
              >
                {savingHighlights ? 'Saving…' : 'Save comment'}
              </button>
              <button
                className="ghost-button highlight-button"
                onClick={() => setHighlightCommentDraft(null)}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : highlight.comment ? (
          <p className="highlight-comment">{highlight.comment}</p>
        ) : null}
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
                openHighlightComment(highlight)
              }}
            >
              {highlight.comment ? 'Edit note' : 'Add note'}
            </button>
            <button
              className="ghost-button highlight-button"
              onClick={(event) => {
                event.stopPropagation()
                void toggleHighlightNoise(highlight.id)
              }}
              disabled={savingHighlights}
              title={isNoise ? 'Convert back to highlight' : 'Convert to noise (strikethrough, hidable)'}
            >
              {isNoise ? 'Un-noise' : 'Noise'}
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
    )
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

  async function changeLabel(newLabel: string) {
    if (!detail) return
    const oldPath = detail.summary.articlePath
    const oldLabel = detail.summary.label
    try {
      const result = await desktopCommand<{ articlePath: string; label: string; moved: boolean }>(
        'change_label',
        { articlePath: oldPath, label: newLabel },
      )
      if (result.moved) {
        await refreshLibrary()
        await loadDocumentByPath(result.articlePath)
        setStatus(`Moved to "${result.label || 'unlabeled'}"`)
      }
    } catch (error) {
      setDetail((current) =>
        current ? { ...current, summary: { ...current.summary, label: oldLabel } } : current,
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

  function applyNotesTransform(transform: NotesTransform) {
    const view = notesEditorViewRef.current
    if (!view) return
    const state = view.state
    const selection = state.selection.main
    const value = state.doc.toString()
    const result = transform(value, selection.from, selection.to)
    view.dispatch({
      changes: { from: 0, to: state.doc.length, insert: result.value },
      selection: EditorSelection.single(result.selStart, result.selEnd),
    })
    view.focus()
  }

  const notesEditorExtensions = useMemo(
    () => [
      markdown(),
      EditorView.lineWrapping,
      keymap.of([
        {
          key: 'Mod-0',
          run: () => {
            applyNotesTransform(setLineHeadingTransform(0))
            return true
          },
        },
        ...['1', '2', '3', '4', '5', '6'].map((digit) => ({
          key: `Mod-${digit}`,
          run: () => {
            applyNotesTransform(setLineHeadingTransform(Number(digit)))
            return true
          },
        })),
        {
          key: 'Mod-b',
          run: () => {
            applyNotesTransform(wrapSelectionTransform('**', '**', 'bold text'))
            return true
          },
        },
        {
          key: 'Mod-i',
          run: () => {
            applyNotesTransform(wrapSelectionTransform('*', '*', 'italic text'))
            return true
          },
        },
        {
          key: 'Mod-k',
          run: () => {
            applyNotesTransform(insertLinkTransform())
            return true
          },
        },
        {
          key: 'Mod-Shift-k',
          run: () => {
            applyNotesTransform(insertCodeBlockTransform())
            return true
          },
        },
        {
          key: 'Mod-Shift-q',
          run: () => {
            applyNotesTransform(insertBlockquoteTransform())
            return true
          },
        },
        {
          key: 'Mod-Shift-c',
          run: () => {
            applyNotesTransform(wrapSelectionTransform('`', '`', 'code'))
            return true
          },
        },
      ]),
    ],
    [],
  )
  const sourceEditorExtensions = useMemo(
    () => [
      markdown(),
      EditorView.lineWrapping,
    ],
    [],
  )

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

  function onSplitterPointerDown(pane: 'left' | 'right' | 'notes', event: React.PointerEvent<HTMLDivElement>) {
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

    if (dragStateRef.current.pane === 'notes') {
      const container = notesScrollRef.current
      if (!container) return
      const rect = container.getBoundingClientRect()
      const available = rect.height
      const next = Math.max(
        MIN_NOTES_TOP_HEIGHT,
        Math.min(event.clientY - rect.top, available - MIN_NOTES_BOTTOM_HEIGHT),
      )
      setNotesTopHeight(next)
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

  const effectiveArticleMarkdown = useMemo(
    () => (detail ? sourceDraft : ''),
    [detail, sourceDraft],
  )

  const articleHeadings = useMemo(
    () => parseHeadings(effectiveArticleMarkdown),
    [effectiveArticleMarkdown],
  )

  const [noisyHeadingIndexes, setNoisyHeadingIndexes] = useState<Set<number>>(() => new Set())

  useEffect(() => {
    if (!effectiveArticleMarkdown) {
      setNoisyHeadingIndexes(new Set())
      return
    }
    const compute = () => {
      const root = document.querySelector('.markdown-body')
      if (!root) {
        setNoisyHeadingIndexes(new Set())
        return
      }
      const all = root.querySelectorAll('h1, h2, h3, h4, h5, h6')
      const next = new Set<number>()
      all.forEach((element, index) => {
        if (
          element.classList.contains('section-noise') ||
          element.closest('.section-noise') ||
          element.closest('mark.inline-noise')
        ) {
          next.add(index)
        }
      })
      setNoisyHeadingIndexes((current) => {
        if (current.size === next.size && [...current].every((value) => next.has(value))) {
          return current
        }
        return next
      })
    }
    const frame = window.requestAnimationFrame(compute)
    return () => window.cancelAnimationFrame(frame)
  }, [effectiveArticleMarkdown, highlights, referencesAreNoise])

  const visibleHeadings = useMemo(() => {
    if (!noisyHeadingIndexes.size) return articleHeadings
    return articleHeadings.filter((entry) => !noisyHeadingIndexes.has(entry.index))
  }, [articleHeadings, noisyHeadingIndexes])

  const tocCandidates = useMemo(() => {
    const q = tocQuery.trim().toLowerCase()
    if (!q) return visibleHeadings
    return visibleHeadings.filter((entry) => entry.text.toLowerCase().includes(q))
  }, [visibleHeadings, tocQuery])

  useEffect(() => {
    if (!tocOpen) return
    if (tocCursor >= tocCandidates.length) {
      setTocCursor(Math.max(0, tocCandidates.length - 1))
    }
  }, [tocCandidates, tocOpen, tocCursor])

  function selectTocEntry(entry: HeadingEntry) {
    setTocOpen(false)
    setTocQuery('')
    setTocCursor(0)
    if (readerMode === 'source') {
      const view = sourceEditorViewRef.current
      if (view) {
        view.dispatch({
          selection: EditorSelection.cursor(entry.offset),
          effects: EditorView.scrollIntoView(entry.offset, { y: 'start' }),
        })
        view.focus()
      }
      return
    }
    setScrollHeadingRequest({ index: entry.index, ts: Date.now() })
  }

  const openDocsSet = useMemo(() => new Set(openDocIds), [openDocIds])
  const switcherCandidates = useMemo(() => {
    const idToDoc = new Map(library.documents.map((doc) => [doc.id, doc]))
    const openDocs: DocumentSummary[] = []
    for (const id of openDocIds) {
      const doc = idToDoc.get(id)
      if (doc) openDocs.push(doc)
    }
    const rest = library.documents.filter((doc) => !openDocsSet.has(doc.id))
    const all = [...openDocs, ...rest]
    const q = switcherQuery.trim().toLowerCase()
    if (!q) return all
    const tokens = q.split(/\s+/).filter(Boolean)
    return all.filter((doc) => {
      const hay = `${doc.title} ${doc.label ?? ''} ${doc.sourceSite ?? ''} ${doc.excerpt ?? ''}`.toLowerCase()
      return tokens.every((token) => hay.includes(token))
    })
  }, [library.documents, openDocIds, openDocsSet, switcherQuery])

  useEffect(() => {
    if (!switcherOpen) return
    if (switcherCursor >= switcherCandidates.length) {
      setSwitcherCursor(Math.max(0, switcherCandidates.length - 1))
    }
  }, [switcherCandidates, switcherOpen, switcherCursor])

  function selectSwitcherCandidate(id: string) {
    setSwitcherOpen(false)
    setSwitcherQuery('')
    setSwitcherCursor(0)
    startTransition(() => setActiveId(id))
  }

  const shellStyle = {
    '--left-pane-width': `${leftPaneWidth}px`,
    '--right-pane-width': `${rightPaneWidth}px`,
    '--notes-top-height': `${notesTopHeight}px`,
  } as CSSProperties & Record<'--left-pane-width' | '--right-pane-width' | '--notes-top-height', string>
  const renderedMarkdown = useMemo(
    () => (detail ? normalizeReaderMarkdown(effectiveArticleMarkdown) : ''),
    [detail, effectiveArticleMarkdown],
  )
  const renderedNotesMarkdown = useMemo(
    () => normalizeReaderMarkdown(notesDraft),
    [notesDraft],
  )
  const notesDirty = notesDraft.trimEnd() !== notesSavedSnapshot.trimEnd()
  const sourceDirty = sourceDraft.trimEnd() !== sourceSavedSnapshot.trimEnd()
  const visibleHighlights = useMemo(
    () => highlights.filter((entry) => entry.variant !== 'noise' && entry.kind !== 'element'),
    [highlights],
  )

  return (
    <div className={`shell${focusMode ? ' focus-mode' : ''}`} style={shellStyle} data-theme={theme}>
      {focusMode ? (
        <button
          type="button"
          className="focus-exit-button"
          onClick={toggleFocusMode}
          title="Exit focus mode (Esc)"
          aria-label="Exit focus mode"
        >
          Exit focus
        </button>
      ) : null}
      {switcherOpen ? (
        <div
          className="switcher-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Switch document"
          onClick={() => setSwitcherOpen(false)}
        >
          <div className="switcher-dialog" onClick={(event) => event.stopPropagation()}>
            <input
              className="switcher-input"
              type="text"
              autoFocus
              placeholder="Switch document… (open buffers first, then library)"
              value={switcherQuery}
              onChange={(event) => {
                setSwitcherQuery(event.target.value)
                setSwitcherCursor(0)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault()
                  setSwitcherOpen(false)
                  return
                }
                if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  setSwitcherCursor((current) =>
                    Math.min(switcherCandidates.length - 1, current + 1),
                  )
                  return
                }
                if (event.key === 'ArrowUp') {
                  event.preventDefault()
                  setSwitcherCursor((current) => Math.max(0, current - 1))
                  return
                }
                if (event.key === 'Enter') {
                  event.preventDefault()
                  const target = switcherCandidates[switcherCursor]
                  if (target) {
                    selectSwitcherCandidate(target.id)
                  }
                }
              }}
            />
            <div className="switcher-list" role="listbox">
              {switcherCandidates.length === 0 ? (
                <div className="switcher-empty">No matching documents.</div>
              ) : (
                switcherCandidates.slice(0, 50).map((doc, index) => {
                  const isOpen = openDocsSet.has(doc.id)
                  const isCursor = index === switcherCursor
                  const isActive = doc.id === activeId
                  return (
                    <button
                      key={doc.id}
                      type="button"
                      className={`switcher-item${isCursor ? ' is-cursor' : ''}${isActive ? ' is-active' : ''}`}
                      role="option"
                      aria-selected={isCursor}
                      onMouseEnter={() => setSwitcherCursor(index)}
                      onClick={() => selectSwitcherCandidate(doc.id)}
                    >
                      <span className={`switcher-dot${isOpen ? ' is-open' : ''}`} aria-hidden="true" />
                      <span className="switcher-item-body">
                        <span className="switcher-item-title">{doc.title}</span>
                        <span className="switcher-item-meta">
                          {doc.label ?? 'unlabeled'} · {doc.sourceSite ?? 'local'}
                          {doc.rating > 0 ? ` · ${'★'.repeat(doc.rating)}` : ''}
                        </span>
                      </span>
                      {isOpen ? (
                        <span
                          className="switcher-close"
                          role="button"
                          tabIndex={-1}
                          aria-label={`Close ${doc.title}`}
                          title="Remove from open buffers"
                          onClick={(event) => {
                            event.stopPropagation()
                            closeBufferEntry(doc.id)
                          }}
                        >
                          ×
                        </span>
                      ) : null}
                    </button>
                  )
                })
              )}
            </div>
            <div className="switcher-footer">
              <span>↑↓ navigate · ↵ open · Esc close · Ctrl+P toggle</span>
              <span>{openDocIds.length} open</span>
            </div>
          </div>
        </div>
      ) : null}
      {tocOpen ? (
        <div
          className="switcher-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Jump to heading"
          onClick={() => setTocOpen(false)}
        >
          <div className="switcher-dialog" onClick={(event) => event.stopPropagation()}>
            <input
              className="switcher-input"
              type="text"
              autoFocus
              placeholder={
                visibleHeadings.length
                  ? 'Jump to section…'
                  : 'No headings in current document'
              }
              disabled={!visibleHeadings.length}
              value={tocQuery}
              onChange={(event) => {
                setTocQuery(event.target.value)
                setTocCursor(0)
              }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault()
                  setTocOpen(false)
                  return
                }
                if (event.key === 'ArrowDown') {
                  event.preventDefault()
                  setTocCursor((current) =>
                    Math.min(tocCandidates.length - 1, current + 1),
                  )
                  return
                }
                if (event.key === 'ArrowUp') {
                  event.preventDefault()
                  setTocCursor((current) => Math.max(0, current - 1))
                  return
                }
                if (event.key === 'Enter') {
                  event.preventDefault()
                  const target = tocCandidates[tocCursor]
                  if (target) {
                    selectTocEntry(target)
                  }
                }
              }}
            />
            <div className="switcher-list" role="listbox">
              {tocCandidates.length === 0 ? (
                <div className="switcher-empty">
                  {visibleHeadings.length ? 'No matching headings.' : 'This document has no headings.'}
                </div>
              ) : (
                tocCandidates.slice(0, 200).map((entry, index) => {
                  const isCursor = index === tocCursor
                  return (
                    <button
                      key={`${entry.index}-${entry.text}`}
                      type="button"
                      className={`switcher-item toc-item toc-level-${entry.level}${isCursor ? ' is-cursor' : ''}`}
                      role="option"
                      aria-selected={isCursor}
                      onMouseEnter={() => setTocCursor(index)}
                      onClick={() => selectTocEntry(entry)}
                    >
                      <span className="toc-bullet" aria-hidden="true">
                        H{entry.level}
                      </span>
                      <span className="switcher-item-body">
                        <span className="switcher-item-title">{entry.text}</span>
                      </span>
                    </button>
                  )
                })
              )}
            </div>
            <div className="switcher-footer">
              <span>↑↓ navigate · ↵ jump · Esc close · Ctrl+Shift+O toggle</span>
              <span>{visibleHeadings.length} headings</span>
            </div>
          </div>
        </div>
      ) : null}
      {lightbox ? (
        <div
          className="lightbox-overlay"
          role="dialog"
          aria-modal="true"
          onClick={() => setLightbox(null)}
        >
          <div className="lightbox-toolbar" onClick={(event) => event.stopPropagation()}>
            <button
              type="button"
              className="ghost-button"
              onClick={() => void copyImageToClipboard(lightbox.src)}
            >
              Copy image
            </button>
            <a
              className="ghost-button"
              href={lightbox.src}
              target="_blank"
              rel="noopener noreferrer"
              download
            >
              Open full size
            </a>
            {lightbox.imageIndex != null ? (
              <button
                type="button"
                className="ghost-button"
                onClick={() => {
                  const idx = lightbox.imageIndex
                  if (idx == null) return
                  void toggleElementNoise('img', idx)
                  setLightbox(null)
                }}
                title="Toggle image noise mark"
              >
                {highlights.some(
                  (h) =>
                    h.kind === 'element' &&
                    h.elementType === 'img' &&
                    h.elementIndex === lightbox.imageIndex &&
                    h.variant === 'noise',
                )
                  ? 'Unmark noise'
                  : 'Mark as noise'}
              </button>
            ) : null}
            <button
              type="button"
              className="ghost-button"
              onClick={() => setLightbox(null)}
              aria-label="Close image viewer"
            >
              Close
            </button>
          </div>
          <img
            className="lightbox-image"
            src={lightbox.src}
            alt={lightbox.alt}
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      ) : null}
      {infoOpen && detail ? (
        <div
          className="info-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Document metadata"
          onClick={() => setInfoOpen(false)}
        >
          <div className="info-panel" onClick={(event) => event.stopPropagation()}>
            <header className="info-header">
              <h2>{detail.summary.title}</h2>
              <button
                type="button"
                className="ghost-button"
                onClick={() => setInfoOpen(false)}
                aria-label="Close metadata view"
              >
                Close
              </button>
            </header>
            <div className="info-body">
              {(() => {
                const entries = renderFrontmatterEntries(detail.frontmatter)
                const citationEntries = buildCitationEntries(
                  detail.bibliography ?? '',
                  detail.frontmatter,
                )
                const frontmatterKeys = new Set(entries.map(([k]) => k))
                const citationOnly = citationEntries.filter(([k]) => !frontmatterKeys.has(k))
                const locationDisplay =
                  infoLocation?.hostDirectoryPath || infoLocation?.directoryPath || ''
                return (
                  <>
                    <dl className="info-grid">
                      <div className="info-row" key="__physical_location">
                        <dt>Physical location</dt>
                        <dd>
                          {locationDisplay ? (
                            <button
                              type="button"
                              className="info-link info-link-button"
                              onClick={() => void handleRevealLocation()}
                              title="Open in file manager (or copy path if unavailable)"
                            >
                              {locationDisplay}
                            </button>
                          ) : (
                            <span className="muted">Loading…</span>
                          )}
                        </dd>
                      </div>
                    </dl>
                    {entries.length ? (
                      <dl className="info-grid">
                        {entries.map(([key, value]) => (
                          <div className="info-row" key={key}>
                            <dt>{key}</dt>
                            <dd>{renderInfoValue(key, value, {
                              onOpenExternalFile: (path, label) =>
                                void handleOpenExternalFile(path, label),
                              onCopy: (text, label) => void copyTextToClipboard(text, label),
                            })}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : (
                      <p className="muted">No frontmatter metadata found.</p>
                    )}
                    {citationOnly.length ? (
                      <>
                        <h3 className="info-section-title">Citation</h3>
                        <dl className="info-grid">
                          {citationOnly.map(([key, value]) => (
                            <div className="info-row" key={`cite-${key}`}>
                              <dt>{key}</dt>
                              <dd>{renderInfoValue(key, value, {
                                onOpenExternalFile: (path, label) =>
                                  void handleOpenExternalFile(path, label),
                                onCopy: (text, label) => void copyTextToClipboard(text, label),
                              })}</dd>
                            </div>
                          ))}
                        </dl>
                      </>
                    ) : null}
                  </>
                )
              })()}
            </div>
          </div>
        </div>
      ) : null}
      {notesStrategyDialogOpen && detail ? (
        <div
          className="info-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Choose notes generation strategy"
          onClick={() => setNotesStrategyDialogOpen(false)}
        >
          <div
            className="info-panel notes-strategy-panel"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="info-header notes-strategy-header">
              <div>
                <h2>Notes already exist</h2>
                <p className="notes-strategy-copy">
                  Choose how the model should handle the current notes draft.
                </p>
              </div>
              <button
                type="button"
                className="ghost-button"
                onClick={() => setNotesStrategyDialogOpen(false)}
                aria-label="Close notes strategy dialog"
              >
                Close
              </button>
            </header>
            <div className="info-body notes-strategy-body">
              <button
                type="button"
                className="ghost-button notes-strategy-button"
                onClick={() => {
                  setNotesStrategyDialogOpen(false)
                  void startNotesGeneration('replace')
                }}
              >
                <span className="notes-strategy-title">Replace</span>
                <span className="notes-strategy-description">
                  Discard the current notes draft and generate a fresh one.
                </span>
              </button>
              <button
                type="button"
                className="ghost-button notes-strategy-button"
                onClick={() => {
                  setNotesStrategyDialogOpen(false)
                  void startNotesGeneration('append')
                }}
              >
                <span className="notes-strategy-title">Append</span>
                <span className="notes-strategy-description">
                  Keep the current notes and add a newly generated section below them.
                </span>
              </button>
              <button
                type="button"
                className="primary-button notes-strategy-button"
                autoFocus
                onClick={() => {
                  setNotesStrategyDialogOpen(false)
                  void startNotesGeneration('fuse')
                }}
              >
                <span className="notes-strategy-title">Fuse</span>
                <span className="notes-strategy-description">
                  Merge the current draft with a new pass into one cleaned-up notes document.
                </span>
              </button>
              <div className="notes-strategy-actions">
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => setNotesStrategyDialogOpen(false)}
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
      {rootPickerOpen ? (
        <div
          className="info-overlay"
          role="dialog"
          aria-modal="true"
          aria-label="Choose corpus root"
          onClick={() => setRootPickerOpen(false)}
        >
          <div
            className="info-panel picker-panel"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="info-header">
              <h2>Choose corpus root</h2>
              <button
                type="button"
                className="ghost-button"
                onClick={() => setRootPickerOpen(false)}
                aria-label="Close directory picker"
              >
                Close
              </button>
            </header>
            <div className="picker-body">
              <div className="picker-toolbar">
                <button
                  type="button"
                  className="ghost-button"
                  disabled={!rootPickerParent || rootPickerLoading}
                  onClick={() => void loadRootPickerPath(rootPickerParent)}
                >
                  ← Parent
                </button>
                <input
                  type="text"
                  className="text-input picker-path"
                  value={rootPickerPath}
                  onChange={(event) => setRootPickerPath(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      void loadRootPickerPath(rootPickerPath.trim() || null)
                    }
                  }}
                  placeholder="/path/to/corpus"
                />
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void loadRootPickerPath(rootPickerPath.trim() || null)}
                  disabled={rootPickerLoading}
                >
                  Go
                </button>
              </div>
              {rootPickerError ? (
                <p className="picker-error">{rootPickerError}</p>
              ) : null}
              <ul className="picker-list">
                {rootPickerLoading ? (
                  <li className="picker-item muted">Loading…</li>
                ) : rootPickerEntries.length === 0 ? (
                  <li className="picker-item muted">No subdirectories</li>
                ) : (
                  rootPickerEntries.map((entry) => (
                    <li key={entry.path}>
                      <button
                        type="button"
                        className="picker-item"
                        onClick={() => void loadRootPickerPath(entry.path)}
                      >
                        📁 {entry.name}
                      </button>
                    </li>
                  ))
                )}
              </ul>
            </div>
            <footer className="picker-footer">
              <button
                type="button"
                className="primary-button"
                onClick={() => void confirmRootPicker()}
                disabled={!rootPickerPath || rootPickerLoading}
              >
                Use this folder
              </button>
            </footer>
          </div>
        </div>
      ) : null}
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
              <button
                className="ghost-button"
                onClick={openRootPicker}
                disabled={loadingLibrary}
                title="Browse filesystem to pick a corpus root"
              >
                Browse…
              </button>
              <button className="ghost-button" onClick={() => void refreshLibrary()} disabled={loadingLibrary}>
                Reload
              </button>
              <button
                className="ghost-button"
                onClick={() => void handleReindex()}
                disabled={reindexing || loadingLibrary}
                title="Rebuild output/index.jsonl from the library bundles"
              >
                {reindexing ? 'Re-indexing…' : 'Re-index'}
              </button>
            </div>
          </div>

          <div className="panel">
            <label className="field-label" htmlFor="search">Search</label>
            <div className="search-input-row">
              <input
                id="search"
                className="text-input search-input"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="term or (Tournier[Author] AND CSD[Title])"
                title={
                  'Pubmed-style queries supported.\n' +
                  'Fields: [Author] [Title] [DOI] [Year] [Body] [Journal] [Publisher] [PMID] [PMCID] [Arxiv] [URL] [Label] [All]\n' +
                  'Operators: AND, OR, NOT, parentheses. Default field is All.'
                }
              />
              <button
                className="ghost-button search-clear-button"
                type="button"
                onClick={() => setSearch('')}
                disabled={!search}
                title="Clear search"
              >
                Clear
              </button>
            </div>

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
                {searching
                  ? 'Searching local indexed corpus…'
                  : searchTotal !== null
                    ? `Using local indexed search. ${searchTotal} match${searchTotal === 1 ? '' : 'es'} found.`
                    : 'Using local indexed search.'}
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
                  {typeof doc.affinityScore === 'number' && doc.affinityScore > 0 ? (
                    <span
                      className="document-affinity"
                      title="Related to the currently open document"
                    >
                      related
                    </span>
                  ) : null}
                </div>
                <strong>{doc.title}</strong>
                {doc.authors ? <span className="document-meta">{doc.authors}</span> : null}
                <span className="document-meta">
                  {[doc.journal ?? doc.sourceSite ?? 'local', doc.year, doc.journal && doc.sourceSite ? doc.sourceSite : null].filter(Boolean).join(' · ')}
                </span>
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
                <select
                  className="label-select"
                  value={detail.summary.label ?? ''}
                  onChange={(e) => {
                    const value = e.target.value
                    if (value === '__new__') {
                      const name = window.prompt('New label name:')
                      if (name?.trim()) void changeLabel(name.trim())
                    } else {
                      void changeLabel(value)
                    }
                  }}
                >
                  <option value="">unlabeled</option>
                  {library.labels.map((l) => (
                    <option key={l} value={l}>{l}</option>
                  ))}
                  <option value="__new__">+ New label…</option>
                </select>
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
            {detail ? (
              <button
                type="button"
                className="badge badge-link"
                onClick={() => void handleGenerateReadingPdf()}
                disabled={generatingReadingPdf}
                title={`Render a reading-friendly PDF (${readingPdfPageSize.toUpperCase()})`}
              >
                {generatingReadingPdf ? 'Generating reading PDF…' : 'Reading PDF'}
              </button>
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
            <div className="reader-toolbar" role="toolbar" aria-label="Reader controls">
              <button
                type="button"
                className="ghost-button reader-toolbar-button"
                onClick={() => {
                  setSwitcherQuery('')
                  setSwitcherCursor(0)
                  setSwitcherOpen(true)
                }}
                title="Switch document (Ctrl+P)"
                aria-label="Open document switcher"
              >
                Docs{openDocIds.length ? ` · ${openDocIds.length}` : ''}
              </button>
              {detail && visibleHeadings.length ? (
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => {
                    setTocQuery('')
                    setTocCursor(0)
                    setTocOpen(true)
                  }}
                  title="Jump to heading (Ctrl+Shift+O)"
                  aria-label="Open table of contents"
                >
                  TOC · {visibleHeadings.length}
                </button>
              ) : null}
              {detail ? (
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => setFindOpen((current) => !current)}
                  title="Find in document (Ctrl+F)"
                  aria-label="Find in document"
                  aria-pressed={findOpen}
                >
                  Find
                </button>
              ) : null}
              {detail ? (
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => setInfoOpen(true)}
                  title="Show document metadata"
                  aria-label="Show document metadata"
                >
                  Info
                </button>
              ) : null}
              {detail?.bibliography ? (
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => void copyTextToClipboard(detail.bibliography, 'citation')}
                  title="Copy citation (BibTeX) to clipboard"
                  aria-label="Copy citation to clipboard"
                >
                  Cite
                </button>
              ) : null}
              {detail ? (
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button${readerMode === 'source' ? ' is-active' : ''}`}
                  onClick={() => setReaderMode((current) => (current === 'rendered' ? 'source' : 'rendered'))}
                  aria-pressed={readerMode === 'source'}
                  title={readerMode === 'source' ? 'Switch to reader view' : 'Switch to source view'}
                >
                  {readerMode === 'source' ? 'Reader' : 'Source'}
                </button>
              ) : null}
              {detail && readerMode === 'source' ? (
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button reader-icon-button${sourceDirty ? ' is-dirty' : ''}`}
                  onClick={() => void saveSource()}
                  disabled={savingSource}
                  title={savingSource ? 'Saving article…' : 'Save article source'}
                  aria-label={savingSource ? 'Saving article' : 'Save article source'}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
                    <path d="M2.5 1h8.8l2.7 2.7V14a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1Zm1 1.5V6h8V3.8L10.7 2.5H9.5V5h-4V2.5h-2Zm1 8.5v2.5h7V11h-7Z" fill="currentColor" />
                  </svg>
                </button>
              ) : null}
              <div className="settings-wrap" ref={settingsMenuRef}>
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button${settingsOpen ? ' is-active' : ''}`}
                  onClick={() => setSettingsOpen((current) => !current)}
                  aria-haspopup="menu"
                  aria-expanded={settingsOpen}
                  title="Reader settings"
                >
                  Settings
                </button>
                {settingsOpen ? (
                  <div className="settings-menu" role="menu">
                    <div className="settings-row">
                      <span className="settings-label">Font size</span>
                      <div className="font-size-controls" aria-label="Reader font size">
                        <button
                          type="button"
                          className="ghost-button reader-toolbar-button"
                          onClick={() => adjustFontSize(-FONT_SIZE_STEP)}
                          disabled={readerFontSize <= MIN_FONT_SIZE + 1e-6}
                          aria-label="Decrease font size"
                          title="Decrease font size"
                        >
                          A−
                        </button>
                        <button
                          type="button"
                          className="ghost-button reader-toolbar-button"
                          onClick={resetFontSize}
                          aria-label="Reset font size"
                          title="Reset font size"
                        >
                          A
                        </button>
                        <button
                          type="button"
                          className="ghost-button reader-toolbar-button"
                          onClick={() => adjustFontSize(FONT_SIZE_STEP)}
                          disabled={readerFontSize >= MAX_FONT_SIZE - 1e-6}
                          aria-label="Increase font size"
                          title="Increase font size"
                        >
                          A+
                        </button>
                      </div>
                    </div>
                    <button
                      type="button"
                      className={`settings-item${hideNoise ? ' is-active' : ''}`}
                      onClick={() => setHideNoise((current) => !current)}
                      role="menuitemcheckbox"
                      aria-checked={hideNoise}
                    >
                      <span>Hide noise</span>
                      <span className={`settings-switch${hideNoise ? ' is-on' : ''}`} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className={`settings-item${referencesAreNoise ? ' is-active' : ''}`}
                      onClick={() => setReferencesAreNoise((current) => !current)}
                      role="menuitemcheckbox"
                      aria-checked={referencesAreNoise}
                      title="Treat the References/Bibliography section as noise"
                    >
                      <span>References = noise</span>
                      <span className={`settings-switch${referencesAreNoise ? ' is-on' : ''}`} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className={`settings-item${focusMode ? ' is-active' : ''}`}
                      onClick={toggleFocusMode}
                      role="menuitemcheckbox"
                      aria-checked={focusMode}
                    >
                      <span>Focus mode</span>
                      <span className={`settings-switch${focusMode ? ' is-on' : ''}`} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className={`settings-item${theme === 'dark' ? ' is-active' : ''}`}
                      onClick={toggleTheme}
                      role="menuitemcheckbox"
                      aria-checked={theme === 'dark'}
                    >
                      <span>Dark theme</span>
                      <span className={`settings-switch${theme === 'dark' ? ' is-on' : ''}`} aria-hidden="true" />
                    </button>
                    <div className="settings-row">
                      <span className="settings-label">Reading PDF page size</span>
                      <div className="inline-row" role="radiogroup" aria-label="Reading PDF page size">
                        <button
                          type="button"
                          className={`ghost-button reader-toolbar-button${readingPdfPageSize === 'a4' ? ' is-active' : ''}`}
                          onClick={() => setReadingPdfPageSize('a4')}
                          role="radio"
                          aria-checked={readingPdfPageSize === 'a4'}
                        >
                          A4
                        </button>
                        <button
                          type="button"
                          className={`ghost-button reader-toolbar-button${readingPdfPageSize === 'a5' ? ' is-active' : ''}`}
                          onClick={() => setReadingPdfPageSize('a5')}
                          role="radio"
                          aria-checked={readingPdfPageSize === 'a5'}
                        >
                          A5
                        </button>
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
              {detail ? (
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button danger-button"
                  onClick={() => void deleteCurrentDocument()}
                  title="Delete this article bundle"
                >
                  Delete
                </button>
              ) : null}
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
          </div>
        </header>

        {pendingSelection && readerMode === 'rendered' ? (
          <div className="selection-banner">
            <span>Selection ready to save or quote.</span>
            <div className="inline-row">
              <button className="ghost-button" onClick={() => void addHighlightFromSelection('highlight')} disabled={savingHighlights}>
                {savingHighlights ? 'Saving…' : 'Add highlight'}
              </button>
              <button
                className="ghost-button"
                onClick={() => void addHighlightFromSelection('noise')}
                disabled={savingHighlights}
                title="Mark selection as noise (strikethrough, hidden when noise is hidden)"
              >
                Mark as noise
              </button>
              <button className="ghost-button" onClick={insertSelectionAsQuote}>
                Quote selection
              </button>
            </div>
          </div>
        ) : null}

        {findOpen && detail && findTarget === 'reader' ? (
          <div className="find-bar" role="search" aria-label="Find in article">
            <input
              ref={findInputRef}
              type="text"
              className="find-input"
              placeholder={readerMode === 'source' ? 'Find in article source…' : 'Find in article…'}
              value={findQuery}
              onChange={(event) => setFindQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault()
                  setFindOpen(false)
                  return
                }
                if (event.key === 'Enter') {
                  event.preventDefault()
                  if (!findMatchCount) return
                  const step = event.shiftKey ? -1 : 1
                  setFindCursor((current) => (current + step + findMatchCount) % findMatchCount)
                }
              }}
              aria-label="Search term"
            />
            <span className="find-count" aria-live="polite">
              {findQuery.trim()
                ? findMatchCount
                  ? `${findCursor + 1} / ${findMatchCount}`
                  : '0 / 0'
                : ''}
            </span>
            <button
              type="button"
              className="ghost-button reader-toolbar-button"
              onClick={() => {
                if (!findMatchCount) return
                setFindCursor((current) => (current - 1 + findMatchCount) % findMatchCount)
              }}
              disabled={!findMatchCount}
              aria-label="Previous match"
              title="Previous match (Shift+Enter)"
            >
              ↑
            </button>
            <button
              type="button"
              className="ghost-button reader-toolbar-button"
              onClick={() => {
                if (!findMatchCount) return
                setFindCursor((current) => (current + 1) % findMatchCount)
              }}
              disabled={!findMatchCount}
              aria-label="Next match"
              title="Next match (Enter)"
            >
              ↓
            </button>
            <button
              type="button"
              className="ghost-button reader-toolbar-button"
              onClick={() => setFindOpen(false)}
              aria-label="Close find bar"
              title="Close (Esc)"
            >
              ✕
            </button>
          </div>
        ) : null}

        <section className="markdown-card" ref={readerScrollRef}>
          {readerMode === 'source' ? (
            <div className="reader-source-wrap">
              <CodeMirror
                className="reader-source-editor"
                value={sourceDraft}
                height="100%"
                basicSetup={{
                  lineNumbers: false,
                  foldGutter: false,
                  highlightActiveLine: false,
                  highlightActiveLineGutter: false,
                }}
                extensions={sourceEditorExtensions}
                onCreateEditor={(view) => {
                  sourceEditorViewRef.current = view
                }}
                onChange={(value) => setSourceDraft(value)}
                onUpdate={(update) => {
                  if (!update.docChanged && !update.focusChanged) {
                    return
                  }
                  sourceEditorViewRef.current = update.view
                }}
                theme={theme === 'dark' ? 'dark' : 'light'}
                placeholder="Edit canonical article markdown here."
              />
            </div>
          ) : (
            <ReaderContent
              loading={loadingDetail}
              markdown={renderedMarkdown}
              highlights={highlights}
              articlePath={detail?.summary.articlePath}
              sourceSite={detail?.summary.sourceSite}
              theme={theme}
              fontSize={readerFontSize}
              scrollHeadingRequest={scrollHeadingRequest}
              hideNoise={hideNoise}
              referencesAreNoise={referencesAreNoise}
              onCopy={copyTextToClipboard}
              onHighlightClick={(id) => setFocusedHighlight({ id, ts: Date.now() })}
              onImageOpen={(src, alt, imageIndex) => setLightbox({ src, alt, imageIndex })}
              onDeleteNoise={(id) => void deleteHighlight(id)}
              onToggleElementNoise={(type, index) => void toggleElementNoise(type, index)}
            />
          )}
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
        </header>

        <div className="notes-scroll" ref={notesScrollRef}>
          <div className="notes-top-scroll">
          <section className="info-card">
            <div className="notes-mode-row">
              <div className="notes-mode-toggle">
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button${findOpen && findTarget === 'notes' ? ' is-active' : ''}`}
                  onClick={() => {
                    setFindTarget('notes')
                    setFindOpen((current) => !current || findTarget !== 'notes')
                  }}
                  title="Find in working notes (Ctrl+F while notes are focused)"
                  aria-label="Find in working notes"
                  aria-pressed={findOpen && findTarget === 'notes'}
                >
                  Find
                </button>
                <button
                  className={`ghost-button reader-toolbar-button ${notesMode === 'preview' ? 'is-active' : ''}`}
                  onClick={() => {
                    if (notesMode === 'preview') {
                      setNotesMode('markdown')
                      return
                    }
                    if (notesPreviewReady) {
                      setNotesMode('preview')
                    }
                  }}
                  disabled={notesMode === 'markdown' && !notesPreviewReady}
                  aria-pressed={notesMode === 'preview'}
                >
                  {notesMode === 'preview' ? 'Markdown' : 'Preview'}
                </button>
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button${generatingNotes || notesPending ? ' is-busy' : ''}${notesPending ? ' is-active' : ''}`}
                  onClick={() => {
                    if (notesPending) {
                      void stopNotesGeneration()
                      return
                    }
                    void generateNotes()
                  }}
                  disabled={!detail || generatingNotes || notesGenerationState === 'cancelling'}
                  aria-pressed={notesPending}
                  title={
                    generatingNotes
                      ? 'Starting notes generation…'
                      : notesGenerationState === 'cancelling'
                      ? 'Stopping notes generation…'
                      : notesPending
                      ? 'Stop notes generation'
                      : 'Generate notes'
                  }
                >
                  {generatingNotes
                    ? 'Starting…'
                    : notesGenerationState === 'cancelling'
                    ? 'Stopping…'
                    : notesPending
                    ? 'Generating'
                    : 'Generate'}
                </button>
                <button
                  type="button"
                  className={`ghost-button reader-toolbar-button reader-icon-button${notesDirty ? ' is-dirty' : ''}`}
                  onClick={() => void saveNotes()}
                  disabled={!detail || savingNotes}
                  title={savingNotes ? 'Saving notes…' : 'Save notes'}
                  aria-label={savingNotes ? 'Saving notes' : 'Save notes'}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
                    <path d="M2.5 1h8.8l2.7 2.7V14a1 1 0 0 1-1 1h-10a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1Zm1 1.5V6h8V3.8L10.7 2.5H9.5V5h-4V2.5h-2Zm1 8.5v2.5h7V11h-7Z" fill="currentColor" />
                  </svg>
                </button>
              </div>
              <span className="search-hint">
                {notesPending
                  ? 'Markdown streams live while the LLM works. Preview unlocks when generation finishes.'
                  : 'Markdown is canonical. Preview is read-only.'}
              </span>
            </div>

            {findOpen && detail && findTarget === 'notes' ? (
              <div className="find-bar" role="search" aria-label="Find in working notes">
                <input
                  ref={findInputRef}
                  type="text"
                  className="find-input"
                  placeholder={notesMode === 'markdown' ? 'Find in notes markdown…' : 'Find in notes preview…'}
                  value={findQuery}
                  onChange={(event) => setFindQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      event.preventDefault()
                      setFindOpen(false)
                      return
                    }
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      if (!findMatchCount) return
                      const step = event.shiftKey ? -1 : 1
                      setFindCursor((current) => (current + step + findMatchCount) % findMatchCount)
                    }
                  }}
                  aria-label="Search notes"
                />
                <span className="find-count" aria-live="polite">
                  {findQuery.trim()
                    ? findMatchCount
                      ? `${findCursor + 1} / ${findMatchCount}`
                      : '0 / 0'
                    : ''}
                </span>
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => {
                    if (!findMatchCount) return
                    setFindCursor((current) => (current - 1 + findMatchCount) % findMatchCount)
                  }}
                  disabled={!findMatchCount}
                  aria-label="Previous match"
                  title="Previous match (Shift+Enter)"
                >
                  ↑
                </button>
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => {
                    if (!findMatchCount) return
                    setFindCursor((current) => (current + 1) % findMatchCount)
                  }}
                  disabled={!findMatchCount}
                  aria-label="Next match"
                  title="Next match (Enter)"
                >
                  ↓
                </button>
                <button
                  type="button"
                  className="ghost-button reader-toolbar-button"
                  onClick={() => setFindOpen(false)}
                  aria-label="Close find bar"
                  title="Close (Esc)"
                >
                  ✕
                </button>
              </div>
            ) : null}

            {notesMode === 'markdown' ? (
              <div className="notes-editor-wrap">
                <CodeMirror
                  className="notes-code-editor"
                  value={notesDraft}
                  height="100%"
                  basicSetup={{
                    lineNumbers: false,
                    foldGutter: false,
                    highlightActiveLine: false,
                    highlightActiveLineGutter: false,
                  }}
                  extensions={notesEditorExtensions}
                  onCreateEditor={(view) => {
                    notesEditorViewRef.current = view
                    setNotesEditorMountTick((current) => current + 1)
                  }}
                  onChange={(value) => setNotesDraft(value)}
                  onBlur={() => {
                    if (notesEditorViewRef.current && notesEditorViewRef.current.hasFocus) {
                      notesEditorViewRef.current.contentDOM.blur()
                    }
                  }}
                  onUpdate={(update) => {
                    if (!update.docChanged && !update.focusChanged) {
                      return
                    }
                    notesEditorViewRef.current = update.view
                  }}
                  theme={theme === 'dark' ? 'dark' : 'light'}
                  placeholder="Write notes here. Markdown shortcuts: Ctrl+1..6 headings, Ctrl+B bold, Ctrl+I italic, Ctrl+K link, Ctrl+Shift+K code block, Ctrl+Shift+Q quote, Ctrl+Shift+C inline code."
                />
              </div>
            ) : (
              <div className="notes-preview markdown-card">
                {notesDraft.trim() ? (
                  <div className="markdown-body notes-preview-body" ref={notesPreviewRef}>
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm, remarkMath]}
                      rehypePlugins={[[rehypeKatex, { output: 'htmlAndMathml', throwOnError: false, strict: 'ignore' }]]}
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
          </div>

          <div
            className="splitter splitter-horizontal"
            role="separator"
            aria-orientation="horizontal"
            aria-label="Resize notes section"
            onPointerDown={(event) => onSplitterPointerDown('notes', event)}
            onPointerMove={onSplitterPointerMove}
            onPointerUp={onSplitterPointerUp}
          />

          <div className="notes-bottom-scroll">
          <section className="info-card">
            <div className="highlights-header">
              <h3>Highlights</h3>
              {visibleHighlights.length ? (
                <div className="notes-mode-toggle">
                  <button
                    className={`ghost-button ${highlightsView === 'list' ? 'is-active' : ''}`}
                    onClick={() => setHighlightsView('list')}
                    title="Show all highlights as a list"
                  >
                    List
                  </button>
                  <button
                    className={`ghost-button ${highlightsView === 'single' ? 'is-active' : ''}`}
                    onClick={() => setHighlightsView('single')}
                    title="Show one highlight at a time"
                  >
                    Single
                  </button>
                </div>
              ) : null}
            </div>
            {visibleHighlights.length ? (
              highlightsView === 'single' ? (
                (() => {
                  const total = visibleHighlights.length
                  const safeIndex = Math.min(Math.max(0, highlightCursor), total - 1)
                  const highlight = visibleHighlights[safeIndex]
                  return (
                    <div className="highlight-single">
                      <div className="highlight-pager">
                        <button
                          type="button"
                          className="ghost-button highlight-button"
                          onClick={() => goToHighlight(safeIndex - 1)}
                          disabled={total <= 1}
                          aria-label="Previous highlight"
                          title="Previous highlight"
                        >
                          ‹ Prev
                        </button>
                        <span className="highlight-pager-count">
                          {safeIndex + 1} / {total}
                        </span>
                        <button
                          type="button"
                          className="ghost-button highlight-button"
                          onClick={() => goToHighlight(safeIndex + 1)}
                          disabled={total <= 1}
                          aria-label="Next highlight"
                          title="Next highlight"
                        >
                          Next ›
                        </button>
                      </div>
                      {renderHighlightCard(highlight)}
                    </div>
                  )
                })()
              ) : (
                <div className="highlight-list">
                  {visibleHighlights.map((highlight) => renderHighlightCard(highlight))}
                </div>
              )
            ) : (
              <p className="search-hint">Select text in the article, then add it as a highlight.</p>
            )}
          </section>

          {detail ? (
            <section className="info-card">
              <div className="highlights-header">
                <h3>Related</h3>
                <div className="notes-mode-toggle">
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => void loadRelatedSuggestions()}
                    disabled={loadingSuggestions}
                    title="Find documents in the library that this one references"
                  >
                    {loadingSuggestions ? 'Suggesting…' : 'Suggest'}
                  </button>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => {
                      setRelatedPickerOpen((value) => !value)
                      setRelatedPickerQuery('')
                    }}
                    disabled={relatedSaving}
                  >
                    {relatedPickerOpen ? 'Cancel' : '+ Link'}
                  </button>
                </div>
              </div>
              {relatedSuggestions.length ? (
                <ul className="related-list">
                  {relatedSuggestions
                    .filter((suggestion) => !relatedLinks.some((link) => link.targetPath === suggestion.articlePath))
                    .map((suggestion) => (
                      <li key={`suggest-${suggestion.id}`} className="related-item related-suggestion">
                        <div className="related-row">
                          <button
                            type="button"
                            className="related-title"
                            onClick={() => void loadDocumentByPath(suggestion.articlePath, suggestion.title)}
                            title={suggestion.articlePath}
                          >
                            {suggestion.title}
                          </button>
                          <button
                            type="button"
                            className="ghost-button highlight-button"
                            onClick={() => void addRelatedLink(suggestion)}
                            disabled={relatedSaving}
                            title="Save as a related link"
                          >
                            + Link
                          </button>
                        </div>
                        {suggestion.reasons.length ? (
                          <p className="muted related-reason">{suggestion.reasons.join(' · ')}</p>
                        ) : null}
                      </li>
                    ))}
                </ul>
              ) : null}
              {relatedPickerOpen ? (
                <div className="related-picker">
                  <input
                    type="text"
                    className="text-input"
                    placeholder="Filter library…"
                    value={relatedPickerQuery}
                    onChange={(event) => setRelatedPickerQuery(event.target.value)}
                    autoFocus
                  />
                  <input
                    type="text"
                    className="text-input"
                    placeholder="Optional note"
                    value={relatedDraftNote}
                    onChange={(event) => setRelatedDraftNote(event.target.value)}
                  />
                  <ul className="related-picker-list">
                    {library.documents
                      .filter((doc) => {
                        if (doc.articlePath === detail.summary.articlePath) return false
                        if (relatedLinks.some((link) => link.targetPath === doc.articlePath)) return false
                        const needle = relatedPickerQuery.trim().toLowerCase()
                        if (!needle) return true
                        return (
                          (doc.title || '').toLowerCase().includes(needle) ||
                          (doc.label || '').toLowerCase().includes(needle)
                        )
                      })
                      .slice(0, 20)
                      .map((doc) => (
                        <li key={doc.id}>
                          <button
                            type="button"
                            className="related-picker-item"
                            onClick={() => void addRelatedLink(doc)}
                            disabled={relatedSaving}
                          >
                            <strong>{doc.title}</strong>
                            <span className="muted">{doc.label ?? 'unlabeled'}</span>
                          </button>
                        </li>
                      ))}
                  </ul>
                </div>
              ) : null}
              {relatedLinks.length ? (
                <ul className="related-list">
                  {relatedLinks.map((link) => (
                    <li key={link.targetPath} className="related-item">
                      <div className="related-row">
                        <button
                          type="button"
                          className="related-title"
                          onClick={() => void loadDocumentByPath(link.targetPath, link.targetTitle)}
                          title={link.targetPath}
                        >
                          {link.targetTitle || link.targetPath}
                        </button>
                        <button
                          type="button"
                          className="ghost-button highlight-button"
                          onClick={() => void removeRelatedLink(link.targetPath)}
                          disabled={relatedSaving}
                          title="Remove link"
                        >
                          ×
                        </button>
                      </div>
                      <input
                        type="text"
                        className="text-input related-note-input"
                        value={link.note}
                        placeholder="Note"
                        onChange={(event) => void updateRelatedNote(link.targetPath, event.target.value)}
                        onBlur={() => void commitRelatedNote(link.targetPath)}
                      />
                    </li>
                  ))}
                </ul>
              ) : !relatedPickerOpen ? (
                <p className="search-hint">Link documents that cite or build on this one.</p>
              ) : null}
            </section>
          ) : null}

          </div>
        </div>

        <footer className="status-bar">{status}</footer>
      </aside>
    </div>
  )
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

const FRONTMATTER_KEY_ORDER = [
  'title',
  'authors',
  'author',
  'label',
  'doc_id',
  'doi',
  'arxiv_id',
  'pmid',
  'pmcid',
  'url',
  'canonical_url',
  'source_site',
  'publisher',
  'journal',
  'year',
  'published',
  'date',
  'ingested_at',
  'rating',
  'type',
  'doc_type',
]

function formatFrontmatterValue(value: FrontmatterValue): string {
  if (value == null) return ''
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return String(value)
  if (Array.isArray(value)) {
    return value.map((entry) => formatFrontmatterValue(entry as FrontmatterValue)).join(', ')
  }
  return String(value)
}

function renderFrontmatterEntries(
  frontmatter: Record<string, FrontmatterValue> | undefined,
): Array<[string, string, FrontmatterValue]> {
  if (!frontmatter) return []
  const seen = new Set<string>()
  const ordered: Array<[string, string, FrontmatterValue]> = []
  const push = (key: string) => {
    if (seen.has(key)) return
    if (!(key in frontmatter)) return
    const raw = frontmatter[key]
    const formatted = formatFrontmatterValue(raw)
    if (!formatted) return
    seen.add(key)
    ordered.push([key, formatted, raw])
  }
  for (const key of FRONTMATTER_KEY_ORDER) push(key)
  for (const key of Object.keys(frontmatter)) push(key)
  return ordered
}

function parseBibTeX(bib: string): { entryType: string; fields: Record<string, string> } | null {
  if (!bib || !bib.trim()) return null
  const match = bib.match(/@(\w+)\s*\{\s*[^,]*,([\s\S]*)\}\s*$/m)
  if (!match) return null
  const entryType = match[1].toLowerCase()
  const body = match[2]
  const fields: Record<string, string> = {}
  const fieldRegex = /(\w+)\s*=\s*(\{((?:[^{}]|\{[^{}]*\})*)\}|"([^"]*)"|(\d+))/g
  let fm: RegExpExecArray | null
  while ((fm = fieldRegex.exec(body)) !== null) {
    const key = fm[1].toLowerCase()
    const braced = fm[3]
    const quoted = fm[4]
    const bareNum = fm[5]
    const raw = braced ?? quoted ?? bareNum ?? ''
    fields[key] = raw.replace(/\s+/g, ' ').replace(/[{}]/g, '').trim()
  }
  return { entryType, fields }
}

function splitBibAuthors(raw: string): string[] {
  if (!raw) return []
  return raw
    .split(/\s+and\s+/i)
    .map((name) => {
      const clean = name.trim()
      if (!clean) return ''
      if (clean.includes(',')) {
        const [last, first] = clean.split(',', 2)
        return `${first.trim()} ${last.trim()}`.trim()
      }
      return clean
    })
    .filter(Boolean)
}

function buildCitationEntries(
  bib: string,
  frontmatter: Record<string, FrontmatterValue> | undefined,
): Array<[string, string]> {
  const parsed = parseBibTeX(bib)
  const entries: Array<[string, string]> = []
  const pushed = new Set<string>()
  const push = (key: string, value: string) => {
    const clean = (value ?? '').trim()
    if (!clean) return
    if (pushed.has(key)) return
    pushed.add(key)
    entries.push([key, clean])
  }
  if (parsed) {
    const f = parsed.fields
    const authorList = splitBibAuthors(f.author ?? '')
    if (authorList.length) push('authors', authorList.join(', '))
    if (f.title) push('title', f.title)
    if (f.journal) push('journal', f.journal)
    if (f.booktitle) push('booktitle', f.booktitle)
    if (f.publisher) push('publisher', f.publisher)
    if (f.year) push('year', f.year)
    if (f.volume) push('volume', f.volume)
    if (f.number) push('number', f.number)
    if (f.pages) push('pages', f.pages)
    if (f.doi) push('doi', f.doi)
    if (f.url) push('url', f.url)
  }
  if (frontmatter) {
    const authors = frontmatter.authors ?? frontmatter.author
    if (authors) push('authors', formatFrontmatterValue(authors))
    if (frontmatter.journal) push('journal', formatFrontmatterValue(frontmatter.journal))
    if (frontmatter.publisher) push('publisher', formatFrontmatterValue(frontmatter.publisher))
    if (frontmatter.year) push('year', formatFrontmatterValue(frontmatter.year))
    if (frontmatter.published) push('published', formatFrontmatterValue(frontmatter.published))
  }
  return entries
}

const FRONTMATTER_LINK_KEYS: Record<string, (value: string) => string | null> = {
  doi: (value) => {
    const clean = value.replace(/^https?:\/\/(dx\.)?doi\.org\//i, '').trim()
    return clean ? `https://doi.org/${clean}` : null
  },
  arxiv_id: (value) => {
    const clean = value.replace(/^arxiv:/i, '').trim()
    return clean ? `https://arxiv.org/abs/${clean}` : null
  },
  pmid: (value) => {
    const clean = value.trim()
    return clean ? `https://pubmed.ncbi.nlm.nih.gov/${clean}/` : null
  },
  pmcid: (value) => {
    const clean = value.replace(/^pmc/i, '').trim()
    return clean ? `https://www.ncbi.nlm.nih.gov/pmc/articles/PMC${clean}/` : null
  },
  url: (value) => (/^https?:\/\//i.test(value) ? value : null),
  canonical_url: (value) => (/^https?:\/\//i.test(value) ? value : null),
  source_url: (value) => (/^https?:\/\//i.test(value) ? value : null),
}

const FRONTMATTER_FILE_KEYS = new Set([
  'article_path',
  'reading_pdf_path',
  'source_pdf_path',
  'notes_path',
  'path',
  'md_path',
])

function renderInfoValue(
  key: string,
  value: string,
  handlers: {
    onOpenExternalFile: (path: string, label: string) => void
    onCopy: (text: string, label: string) => void
  },
): ReactNode {
  if (!value) return value
  const linker = FRONTMATTER_LINK_KEYS[key]
  if (linker) {
    const href = linker(value)
    if (href) {
      return (
        <a
          className="info-link"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(event) => {
            event.preventDefault()
            window.open(href, '_blank', 'noopener,noreferrer')
          }}
        >
          {value}
        </a>
      )
    }
  }
  if (FRONTMATTER_FILE_KEYS.has(key) || /\.(md|pdf|json|txt|bib|html?)$/i.test(value)) {
    const looksLikePath = value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value)
    if (looksLikePath) {
      return (
        <button
          type="button"
          className="info-link info-link-button"
          onClick={() => handlers.onOpenExternalFile(value, key)}
          title="Open with system default"
        >
          {value}
        </button>
      )
    }
  }
  if (/^https?:\/\//i.test(value)) {
    return (
      <a
        className="info-link"
        href={value}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(event) => {
          event.preventDefault()
          window.open(value, '_blank', 'noopener,noreferrer')
        }}
      >
        {value}
      </a>
    )
  }
  return (
    <button
      type="button"
      className="info-link info-link-button info-link-subtle"
      onClick={() => handlers.onCopy(value, key)}
      title="Copy to clipboard"
    >
      {value}
    </button>
  )
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
