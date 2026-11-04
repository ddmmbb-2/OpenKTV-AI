import './environment.js';
import express from 'express';
import { createServer } from 'http';
import { Server } from 'socket.io';
import { rateLimit } from 'express-rate-limit';
import fs from 'fs';
import os from 'os';
import { timingSafeEqual } from 'crypto';
import { allowsPageAccess, isLocalAddress } from './page-access.js';
import { createMicRelay } from './mic-relay.js';
import { startHttpsProxy } from './https-proxy.js';
import csv from 'csv-parser';
import MiniSearch from 'minisearch';
import QRCode from 'qrcode';
import path from 'path';
import { fileURLToPath } from 'url';
import { DatabaseSync } from 'node:sqlite';
import multer from 'multer';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, '..');
const songsDir = path.resolve(projectRoot, process.env.KTV_SONGS_DIR || path.join(projectRoot, 'ktv_songs'));
const catalogPath = path.resolve(projectRoot, process.env.KTV_LIBRARY_INDEX_PATH || path.join(songsDir, 'library_index.csv'));
const dbPath = path.resolve(projectRoot, process.env.KTV_LIBRARY_DB_PATH || path.join(songsDir, 'library.db'));
const legacyCatalogPath = path.join(__dirname, 'songs.csv');
const statsPath = path.resolve(songsDir, 'song_stats.json');

let songStats = {};

function loadSongStats() {
  try {
    if (fs.existsSync(statsPath)) {
      songStats = JSON.parse(fs.readFileSync(statsPath, 'utf8'));
    } else {
      songStats = {};
    }
  } catch (err) {
    songStats = {};
  }
}

function saveSongStats() {
  try {
    fs.writeFileSync(statsPath, JSON.stringify(songStats, null, 2), 'utf8');
  } catch (err) {
    console.warn('[Stats] Could not write song_stats.json:', err.message);
  }
}

function recordSongPlay(songId) {
  if (!songId) return;
  const id = String(songId);
  if (!songStats[id]) {
    songStats[id] = { playCount: 0, lastPlayedAt: 0 };
  }
  songStats[id].playCount = (songStats[id].playCount || 0) + 1;
  songStats[id].lastPlayedAt = Date.now();
  saveSongStats();
}

function enrichSong(song) {
  if (!song) return song;
  const stats = songStats[song.id] || {};
  return {
    ...song,
    playCount: stats.playCount || 0,
    lastPlayedAt: stats.lastPlayedAt || null,
  };
}

function mediaPath(filename) {
  if (!filename || path.basename(filename) !== filename
    || !['.mp4', '.m4a', '.wav', '.lrc', '.json'].includes(path.extname(filename).toLowerCase())) return null;
  try {
    const resolved = path.resolve(songsDir, filename);
    if (!resolved.startsWith(`${songsDir}${path.sep}`) || !fs.existsSync(resolved)) return null;
    const realSongsDir = fs.realpathSync(songsDir);
    const realMediaPath = fs.realpathSync(resolved);
    return realMediaPath.startsWith(`${realSongsDir}${path.sep}`) ? realMediaPath : null;
  } catch {
    return null;
  }
}

function mediaUrl(filename) {
  return `/media/${encodeURIComponent(filename)}`;
}

function normalizeSong(row) {
  const filename = row.video_filename || row.path?.replace(/^\/songs\//, '') || row.song || '';
  const song = {
    ...row,
    id: String(row.id || ''),
    title: row.title || row.song_title || row.name || '',
    artist: row.artist || row.singer || '',
    language: row.language || '',
    char_count: Number(row.char_count) || (row.title || row.song_title || '').length,
    pinyin_abbr: (row.pinyin_abbr || '').toUpperCase(),
    zhuyin_abbr: row.zhuyin_abbr || '',
    video_filename: filename,
    instrumental_filename: row.instrumental_filename || row.instrumental || '',
    lyrics_filename: row.lyrics_filename || row.lyrics_lrc || '',
    pitch_filename: row.pitch_filename || '',
  };
  if (!song.instrumental_filename && filename.toLowerCase().endsWith('.mp4')) {
    song.instrumental_filename = `${filename.slice(0, -4)}.instrumental.m4a`;
  }
  if (!song.lyrics_filename && filename.toLowerCase().endsWith('.mp4')) {
    song.lyrics_filename = `${filename.slice(0, -4)}.lrc`;
  }
  if (!song.pitch_filename && filename.toLowerCase().endsWith('.mp4')) {
    song.pitch_filename = `${filename.slice(0, -4)}.pitch.json`;
  }
  song.path = filename ? mediaUrl(filename) : '';
  song.instrumental_path = song.instrumental_filename ? mediaUrl(song.instrumental_filename) : '';
  song.pitch_path = song.pitch_filename ? mediaUrl(song.pitch_filename) : '';
  return song;
}

async function readCatalog(filePath) {
  if (!fs.existsSync(filePath)) return [];
  return new Promise((resolve, reject) => {
    const rows = [];
    fs.createReadStream(filePath)
      .pipe(csv())
      .on('data', row => rows.push(normalizeSong(row)))
      .on('end', () => resolve(rows))
      .on('error', reject);
  });
}

function readCatalogFromDb(databasePath) {
  if (!fs.existsSync(databasePath)) return null;
  try {
    const db = new DatabaseSync(databasePath);
    const stmt = db.prepare('SELECT * FROM songs ORDER BY artist, title');
    const rows = stmt.all();
    db.close();
    return rows.map(r => normalizeSong(r));
  } catch (err) {
    console.warn('[Database] Read from SQLite failed, falling back to CSV:', err.message);
    return null;
  }
}

async function loadSongs() {
  const fromDb = readCatalogFromDb(dbPath);
  if (fromDb && fromDb.length > 0) return fromDb;
  let songs = await readCatalog(catalogPath);
  if (!songs.length && fs.existsSync(legacyCatalogPath) && catalogPath !== legacyCatalogPath) {
    songs = await readCatalog(legacyCatalogPath);
  }
  return songs;
}

function createSearchIndex(songs) {
  const index = new MiniSearch({
    fields: ['title', 'artist', 'lyrics', 'language', 'pinyin_abbr', 'zhuyin_abbr'],
    storeFields: ['id', 'title', 'artist', 'lyrics', 'language', 'char_count', 'path', 'instrumental_path',
      'video_filename', 'instrumental_filename', 'lyrics_filename'],
    searchOptions: { prefix: true, fuzzy: 0.2, boost: { title: 4, artist: 3, lyrics: 1 } },
  });
  index.addAll(songs);
  return index;
}

function createInverseIndex(songs) {
  const index = new Map();
  for (const song of songs) {
    const fields = [song.title, song.artist, song.lyrics, song.pinyin_abbr, song.zhuyin_abbr, song.language];
    const tokens = new Set();
    for (const field of fields) {
      const value = String(field || '').toUpperCase();
      for (const token of value.match(/[\p{L}\p{N}]+/gu) || []) tokens.add(token);
      for (const char of [...value]) {
        if (/^[\p{L}\p{N}]$/u.test(char)) tokens.add(char);
      }
    }
    for (const token of tokens) {
      if (!index.has(token)) index.set(token, new Set());
      index.get(token).add(song.id);
    }
  }
  return index;
}

function inverseSearch(songs, inverseIndex, query) {
  const clean = String(query || '').trim().toUpperCase();
  if (!clean) return songs;
  const terms = clean.match(/[\p{L}\p{N}]+/gu) || [...clean].filter(ch => /^[\p{L}\p{N}]$/u.test(ch));
  if (!terms.length) return [];
  let candidates = null;
  for (const term of terms) {
    const ids = new Set();
    for (const [token, values] of inverseIndex.entries()) {
      if (token.startsWith(term) || token.includes(term)) for (const id of values) ids.add(id);
    }
    candidates = candidates === null ? ids : new Set([...candidates].filter(id => ids.has(id)));
  }
  const byId = new Map(songs.map(song => [song.id, song]));
  return [...(candidates || [])].map(id => byId.get(id)).filter(Boolean);
}

function findSongs(songs, miniSearch, inverseIndex, query, language, charCount, mode = 'keyword', sort = 'relevance') {
  const cleanQuery = String(query || '').trim();
  let results = songs;

  if (cleanQuery) {
    const isZhuyin = /^[\u3105-\u3129\u02CA\u02C7\u02CB\u02D9\s]+$/.test(cleanQuery);
    const isAlphaNum = /^[A-Za-z0-9\s]+$/.test(cleanQuery);

    if (mode === 'zhuyin' || (mode === 'keyword' && isZhuyin)) {
      const target = cleanQuery.replace(/\s+/g, '');
      results = songs.filter(song => (song.zhuyin_abbr || '').replace(/\s+/g, '').includes(target));
      if (results.length < 5) {
        const fuzzy = miniSearch.search(cleanQuery, { fields: ['zhuyin_abbr'] })
          .map(r => songs.find(s => s.id === r.id)).filter(Boolean);
        results = [...new Map([...results, ...fuzzy].map(s => [s.id, s])).values()];
      }
    } else if (mode === 'pinyin' || (mode === 'keyword' && isAlphaNum)) {
      const targetUpper = cleanQuery.toUpperCase().replace(/\s+/g, '');
      const pinyinHits = songs.filter(song => (song.pinyin_abbr || '').replace(/\s+/g, '').includes(targetUpper));
      const titleHits = songs.filter(song => (song.title || '').toUpperCase().includes(cleanQuery.toUpperCase()));
      const artistHits = songs.filter(song => (song.artist || '').toUpperCase().includes(cleanQuery.toUpperCase()));
      const combined = targetUpper.length <= 5
        ? [...pinyinHits, ...titleHits, ...artistHits]
        : [...titleHits, ...artistHits, ...pinyinHits];
      results = [...new Map(combined.map(s => [s.id, s])).values()];
      if (results.length < 5) {
        const fuzzy = miniSearch.search(cleanQuery.toUpperCase())
          .map(r => songs.find(s => s.id === r.id)).filter(Boolean);
        results = [...new Map([...results, ...fuzzy].map(s => [s.id, s])).values()];
      }
    } else {
      const fieldForMode = { lyrics: 'lyrics', artist: 'artist' }[mode];
      if (fieldForMode) {
        const cleanUpper = cleanQuery.toUpperCase();
        results = songs.filter(song => String(song[fieldForMode] || '').toUpperCase().includes(cleanUpper));
      } else {
        results = inverseSearch(songs, inverseIndex, cleanQuery);
        if (results.length < 5) {
          const fuzzy = miniSearch.search(cleanQuery.toUpperCase())
            .map(r => songs.find(s => s.id === r.id)).filter(Boolean);
          results = [...new Map([...results, ...fuzzy].map(s => [s.id, s])).values()];
        }
      }
    }
  }

  if (language) results = results.filter(song => song.language === language);
  if (charCount) results = results.filter(song => song.char_count === Number(charCount));

  if (sort === 'title') results = [...results].sort((a, b) => a.title.localeCompare(b.title));
  else if (sort === 'artist') results = [...results].sort((a, b) => a.artist.localeCompare(b.artist));
  else if (sort === 'duration') results = [...results].sort((a, b) => (a.duration_seconds || 0) - (b.duration_seconds || 0));
  else if (sort === 'trending' || sort === 'popular') {
    results = [...results].sort((a, b) => {
      const countA = songStats[a.id]?.playCount || 0;
      const countB = songStats[b.id]?.playCount || 0;
      if (countB !== countA) return countB - countA;
      return a.title.localeCompare(b.title);
    });
  } else if (sort === 'newest') {
    results = [...results].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
  }

  return results.map(enrichSong);
}

function parseRange(range, size) {
  const match = /^bytes=(\d*)-(\d*)$/.exec(range || '');
  if (!match) return null;
  let start;
  let end;
  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!suffixLength) return null;
    start = Math.max(0, size - suffixLength);
    end = size - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : size - 1;
  }
  if (start >= size || end < start) return null;
  return { start, end: Math.min(end, size - 1) };
}

export function startServer(port = Number(process.env.KTV_NODE_PORT) || 3000) {
  return new Promise(async (resolve, reject) => {
    try {
      loadSongStats();
      let songs = await loadSongs();
      let miniSearch = createSearchIndex(songs);
      let inverseIndex = createInverseIndex(songs);
      const app = express();
      const httpServer = createServer(app);
      const io = new Server(httpServer, { cors: { origin: "*", methods: ["GET", "POST"] } });
      const micRelay = createMicRelay(io);
      let advanceQueue = () => {};
      app.use(express.json({ limit: '32kb' }));
      const mediaRequestLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 600,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });
      const botApiLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 60,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });
      const pageRequestLimiter = rateLimit({
        windowMs: 60 * 1000,
        limit: 120,
        standardHeaders: 'draft-8',
        legacyHeaders: false,
      });

      const publicDirectory = path.join(__dirname, 'public');
      app.use((req, res, next) => {
        const proxyClientAddress = isLocalAddress(req.socket.remoteAddress)
          ? req.get('x-ktv-proxy-client-ip')
          : null;
        if (!allowsPageAccess(req.originalUrl, proxyClientAddress || req.socket.remoteAddress)) {
          return res.redirect('/remote');
        }
        return next();
      });
      app.use(express.static(publicDirectory));
      app.get(['/', '/home'], pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'home.html')));
      app.get('/player', (_req, res) => res.redirect('/player.html'));
      app.get(['/remote', '/queue'], (_req, res) => res.redirect('/remote.html'));
      app.get('/operator', pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'console.html')));
      app.get(['/console', '/ktv'], pageRequestLimiter, (_req, res) => res.sendFile(path.join(publicDirectory, 'console.html')));
      app.get('/api/room', (_req, res) => res.json({
        songCount: songs.length,
        clientCount: io.engine.clientsCount,
        isPlaying: playerState.isPlaying,
        currentSong: currentSong(),
        queueCount: queue.length,
      }));
      app.get('/api/webrtc-config', (_req, res) => {
        const iceServers = String(process.env.KTV_STUN_URLS || 'stun:stun.l.google.com:19302')
          .split(',')
          .map(url => url.trim())
          .filter(Boolean)
          .map(urls => ({ urls }));
        const turnUrls = String(process.env.KTV_TURN_URLS || '').split(',').map(url => url.trim()).filter(Boolean);
        if (turnUrls.length && process.env.KTV_TURN_USERNAME && process.env.KTV_TURN_CREDENTIAL) {
          iceServers.push({
            urls: turnUrls,
            username: process.env.KTV_TURN_USERNAME,
            credential: process.env.KTV_TURN_CREDENTIAL,
          });
        }
        res.setHeader('Cache-Control', 'no-store');
        return res.json({ iceServers });
      });
      app.get('/api/services', pageRequestLimiter, async (_req, res) => {
        const pythonPort = Number(process.env.KTV_PORT) || 5000;
        try {
          const response = await fetch(`http://127.0.0.1:${pythonPort}/health`, {
            signal: AbortSignal.timeout(1500),
          });
          const health = response.ok ? await response.json() : null;
          return res.json({
            node: { ok: true },
            python: { ok: Boolean(health?.ok && health.service === 'python') },
            pythonPort,
          });
        } catch {
          return res.status(503).json({ node: { ok: true }, python: { ok: false }, pythonPort });
        }
      });
      app.get('/api/qr', pageRequestLimiter, async (req, res) => {
        let target;
        const isHttpsProxyRequest = isLocalAddress(req.socket.remoteAddress)
          && Boolean(req.get('x-ktv-proxy-client-ip'));
        const httpsConfigured = Boolean(process.env.KTV_TLS_CERT_PATH && process.env.KTV_TLS_KEY_PATH);
        try {
          target = new URL(`${isHttpsProxyRequest ? 'https' : req.protocol}://${req.get('host')}/remote`);
        } catch {
          return res.status(400).json({ error: '無效的服務網址' });
        }
        if (['localhost', '127.0.0.1', '::1'].includes(target.hostname)) {
          const addresses = Object.values(os.networkInterfaces()).flatMap(items => items || []);
          const lanAddress = addresses.find(item => (item.family === 'IPv4' || item.family === 4) && !item.internal);
          if (lanAddress) target.hostname = lanAddress.address;
        }
        if (httpsConfigured && !isHttpsProxyRequest) {
          target.protocol = 'https:';
          target.port = String(Number(process.env.KTV_HTTPS_PORT) || 3443);
        }
        try {
          const url = target.toString();
          const dataUrl = await QRCode.toDataURL(url, { margin: 2, width: 440, errorCorrectionLevel: 'M' });
          return res.json({ dataUrl, url });
        } catch {
          return res.status(503).json({ error: '無法產生遙控器 QR Code' });
        }
      });
      app.get('/api/resolve_yt_title', pageRequestLimiter, async (req, res) => {
        const pythonPort = Number(process.env.KTV_PORT) || 5000;
        try {
          const target = new URL(`http://127.0.0.1:${pythonPort}/api/resolve_yt_title`);
          target.searchParams.set('url', String(req.query.url || ''));
          const response = await fetch(target, { signal: AbortSignal.timeout(30000) });
          const payload = await response.json();
          return res.status(response.status).json(payload);
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.patch('/api/catalog/:songId', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}`, {
            method: 'PATCH',
            headers: {
              authorization: req.get('authorization') || '',
              'content-type': 'application/json',
            },
            body: JSON.stringify(req.body || {}),
            signal: AbortSignal.timeout(5000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.delete('/api/catalog/:songId', pageRequestLimiter, async (req, res) => {
        try {
          const target = new URL(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}`);
          if (req.query.delete_files || req.query.deleteMediaFiles || req.body?.delete_files) {
            target.searchParams.set('delete_files', 'true');
          }
          const response = await fetch(target, {
            method: 'DELETE',
            headers: {
              authorization: req.get('authorization') || '',
              'content-type': 'application/json',
            },
            body: JSON.stringify(req.body || {}),
            signal: AbortSignal.timeout(10000),
          });
          const payload = await response.json();
          songs = await loadSongs();
          miniSearch = createSearchIndex(songs);
          inverseIndex = createInverseIndex(songs);
          io.emit('catalog_updated', { songCount: songs.length });
          return res.status(response.status).json(payload);
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.get('/api/catalog/health', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/health`, {
            signal: AbortSignal.timeout(10000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: '無法取得曲庫健康度資訊' });
        }
      });
      app.post('/api/catalog/cleanup-orphans', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/cleanup-orphans`, {
            method: 'POST',
            headers: { authorization: req.get('authorization') || '' },
            signal: AbortSignal.timeout(30000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });
      app.put('/api/catalog/:songId/lyrics', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}/lyrics`, {
            method: 'PUT',
            headers: {
              authorization: req.get('authorization') || '',
              'content-type': 'application/json',
            },
            body: JSON.stringify(req.body || {}),
            signal: AbortSignal.timeout(10000),
          });
          const payload = await response.json();
          if (response.ok && payload.ok) {
            io.emit('lyrics_updated', { song_id: req.params.songId, lyrics_filename: payload.lyrics_filename });
          }
          return res.status(response.status).json(payload);
        } catch {
          return res.status(503).json({ ok: false, error: '無法更新歌詞檔案' });
        }
      });
      app.post('/api/catalog/:songId/reprocess', pageRequestLimiter, async (req, res) => {
        try {
          const response = await fetch(`http://127.0.0.1:${Number(process.env.KTV_PORT) || 5000}/api/catalog/${encodeURIComponent(req.params.songId)}/reprocess`, {
            method: 'POST',
            headers: {
              authorization: req.get('authorization') || '',
              'content-type': 'application/json',
            },
            body: JSON.stringify(req.body || {}),
            signal: AbortSignal.timeout(10000),
          });
          return res.status(response.status).json(await response.json());
        } catch {
          return res.status(503).json({ ok: false, error: 'Python 處理服務目前無法使用' });
        }
      });

      const localUpload = multer({
        storage: multer.diskStorage({
          destination: (req, file, cb) => cb(null, songsDir),
          filename: (req, file, cb) => {
            const rawName = Buffer.from(file.originalname, 'latin1').toString('utf8');
            cb(null, rawName);
          },
        }),
        limits: { fileSize: 500 * 1024 * 1024 },
      });

      app.post('/api/catalog/upload-local', pageRequestLimiter, localUpload.fields([{ name: 'file', maxCount: 1 }, { name: 'lyrics', maxCount: 1 }]), async (req, res) => {
        try {
          const uploadedFile = req.files?.file?.[0];
          if (!uploadedFile) {
            return res.status(400).json({ ok: false, error: '未上傳任何媒體檔案' });
          }
          const uploadedLrc = req.files?.lyrics?.[0];
          const filename = uploadedFile.filename;
          const stem = path.basename(filename, path.extname(filename));

          let artist = (req.body?.artist || '').trim();
          let title = (req.body?.title || '').trim();
          if (!title) {
            if (stem.includes(' - ')) {
              const parts = stem.split(' - ');
              artist = artist || parts[0].trim();
              title = parts.slice(1).join(' - ').trim();
            } else {
              title = stem;
            }
          }

          songs = await loadSongs();
          miniSearch = createSearchIndex(songs);
          inverseIndex = createInverseIndex(songs);
          io.emit('catalog_updated', { songCount: songs.length });
          io.emit('refresh_list');

          return res.json({
            ok: true,
            filename,
            artist,
            title,
            lyrics_filename: uploadedLrc?.filename || null,
          });
        } catch (err) {
          return res.status(500).json({ ok: false, error: err.message });
        }
      });
      app.get('/api/songs', pageRequestLimiter, (req, res) => {
        const query = String(req.query.q || '').trim();
        const language = String(req.query.language || '');
        const charCount = Number(req.query.charCount) || 0;
        const offset = Math.max(0, Number.parseInt(req.query.offset, 10) || 0);
        const limit = Math.max(1, Math.min(100, Number.parseInt(req.query.limit, 10) || 50));
        const sort = String(req.query.sort || 'relevance');
        const mode = String(req.query.mode || 'keyword');
        const results = findSongs(songs, miniSearch, inverseIndex, query, language, charCount, mode, sort);
        return res.json({ items: results.slice(offset, offset + limit), total: results.length, offset, limit });
      });
      app.get('/api/songs/trending', pageRequestLimiter, (req, res) => {
        const limit = Math.max(1, Math.min(50, Number(req.query.limit) || 20));
        const sorted = [...songs].sort((a, b) => {
          const countA = songStats[a.id]?.playCount || 0;
          const countB = songStats[b.id]?.playCount || 0;
          if (countB !== countA) return countB - countA;
          return (a.title || '').localeCompare(b.title || '');
        });
        return res.json({ items: sorted.slice(0, limit).map(enrichSong), total: sorted.length });
      });
      app.get('/api/artists/popular', pageRequestLimiter, (req, res) => {
        const artistMap = new Map();
        for (const song of songs) {
          const raw = (song.artist || '').trim();
          if (!raw) continue;
          const primary = raw.split(/[,&/、]/)[0].trim();
          if (!primary) continue;
          const existing = artistMap.get(primary) || { name: primary, songCount: 0, playCount: 0, sampleSong: song };
          existing.songCount += 1;
          existing.playCount += (songStats[song.id]?.playCount || 0);
          artistMap.set(primary, existing);
        }
        const list = [...artistMap.values()].sort((a, b) => {
          if (b.playCount !== a.playCount) return b.playCount - a.playCount;
          return b.songCount - a.songCount;
        });
        return res.json({ items: list.slice(0, 30) });
      });
      app.get('/api/songs/recent', pageRequestLimiter, (req, res) => {
        const limit = Math.max(1, Math.min(50, Number(req.query.limit) || 20));
        const sorted = [...songs].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));
        return res.json({ items: sorted.slice(0, limit).map(enrichSong), total: sorted.length });
      });
      app.get('/api/bot/room', botApiLimiter, (req, res) => {
        const expected = process.env.KTV_BOT_API_TOKEN || '';
        const supplied = (req.get('authorization') || '').replace(/^Bearer\s+/i, '');
        const expectedBuffer = Buffer.from(expected);
        const suppliedBuffer = Buffer.from(supplied);
        const authorized = expectedBuffer.length > 0 && suppliedBuffer.length === expectedBuffer.length
          && timingSafeEqual(suppliedBuffer, expectedBuffer);
        if (!authorized) return res.sendStatus(401);
        return res.json({ songCount: songs.length, ...snapshot() });
      });
      app.get('/media/:filename', mediaRequestLimiter, (req, res) => {
        const fullPath = mediaPath(req.params.filename);
        if (!fullPath || !fs.existsSync(fullPath) || !fs.statSync(fullPath).isFile()) {
          return res.sendStatus(404);
        }
        const size = fs.statSync(fullPath).size;
        const mimeTypes = {
          '.mp4': 'video/mp4',
          '.m4a': 'audio/mp4',
          '.wav': 'audio/wav',
          '.lrc': 'text/plain; charset=utf-8',
          '.json': 'application/json; charset=utf-8',
        };
        res.setHeader('Content-Type', mimeTypes[path.extname(fullPath).toLowerCase()] || 'application/octet-stream');
        res.setHeader('Access-Control-Allow-Origin', '*');
        res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
        const rangeHeader = req.headers.range;
        res.setHeader('Accept-Ranges', 'bytes');
        if (!rangeHeader) {
          res.setHeader('Content-Length', size);
          if (req.method === 'HEAD') return res.end();
          return fs.createReadStream(fullPath).pipe(res);
        }
        const range = parseRange(rangeHeader, size);
        if (!range) {
          res.setHeader('Content-Range', `bytes */${size}`);
          return res.sendStatus(416);
        }
        res.status(206);
        res.setHeader('Content-Range', `bytes ${range.start}-${range.end}/${size}`);
        res.setHeader('Content-Length', range.end - range.start + 1);
        if (req.method === 'HEAD') return res.end();
        return fs.createReadStream(fullPath, range).pipe(res);
      });

      const queue = [];
      const history = [];
      const playerState = {
        isPlaying: false,
        audioMode: 'original',
        musicVol: 0.8,
        pitch: 0,
        position: 0,
        startedAt: null,
        serverTime: Date.now(),
      };

      const currentPosition = () => playerState.position + (
        playerState.isPlaying && playerState.startedAt !== null
          ? Math.max(0, (Date.now() - playerState.startedAt) / 1000)
          : 0
      );
      const currentSong = () => queue[0] || null;
      const canPlayInstrumental = song => {
        try {
          const fullPath = mediaPath(song?.instrumental_filename || '');
          const stat = fullPath && fs.statSync(fullPath);
          return Boolean(stat?.isFile() && stat.size > 0);
        } catch {
          return false;
        }
      };
      const snapshot = () => ({
        queue: [...queue],
        history: [...history],
        currentSong: currentSong(),
        state: {
          isPlaying: playerState.isPlaying,
          audioMode: playerState.audioMode,
          musicVol: playerState.musicVol,
          pitch: playerState.pitch,
          position: currentPosition(),
          serverTime: Date.now(),
        },
      });
      const emitState = () => {
        const payload = snapshot();
        io.emit('sync_state', { queue: payload.queue, history: payload.history, state: payload.state });
        io.emit('queue_updated', payload.queue);
        io.emit('playback_snapshot', payload);
      };
      const startCurrent = () => {
        playerState.position = 0;
        playerState.pitch = Number(currentSong()?.key) || 0;
        if (!currentSong()) playerState.isPlaying = false;
        else if (queue.length === 1) playerState.isPlaying = true;
        if (currentSong() && playerState.audioMode === 'accompaniment' && !canPlayInstrumental(currentSong())) {
          playerState.audioMode = 'original';
          io.emit('playback_error', { message: '新歌曲沒有有效伴奏檔，已切回原唱模式。' });
        }
        if (currentSong()) {
          recordSongPlay(currentSong().id);
        }
        playerState.startedAt = playerState.isPlaying ? Date.now() : null;
      };
      const findSong = song => songs.find(item => item.id === String(song?.id));
      advanceQueue = () => {
        if (queue.length) {
          history.unshift(queue.shift());
          history.splice(100);
        }
        startCurrent();
        emitState();
      };

      app.get('/health', (_req, res) => res.json({ ok: true, songs: songs.length }));

      const socketRateLimits = new Map();
      const checkSocketRateLimit = (socketId, action, limitPerSec) => {
        const now = Date.now();
        let entry = socketRateLimits.get(socketId);
        if (!entry) {
          entry = {};
          socketRateLimits.set(socketId, entry);
        }
        const bucket = entry[action] || { tokens: limitPerSec, last: now };
        const elapsed = (now - bucket.last) / 1000;
        bucket.tokens = Math.min(limitPerSec, bucket.tokens + elapsed * limitPerSec);
        bucket.last = now;
        if (bucket.tokens >= 1) {
          bucket.tokens -= 1;
          entry[action] = bucket;
          return true;
        }
        return false;
      };

      io.on('connection', socket => {
        micRelay.attach(socket);
        const payload = snapshot();
        socket.emit('sync_state', { queue: payload.queue, history: payload.history, state: payload.state });
        socket.emit('playback_snapshot', payload);
        socket.on('time_ping', clientSentAt => {
          socket.emit('time_pong', { clientSentAt: Number(clientSentAt), serverTime: Date.now() });
        });
        socket.on('search_songs', ({ query = '', language = '', charCount = 0, offset = 0, limit = 50, paginated = false, requestId, mode = 'keyword', sort = 'relevance' } = {}) => {
          const cleanQuery = String(query).trim();
          const results = findSongs(songs, miniSearch, inverseIndex, cleanQuery, language, charCount, mode, sort);
          if (paginated) {
            const safeOffset = Math.max(0, Number.parseInt(offset, 10) || 0);
            const safeLimit = Math.max(1, Math.min(100, Number.parseInt(limit, 10) || 50));
            socket.emit('search_page_results', {
              items: results.slice(safeOffset, safeOffset + safeLimit),
              total: results.length,
              offset: safeOffset,
              limit: safeLimit,
              requestId,
            });
          } else {
            socket.emit('search_results', results.slice(0, 50));
          }
        });
        socket.on('add_to_queue', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          queue.push({
            ...song,
            requestedBy: String(payloadSong?.requestedBy || '').trim().slice(0, 24),
            queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          });
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('insert_to_top', payloadSong => {
          const song = findSong(payloadSong);
          if (!song || !song.video_filename) return;
          const item = {
            ...song,
            requestedBy: String(payloadSong?.requestedBy || '').trim().slice(0, 24),
            queueId: `${song.id}_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
          };
          queue.splice(queue.length ? 1 : 0, 0, item);
          if (queue.length === 1) startCurrent();
          emitState();
        });
        socket.on('remove_from_queue', queueId => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index < 0) return;
          const wasCurrent = index === 0;
          const [removed] = queue.splice(index, 1);
          if (wasCurrent) {
            history.unshift(removed);
            history.splice(100);
            startCurrent();
          }
          emitState();
        });
        socket.on('move_to_top', queueId => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index <= 1) return;
          const [item] = queue.splice(index, 1);
          queue.splice(1, 0, item);
          emitState();
        });
        socket.on('move_queue_item', ({ queueId, direction } = {}) => {
          const index = queue.findIndex(item => item.queueId === queueId);
          if (index <= 0) return;
          if (direction === 'up' && index > 1) {
            [queue[index - 1], queue[index]] = [queue[index], queue[index - 1]];
            emitState();
          } else if (direction === 'down' && index < queue.length - 1) {
            [queue[index], queue[index + 1]] = [queue[index + 1], queue[index]];
            emitState();
          }
        });
        socket.on('swap_queue_items', ({ fromIndex, toIndex } = {}) => {
          const from = Number(fromIndex);
          const to = Number(toIndex);
          if (from > 0 && to > 0 && from < queue.length && to < queue.length && from !== to) {
            [queue[from], queue[to]] = [queue[to], queue[from]];
            emitState();
          }
        });
        socket.on('shuffle_queue', () => {
          for (let index = queue.length - 1; index > 1; index--) {
            const target = 1 + Math.floor(Math.random() * index);
            [queue[index], queue[target]] = [queue[target], queue[index]];
          }
          emitState();
        });
        socket.on('clear_queue', () => {
          queue.splice(1);
          emitState();
        });
        socket.on('reorder_queue', ids => {
          if (!Array.isArray(ids)) return;
          const byId = new Map(queue.map(item => [item.queueId, item]));
          const reordered = ids.map(item => byId.get(typeof item === 'string' ? item : item?.queueId)).filter(Boolean);
          queue.splice(0, queue.length, ...reordered, ...queue.filter(item => !reordered.includes(item)));
          emitState();
        });
        socket.on('next_song', () => {
          advanceQueue();
        });
        socket.on('replay_current', () => {
          playerState.position = 0;
          playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          emitState();
        });
        socket.on('seek', value => {
          const position = Number(value?.position ?? value);
          if (!Number.isFinite(position) || position < 0) return;
          const duration = Number(currentSong()?.duration_seconds);
          playerState.position = duration > 0 ? Math.min(position, duration) : position;
          playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          emitState();
        });
        socket.on('player_command', command => {
          if (!command || typeof command !== 'object') return;
          if (command.isPlaying !== undefined) {
            const position = currentPosition();
            playerState.position = position;
            playerState.isPlaying = Boolean(command.isPlaying);
            playerState.startedAt = playerState.isPlaying ? Date.now() : null;
          }
          if (command.audioMode === 'original') playerState.audioMode = 'original';
          if (command.audioMode === 'accompaniment') {
            const song = currentSong();
            if (!canPlayInstrumental(song)) {
              io.emit('playback_error', { message: '伴奏檔缺失或無效，已保留原唱模式。' });
              command = { ...command, audioMode: undefined };
            } else playerState.audioMode = 'accompaniment';
          }
          for (const key of ['musicVol']) {
            if (command[key] !== undefined && Number.isFinite(Number(command[key]))) {
              playerState[key] = Math.max(0, Math.min(1, Number(command[key])));
            }
          }
          if (command.pitch !== undefined && Number.isFinite(Number(command.pitch))) {
            playerState.pitch = Math.max(-6, Math.min(6, Math.round(Number(command.pitch))));
          }
          playerState.serverTime = Date.now();
          io.emit('execute_command', command);
          emitState();
        });
        socket.on('play_sfx', payload => {
          const type = String(payload?.type || payload || '');
          if (['applause', 'cheer', 'laughter', 'horn', 'drum'].includes(type)) {
            if (!checkSocketRateLimit(socket.id, 'sfx', 5)) return;
            io.emit('play_sfx', { type, serverTime: Date.now() });
          }
        });
        socket.on('report_score', scorePayload => socket.broadcast.emit('score_updated', scorePayload));
        socket.on('pitch_data', payload => {
          if (payload && typeof payload === 'object') {
            socket.broadcast.emit('remote_pitch', {
              ...payload,
              serverTime: Date.now(),
            });
          }
        });
        socket.on('crowd_interaction', payload => {
          if (!payload) return;
          const allowedTypes = ['applause', 'cheer', 'laughter', 'horn', 'drum', 'message', 'danmaku', 'sticker', 'sfx'];
          const type = allowedTypes.includes(payload.type) ? payload.type : 'danmaku';
          const rateLimit = (type === 'danmaku' || type === 'message' || type === 'sticker') ? 3 : 5;
          if (!checkSocketRateLimit(socket.id, 'interaction', rateLimit)) return;

          const text = String(payload.text || '').trim().slice(0, 100);
          if ((type === 'message' || type === 'danmaku') && !text) return;

          if (['applause', 'cheer', 'laughter', 'horn', 'drum'].includes(type)) {
            io.emit('play_sfx', { type, serverTime: Date.now() });
          }

          const safeColors = ['#ffffff', '#ffd700', '#00f0ff', '#ff3366', '#00ff88', '#b537f2'];
          const color = safeColors.includes(payload.color) ? payload.color : '#ffffff';
          const danmakuType = ['scroll', 'top', 'sticker'].includes(payload.danmakuType) ? payload.danmakuType : 'scroll';
          const stickerIcon = String(payload.stickerIcon || '').slice(0, 10);

          io.emit('crowd_interaction', {
            type,
            text,
            user: String(payload.user || '來賓').trim().slice(0, 24),
            color,
            danmakuType,
            stickerIcon,
            serverTime: Date.now(),
          });
        });
        socket.on('disconnect', () => {
          socketRateLimits.delete(socket.id);
        });
      });

      setInterval(async () => {
        try {
          const dbStat = fs.existsSync(dbPath) ? fs.statSync(dbPath) : null;
          const csvStat = fs.existsSync(catalogPath) ? fs.statSync(catalogPath) : null;
          const currentMtime = Math.max(dbStat?.mtimeMs || 0, csvStat?.mtimeMs || 0);
          if (currentMtime && currentMtime !== catalogMtime) {
            songs = await loadSongs();
            miniSearch = createSearchIndex(songs);
            inverseIndex = createInverseIndex(songs);
            catalogMtime = currentMtime;
            io.emit('catalog_updated', { songCount: songs.length });
          }
        } catch (error) {
          console.error('[Database] Catalog reload failed:', error.message);
        }
        if (currentSong()) emitState();
      }, 1000).unref();

      let catalogMtime = Math.max(
        fs.existsSync(dbPath) ? fs.statSync(dbPath).mtimeMs : 0,
        fs.existsSync(catalogPath) ? fs.statSync(catalogPath).mtimeMs : 0
      );
      httpServer.once('error', reject);
      httpServer.listen(port, '0.0.0.0', () => {
        const httpsPort = Number(process.env.KTV_HTTPS_PORT) || 3443;
        startHttpsProxy({
          port: httpsPort,
          targetPort: port,
          certPath: process.env.KTV_TLS_CERT_PATH,
          keyPath: process.env.KTV_TLS_KEY_PATH,
        }).then(httpsProxy => {
          console.log(`[Database] Loaded ${songs.length} songs.`);
          console.log(`[Server] KTV service listening on port ${port}.`);
          if (httpsProxy) console.log(`[Server] HTTPS remote available on port ${httpsPort}.`);
          resolve({
            close: async () => {
              await httpsProxy?.close();
              await new Promise(resolveClose => io.close(resolveClose));
            },
          });
        }).catch(error => {
          io.close(() => reject(error));
        });
      });
    } catch (error) {
      reject(error);
    }
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  startServer().catch(error => {
    console.error('[Server] Startup failed:', error.message);
    process.exitCode = 1;
  });
}
