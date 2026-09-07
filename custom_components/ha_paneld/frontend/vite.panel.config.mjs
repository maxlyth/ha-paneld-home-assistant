import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  publicDir: false,
  build: {
    target: 'es2022',
    outDir: '../static',
    emptyOutDir: false,
    sourcemap: false,
    lib: {
      entry: fileURLToPath(new URL('./src/ha-install-panel.mjs', import.meta.url)),
      formats: ['es'],
      fileName: () => 'ha-panel.js',
    },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
