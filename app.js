const STORAGE_KEY = 'personal-yt-stream:v1';
const form = document.querySelector('#add-form');
const input = document.querySelector('#video-url');
const saveButton = document.querySelector('#save-button');
const formStatus = document.querySelector('#form-status');
const playerStatus = document.querySelector('#player-status');
const list = document.querySelector('#video-list');
const mount = document.querySelector('#player-mount');
const placeholder = document.querySelector('#player-placeholder');
const retryButton = document.querySelector('#retry-button');
let videos = [];
let storageAvailable = true;
let saving = false;
let activeId = null;
let player = null;
let playerRequest = 0;
let apiPromise;
let hlsPromise;
let playback = null;

function status(element, message, error = false) {
  element.textContent = message;
  element.dataset.tone = error ? 'error' : '';
}

function videoId(value) {
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port) throw new Error('invalid');
  const host = url.hostname.toLowerCase();
  let id;
  if (host === 'youtu.be') id = url.pathname.split('/')[1];
  else if (['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'].includes(host)) {
    const parts = url.pathname.split('/');
    id = url.pathname === '/watch' ? url.searchParams.get('v') : ['shorts', 'live', 'embed'].includes(parts[1]) ? parts[2] : null;
  }
  if (!/^[\w-]{11}$/.test(id || '')) throw new Error('invalid');
  return id;
}

function loadVideos() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return;
    const saved = JSON.parse(raw);
    if (!Array.isArray(saved) || saved.some(v => !v || typeof v.id !== 'string' || !/^[\w-]{11}$/.test(v.id) || typeof v.url !== 'string' || videoId(v.url) !== v.id || typeof v.title !== 'string' || typeof v.thumbnailUrl !== 'string')) throw new Error('invalid storage');
    videos = saved.filter((v, index) => saved.findIndex(other => other.id === v.id) === index);
  } catch {
    storageAvailable = false;
    status(formStatus, 'Koleksi lokal tidak dapat dibaca. Data lama tidak diubah. Periksa izin penyimpanan browser atau cadangkan data lokal sebelum meresetnya.', true);
  }
}

function persist(next) {
  if (!storageAvailable) return false;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    videos = next;
    return true;
  } catch {
    status(formStatus, 'Video belum tersimpan. Penyimpanan browser penuh atau tidak diizinkan. Kosongkan ruang atau periksa izin, lalu coba lagi.', true);
    return false;
  }
}

function render() {
  list.replaceChildren();
  document.querySelector('#video-count').textContent = `${videos.length} video`;
  document.querySelector('#collection-empty').hidden = videos.length > 0;
  for (const video of videos) {
    const item = document.querySelector('#video-template').content.firstElementChild.cloneNode(true);
    item.classList.toggle('is-active', video.id === activeId);
    const image = item.querySelector('img');
    image.addEventListener('error', () => {
      image.hidden = true;
      item.querySelector('.thumbnail-fallback').hidden = false;
    }, { once: true });
    // Only use YouTube thumbnail hosts, including for data read from localStorage.
    let thumbnail = `https://i.ytimg.com/vi/${video.id}/hqdefault.jpg`;
    try {
      const url = new URL(video.thumbnailUrl);
      if (url.protocol === 'https:' && ['i.ytimg.com', 'img.youtube.com'].includes(url.hostname)) thumbnail = url.href;
    } catch { /* Use the thumbnail derived from the validated video ID. */ }
    image.src = thumbnail;
    item.querySelector('h3').textContent = video.title;
    item.querySelector('h3').title = video.title;
    const play = item.querySelector('.play-button');
    play.setAttribute('aria-label', `Play: ${video.title}`);
    play.setAttribute('aria-pressed', String(video.id === activeId));
    play.addEventListener('click', () => playVideo(video));
    const remove = item.querySelector('.delete-button');
    remove.setAttribute('aria-label', `Hapus: ${video.title}`);
    remove.addEventListener('click', () => {
      if (!confirm(`Hapus “${video.title}” dari koleksi?`)) return;
      if (!persist(videos.filter(v => v.id !== video.id))) return;
      if (activeId === video.id) resetPlayer();
      render();
      status(formStatus, 'Video dihapus dari koleksi.');
      input.focus();
    });
    list.append(item);
  }
  saveButton.disabled = saving || !storageAvailable;
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (saveButton.disabled) return;
  let id;
  try { id = videoId(input.value.trim()); }
  catch {
    status(formStatus, 'Link tidak valid. Gunakan link video youtube.com atau youtu.be.', true);
    input.focus();
    return;
  }
  if (videos.some(v => v.id === id)) {
    status(formStatus, 'Video ini sudah ada di koleksimu. Tekan Play untuk menontonnya.');
    return;
  }
  saving = true;
  saveButton.disabled = true;
  saveButton.textContent = 'Menyimpan…';
  status(formStatus, 'Mengambil judul dan thumbnail…');
  const url = `https://www.youtube.com/watch?v=${id}`;
  let title = `Video YouTube · ${id}`;
  let thumbnailUrl = `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
  let metadataFailed = false;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(`https://www.youtube.com/oembed?url=${encodeURIComponent(url)}&format=json`, { signal: controller.signal });
    if (!response.ok) throw new Error('metadata unavailable');
    const data = await response.json();
    if (typeof data.title !== 'string' || !data.title.trim()) throw new Error('missing title');
    title = data.title;
    if (typeof data.thumbnail_url === 'string') thumbnailUrl = data.thumbnail_url;
  } catch { metadataFailed = true; }
  finally { clearTimeout(timeout); }
  if (persist([{ id, url, title, thumbnailUrl }, ...videos])) {
    render();
    input.value = '';
    status(formStatus, metadataFailed ? 'Link tersimpan. Judul asli belum dapat diambil; untuk sementara digunakan ID video.' : 'Video tersimpan. Pilih Play untuk menonton.');
  }
  saving = false;
  saveButton.disabled = !storageAvailable;
  saveButton.innerHTML = '<span aria-hidden="true">+</span> Simpan video';
  input.focus();
});

function loadPlayerAPI() {
  if (window.YT?.Player) return Promise.resolve();
  if (apiPromise) return apiPromise;
  apiPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const timeout = setTimeout(() => fail(), 12000);
    function fail() {
      clearTimeout(timeout);
      script.remove();
      apiPromise = undefined;
      reject(new Error('YouTube player unavailable'));
    }
    window.onYouTubeIframeAPIReady = () => { clearTimeout(timeout); resolve(); };
    script.onerror = fail;
    script.src = 'https://www.youtube.com/iframe_api';
    document.head.append(script);
  });
  return apiPromise;
}

function isCurrent(state) {
  return playback === state && state.request === playerRequest;
}

function setPlayerStatus(message, label, error = false) {
  status(playerStatus, message, error);
  document.querySelector('#player-label').textContent = label;
}

function clearPlayerTimers(state) {
  for (const key of ['readyTimer', 'bufferTimer', 'fallbackStartupTimer', 'fallbackBufferTimer']) clearTimeout(state[key]);
}

function releaseSession(id) {
  if (id) fetch(`/api/playback/${id}`, { method: 'DELETE', keepalive: true }).catch(() => {});
}

function stopOfficial(state) {
  clearTimeout(state.readyTimer);
  clearTimeout(state.bufferTimer);
  const previous = player;
  player = null;
  if (previous) previous.destroy();
}

function stopFallback(state) {
  clearTimeout(state.fallbackStartupTimer);
  clearTimeout(state.fallbackBufferTimer);
  const media = state.media;
  state.media = null;
  if (state.hls) state.hls.destroy();
  state.hls = null;
  if (media) {
    media.pause();
    media.removeAttribute('src');
    media.load();
  }
  releaseSession(state.sessionId);
  state.sessionId = null;
}

function failPlayback(state, message) {
  if (!isCurrent(state) || state.failed) return;
  state.failed = true;
  clearPlayerTimers(state);
  state.controller?.abort();
  stopOfficial(state);
  stopFallback(state);
  showPlayerMessage('Video belum dapat diputar.');
  setPlayerStatus(message, 'Tidak dapat diputar', true);
  retryButton.hidden = false;
}

function showPlayerMessage(message) {
  const text = document.createElement('p');
  text.className = 'player-loading';
  text.textContent = message;
  mount.replaceChildren(text);
  mount.hidden = false;
  placeholder.hidden = true;
}

function resetPlayer() {
  playerRequest++;
  const previous = playback;
  playback = null;
  if (previous) {
    clearPlayerTimers(previous);
    previous.controller?.abort();
    stopOfficial(previous);
    stopFallback(previous);
  }
  retryButton.hidden = true;
  mount.replaceChildren();
  mount.hidden = true;
  placeholder.hidden = false;
  activeId = null;
  document.querySelector('#current-title').textContent = 'Belum ada video';
  document.querySelector('#player-label').textContent = 'Belum ada video dipilih';
  status(playerStatus, 'Pilih Play pada video di koleksimu.');
}

function loadHls() {
  if (window.Hls) return Promise.resolve();
  if (hlsPromise) return hlsPromise;
  hlsPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    const timer = setTimeout(fail, 12000);
    function fail() {
      clearTimeout(timer);
      script.remove();
      hlsPromise = undefined;
      reject(new Error('Library pemutar alternatif gagal dimuat. Tekan Coba lagi.'));
    }
    script.onload = () => { clearTimeout(timer); resolve(); };
    script.onerror = fail;
    script.src = 'assets/vendor/hls-1.7.3.min.js';
    document.head.append(script);
  });
  return hlsPromise;
}

async function prepareFallback(state, position) {
  state.preparing = true;
  stopFallback(state);
  showPlayerMessage('Menyiapkan pemutar alternatif…');
  setPlayerStatus(state.renewed ? 'Memperbarui sesi streaming…' : 'Mencoba pemutar alternatif…', 'Pemutar alternatif');
  const controller = new AbortController();
  state.controller = controller;
  const timeout = setTimeout(() => controller.abort(), 105000);
  try {
    const response = await fetch('/api/playback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ videoId: state.video.id }), signal: controller.signal
    });
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error('Server alternatif tidak tersedia. Jalankan app dengan python3 server.py.');
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || 'Stream alternatif tidak dapat disiapkan.');
    if (!/^[a-f0-9]{32}$/.test(data.sessionId)) throw new Error('Respons server alternatif tidak valid.');
    const mp4 = typeof data.mediaUrl === 'string';
    const validSource = mp4
      ? !data.manifestUrl && new RegExp(`^/api/playback/${data.sessionId}/resource/[a-f0-9]{24}$`).test(data.mediaUrl)
      : data.manifestUrl === `/api/playback/${data.sessionId}/manifest.m3u8`;
    if (!validSource) {
      throw new Error('Respons server alternatif tidak valid.');
    }
    if (!isCurrent(state) || state.failed) { releaseSession(data.sessionId); return; }
    state.sessionId = data.sessionId;
    const media = document.createElement('video');
    media.id = 'fallback-player';
    media.controls = true;
    media.playsInline = true;
    media.preload = 'auto';
    media.setAttribute('aria-label', `Pemutar alternatif: ${state.video.title}`);
    let nativeHls = !!media.canPlayType('application/vnd.apple.mpegurl');
    // Chromium's native HLS can fail on seeks in long recordings; prefer MSE when supported.
    if (!mp4 && (window.MediaSource || window.ManagedMediaSource)) {
      await loadHls();
      if (!isCurrent(state) || state.failed) return;
      if (Hls.isSupported()) nativeHls = false;
    }
    if (!mp4 && !nativeHls && !window.Hls?.isSupported()) throw new Error('Browser ini belum mendukung pemutar alternatif HLS.');
    clearTimeout(timeout);
    state.media = media;
    state.preparing = false;
    const current = () => isCurrent(state) && !state.failed && state.media === media;
    mount.replaceChildren(media);
    let started = false;
    let buffering = false;
    function clearFallbackTimers() {
      clearTimeout(state.fallbackStartupTimer);
      clearTimeout(state.fallbackBufferTimer);
    }
    function waitForProgress() {
      clearTimeout(state.fallbackBufferTimer);
      state.fallbackBufferTimer = setTimeout(() => {
        if (current()) failPlayback(state, 'Stream alternatif tidak mengirim data selama 60 detik. Periksa koneksi lalu tekan Coba lagi.');
      }, 60000);
    }
    function receivedData() {
      if (current() && buffering && !media.paused) waitForProgress();
    }
    // Long recordings must load separate audio/video playlists before playback can start.
    function waitForStartup() {
      clearTimeout(state.fallbackStartupTimer);
      state.fallbackStartupTimer = setTimeout(() => {
        if (current()) failPlayback(state, 'Persiapan stream alternatif melewati batas 90 detik. Periksa koneksi lalu tekan Coba lagi.');
      }, 90000);
    }
    waitForStartup();
    media.addEventListener('play', () => {
      if (current() && !started) waitForStartup();
    });
    media.addEventListener('progress', receivedData);
    media.addEventListener('loadedmetadata', () => {
      if (!current() || !position) return;
      if (media.seekable.length) {
        media.currentTime = Math.max(media.seekable.start(0), Math.min(position, media.seekable.end(media.seekable.length - 1) - 0.1));
      } else if (Number.isFinite(media.duration)) {
        media.currentTime = Math.min(position, Math.max(0, media.duration - 0.1));
      }
    }, { once: true });
    media.addEventListener('playing', () => {
      if (!current()) return;
      started = true;
      buffering = false;
      clearFallbackTimers();
      setPlayerStatus('Sedang diputar melalui pemutar alternatif.', 'Pemutar alternatif');
    });
    media.addEventListener('pause', () => {
      if (!current() || media.ended) return;
      buffering = false;
      clearFallbackTimers();
      setPlayerStatus('Dijeda — pemutar alternatif.', 'Pemutar alternatif');
    });
    media.addEventListener('waiting', () => {
      if (!current() || media.paused) return;
      setPlayerStatus('Memuat stream alternatif…', 'Pemutar alternatif');
      if (!started || buffering) return;
      buffering = true;
      waitForProgress();
    });
    media.addEventListener('ended', () => {
      if (!current()) return;
      buffering = false;
      clearFallbackTimers();
      setPlayerStatus('Selesai diputar — pemutar alternatif.', 'Pemutar alternatif');
    });
    media.addEventListener('error', () => {
      if (!current()) return;
      if ((mp4 || nativeHls) && media.error?.code === 2 && !state.renewed) renewFallback(state);
      else failPlayback(state, 'Stream alternatif tidak dapat diputar. Tekan Coba lagi atau pilih video lain.');
    });
    if (mp4 || nativeHls) media.src = mp4 ? data.mediaUrl : data.manifestUrl;
    else {
      const hls = new Hls({
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        playlistLoadPolicy: { default: {
          ...Hls.DefaultConfig.playlistLoadPolicy.default,
          maxTimeToFirstByteMs: 60000,
          maxLoadTimeMs: 60000
        } }
      });
      state.hls = hls;
      for (const event of [Hls.Events.MANIFEST_LOADED, Hls.Events.LEVEL_LOADED, Hls.Events.AUDIO_TRACK_LOADED, Hls.Events.FRAG_LOADED]) {
        hls.on(event, receivedData);
      }
      hls.on(Hls.Events.ERROR, (_event, details) => {
        if (!current() || state.preparing) return;
        const code = details.response?.code;
        if ([403, 410].includes(code)) {
          if (!state.renewed) renewFallback(state);
          else failPlayback(state, 'Sumber streaming tetap menolak akses setelah sesi diperbarui. Pilih video lain atau tekan Coba lagi.');
        } else if (details.fatal) {
          failPlayback(state, 'Stream alternatif tidak dapat dimuat. Periksa koneksi lalu tekan Coba lagi.');
        }
      });
      hls.loadSource(data.manifestUrl);
      hls.attachMedia(media);
    }
    setPlayerStatus('Memuat stream alternatif…', 'Pemutar alternatif');
    media.play().catch(error => {
      if (!current()) return;
      if (error.name === 'NotAllowedError') {
        buffering = false;
        clearFallbackTimers();
        setPlayerStatus('Browser menahan autoplay. Tekan Play di pemutar alternatif.', 'Pemutar alternatif');
      } else if (error.name !== 'AbortError') failPlayback(state, 'Stream alternatif tidak dapat diputar. Tekan Coba lagi.');
    });
  } catch (error) {
    if (!isCurrent(state) || state.failed) return;
    const message = error.name === 'AbortError' ? 'Penyiapan stream alternatif terlalu lama. Tekan Coba lagi.'
      : error instanceof TypeError ? 'Server alternatif tidak dapat dihubungi. Jalankan python3 server.py lalu tekan Coba lagi.' : error.message;
    failPlayback(state, message);
  } finally {
    clearTimeout(timeout);
  }
}

function renewFallback(state) {
  if (!isCurrent(state) || state.failed || state.preparing || state.renewed) return;
  state.renewed = true;
  prepareFallback(state, state.media?.currentTime || 0);
}

function startFallback(state) {
  if (!isCurrent(state) || state.failed || state.fallbackStarted) return;
  let position = 0;
  try { position = player?.getCurrentTime() || 0; } catch { /* The iframe may never have become ready. */ }
  state.fallbackStarted = true;
  stopOfficial(state);
  prepareFallback(state, position);
}

async function playVideo(video) {
  resetPlayer();
  activeId = video.id;
  const request = playerRequest;
  const state = { request, video, fallbackStarted: false, renewed: false, failed: false, playRequested: true };
  playback = state;
  const official = () => isCurrent(state) && !state.fallbackStarted && !state.failed;
  document.querySelector('#current-title').textContent = video.title;
  document.querySelector('#player-label').textContent = 'Video dipilih';
  status(playerStatus, 'Memuat pemutar YouTube…');
  render();
  try {
    await loadPlayerAPI();
    if (!official()) return;
    const iframe = document.createElement('iframe');
    iframe.id = 'youtube-player';
    iframe.title = video.title;
    iframe.referrerPolicy = 'strict-origin-when-cross-origin';
    iframe.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen';
    iframe.allowFullscreen = true;
    iframe.src = `https://www.youtube-nocookie.com/embed/${video.id}?enablejsapi=1&autoplay=1&playsinline=1&origin=${encodeURIComponent(location.origin)}`;
    mount.append(iframe);
    mount.hidden = false;
    placeholder.hidden = true;
    state.readyTimer = setTimeout(() => startFallback(state), 15000);
    player = new YT.Player(iframe, { events: {
      onReady: () => {
        if (!official()) return;
        clearTimeout(state.readyTimer);
        status(playerStatus, 'Jika video belum mulai, tekan Play di pemutar.');
      },
      onStateChange: event => {
        if (!official()) return;
        clearTimeout(state.bufferTimer);
        if (event.data === 1) state.playRequested = true;
        if ([0, 2].includes(event.data)) state.playRequested = false;
        if (event.data === 3 && state.playRequested) state.bufferTimer = setTimeout(() => startFallback(state), 30000);
        const labels = { 1: 'Sedang diputar', 2: 'Dijeda', 0: 'Selesai diputar', 3: 'Memuat video…' };
        if (labels[event.data]) {
          status(playerStatus, labels[event.data]);
          document.querySelector('#player-label').textContent = labels[event.data];
        }
      },
      onAutoplayBlocked: () => {
        if (!official()) return;
        state.playRequested = false;
        clearTimeout(state.readyTimer);
        clearTimeout(state.bufferTimer);
        status(playerStatus, 'Browser menahan autoplay. Tekan Play di pemutar untuk mulai.');
      },
      onError: event => {
        if (!official()) return;
        if ([2, 100].includes(event.data)) failPlayback(state, event.data === 2 ? 'Link video tidak valid. Pilih video lain.' : 'Video tidak tersedia atau bersifat privat. Pilih video lain.');
        else startFallback(state);
      }
    } });
    document.querySelector('#watch-heading').scrollIntoView({ block: 'nearest' });
  } catch {
    if (official()) startFallback(state);
  }
}

retryButton.addEventListener('click', () => { if (playback) playVideo(playback.video); });
window.addEventListener('pagehide', resetPlayer);

loadVideos();
render();
