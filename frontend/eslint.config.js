import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // cva variant exports (badge/button) are style constants, not components —
      // the standard shadcn/ui pattern; the fast-refresh rule does not apply.
      'react-refresh/only-export-components': 'off',
      // Data-fetching effects intentionally setState on mount/reset (loading +
      // error lifecycle). Tracked for a future data-layer (react-query) cleanup.
      'react-hooks/set-state-in-effect': 'warn',
    },
  },
])
