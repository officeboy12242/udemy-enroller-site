"""Monochrome 24x24 SVG paths for Udemy's categories (inline, no icon font/CDN)."""

_CATEGORY_PATHS = {
    "Development": "M8.7 16.6 4.1 12l4.6-4.6L7.3 6l-6 6 6 6 1.4-1.4Zm6.6 0 4.6-4.6-4.6-4.6L16.7 6l6 6-6 6-1.4-1.4Z",
    "Business": "M10 4h4a2 2 0 0 1 2 2v1h4a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h4V6a2 2 0 0 1 2-2Zm0 3h4V6h-4v1Z",
    "Finance & Accounting": "M3 3h2v18H3V3Zm4 10h3v8H7v-8Zm5-6h3v14h-3V7Zm5 3h3v11h-3V10Z",
    "IT & Software": "M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-6v2h3v2H7v-2h3v-2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z",
    "Office Productivity": "M6 2h8l6 6v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm7 1.5V9h5.5L13 3.5Z",
    "Personal Development": "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 2c-4 0-8 2-8 5v1h16v-1c0-3-4-5-8-5Z",
    "Design": "M3 17.2V21h3.8l11-11-3.8-3.8-11 11Zm17.7-10.1a1 1 0 0 0 0-1.4l-2.4-2.4a1 1 0 0 0-1.4 0l-1.8 1.8 3.8 3.8 1.8-1.8Z",
    "Marketing": "M3 10v4a1 1 0 0 0 1 1h2l4 4V5L6 9H4a1 1 0 0 0-1 1Zm13.5 2A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4ZM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6Z",
    "Lifestyle": "M12 21s-7.5-4.6-9.5-9.1C1 8.6 3.2 5 6.6 5c2 0 3.4 1.1 4.4 2.5C12 6.1 13.4 5 15.4 5 18.8 5 21 8.6 19.5 11.9 17.5 16.4 12 21 12 21Z",
    "Photography & Video": "M9 4 7.2 6H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-3.2L15 4H9Zm3 4.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9Z",
    "Health & Fitness": "M6 7h2v10H6V7Zm10 0h2v10h-2V7ZM3 9h2v6H3V9Zm16 0h2v6h-2V9ZM8 11h8v2H8v-2Z",
    "Music": "M12 3v10.6A4 4 0 1 0 14 17V7h4V3h-6Z",
    "Teaching & Academics": "M12 3 1 9l11 6 9-4.9V17h2V9L12 3ZM5 13.2V17c0 1.7 3.1 3 7 3s7-1.3 7-3v-3.8l-7 3.8-7-3.8Z",
}
_FALLBACK = "M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z"


def cat_icon(name: str | None) -> str:
    """SVG path data for a category name (grid glyph for unknown ones)."""
    return _CATEGORY_PATHS.get(name or "", _FALLBACK)
