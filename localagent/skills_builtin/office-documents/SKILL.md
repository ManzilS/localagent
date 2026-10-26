---
name: office-documents
description: Create or edit Word, Excel, PowerPoint or PDF files
---
1. Use run_python. Libraries: python-docx (Word), openpyxl (Excel), python-pptx (PowerPoint), reportlab or the docx→PDF route for PDFs. If one is missing: run_shell("pip install <package>").
2. Editing an existing file: read it first (read_document), write the result to a NEW file next to it (name-edited.docx) unless the user said to change it in place. The original stays untouched.
3. Excel: write real formulas (=SUM(B2:B20)) rather than pasted numbers when values depend on each other; freeze the header row; set column widths.
4. Word: use real headings (Heading 1/2), not bold paragraphs, so the document has a navigable outline.
5. After saving, read_document the result to check it opened and looks right.
6. Tell the user the file path.
