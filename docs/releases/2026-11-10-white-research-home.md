# White research home

The workspace now uses a white background and a consistent light appearance,
including when the operating system prefers dark mode. The geometric wallpaper
is removed. System forced colors and increased contrast remain supported.

The independent Home keeps the AutoScholar identity and existing routes, with
centered typography, restrained colored research icons and a searchable collection
of six introductions: research, literature review, technical comparison, research
proposal, project PDF analysis and the project evidence bank. A guide explains
each option, offers an example and links to the project collection. Opening a guide
or filtering introductions does not create a task or query a model. An unmatched
search has a clear empty state and can be reset.

The top glass Dock, three navigation choices, equal padding, 480ms page transitions,
reduced-motion support and static read indicators are retained. Project, task,
model, usage and evidence workflows continue through their existing pages.

The supplied local demo informed the composition and content browsing pattern.
It remains local and is excluded from Git and Docker build contexts. This change
uses the application's existing UI library and system fonts, without adding a
dependency or loading external fonts, logos, screenshots or placeholder statistics.

Validation: production build and TypeScript checks, 113 unit tests, 18 focused
workspace checks and 76 full browser regression checks passed with intercepted,
synthetic API responses. Desktop and mobile screenshots were inspected. A final
reduced-motion adjustment for modal animations passed another three home-guide
checks and a production build. No paid API calls or deployment were performed.
