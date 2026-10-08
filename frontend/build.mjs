import {build} from 'esbuild';
await build({entryPoints:[new URL('./src/App.jsx',import.meta.url).pathname],bundle:true,minify:true,jsx:'automatic',format:'iife',target:['es2022'],outfile:new URL('../src/orchestra_kit/web/app.js',import.meta.url).pathname,define:{'process.env.NODE_ENV':'"production"'},legalComments:'eof'});
