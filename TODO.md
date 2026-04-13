# Updates do Corpus Scribe app

## Search

1. [x] Feature: Show the ranking stars
2. [x] Feature: Allow to remove articles from the library
3. [ ] Feature: Use file selector dialog to set the corpus root
4. [x] Bug: Do not load automatically the first document
5. [ ] Feature: Search related to the current document (based on references found in the current document, or semantic search)
6. [ ] Feature: Allow to link a related document to the current document - allow to add a note
7. [ ] Feature: Allow to display and image in full resolution
8. [~] Feature: search should prioritize highly ranked sources, affinity to the current document (rating priority done; affinity pending)
9. [ ] Feature: In search use author, latex article id, date, etc.
10. [ ] Feature: Add pubmed style search criteria (e.g. "((Tournier JD[Author]) AND (CSD[Title])) AND (GPU)")

## Reader

1. [x] Feature: Allow to mark the quality of the article. Add ranking as a star system (5 stars)
2. [x] Bug: Adding highlight reloads the article and scrolls to the top - disturbs the reading flow
3. [x] Feature: Add focus mode, where only the Reader is displayed
4. [x] Feature: Add dark mode
5. [x] Feature: If the original source contained a code block - allow to copy
6. [x] Feature: Allow to copy equations as latex
7. [x] Feature: Allow to download the linked PDF
8. [ ] Feature: Clicking on a highlight shows it in the Highlights panel
9. [ ] Feature: Allow to copy table in a CSV format
10. [x] Feature: Make external links clickable (in a new tab)
11. [x] Feature: Add link to the source webpage
12. [x] Feature: Allow to close the current document
13. [x] Feature: Persist the reading position
14. [ ] Feature: Allow to display images in full resolution
15. [ ] Feature: Allow to copy an image to a Clipboard
16. [ ] Feature: Add a list of opened documents and allow to easily switch between them (tabs?)
17. [x] Feature: Allow to change the font size

## Notes

1. [ ] Feature: add Typora key bindings for quicker markdown edits (e.g. ctrl + 1 - is h1, etc. )
2. [ ] Feature: Allow to resize the Notes panel
3. [ ] Idea (low priority): Add LLM chat panel. Drop current article, notes, highlights to the LLM context. LLM should always have the current version of the modified entities. The most recent context should be the first user message. Remove within article References to save tokens.

## Highlights

1. [ ] Feature: Display highlights in a single row with "next" / "previous" buttons
2. [x] Feature: Clicking on the highlight scrolls the Reader to show it in the middle of the reading area
3. [x] Feature: Allow to add a comment to the Highlight
4. [x] Feature: Allow to copy the highlight to the Clipboard

## Bibliography

1. [x] Feature: Allow to copy to the Clipboard

## Related

1. [ ] Feature: Show documents linked by the user

## Updates to Corpus Extractor

1. [ ] Bug: References are not correctly processed (e.g. "Validation of Fractional Anisotropy*") 
2. [ ] Feature: Save images in a displayed resolution (current) and full resolution (if available)
3. [x] Bug: When sanitizing equations add surround with new lines (if not already surrounded) single line equations

## MCP server - instead of a chat panel, allow to use any LLM app to interface with the corpus db via MCP

1. [ ] Feature: Tool - Search

2. [ ] Feature: Tool - Get Currently Opened Document current article

3. [ ] Feature: Tool - Get Current Context - returns notes, highlights, links, etc.

4. [ ] Feature: Tool - Update notes

5. [ ] Feature: CLI wrapper of the MCP server. Allows to do everything what MCP does but via CLI

## Chrome Extension

1. [ ] Feature: Remove send to scribe button
2. [ ] Feature: Add a button to open the document in Corpus Scribe
