// Android uses native WAV streaming for background playback and media controls.
// Desktop live audio: one long fetch() of raw PCM16 (X-Sample-Rate header) from
// /api/audio, each chunk scheduled back to back as an AudioBufferSourceNode.
// Works over plain http:// to an IP (no AudioWorklet, which needs HTTPS).
// Keeps ~0.4 s of latency and resyncs after gaps (no transmission = no data). 0.25 s was
// too little to ride out the server's occasional ~220 ms stalls (tested in a browser, gotcha 51).

const LATENCY_S = 0.4;
const MAX_AHEAD_S = 1.2;
// Keep the receiver sample clock unchanged: playbackRate correction bends tuned audio.
// The jitter reserve handles bursts; only a genuine underrun resets the timeline.

// Listening aids, in the browser so they work for every device (a phone has no louder setting than
// its maximum). Chain: source -> boost -> compressor -> volume. The compressor is a safety net (a loud
// burst after a quiet stretch never reaches the ears at full force) and what makes boost usable.
export const BOOST_STEPS_DB = [0, 6, 12, 18];
const COMPRESSOR = { threshold: -14, knee: 10, ratio: 12, attack: 0.003, release: 0.2 };
const boostDb = () => {
  const v = parseInt(localStorage.getItem('boostDb') ?? '0', 10);
  return BOOST_STEPS_DB.includes(v) ? v : 0;
};

export function makeLoudnessChain(ctx, boostDbValue) {
  const boost = ctx.createGain();
  boost.gain.value = 10 ** (boostDbValue / 20);
  const comp = ctx.createDynamicsCompressor();
  for (const [k, v] of Object.entries(COMPRESSOR)) comp[k].value = v;
  boost.connect(comp);
  return { input: boost, boost, output: comp };
}

export const liveAudio = {
  // Android must own the network audio as media, rather than a JS-fed MediaStream.
  // This works over the receiver's plain HTTP URL as well as HTTPS.
  native: /Android/i.test(navigator.userAgent),
  generation: 0,
  retry: null,
  ctx: null,
  gain: null,
  chain: null,
  boostDb: boostDb(),
  ctrl: null,
  sources: new Set(),
  playing: false,
  broadcast: false,
  volume: parseFloat(localStorage.getItem('volume') ?? '0.9'),
  onchange: () => {},
  t: 0,

  async prepare() {
    if (!this.ctx) {
      this.ctx = new (window.AudioContext || window.webkitAudioContext)({ latencyHint: 'playback' });
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.chain = makeLoudnessChain(this.ctx, this.boostDb);
      this.chain.output.connect(this.gain);
      if (this.native) {
        this.el = new Audio();
        this.el.preload = 'none';
        this.input = this.ctx.createMediaElementSource(this.el);
        this.input.connect(this.broadcast ? this.gain : this.chain.input);
        this.gain.connect(this.ctx.destination);
        this.el.onplay = () => this.ctx.resume().catch(e => console.warn('live audio:', e));
        this.el.onerror = this.el.onended = () => this._retryNative();
        this.el.onpause = () => {
          if (this.playing && this.el.paused && !this.el.error && !this.el.ended && this.el.readyState >= 2) this.stop();
        };
      } else if (this.ctx.createMediaStreamDestination) {
        const dest = this.ctx.createMediaStreamDestination();
        this.gain.connect(dest);
        this.el = new Audio();
        this.el.srcObject = dest.stream;
      } else this.gain.connect(this.ctx.destination);
    }
    // Both calls must originate in the Listen tap, before awaiting either promise.
    const resumed = this.ctx.resume();
    if (this.native && !this.el.getAttribute('src')) this.el.src = this._nativeURL();
    const playback = this.el ? this.el.play() : Promise.resolve();
    if (this.native) await Promise.all([resumed, playback]);
    else { playback.catch(() => {}); await resumed; }
  },

  async start() {
    const generation = ++this.generation;
    try { await this.prepare(); }
    catch (e) { if (generation === this.generation) this.stop(); throw e; }
    if (generation !== this.generation) return;
    if ('mediaSession' in navigator) {
      if ('MediaMetadata' in window) navigator.mediaSession.metadata = new MediaMetadata({ title: 'AliSDR', artist: 'Live' });
      navigator.mediaSession.setActionHandler('play', () => this.start().catch(e => console.warn('live audio:', e)));
      navigator.mediaSession.setActionHandler('pause', () => this.stop());
      navigator.mediaSession.setActionHandler('stop', () => this.stop());
      navigator.mediaSession.playbackState = 'playing';
    }
    this.playing = true;
    this.onchange();
    if (!this.native && !this.ctrl) this._stream();
  },

  stop() {
    ++this.generation;
    this.playing = false;
    this.resetStream();
    if (this.el) this.el.pause();
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    this.onchange();
  },

  useBroadcast(enabled) {
    if (this.broadcast === enabled) return;
    this.broadcast = enabled;
    this.resetStream();
    if (this.native && this.input) {
      this.input.disconnect();
      this.input.connect(this.broadcast ? this.gain : this.chain.input);
    }
    if (this.playing) {
      if (this.native) this.start().catch(e => console.warn('live audio:', e));
      else this._stream();
    }
  },

  _nativeURL() {
    return '/api/audio?format=wav' + (this.broadcast ? '&broadcast=1' : '');
  },

  _retryNative() {
    if (!this.playing || this.retry) return;
    const generation = this.generation;
    this.retry = setTimeout(() => {
      this.retry = null;
      if (!this.playing || generation !== this.generation) return;
      this.el.src = this._nativeURL();
      this.el.play().catch(e => {
        if (e.name === 'NotAllowedError') this.stop();
        else this._retryNative();
      });
    }, 1000);
  },

  resetStream() {
    clearTimeout(this.retry);
    this.retry = null;
    if (this.native && this.el) {
      this.el.removeAttribute('src');
      this.el.load();  // abort the media request, including while paused
    }
    if (this.ctrl) { this.ctrl.abort(); this.ctrl = null; }
    for (const source of this.sources) source.stop();
    this.sources.clear();
  },

  async toggle() { return this.playing ? this.stop() : this.start(); },

  setVolume(v) {
    this.volume = v;
    localStorage.setItem('volume', String(v));
    if (this.gain) this.gain.gain.value = v;
  },

  setBoost(db) {
    this.boostDb = db;
    localStorage.setItem('boostDb', String(db));
    if (this.chain) this.chain.boost.gain.value = 10 ** (db / 20);
  },

  async _stream() {
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.t = 0;
    let rest = null;
    try {
      const r = await fetch(this.broadcast ? '/api/audio?broadcast=1' : '/api/audio', { signal: ctrl.signal, cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      const rate = +(r.headers.get('X-Sample-Rate') || 12500);
      const channels = r.headers.get('X-Audio-Channels') === '2' ? 2 : 1;
      const frameBytes = channels * 2;
      const reader = r.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done || ctrl.signal.aborted) break;
        let u = value;
        if (rest) { const m = new Uint8Array(rest.length + u.length); m.set(rest); m.set(u, rest.length); u = m; rest = null; }
        const length = u.length - u.length % frameBytes;
        if (length < u.length) rest = u.slice(length);
        if (!length) continue;
        const pcm = new DataView(u.buffer, u.byteOffset, length);
        const frames = length / frameBytes;
        const buf = this.ctx.createBuffer(channels, frames, rate);
        for (let channel = 0; channel < channels; channel++) {
          const out = buf.getChannelData(channel);
          for (let i = 0; i < frames; i++) out[i] = pcm.getInt16(i * frameBytes + channel * 2, true) / 32768;
        }
        const src = this.ctx.createBufferSource();
        src.buffer = buf;
        src.connect(channels === 2 ? this.gain : this.chain.input);
        const now = this.ctx.currentTime;
        // Really ran dry (or a new transmission after silence) / far too far ahead: start over.
        if (this.t <= now || this.t > now + MAX_AHEAD_S) this.t = now + LATENCY_S;
        src.onended = () => this.sources.delete(src);
        src.start(this.t);
        this.sources.add(src);
        this.t += buf.duration;
      }
    } catch (e) {
      if (!ctrl.signal.aborted) console.warn('live audio stream:', e);  // dropped - reconnect below
    }
    // Close this response for good before a reconnect: an error that left it open kept the
    // connection, and every retry took another of the browser's 6 per server - the page froze.
    const dropped = !ctrl.signal.aborted;
    ctrl.abort();
    if (this.ctrl === ctrl) this.ctrl = null;
    if (this.playing && dropped) setTimeout(() => { if (this.playing && !this.ctrl) this._stream(); }, 1000);
  },
};
