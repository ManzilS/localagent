---
name: build-web-app
description: Build a small website, dashboard, game or tool the user can open in their browser
---
1. Make a folder in the workspace named after the app. Default to a single self-contained index.html (HTML + CSS + JS inline) unless the user needs a backend.
2. Write real, working code — no placeholders. Store data in localStorage for simple apps.
3. Make it look good on desktop and phone: readable font sizes, generous spacing, visible focus states.
4. Start it: start_process("python -m http.server 8000", cwd=<folder>). Then check it loads with web_fetch("http://localhost:8000") and fix any errors.
5. If the browser tool is available, take a screenshot and look_at it to check the layout.
6. Tell the user the folder path and http://localhost:8000. Mention stop_process when they're done.
