# Design System and Tokens Plan (Tailwind)

Goal: Establish a scalable, themeable design system using semantic design tokens and normalized components.

Findings (current state)
- **Tailwind config**: minimal; no theme extensions or plugins.
- **Styles entry**: assets/css/input.css sets dark scheme and bg `slate-950` globally.
- **Components**: hard-coded colors (e.g., `sky-600`, `rose-600`, `slate-900/60`, `border-slate-800`) across `button`, `card`, `alert`, etc.
- **Templates**: system is HTMX/Alpine + Jinja macros; good for centralizing component logic.

# Objectives
- **Tokens**: Define semantic tokens (colors, radius, spacing, shadows, typography) via CSS variables.
- **Tailwind mapping**: Map tokens to Tailwind theme for first-class utility usage.
- **Components**: Refactor to use semantic utilities (e.g., `bg-surface`, `text-content`, `border-border`, `btn-primary`) rather than raw colors.
- **Dark mode**: Keep `dark` class strategy, provide token overrides in `.dark` for themes.
- **Guardrails**: Prevent raw color usage in components; add lint/grep rules.
- **Docs**: Provide a styleguide preview page to validate components visually.

# Work Plan (Phases)

- **[ds-tokens]** Define design tokens
- **[ds-config]** Update Tailwind config
- **[ds-stylesheet]** Create tokens stylesheet and semantic utilities
- **[ds-components]** Refactor core components to tokens
- **[ds-docs]** Document component API and examples
- **[ds-guardrails]** Add checks to prevent regressions
- **[ds-previews]** Add styleguide page for visual QA
- **[ds-migration]** Migration/search-replace plan

# 1) Define tokens (CSS variables)
Create semantic variables in `:root` and `.dark`.

Colors
- `--bg` (page background)
- `--surface` (cards, panels)
- `--surface-muted`
- `--border`
- `--content` (base text)
- `--content-muted`
- `--primary`, `--primary-contrast`
- `--destructive`, `--destructive-contrast`
- `--accent`, `--accent-contrast`
- `--ring` (focus ring)

Radiuses
- `--radius-xs`, `--radius-sm`, `--radius-md`, `--radius-lg`

Shadows
- `--shadow-sm`, `--shadow-md`, `--shadow-lg`

Typography
- `--font-sans`, `--font-mono`
- `--leading`, `--tracking`

# 2) Tailwind config updates
File: `tailwind.config.js`
- Add content paths if needed later (e.g., Python-rendered fragments still under `templates/**/*.html`).
- Extend theme with token-backed colors and radiuses.
- Add plugins: `@tailwindcss/forms`, `@tailwindcss/typography`.

Example (conceptual, to be adapted):
```js
// tailwind.config.js (conceptual)
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        surface: 'var(--surface)',
        surfaceMuted: 'var(--surface-muted)',
        border: 'var(--border)',
        content: 'var(--content)',
        contentMuted: 'var(--content-muted)',
        primary: 'var(--primary)',
        primaryContrast: 'var(--primary-contrast)',
        destructive: 'var(--destructive)',
        destructiveContrast: 'var(--destructive-contrast)',
        accent: 'var(--accent)',
        accentContrast: 'var(--accent-contrast)',
        ring: 'var(--ring)'
      },
      borderRadius: {
        xs: 'var(--radius-xs)',
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)'
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)'
      },
      fontFamily: {
        sans: 'var(--font-sans)',
        mono: 'var(--font-mono)'
      }
    }
  },
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/typography')]
}
```

Install plugins
```bash
npm i -D @tailwindcss/forms @tailwindcss/typography
```

# 3) Tokens stylesheet and semantic utilities
File: `assets/css/input.css`
- Add variables to `@layer base` with `:root` and `.dark`.
- Define semantic utility classes in `@layer components` to encapsulate common patterns.

Example variables (initial values mirror current design)
```css
@layer base {
  :root {
    color-scheme: dark;
    /* Backgrounds */
    --bg: #020617;            /* slate-950 */
    --surface: rgba(2, 6, 23, 0.9); /* ~slate-950/90 for panels */
    --surface-muted: #0f172a; /* slate-900 */
    --border: #1f2937;        /* slate-800 */
    /* Text */
    --content: #e2e8f0;       /* slate-200 */
    --content-muted: #94a3b8; /* slate-400 */
    /* Brand */
    --primary: #0284c7;       /* sky-600 */
    --primary-contrast: #ffffff;
    --destructive: #e11d48;   /* rose-600 */
    --destructive-contrast: #ffffff;
    --accent: #22c55e;        /* green-500 */
    --accent-contrast: #052e16;
    /* Focus ring */
    --ring: #38bdf8;          /* sky-400 */
    /* Radius */
    --radius-xs: 0.25rem;
    --radius-sm: 0.375rem;
    --radius-md: 0.5rem;
    --radius-lg: 0.75rem;
    /* Shadows */
    --shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.2);
    --shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.25);
    --shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.3);
    /* Typography */
    --font-sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica Neue, Arial, "Apple Color Emoji", "Segoe UI Emoji";
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  }
  .dark {
    /* Inherit same for now; later adjust for alternate dark themes */
  }
  html, body { @apply bg-bg text-content; }
}

@layer components {
  /* Containers */
  .surface { @apply bg-surface border border-border rounded-lg shadow-md; }
  .surface-muted { @apply bg-surfaceMuted border border-border rounded-lg; }

  /* Buttons */
  .btn { @apply inline-flex items-center justify-center px-3 py-2 text-sm font-medium rounded-md; }
  .btn-primary { @apply btn; background-color: var(--primary); color: var(--primary-contrast); }
  .btn-primary:hover { filter: brightness(1.05); }
  .btn-secondary { @apply btn; background-color: var(--surface-muted); color: var(--content); border: 1px solid var(--border); }
  .btn-destructive { @apply btn; background-color: var(--destructive); color: var(--destructive-contrast); }
  .btn-ghost { @apply btn; background-color: transparent; color: var(--content); border: 1px solid transparent; }

  /* Inputs */
  .field { @apply bg-surface text-content border border-border rounded-md placeholder:text-contentMuted focus:outline-none focus:ring-2; }
  .field:focus { box-shadow: 0 0 0 2px var(--ring); }

  /* Badges */
  .badge { @apply inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium border border-border; }
}
```

# 4) Component refactors (macro-level)
Refactor templates under `templates/components/elements/` to use semantic utilities:

- `button.html`
  - Map variants to `.btn-*` classes (primary, secondary, destructive, ghost).
  - Remove hard-coded colors (`sky-600`, `rose-600`, etc.).
- `card.html`
  - Replace `bg-slate-900/60 border-slate-800` with `.surface` and use tokenized header/footer borders.
- `alert.html`
  - Introduce alert variants: info, success, warning, error using tokens (`--accent`, `--destructive`).
- `input.html`
  - Replace raw colors with `.field` base class; add size variants if needed.
- `badge.html`
  - Use `.badge` with token-based color accents.
- `dropdown.html`, `integration_card.html`
  - Normalize panel/background/border/spacing with `.surface` utilities.

# 5) Component API and docs
- Document macro signatures and allowed `variant` values.
- Provide examples in a new `templates/styleguide.html` showcasing:
  - Buttons (all variants + states + with_spinner)
  - Cards (with/without headers)
  - Alerts (variants)
  - Inputs (focus, error)
  - Badges and dropdowns

# 6) Guardrails
- Add a simple CI check to forbid raw color utilities in component templates:
```bash
grep -R -nE "bg-(slate|sky|rose|green|red|blue)-|text-(slate|sky|rose|green|red|blue)-|border-(slate|sky|rose|green|red|blue)-" templates/components/elements && {
  echo "Found raw color classes in components. Use tokens instead."; exit 1; }
```
- Optionally add a lightweight lint script in `package.json`:
```json
{
  "scripts": {
    "lint:design": "bash scripts/lint-design.sh"
  }
}
```

# 7) Migration strategy
- Phase-by-phase PRs:
  1. Tokens + Tailwind config + CSS utilities (no component changes yet).
  2. Refactor buttons, card, inputs.
  3. Refactor alerts, badges, dropdowns, integration_card.
  4. Add styleguide page and visual QA.
- Search/replace helpers:
```bash
# Example: find raw slate colors in components
grep -R -nE "(bg|text|border)-slate-" templates/components/elements
# Example: replace button primary classes (manual, review each)
# ripgrep patterns to assist review before changing
```

# 8) Build and verify
- Install plugins and rebuild CSS:
```bash
npm i -D @tailwindcss/forms @tailwindcss/typography
npm run build:css
```
- Load key pages and check:
  - Background, text, borders reflect tokens.
  - Buttons and cards use new utilities.
  - Focus ring visible and consistent.

# Acceptance criteria
- No component templates contain raw Tailwind color classes (except tokens helpers).
- All variants are documented and previewed in styleguide.
- A single place (tokens) can update the whole theme.
- Dark mode supported via `.dark` overrides.
