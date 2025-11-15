/**** Tailwind CLI config ****/
/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './templates/**/*.html',
  ],
  theme: {
    extend: {
      colors: {
        // Semantic tokens (for theming)
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
        warning: 'var(--warning)',
        warningContrast: 'var(--warning-contrast)',
        ring: 'var(--ring)',
        // Standard Tailwind colors (still available for utility use)
        // These are the default Tailwind color palette
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
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography')
  ],
}
