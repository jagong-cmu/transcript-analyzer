"""Deterministic rendering of study notes: markdown, HTML, and PDF.

Kept out of `pipeline/` because none of it calls the API: given a
`StudyNotes`, the markdown and HTML are pure functions, and only `pdf` needs a
browser. That split is what makes "what do the notes say" testable apart from
"does this diagram draw".
"""
