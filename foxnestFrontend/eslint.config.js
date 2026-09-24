import js from '@eslint/js'
import globals from 'globals'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs['recommended-latest'],
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    plugins: { react },
    rules: {
      // Base ESLint does not parse JSX, so an identifier used only in markup --
      // `motion` in <motion.div>, or an `{ icon: Icon }` rendered as <Icon /> --
      // looks unused and gets reported. This rule marks those as read, which is
      // what lets no-unused-vars below run without ignore patterns: an unused name
      // it now reports is genuinely dead rather than merely used in a way ESLint
      // cannot see.
      'react/jsx-uses-vars': 'error',
      // The uppercase exemption is what lets `import React` stay in every file under
      // React 19's automatic runtime, where it is no longer referenced. Arguments get
      // no such exemption: jsx-uses-vars now accounts for the component-shaped ones.
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
    },
  },
])
