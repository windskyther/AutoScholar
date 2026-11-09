# Apple-inspired research workspace

The workspace uses a centered top Dock in place of the sidebar, with the existing
Projects, Models and Usage destinations and their routes. Project browsing now uses
document-style cards with the original create, delete and server-paginated behavior.
A local static monochrome geometric wallpaper, system typography, rounded surfaces
and coordinated controls carry through project details, tasks, models and usage.
Appearance follows the system, and route changes begin at the top of the new view.

The Dock's Web glass material combines a bounded Canvas displacement map, an SVG
filter, translucent fill and edge highlights. The map is regenerated for size changes,
not for every pointer movement. Chromium uses the SVG backdrop path; other engines
retain CSS glass. This is an approximation inspired by Apple materials, rather than a
native Apple framework implementation.

Navigation remains labeled and keyboard accessible, including a skip-to-content link.
Reduced motion, increased contrast, forced colors, reduced transparency and unavailable
Canvas contexts have fallbacks. Observers, frame callbacks and media-query subscriptions
are cleaned up on unmount. Navigation sits outside the animated page content so that
page-entry opacity does not alter its backdrop sampling boundary.

Validation: frontend build/type check, 113 unit tests, 58 existing browser interaction
checks and 10 new workspace checks passed. New checks cover actual pixel differences
from refraction, first navigation, nested-route selection, project pagination, route
scroll reset, 320/390px layouts, system appearance, keyboard use, accessibility
preferences, Canvas fallback and bounded regeneration on resize. Desktop, dark and
narrow screenshots were inspected with synthetic API records. This is a frontend-only
change using the existing dependencies. All browser checks used mocked API responses;
no paid API calls or deployment were performed.

The displacement technique draws on shuding/liquid-glass. Attribution and its MIT
license ship in `web/public/THIRD_PARTY_NOTICES.txt` and are included in the built site.
