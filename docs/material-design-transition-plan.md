# Material Design 3 Transition Plan

This plan decomposes the migration of our Tailwind + Alpine + Jinja UI into Material Design 3 (M3) phases. Each subsection lists actionable, verifiable tasks for large language model (LLM) implementers. Every task references the relevant M3 spec and identifies the files, configs, or Figmas that must be updated.

---

## Phase 1 · Foundations

### 1. Design Tokens ([M3 Foundations → Design Tokens](https://m3.material.io/foundations/design-tokens/overview))

| Task ID | Description | Owner Inputs | Output / Acceptance | Status / Links |
| --- | --- | --- | --- | --- |
| DT-1 | Audit current `tailwind.config.js` tokens (color palette, typography scale, spacing, radius, elevation shadows). Document gaps vs. M3 token taxonomy (color roles, type roles, spacing steps, shape scale, motion). | Existing Tailwind config, design docs. | `docs/material-tokens-audit.md` summarizing the delta and mapping placeholders for Figma exports. | ✅ Completed – see [`docs/material-tokens-audit.md`](./material-tokens-audit.md). |
| DT-2 | Define canonical color roles (primary, secondary, tertiary, neutral, neutral-variant, error, surface variants) using exported Tailwind CSS variables from Figma. | Figma exports for color schemes (light/dark). | Update `tailwind.config.js` theme colors + add `static/css/tokens/colors.css`. | ✅ Implemented via CSS variables + Tailwind colors in [`assets/css/input.css`](../assets/css/input.css) and [`tailwind.config.js`](../tailwind.config.js). |
| DT-3 | Establish typography roles (display, headline, title, body, label) matching M3 type scale and set Tailwind font families, weights, and line heights accordingly. | Type ramp Figma. | Update Tailwind `fontSize`, `fontWeight`, `lineHeight`, plus `templates/macros/typography.jinja`. | ✅ Utility classes + [`templates/macros/typography.html`](../templates/macros/typography.html) demonstrate the full ramp. |
| DT-4 | Normalize spacing & layout tokens (4dp base grid, density adjustments) and expose utilities/classes that mirror M3 spacing steps. | Layout spacing Figma. | Extend Tailwind `spacing` scale + document usage in `docs/component-api.md`. | ✅ Spacing tokens defined in CSS + Tailwind `spacing` scale (`dp-*` classes). |
| DT-5 | Define elevation tokens (level 0–5) as box-shadow presets + overlay colors. | Elevation spec Figma, [M3 Elevation](https://m3.material.io/styles/elevation/overview). | Create `static/css/tokens/elevation.css` and Tailwind plugin for `shadow-elevation-*`. | ✅ Elevation levels published as CSS variables and Tailwind `shadow-elevation-*` entries. |
| DT-6 | Shape tokens: map corner radius scale (0, 4, 8, 12, 16, 28, 999). | Shape spec from Figma. | Tailwind `borderRadius` extension + helper classes in `templates/macros`. | ✅ `rounded-*` utilities now mirror the Material shape scale; components consume the same tokens. |
| DT-7 | Motion tokens: define durations and easing curves (standard, emphasized, decelerated, accelerated) referencing [M3 Motion](https://m3.material.io/styles/motion/overview/how-it-works). | Motion spec. | Add CSS variables + JS constants for Alpine components in `static/js/motion.js`. | ✅ CSS vars + [`static/js/motion.js`](../static/js/motion.js) expose durations/easings and helper methods. |

### 2. Interactions ([M3 Foundations → Interaction](https://m3.material.io/foundations/interaction/))

| Task ID | Description | Owner Inputs | Output / Acceptance |
| --- | --- | --- | --- |
| IN-1 | Inventory interactive components (buttons, FABs, navigation, list items, dialogs) and document current state transitions (hover, focus, pressed, dragged). | Existing component docs + UX recordings. | Checklist in `docs/interaction-inventory.md` referencing component template paths. |
| IN-2 | Define state tokens (state layer opacities for hover/pressed/focus, container motion) aligned with tokens from Phase 1. | M3 state layer guidance. | Add `static/css/tokens/state-layers.css` + usage guidelines. |
| IN-3 | Update Alpine behaviors for ripple/pressed feedback using pure CSS/JS mirroring M3 motion curves. | Motion tokens, Alpine component list. | PR touching relevant `templates/components/*.jinja`. |
| IN-4 | Standardize accessibility focus rings (contrast, offset) per M3. | Accessibility checklist. | Utility classes + tests in `tests/ui_accessibility.test.js`. |
| IN-5 | Document interaction patterns (gestures, scroll behaviors) for each component. | Product UX specs. | `docs/material-interaction-playbook.md` with tables referencing M3 docs. |

### 3. Layout ([M3 Foundations → Layout](https://m3.material.io/foundations/layout/understanding-layout/overview))

| Task ID | Description | Owner Inputs | Output / Acceptance |
| --- | --- | --- | --- |
| LA-1 | Define adaptive layout grid (column counts, gutters, margins) per breakpoint, aligning with current Tailwind screens. | Layout Figma exports. | Update `tailwind.config.js` `screens` + add `docs/layout-grid.md`. |
| LA-2 | Create layout templates (app shell, navigation rail, bottom bar, details pane) using Alpine partials. | M3 layout spec, user journeys. | New `templates/layouts/*.jinja`. |
| LA-3 | Document responsive behavior for drawers, lists, cards (modal/inline) referencing M3 canonical behavior. | Interaction outputs. | Add behavior tables to `docs/component-api.md`. |
| LA-4 | Integrate density & window-size classes enabling compact/comfortable display modes. | Product requirements. | Tailwind plugin or variant for density in `tailwind.config.js`. |
| LA-5 | Align spacing tokens to layout components (sections, dividers) with annotated examples. | Updated spacing tokens. | `docs/layout-examples.md` containing component diagrams.

---

## Phase 2 · Styles

### 4. Color System ([M3 Styles → Color](https://m3.material.io/styles/color/system/overview))

- [ ] CO-1 · Generate core palettes via Material Theme Builder, export Tailwind variables, and store under `static/css/tokens/color-light.css` & `color-dark.css`.
- [ ] CO-2 · Map semantic color roles (surface, surface container variants, inverse roles) to templates; document fallback rules for legacy components in `docs/icon-usage.md`.
- [ ] CO-3 · Implement tonal elevation overlays for surfaces using CSS custom properties.
- [ ] CO-4 · Establish guidance for dynamic color (if system accent support is planned) and capture runtime injection plan in `docs/material-dynamic-color.md`.

### 5. Elevation ([M3 Styles → Elevation](https://m3.material.io/styles/elevation/overview))

- [ ] EL-1 · Extend token set with ambient & key shadow pairs plus tonal overlays; update `static/css/tokens/elevation.css`.
- [ ] EL-2 · Update component templates (cards, app bar, FAB, dialogs) to consume the elevation utilities and respect scroll-driven elevation. Reference component file paths.
- [ ] EL-3 · Document elevation usage rules (when to layer, transitions between states) inside `docs/material-interaction-playbook.md`.

### 6. Icons ([M3 Styles → Icons](https://m3.material.io/styles/icons/overview))

- [ ] IC-1 · Inventory current icons (`static/icons/`, `templates/includes/icon.jinja`) and classify per M3 categories (Outlined, Rounded, Sharp). Create `docs/icon-mapping.md`.
- [ ] IC-2 · Define size ramps (16, 20, 24, 32, 48) with optical padding adjustments using Tailwind utilities.
- [ ] IC-3 · Align icon colors + state layers with color tokens; update `icon-usage.md` with examples referencing Material guidance.
- [ ] IC-4 · Establish fallback strategy for custom icons (SVG) ensuring they respect motion & interaction specs.

### 7. Motion ([M3 Styles → Motion](https://m3.material.io/styles/motion/overview/how-it-works))

- [ ] MO-1 · Implement motion tokens from DT-7 into a reusable Alpine helper (e.g., `motionController`) for sequences like enter/exit transitions.
- [ ] MO-2 · Document choreography rules (delays, staggering) for components like FAB, navigation rail, and dialogs.
- [ ] MO-3 · Update existing transitions (`static/js/transitions.js`) to use the standardized curves/durations and provide before/after demos (GIF or Loom links placeholder).

### 8. Typography ([M3 Styles → Typography](https://m3.material.io/styles/typography/overview))

- [ ] TY-1 · Confirm each typography role has Tailwind utility (`text-display-large`, etc.) mapped to CSS vars for weight/line-height.
- [ ] TY-2 · Update `templates/macros/typography.jinja` to enforce semantic HTML + proper roles.
- [ ] TY-3 · Document type scale usage per component (buttons, chips, inputs) with cross-links to `docs/component-api.md`.

---

## Phase 3 · Components ([M3 Components](https://m3.material.io/components))

For each component category, add a backlog entry describing the Tailwind/Alpine/Jinja deliverables plus required Figma exports. Track completion in `docs/component-api.md` tables.

1. **Action & Navigation**
   - [ ] Buttons (filled, tonal, outlined, text) → update `templates/components/buttons.jinja`, include icon affordances.
   - [ ] Floating Action Button + Extended FAB → define elevation/motion combos, ripple states.
   - [ ] Navigation bar, navigation rail, navigation drawer → align layout breakpoints + interactions.
2. **Containment**
   - [ ] Cards (elevated, filled, outlined) with adaptive layout slots.
   - [ ] Dialogs, bottom sheets, banners, snackbars → tie to interaction + motion tokens.
3. **Communication**
   - [ ] Badges, chips, tooltips, progress indicators → ensure color/typography tokens apply.
4. **Data inputs**
   - [ ] Text fields, date pickers, sliders, switches, checkboxes, radio buttons → define state layers + focus rings.
5. **Lists & Tables**
   - [ ] List items, tables, expansion panels → adopt typography + interaction states.
6. **Top app bars & tabs**
   - [ ] Medium, large, center-aligned bars + tabs with indicator motion and scroll behaviors.

Each component task should be expanded into:
- Required tokens (color, typography, elevation, motion).
- Alpine behaviors (focus handling, keyboard navigation).
- Tailwind utility gaps.
- QA checklist (unit + visual regression references).

---

## Operational Next Steps

1. Schedule Figma export batches per phase and attach the exported Tailwind snippets to the referenced docs.
2. Create GitHub issues per Task ID with clear acceptance criteria referencing this plan.
3. Adopt a rolling PR strategy: tokens → foundational behaviors → component skins.
4. Update this document after each milestone with links to merged PRs and refreshed checklists.
