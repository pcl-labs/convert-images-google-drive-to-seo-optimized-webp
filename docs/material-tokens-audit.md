# Material Design 3 Token Audit

This document captures the mapping between the legacy Tailwind tokens and the Material Design 3 (M3) roles defined in [`assets/css/input.css`](../assets/css/input.css) and `tailwind.config.js`. It provides the evidence for completing Phase 1 · Design Tokens tasks DT-1 through DT-7 in `docs/material-design-transition-plan.md`.

## 1. Existing Tokens vs. M3 Roles (DT-1)

| Legacy Token | Usage Context | M3 Replacement | Notes |
| --- | --- | --- | --- |
| `--bg` / `bg-bg` | Page background, shell containers | `--md-sys-color-background` (`background` Tailwind color) | Alias retained for backwards compatibility. |
| `--surface` / `bg-surface` | Cards, modals | `--md-sys-color-surface` | Alias retained; use `surface` or `surface-container*` scales for new work. |
| `--surface-muted` | Secondary surfaces, muted cards | `--md-sys-color-surface-container` | Adds `surface-container-low`, `surface-container-high`, etc. |
| `--content` | Primary text | `--md-sys-color-on-surface` | Tailwind class `text-on-surface`. |
| `--content-muted` | Secondary text | `--md-sys-color-on-surface-variant` | Tailwind class `text-on-surface-variant`. |
| `--primary` | Brand color | `--md-sys-color-primary` | Adds `primary-container`, `on-primary-container`, `inverse-primary`. |
| `--accent` | Previous accent/tertiary | `--md-sys-color-tertiary` | Maintains alias `accent`. |
| `--destructive` | Error/destructive states | `--md-sys-color-error` | Adds error container roles. |
| `--warning` | Warning/toast emphasis | `--md-sys-color-secondary` | Consolidated with Material secondary palette. |
| `--radius-*` | Border radii | `--md-sys-shape-corner-*` | All shape tokens expressed in rem (dp ÷ 16). |
| `--shadow-*` | Elevation | `--md-sys-elevation-level*` | Level 0–5 match Material baseline. |
| `--font-sans` | Default sans family | `--md-ref-typeface-plain` | Adds brand typeface token `--md-ref-typeface-brand`. |

## 2. Color Tokens Implementation (DT-2)

- Light and dark schemes follow the [Material baseline seed palette](https://m3.material.io/styles/color/system/overview). RGB values are encoded as CSS custom properties to preserve opacity support.
- Tailwind theme colors map directly to these roles so that classes such as `bg-primary`, `text-on-primary`, `border-outline-variant`, and `bg-surface-container-high` now exist.
- Legacy aliases (`bg`, `surface`, etc.) still resolve to the new variables to avoid regressions. Future components should prefer the explicit Material role classes.

## 3. Typography Roles (DT-3)

- Every M3 type role is represented as a utility class (`text-display-large`, `text-title-medium`, `text-body-small`, etc.) inside `assets/css/input.css`.
- A reusable macro (`templates/macros/typography.html`) renders specimens and enforces semantic markup for upcoming component work.
- Tailwind `fontFamily` now exposes `font-brand` (`var(--md-ref-typeface-brand)`) and `font-sans` so utilities like `font-brand text-title-large` match the spec.

## 4. Spacing & Layout Tokens (DT-4)

- The 4 dp base grid is encoded as `--md-sys-spacing-scale-*` CSS variables and exposed via Tailwind spacing shortcuts (`p-dp-4`, `gap-dp-2`, etc.).
- Components use these tokens (see `.btn`, `.field`, `.badge` definitions) to keep padding consistent with Material density guidelines.

## 5. Elevation Tokens (DT-5)

- `--md-sys-elevation-level0` through `--md-sys-elevation-level5` implement Material’s key + ambient shadow pairs.
- Tailwind `boxShadow` exposes utilities such as `shadow-elevation-2` and `shadow-elevation-5` for component authors.
- Utility classes `.surface` and `.btn-*` now rely on the new elevation levels.

## 6. Shape Tokens (DT-6)

- Shape tokens cover the M3 corner scale (0 dp, 4 dp, 8 dp, 12 dp, 16 dp, 28 dp, full).
- Tailwind `borderRadius` maps to these variables so classes like `rounded-sm` and `rounded-xl` align with Material guidance.

## 7. Motion Tokens (DT-7)

- CSS variables define the canonical durations and easing curves (`--md-sys-motion-duration-short1`, `--md-sys-motion-easing-emphasized`, etc.).
- `static/js/motion.js` exposes a `window.materialMotion` helper that Alpine components can call to apply the tokens programmatically.
- Tailwind `transitionDuration` and `transitionTimingFunction` entries mirror the same token names for utility-first usage.

## 8. Next Inputs Needed

| Area | Needed from Design | How to Attach |
| --- | --- | --- |
| Color | Light/dark core palettes for branded themes | Export Tailwind-ready CSS variables from Figma and drop them into the color block at the top of `assets/css/input.css`. |
| Typography | Confirmation on brand/secondary typefaces | Update `--md-ref-typeface-brand` and `--md-ref-typeface-plain` and regenerate typography specimens. |
| Motion | Component-specific sequences | Document in `docs/material-interaction-playbook.md` once interactions phase begins. |

