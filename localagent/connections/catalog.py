"""What can be connected, grouped the way people think about it.

how:
  email     — address + app password (IMAP)       → built in
  calendar  — the calendar's private iCal link     → built in
  notes     — a notes folder / app on this PC      → built in
  signin    — hosted MCP connector, browser sign-in (OAuth, checked to allow
              new apps to register themselves)
  via       — not directly; reachable through another entry (e.g. Zapier)
  storage   — cloud storage through rclone (browser sign-in or password) or a drive/sync folder on this PC
  google    — the full Google account through the Google Workspace connector (one-time Google Cloud setup)
  token     — hosted MCP connector that takes a personal access token
  feeds / weather — built in, no account
  open      — hosted MCP connector that needs no sign-in at all
  web       — no usable API: sign in once in LocalAgent's browser and the agent uses the website like you would
"""
from __future__ import annotations

CATALOG: list[dict] = [
    # ---------------------------------------------------------------- google
    {"id": "google", "group": "Google", "alt": True, "name": "Your own Google Cloud app (advanced)", "how": "google",
     "blurb": "Gmail, Drive, Docs, Sheets, Slides, Calendar, Tasks, Contacts and Forms — read and write. "
              "Needs a one-time 10-minute setup in Google Cloud (free); after that it's one sign-in."},
    {"id": "gmail", "group": "Google", "alt": True, "name": "Gmail with an app password", "how": "email", "preset": "gmail",
     "blurb": "Read, search and draft replies with an app password — no Google Cloud setup. Sending always asks you first."},
    {"id": "gcal", "group": "Google", "alt": True, "name": "Google Calendar with its private link", "how": "calendar", "provider": "google",
     "blurb": "See your schedule with the calendar's private link. Read-only; new events are prepared for you to save."},
    {"id": "gdrive", "group": "Google", "alt": True, "name": "Google Drive through rclone", "how": "storage", "provider": "drive",
     "blurb": "Browse, search, find big files and move things in your Drive with one sign-in. Google Docs download as Office files."},
    {"id": "gmail_web", "group": "Google", "alt": True, "name": "Gmail in LocalAgent's browser", "how": "web",
     "url": "https://mail.google.com/mail/u/0/#inbox", "blurb": "No setup: sign in to Google once in LocalAgent's browser and it uses Gmail like you would.",
     "tips": "Inbox list in the middle; search bar at the top understands Gmail search (from:, is:unread). Compose button top left."},
    {"id": "gcal_web", "group": "Google", "alt": True, "name": "Google Calendar in LocalAgent's browser", "how": "web",
     "url": "https://calendar.google.com/calendar/u/0/r", "tips": "Day/Week/Month switch at the top right. Create button top left."},
    {"id": "gdrive_web", "group": "Google", "alt": True, "name": "Google Drive in LocalAgent's browser", "how": "web",
     "url": "https://drive.google.com/drive/my-drive", "tips": "Storage use: drive.google.com/drive/quota (biggest files first)."},
    {"id": "gkeep", "group": "Google", "name": "Google Keep", "how": "web", "url": "https://keep.google.com/",
     "blurb": "No API for personal accounts, so LocalAgent uses the Keep website with your sign-in.",
     "tips": "Notes are cards on the main page; use read to get their text. Search box at the top. Labels on the left."},
    {"id": "gphotos", "group": "Google", "name": "Google Photos", "how": "web", "url": "https://photos.google.com/",
     "blurb": "Google no longer lets apps browse your library, so LocalAgent uses the Photos website with your sign-in.",
     "tips": "Search box at the top understands things like 'receipts', 'beach 2024', a person's name. Storage use is at photos.google.com/storage."},
    {"id": "youtube", "group": "Google", "name": "YouTube", "how": "web", "url": "https://www.youtube.com/",
     "blurb": "Your subscriptions, Watch Later and history, through the YouTube website.",
     "tips": "Subscriptions: /feed/subscriptions. Watch Later: /playlist?list=WL. History: /feed/history."},

    # ---------------------------------------------------------------- email
    {"id": "yahoo", "group": "Email", "name": "Yahoo Mail", "how": "email", "preset": "yahoo"},
    {"id": "icloudmail", "group": "Email", "name": "iCloud Mail", "how": "email", "preset": "icloud"},
    {"id": "fastmail", "group": "Email", "name": "Fastmail", "how": "email", "preset": "fastmail"},
    {"id": "aolmail", "group": "Email", "name": "AOL Mail", "how": "email", "preset": "aol"},
    {"id": "othermail", "group": "Email", "name": "Other email (IMAP)", "how": "email", "preset": "other",
     "blurb": "Work, school or any provider with IMAP."},
    {"id": "outlookmail", "group": "Email", "name": "Outlook.com / Hotmail", "how": "web", "url": "https://outlook.live.com/mail/0/",
     "blurb": "Microsoft blocks password logins for outside apps, so LocalAgent uses Outlook on the web with your sign-in. Sending still asks you first.",
     "tips": "Inbox list on the left/middle; click a message to open it, then read. New mail button top left. Search box at the top."},

    # ------------------------------------------------------------- calendar
    {"id": "outlookcal", "group": "Calendar & scheduling", "name": "Outlook calendar", "how": "calendar", "provider": "outlook"},
    {"id": "icloudcal", "group": "Calendar & scheduling", "name": "iCloud calendar", "how": "calendar", "provider": "icloud"},
    {"id": "calendly", "group": "Calendar & scheduling", "name": "Calendly", "how": "signin", "url": "https://mcp.calendly.com/mcp",
     "blurb": "See who booked time with you and share scheduling links."},

    # -------------------------------------------------------------- storage
    {"id": "onedrive", "group": "Online storage", "name": "OneDrive", "how": "storage", "provider": "onedrive"},
    {"id": "dropbox", "group": "Online storage", "name": "Dropbox", "how": "storage", "provider": "dropbox",
     "blurb": "Browse, search, find big files, move things and free up space. Nothing is ever deleted."},
    {"id": "box", "group": "Online storage", "name": "Box", "how": "storage", "provider": "box"},
    {"id": "pcloud", "group": "Online storage", "name": "pCloud", "how": "storage", "provider": "pcloud"},
    {"id": "mega", "group": "Online storage", "name": "MEGA", "how": "storage", "provider": "mega"},
    {"id": "protondrive", "group": "Online storage", "name": "Proton Drive", "how": "storage", "provider": "protondrive"},
    {"id": "icloud", "group": "Online storage", "name": "iCloud Drive", "how": "storage", "provider": "folder",
     "blurb": "Install iCloud for Windows; its iCloud Drive folder then shows up under “Found on this computer”."},
    {"id": "otherdrive", "group": "Online storage", "name": "Any cloud drive or sync folder", "how": "storage", "provider": "folder",
     "blurb": "Icedrive, Sync.com, Tresorit, a NAS or any drive letter — if it shows up in File Explorer, LocalAgent can manage it."},

    # ---------------------------------------------------------------- notes
    {"id": "obsidian", "group": "Notes & reading", "name": "Obsidian", "how": "notes", "app": "obsidian",
     "blurb": "Search, read and add to your vault. Never overwrites a note."},
    {"id": "logseq", "group": "Notes & reading", "name": "Logseq", "how": "notes", "app": "logseq"},
    {"id": "joplin", "group": "Notes & reading", "name": "Joplin", "how": "notes", "app": "joplin"},
    {"id": "mdfolder", "group": "Notes & reading", "name": "Any folder of notes", "how": "notes", "app": "folder",
     "blurb": "Markdown or text files anywhere on this computer."},
    {"id": "notion", "group": "Notes & reading", "name": "Notion", "how": "signin", "url": "https://mcp.notion.com/mcp",
     "blurb": "Search and update your Notion pages and databases."},
    {"id": "evernote", "group": "Notes & reading", "name": "Evernote", "how": "signin", "url": "https://mcp.evernote.com/mcp"},
    {"id": "readwise", "group": "Notes & reading", "name": "Readwise", "how": "signin", "url": "https://mcp2.readwise.io/mcp",
     "blurb": "Your highlights from books, articles and Kindle."},
    {"id": "craft", "group": "Notes & reading", "name": "Craft", "how": "signin", "url": "https://mcp.craft.do/mcp"},
    {"id": "mem", "group": "Notes & reading", "name": "Mem", "how": "signin", "url": "https://mcp.mem.ai/mcp"},
    {"id": "coda", "group": "Notes & reading", "name": "Coda", "how": "signin", "url": "https://coda.io/apis/mcp"},
    {"id": "onenote", "group": "Notes & reading", "name": "OneNote", "how": "web", "url": "https://www.onenote.com/notebooks",
     "tips": "Notebooks list → sections → pages. Open a page then read."},
    {"id": "news", "group": "Notes & reading", "name": "News & blogs (RSS)", "how": "feeds",
     "blurb": "Headlines from sites you pick — news, blogs, YouTube channels, podcasts, Substack, Reddit. No account."},

    # ---------------------------------------------------------------- tasks
    {"id": "todoist", "group": "To-dos & projects", "name": "Todoist", "how": "signin", "url": "https://ai.todoist.net/mcp",
     "blurb": "Add, find and finish to-dos."},
    {"id": "mstodo", "group": "To-dos & projects", "name": "Microsoft To Do", "how": "web", "url": "https://to-do.live.com/tasks/",
     "tips": "Lists on the left (My Day, Important, Planned, Tasks). 'Add a task' box at the top of each list."},
    {"id": "asana", "group": "To-dos & projects", "name": "Asana", "how": "signin", "url": "https://mcp.asana.com/sse"},
    {"id": "clickup", "group": "To-dos & projects", "name": "ClickUp", "how": "signin", "url": "https://mcp.clickup.com/mcp"},
    {"id": "linear", "group": "To-dos & projects", "name": "Linear", "how": "signin", "url": "https://mcp.linear.app/mcp"},
    {"id": "monday", "group": "To-dos & projects", "name": "monday.com", "how": "signin", "url": "https://mcp.monday.com/mcp"},
    {"id": "atlassian", "group": "To-dos & projects", "name": "Jira & Confluence", "how": "signin",
     "url": "https://mcp.atlassian.com/v1/mcp"},
    {"id": "airtable", "group": "To-dos & projects", "name": "Airtable", "how": "signin", "url": "https://mcp.airtable.com/mcp"},
    {"id": "miro", "group": "To-dos & projects", "name": "Miro", "how": "signin", "url": "https://mcp.miro.com/",
     "blurb": "Read and add to your Miro boards."},

    # -------------------------------------------------------- create & meet
    {"id": "canva", "group": "Create & meet", "name": "Canva", "how": "signin", "url": "https://mcp.canva.com/mcp"},
    {"id": "gamma", "group": "Create & meet", "name": "Gamma", "how": "signin", "url": "https://mcp.gamma.app/mcp",
     "blurb": "Make presentations, documents and web pages."},
    {"id": "granola", "group": "Create & meet", "name": "Granola", "how": "signin", "url": "https://mcp.granola.ai/mcp",
     "blurb": "Your meeting notes and transcripts."},
    {"id": "fireflies", "group": "Create & meet", "name": "Fireflies", "how": "signin", "url": "https://api.fireflies.ai/mcp",
     "blurb": "Meeting transcripts and summaries."},
    {"id": "jotform", "group": "Create & meet", "name": "Jotform", "how": "signin", "url": "https://mcp.jotform.com/",
     "blurb": "Your forms and their responses."},
    {"id": "wix", "group": "Create & meet", "name": "Wix", "how": "signin", "url": "https://mcp.wix.com/mcp"},
    {"id": "webflow", "group": "Create & meet", "name": "Webflow", "how": "signin", "url": "https://mcp.webflow.com/mcp"},

    # ----------------------------------------------------------- work & life
    {"id": "indeed", "group": "Jobs, home & money", "name": "Indeed", "how": "signin", "url": "https://mcp.indeed.com/claude/mcp",
     "blurb": "Search jobs and keep track of ones you like."},
    {"id": "homeassistant", "group": "Jobs, home & money", "name": "Home Assistant", "how": "token",
     "url": "{base}/api/mcp", "fields": [{"key": "base", "label": "Home Assistant address", "placeholder": "http://homeassistant.local:8123"}],
     "token_help": "In Home Assistant: Settings → Devices & services → add “Model Context Protocol Server”. Then your profile → Security → "
                   "Long-lived access tokens → Create token.",
     "blurb": "Lights, thermostat, sensors and anything else in your smart home."},
    {"id": "weather", "group": "Jobs, home & money", "name": "Weather", "how": "weather",
     "blurb": "Forecasts for your city in the morning brief and whenever you ask. No account."},
    {"id": "stripe", "group": "Jobs, home & money", "name": "Stripe", "how": "signin", "url": "https://mcp.stripe.com",
     "blurb": "For people who sell things: payments, customers and invoices. Anything involving money always asks you first."},

    # ---------------------------------------------------- password managers
    {"id": "1password", "group": "Password managers", "name": "1Password", "how": "passwords", "provider": "1password",
     "blurb": "LocalAgent can sign in to sites for you with your saved logins and one-time codes. You approve each sign-in, "
              "1Password asks for Windows Hello, and the AI never sees a password."},
    {"id": "bitwarden", "group": "Password managers", "name": "Bitwarden", "how": "passwords", "provider": "bitwarden",
     "blurb": "Sign in to sites with your saved Bitwarden logins. You approve each one and the AI never sees a password. "
              "Works with Vaultwarden too."},
    {"id": "pm_extension", "group": "Password managers", "name": "Any password manager (extension)", "how": "extension",
     "blurb": "Dashlane, Proton Pass, Keeper, LastPass, NordPass, RoboForm, Enpass… Add its browser extension to LocalAgent's "
              "browser, sign in to it once, and it fills logins there the same way it does in your normal browser."},

    # ------------------------------------------------------------- websites
    {"id": "outlookcal_web", "group": "Websites (no API needed)", "name": "Outlook calendar (web)", "how": "web",
     "url": "https://outlook.live.com/calendar/0/", "tips": "Switch Day/Week/Month at the top. New event button top left."},
    {"id": "icloud_web", "group": "Websites (no API needed)", "name": "iCloud (Notes, Reminders, Photos)", "how": "web",
     "url": "https://www.icloud.com/", "tips": "Apps: /notes, /reminders, /photos, /iclouddrive. Apple asks for a code on your iPhone the first time."},
    {"id": "whatsapp", "group": "Websites (no API needed)", "name": "WhatsApp", "how": "web", "url": "https://web.whatsapp.com/",
     "blurb": "Read and draft messages. You link it once by scanning a QR code with your phone. Sending always asks you first.",
     "tips": "Chat list on the left with a search box; open a chat and read. Message box at the bottom; Enter sends (asks the user first)."},
    {"id": "messenger", "group": "Websites (no API needed)", "name": "Messenger", "how": "web", "url": "https://www.messenger.com/"},
    {"id": "instagram", "group": "Websites (no API needed)", "name": "Instagram", "how": "web", "url": "https://www.instagram.com/"},
    {"id": "facebook", "group": "Websites (no API needed)", "name": "Facebook", "how": "web", "url": "https://www.facebook.com/"},
    {"id": "linkedin", "group": "Websites (no API needed)", "name": "LinkedIn", "how": "web", "url": "https://www.linkedin.com/feed/",
     "tips": "Messages: /messaging. Jobs: /jobs. Notifications: /notifications. Posting always asks the user first."},
    {"id": "x", "group": "Websites (no API needed)", "name": "X (Twitter)", "how": "web", "url": "https://x.com/home"},
    {"id": "reddit", "group": "Websites (no API needed)", "name": "Reddit", "how": "web", "url": "https://www.reddit.com/"},
    {"id": "discord", "group": "Websites (no API needed)", "name": "Discord", "how": "web", "url": "https://discord.com/app"},
    {"id": "slack", "group": "Websites (no API needed)", "name": "Slack", "how": "web", "url": "https://app.slack.com/client",
     "tips": "Channels and DMs on the left. Activity tab for mentions. Search at the top."},
    {"id": "spotify", "group": "Websites (no API needed)", "name": "Spotify", "how": "web", "url": "https://open.spotify.com/",
     "tips": "Search at the top; your playlists in the left sidebar under Your Library."},
    {"id": "amazon", "group": "Websites (no API needed)", "name": "Amazon (orders & deliveries)", "how": "web",
     "url": "https://www.amazon.com/gp/css/order-history",
     "blurb": "Track orders, returns and deliveries. Buying anything always asks you first.",
     "tips": "Order history page lists orders with dates, totals and 'Track package'."},
    {"id": "website", "group": "Websites (no API needed)", "name": "Any other website", "how": "web", "url": "",
     "blurb": "Your school portal, bank, utility company, pharmacy, airline… If you can use it in a browser, LocalAgent can too. "
              "Anything that pays, sends or deletes asks you first."},

    # ------------------------------------------------------------ developer
    {"id": "github", "group": "Developer", "name": "GitHub", "how": "token", "url": "https://api.githubcopilot.com/mcp/",
     "token_help": "github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate. "
                   "Pick the repositories and permissions you want LocalAgent to have.",
     "blurb": "Repositories, issues and pull requests."},
    {"id": "huggingface", "group": "Developer", "name": "Hugging Face", "how": "open", "url": "https://huggingface.co/mcp",
     "blurb": "Search models, datasets, papers and Spaces. No sign-in needed."},
    {"id": "deepwiki", "group": "Developer", "name": "DeepWiki", "how": "open", "url": "https://mcp.deepwiki.com/mcp",
     "blurb": "Ask questions about any public GitHub repository."},
    {"id": "context7", "group": "Developer", "name": "Context7 docs", "how": "open", "url": "https://mcp.context7.com/mcp",
     "blurb": "Up-to-date documentation for programming libraries."},
    {"id": "vercel", "group": "Developer", "name": "Vercel", "how": "signin", "url": "https://mcp.vercel.com"},
    {"id": "netlify", "group": "Developer", "name": "Netlify", "how": "signin", "url": "https://netlify-mcp.netlify.app/mcp"},
    {"id": "supabase", "group": "Developer", "name": "Supabase", "how": "signin", "url": "https://mcp.supabase.com/mcp"},
    {"id": "sentry", "group": "Developer", "name": "Sentry", "how": "signin", "url": "https://mcp.sentry.dev/mcp"},
    {"id": "cloudflare", "group": "Developer", "name": "Cloudflare", "how": "signin", "url": "https://bindings.mcp.cloudflare.com/mcp"},
    {"id": "stackoverflow", "group": "Developer", "name": "Stack Overflow", "how": "signin", "url": "https://mcp.stackoverflow.com"},

    # ------------------------------------------------------------ everything
    {"id": "zapier", "group": "Everything else", "name": "Zapier (8,000+ apps)", "how": "signin",
     "url": "https://mcp.zapier.com/api/v1/connect",
     "blurb": "One sign-in that reaches Outlook, OneNote, Microsoft To Do, Slack, YouTube, Spotify, Google Sheets and thousands more. "
              "Pick which apps and actions to allow on zapier.com. Each action uses 2 tasks from your Zapier plan."},
    {"id": "make", "group": "Everything else", "name": "Make (3,000+ apps)", "how": "signin", "url": "https://www.make.com/mcp",
     "blurb": "Run your Make scenarios and reach the apps connected there."},
    {"id": "pipedream", "group": "Everything else", "name": "Pipedream (2,800+ apps)", "how": "signin", "url": "https://remote.mcp.pipedream.net",
     "blurb": "Another bridge to thousands of apps, with a generous free tier."},
]

GROUP_ORDER = ["Google", "Online storage", "Email", "Calendar & scheduling", "Notes & reading", "To-dos & projects",
               "Password managers", "Websites (no API needed)", "Create & meet", "Jobs, home & money", "Developer", "Everything else"]


def entry(eid: str) -> dict | None:
    return next((e for e in CATALOG if e["id"] == eid), None)
