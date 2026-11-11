// Merged into Evidence's own svelte.config.js at build time.
//
// The image builds the site once in SPA mode (VITE_EVIDENCE_SPA=true): no page
// is prerendered with data, and the browser runs every query against the
// parquet files listed in /data/manifest.json. That lets the Pi refresh the
// data with the light `evidence sources` step alone, instead of a full build
// that needs ~3.7 GB of RAM. SPA mode emits no HTML of its own, so the
// fallback below provides the one shell page that nginx serves for every route.
import adapter from '@sveltejs/adapter-static';

export default {
	kit: {
		adapter: adapter({
			pages: './build',
			fallback: 'index.html',
			strict: false
		})
	}
};
