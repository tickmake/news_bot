// Keeps the dashboard's data in step with the bot's database.
//
// The site itself is built once, in the image (SPA mode, see svelte.config.js),
// and holds no data: the browser reads /data/manifest.json and the parquet
// files it lists. This loop is the only thing that changes /data. Whenever the
// bot's DB file changes it:
//   1. snapshots the DB with SQLite's backup API (the bot keeps the file open,
//      so a plain copy can be torn), into tmpfs;
//   2. runs `evidence sources` against the snapshot, writing parquet files into
//      a fresh release directory on the site volume;
//   3. points the /data symlink at that release with an atomic rename, so a
//      browser never sees half a refresh, and prunes old releases.
// The DB volume is mounted read-only; nothing here can change the bot's data.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import sqlite3 from 'sqlite3';

const DB_FILE = process.env.DASHBOARD_DB_FILE ?? '/bot-data/.news_bot_trade_history.db';
const SITE_DIR = process.env.DASHBOARD_SITE_DIR ?? '/site';
const APP_BUILD_DIR = process.env.DASHBOARD_APP_BUILD_DIR ?? '/app/build';
// sources/trade_history/trade_history.db is a symlink to this (see Dockerfile).
const SNAPSHOT_FILE = '/tmp/trade_history.db';
const POLL_SECONDS = Number(process.env.DASHBOARD_POLL_SECONDS ?? 60);
// The bot writes a screen as many small transactions; wait for the file to
// sit still this long so one refresh sees the whole screen.
const SETTLE_SECONDS = Number(process.env.DASHBOARD_SETTLE_SECONDS ?? 30);
// Keep the previous release too: a browser that loaded the old manifest a
// moment ago can still fetch its parquet files.
const KEEP_RELEASES = 2;

const log = (msg) => console.log(`${new Date().toISOString()} ${msg}`);

function snapshot() {
	return new Promise((resolve, reject) => {
		fs.rmSync(SNAPSHOT_FILE, { force: true });
		const db = new sqlite3.Database(DB_FILE, sqlite3.OPEN_READONLY, (err) => {
			if (err) return reject(err);
			const backup = db.backup(SNAPSHOT_FILE, (err) => {
				if (err) return db.close(() => reject(err));
				backup.step(-1, (err) => {
					backup.finish();
					db.close(() => (err ? reject(err) : resolve()));
				});
			});
		});
	});
}

function runSources(outDir) {
	return new Promise((resolve, reject) => {
		const child = spawn(
			process.execPath,
			['node_modules/@evidence-dev/evidence/cli.js', 'sources', '--strict'],
			{
				cwd: '/app',
				stdio: 'inherit',
				env: { ...process.env, EVIDENCE_DATA_DIR: outDir }
			}
		);
		child.on('error', reject);
		child.on('exit', (code) =>
			code === 0 ? resolve() : reject(new Error(`evidence sources exited ${code}`))
		);
	});
}

function publish(release) {
	const tmpLink = path.join(SITE_DIR, 'data.tmp');
	fs.rmSync(tmpLink, { force: true });
	fs.symlinkSync(path.join('releases', release), tmpLink);
	fs.renameSync(tmpLink, path.join(SITE_DIR, 'data'));

	const releasesDir = path.join(SITE_DIR, 'releases');
	const old = fs.readdirSync(releasesDir).sort().slice(0, -KEEP_RELEASES);
	for (const name of old) fs.rmSync(path.join(releasesDir, name), { recursive: true, force: true });
}

async function refresh() {
	const release = new Date().toISOString().replace(/[:.]/g, '-');
	const outDir = path.join(SITE_DIR, 'releases', release);
	try {
		await snapshot();
		await runSources(outDir);
		publish(release);
		log(`refreshed data, release ${release}`);
	} catch (err) {
		fs.rmSync(outDir, { recursive: true, force: true });
		throw err;
	} finally {
		fs.rmSync(SNAPSHOT_FILE, { force: true });
	}
}

// Replace the app files with the ones from this image, so a new image takes
// effect on restart. Releases and the data link stay.
function installApp() {
	for (const name of fs.readdirSync(SITE_DIR)) {
		if (name === 'releases' || name === 'data') continue;
		fs.rmSync(path.join(SITE_DIR, name), { recursive: true, force: true });
	}
	for (const name of fs.readdirSync(APP_BUILD_DIR)) {
		if (name === 'data') continue;
		fs.cpSync(path.join(APP_BUILD_DIR, name), path.join(SITE_DIR, name), { recursive: true });
	}
	fs.mkdirSync(path.join(SITE_DIR, 'releases'), { recursive: true });
	log('installed app files');
}

installApp();
let lastRefreshedMtime = null;
for (;;) {
	try {
		const mtime = fs.statSync(DB_FILE).mtimeMs;
		const stillFor = (Date.now() - mtime) / 1000;
		if (mtime !== lastRefreshedMtime && stillFor >= SETTLE_SECONDS) {
			await refresh();
			lastRefreshedMtime = mtime;
		}
	} catch (err) {
		// Keep serving the last good data; try again on the next poll.
		log(`refresh failed: ${err.message}`);
	}
	await new Promise((r) => setTimeout(r, POLL_SECONDS * 1000));
}
