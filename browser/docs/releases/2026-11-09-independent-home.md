# Independent home and open workspace layout

Opening `/` now shows a separate welcome page with research and model setup links.
The project collection moves to `/projects`; the three Dock destinations remain
Projects, Models and Usage. No Dock item is selected on Home. The AutoScholar brand
returns to Home, while project breadcrumbs, deletion and connection recovery return
to the project collection where appropriate. Existing project, task and evidence
detail URLs retain their paths.

Typography takes its proportions from [Mobbin's public homepage](https://mobbin.com/): larger, compact
headings, quieter body text and more open spacing. The implementation uses local
system fonts without downloading Mobbin's font or assets. The desktop Dock grows
from 358px to 548px, with 36px between its three choices; narrow layouts adapt the
spacing and place the brand above the Dock.

The shared content area has no background, outline, corner radius or shadow. A
static geometric wallpaper and a page-wide tonal wash provide readable contrast.
Dark appearance reverses the wallpaper tones. Individual controls, tables, dialogs
and project cards retain their own surfaces. Route entry takes 480ms instead of
180ms and still waits for the actual page content. Reduced motion disables the
animation, and increased contrast, reduced transparency and forced colors retain
solid, readable backgrounds without restoring the enclosing content panel.

The change uses existing frontend dependencies and preserves the Dock's bounded
glass refraction and fallbacks. It does not alter task execution or provider calls.

Validation: the production frontend build and TypeScript checks passed, along with
113 unit tests and all 73 browser checks using synthetic, intercepted API responses.
The browser suite covers Home, nested navigation, collection recovery and deletion,
initial lazy loading, 480ms transitions, narrow layouts, appearance, accessibility
preferences, glass refraction and existing research interactions. Desktop, mobile
and dark screenshots were inspected. No paid API calls or deployment were performed.
Local and stack browser fixture entry paths were updated for `/projects`; those
separate integration suites were not rerun for this frontend change.
